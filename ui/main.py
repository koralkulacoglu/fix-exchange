import asyncio
import datetime
import json
import os
import socket
import struct
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fix_client import AsyncFixSession

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ui_dir = os.path.dirname(os.path.abspath(__file__))
_static_dir = os.path.join(_ui_dir, "static")
_project_root = os.path.dirname(_ui_dir)

# ---------------------------------------------------------------------------
# Config — parse [DEFAULT] and [EXCHANGE] from exchange.cfg
# ---------------------------------------------------------------------------

def _read_cfg(path: str) -> dict:
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("["):
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg

_cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_project_root, "config", "exchange.cfg")
_cfg = _read_cfg(_cfg_path)

FIX_HOST   = "127.0.0.1"
FIX_PORT   = int(_cfg.get("SocketAcceptPort", 5001))
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = int(_cfg.get("AdminPort", 5002))
MD_GROUP   = _cfg.get("MulticastGroup", "239.1.1.1")
MD_PORT    = int(_cfg.get("MulticastPort", 5003))
SYMBOLS    = [s.strip() for s in _cfg.get("Symbols", "AAPL,MSFT,GOOG,AMZN").split(",") if s.strip()]

MD_FMT  = "<Q B B 8s d i 16s"
MD_SIZE = struct.calcsize(MD_FMT)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

order_book:  dict = {sym: {"bids": {}, "asks": {}} for sym in SYMBOLS}
order_state: dict = {}  # exchange_id -> {price, qty, side, symbol}
ws_clients:  set  = set()
exec_log:    list = []  # all exec reports received this server lifetime

# FIX session — owned by the server, shared across all WebSocket connections
fix:         AsyncFixSession | None = None
comp_id:     str  = ""
seq_counter: list = [0]

# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

async def admin_send(cmd: str) -> str:
    reader, writer = await asyncio.open_connection(ADMIN_HOST, ADMIN_PORT)
    writer.write((cmd + "\n").encode("ascii"))
    await writer.drain()
    resp = await asyncio.wait_for(reader.readline(), timeout=3.0)
    writer.close()
    await writer.wait_closed()
    return resp.decode("ascii").strip()

async def claim_session() -> str:
    resp = await admin_send("CLAIM-SESSION")
    if not resp.startswith("OK "):
        raise RuntimeError(f"CLAIM-SESSION failed: {resp!r}")
    return resp.split()[1]

async def release_session(cid: str):
    try:
        await admin_send(f"RELEASE-SESSION {cid}")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Market data — order book state
# ---------------------------------------------------------------------------

def _levels(symbol: str, side: int) -> dict | None:
    if symbol not in order_book:
        order_book[symbol] = {"bids": {}, "asks": {}}
    if side == ord('0'):
        return order_book[symbol]["bids"]
    if side == ord('1'):
        return order_book[symbol]["asks"]
    return None  # trade side


def _set_level(levels: dict, price: float, qty: int):
    if qty <= 0:
        levels.pop(price, None)
    else:
        levels[price] = qty


def _apply_md(evt: int, side: int, symbol: str, price: float, qty: int, exchange_id: str):
    lvls = _levels(symbol, side)
    if lvls is None:
        return  # trade side — no book state change

    if evt == 0:  # NewOrder
        order_state[exchange_id] = {"price": price, "qty": qty, "symbol": symbol, "side": side}
        _set_level(lvls, price, lvls.get(price, 0) + qty)

    elif evt == 1:  # Cancel
        prev = order_state.pop(exchange_id, None)
        if prev:
            _set_level(lvls, prev["price"], lvls.get(prev["price"], 0) - prev["qty"])

    elif evt == 2:  # FillResting
        prev = order_state.get(exchange_id)
        if prev:
            filled = prev["qty"] - qty
            _set_level(lvls, prev["price"], lvls.get(prev["price"], 0) - filled)
            if qty == 0:
                order_state.pop(exchange_id)
            else:
                prev["qty"] = qty

    elif evt == 4:  # ReplaceInPlace
        prev = order_state.get(exchange_id)
        if prev:
            reduction = prev["qty"] - qty
            _set_level(lvls, prev["price"], lvls.get(prev["price"], 0) - reduction)
            prev["qty"] = qty

    elif evt == 5:  # ReplaceDelete
        prev = order_state.pop(exchange_id, None)
        if prev:
            _set_level(lvls, prev["price"], lvls.get(prev["price"], 0) - prev["qty"])

    elif evt == 6:  # ReplaceNew
        order_state[exchange_id] = {"price": price, "qty": qty, "symbol": symbol, "side": side}
        _set_level(lvls, price, lvls.get(price, 0) + qty)


async def _safe_send(ws: WebSocket, msg: str):
    try:
        await ws.send_text(msg)
    except Exception:
        ws_clients.discard(ws)


class MdProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr):
        if len(data) != MD_SIZE:
            return
        seq, evt, side, sym_b, price, qty, exch_b = struct.unpack(MD_FMT, data)
        symbol      = sym_b.rstrip(b"\x00").decode("ascii")
        exchange_id = exch_b.rstrip(b"\x00").decode("ascii")
        is_new = symbol not in order_book
        _apply_md(evt, side, symbol, price, qty, exchange_id)
        if is_new:
            sym_msg = json.dumps({"type": "symbols", "symbols": list(order_book.keys())})
            for ws in list(ws_clients):
                asyncio.create_task(_safe_send(ws, sym_msg))
        msg = json.dumps({
            "type": "md", "seq": seq, "evt": evt, "side": side,
            "symbol": symbol, "price": price, "qty": qty, "eid": exchange_id,
        })
        for ws in list(ws_clients):
            asyncio.create_task(_safe_send(ws, msg))


def _book_snapshot() -> dict:
    snap = {}
    for sym, sides in order_book.items():
        snap[sym] = {
            "bids": sorted(sides["bids"].items(), key=lambda x: -x[0])[:10],
            "asks": sorted(sides["asks"].items(), key=lambda x: x[0])[:10],
        }
    return snap

# ---------------------------------------------------------------------------
# FIX reader — runs once for the server lifetime, fans out exec reports
# ---------------------------------------------------------------------------

async def _fix_reader():
    while True:
        msg = await fix.recv()
        if msg.get("35") == "1":   # TestRequest — reply with Heartbeat
            await fix.send("0", {"112": msg.get("112", "")})
        elif msg.get("35") == "8":
            exec_log.append(msg)
            text = json.dumps({"type": "exec", **msg})
            for ws in list(ws_clients):
                asyncio.create_task(_safe_send(ws, text))
        elif msg.get("35") == "W":  # MarketDataSnapshotFullRefresh — seed book
            symbol = msg.get("55", "")
            if symbol in order_book:
                bids, asks = {}, {}
                for e in msg.get("md_entries", []):
                    p, q = e.get("price"), e.get("qty", 0)
                    if p is None or q <= 0:
                        continue
                    eid = e.get("eid")
                    if e["type"] == "0":
                        bids[p] = bids.get(p, 0) + q
                        if eid:
                            order_state[eid] = {"price": p, "qty": q, "symbol": symbol, "side": ord('0')}
                    elif e["type"] == "1":
                        asks[p] = asks.get(p, 0) + q
                        if eid:
                            order_state[eid] = {"price": p, "qty": q, "symbol": symbol, "side": ord('1')}
                order_book[symbol]["bids"] = bids
                order_book[symbol]["asks"] = asks

# ---------------------------------------------------------------------------
# Lifespan: claim FIX session, bind UDP multicast socket
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global fix, comp_id

    # Claim and connect FIX session
    comp_id = await claim_session()
    print(f"Claimed FIX session: {comp_id}")
    fix = AsyncFixSession(comp_id)
    await fix.connect(FIX_HOST, FIX_PORT)
    await fix.logon()
    exec_log.extend(fix.order_statuses)

    # Seed order book with a full snapshot from the exchange before going live
    syms = list(order_book.keys())
    if syms:
        md_body = [
            ("262", "INIT"), ("263", "0"), ("264", "0"),
            ("267", "2"), ("269", "0"), ("269", "1"),
            ("146", str(len(syms))),
        ] + [("55", s) for s in syms]
        await fix.send("V", md_body)

    asyncio.create_task(_fix_reader())

    # Bind UDP multicast socket
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MD_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MD_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    transport, _ = await loop.create_datagram_endpoint(MdProtocol, sock=sock)
    print(f"Market data listener joined {MD_GROUP}:{MD_PORT}")

    yield

    transport.close()
    await fix.logout()
    await fix.close()
    await release_session(comp_id)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(_static_dir, "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)

    try:
        # Replay all exec reports so the blotter survives page reloads
        for entry in exec_log:
            await ws.send_text(json.dumps({"type": "exec", **entry}))

        # Send book snapshot with per-order state so the client can seed bookOrders
        await ws.send_text(json.dumps({
            "type": "snapshot",
            "symbols": list(order_book.keys()),
            "book": _book_snapshot(),
            "orders": [
                {"eid": eid, "symbol": s["symbol"], "side": s["side"],
                 "price": s["price"], "qty": s["qty"]}
                for eid, s in order_state.items()
            ],
        }))

        # Read orders from browser and forward to exchange via shared FIX session
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")
            if data["type"] == "new_order":
                seq_counter[0] += 1
                clord_id = f"{comp_id}-{seq_counter[0]}"
                await fix.send("D", {
                    "11": clord_id,
                    "21": "1",
                    "55": data["symbol"],
                    "54": "1" if data["side"] == "buy" else "2",
                    "40": "2",
                    "44": str(data["price"]),
                    "38": str(data["qty"]),
                    "60": ts,
                })
            elif data["type"] == "cancel":
                seq_counter[0] += 1
                await fix.send("F", {
                    "41": data["orig_clord_id"],
                    "11": f"{comp_id}-{seq_counter[0]}",
                    "55": data["symbol"],
                    "54": "1" if data["side"] == "buy" else "2",
                    "38": str(data["qty"]),
                    "60": ts,
                })

    except (WebSocketDisconnect, ConnectionError):
        pass
    finally:
        ws_clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    port = 8080
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print(f"Starting fix-exchange UI at http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
