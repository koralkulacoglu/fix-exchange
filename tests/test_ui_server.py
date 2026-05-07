"""
UI server integration tests.

Each test starts a fresh uvicorn subprocess for the UI server, connects via
WebSocket, and validates the JSON messages the server produces.  The exchange
must already be running (started by run_all.py before this module is imported).

AMZN is used as the test symbol: after the TIF/FOK tests its book is clean
(all orders were cancelled or fully filled), so resting orders placed here
won't accidentally cross existing book state.
"""

import asyncio
import contextlib
import json
import socket
import subprocess
import time

import websockets

from helpers import (
    FixSession, claim_session, release_session,
    recv_exec, drain, now_str,
)

UI_PORT = 18080
UI_URL  = f"ws://127.0.0.1:{UI_PORT}/ws"


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def ui_server_ctx():
    """Start the UI server as a subprocess, wait until it accepts connections."""
    proc = subprocess.Popen(
        ["python3", "ui/main.py", "config/exchange.cfg", "--port", str(UI_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            socket.create_connection(("127.0.0.1", UI_PORT), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("UI server did not start within 6 s")
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


async def ws_recv_until(ws, pred, timeout=8.0):
    """Drain WebSocket messages until pred(msg) returns True or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("No matching WebSocket message within timeout")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if pred(msg):
            return msg


def is_exec(exec_type):
    return lambda m: m.get("type") == "exec" and m.get("150") == exec_type


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_snapshot_trade_history():
    """Trade history from prior persistence tests must appear in the first snapshot."""
    async def _run():
        async with websockets.connect(UI_URL) as ws:
            snap = await ws_recv_until(ws, lambda m: m["type"] == "snapshot")
            history = snap.get("trade_history", {})
            assert history, \
                "Expected non-empty trade_history in snapshot; check persistence tests ran first"
            sym = next(iter(history))
            entries = history[sym]
            assert entries, f"Expected trade entries for {sym}, got empty list"
            assert "time" in entries[0] and "value" in entries[0], \
                f"Expected {{time, value}} entries, got {entries[0]}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_blotter_exec_replay():
    """exec_log is replayed to every new WebSocket client; ExecType=2 visible on reconnect."""
    async def _run():
        async with websockets.connect(UI_URL) as ws1:
            # Drain the initial snapshot so it doesn't interfere
            await ws_recv_until(ws1, lambda m: m["type"] == "snapshot")

            # Send a sell via the UI's WebSocket — goes through the UI's FIX session
            ws1_send = json.dumps({
                "type": "new_order", "symbol": "AMZN",
                "side": "sell", "price": 9999.0, "qty": 1,
            })
            await ws1.send(ws1_send)
            await ws_recv_until(ws1, is_exec("0"))  # ExecType=New

            # Trigger a fill from a separate FIX session (taker buy crosses the sell)
            comp_id = claim_session()
            try:
                helper = FixSession(sender=comp_id)
                helper.connect()
                helper.logon()
                helper.send("D", {
                    "11": "UI-BLOTTER-BUY",
                    "21": "1",
                    "55": "AMZN",
                    "54": "1",
                    "40": "2",
                    "44": "9999.00",
                    "38": "1",
                    "60": now_str(),
                })
                recv_exec(helper)
                drain(helper, timeout=0.5)
                helper.logout()
                helper.close()
            finally:
                release_session(comp_id)

            # The UI server's _fix_reader delivers the maker fill to WS1
            fill_msg = await ws_recv_until(ws1, lambda m: m.get("type") == "exec" and m.get("150") in ("1", "2"))
            assert fill_msg.get("150") in ("1", "2"), \
                f"Expected fill ExecType on WS1, got {fill_msg.get('150')}"

        # New connection: exec_log should replay the fill without placing a new order
        async with websockets.connect(UI_URL) as ws2:
            replayed = await ws_recv_until(
                ws2,
                lambda m: m.get("type") == "exec" and m.get("150") in ("1", "2"),
            )
            assert replayed.get("150") in ("1", "2"), \
                f"Expected replayed fill ExecType on WS2, got {replayed.get('150')}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_new_order_routes_to_fix():
    """new_order from WebSocket client reaches exchange and returns ExecType=0."""
    async def _run():
        async with websockets.connect(UI_URL) as ws:
            await ws_recv_until(ws, lambda m: m["type"] == "snapshot")
            await ws.send(json.dumps({
                "type": "new_order", "symbol": "AMZN",
                "side": "buy", "price": 0.01, "qty": 1,
            }))
            ack = await ws_recv_until(ws, is_exec("0"))
            assert ack["55"] == "AMZN", f"Expected AMZN exec report, got {ack.get('55')}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_cancel_routes_to_fix():
    """cancel from WebSocket client reaches exchange and returns ExecType=4."""
    async def _run():
        async with websockets.connect(UI_URL) as ws:
            await ws_recv_until(ws, lambda m: m["type"] == "snapshot")

            # Place a resting buy
            await ws.send(json.dumps({
                "type": "new_order", "symbol": "AMZN",
                "side": "buy", "price": 0.01, "qty": 1,
            }))
            ack = await ws_recv_until(ws, is_exec("0"))
            clord_id = ack.get("11")
            assert clord_id, "Expected tag-11 ClOrdID in exec report"

            # Cancel it
            await ws.send(json.dumps({
                "type": "cancel",
                "orig_clord_id": clord_id,
                "symbol": "AMZN",
                "side": "buy",
                "qty": 1,
            }))
            cxl = await ws_recv_until(ws, is_exec("4"))
            assert cxl.get("11") == clord_id or cxl.get("41") == clord_id, \
                f"Expected cancel confirm for {clord_id!r}, got {cxl}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_multi_client_fanout():
    """ExecType=0 from one client's order is fan-out broadcast to all WS clients."""
    async def _run():
        async with websockets.connect(UI_URL) as ws1:
            async with websockets.connect(UI_URL) as ws2:
                # Both drain their snapshots
                await asyncio.gather(
                    ws_recv_until(ws1, lambda m: m["type"] == "snapshot"),
                    ws_recv_until(ws2, lambda m: m["type"] == "snapshot"),
                )

                # WS1 places an order
                await ws1.send(json.dumps({
                    "type": "new_order", "symbol": "AMZN",
                    "side": "buy", "price": 0.01, "qty": 1,
                }))

                # Both clients should receive ExecType=0
                ack1, ack2 = await asyncio.gather(
                    ws_recv_until(ws1, is_exec("0")),
                    ws_recv_until(ws2, is_exec("0")),
                )
                assert ack1.get("55") == "AMZN", f"WS1 expected AMZN exec, got {ack1.get('55')}"
                assert ack2.get("55") == "AMZN", f"WS2 expected AMZN exec, got {ack2.get('55')}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_book_populated_in_snapshot():
    """Order book from the exchange is reflected in the WebSocket snapshot."""
    # Place a resting order BEFORE starting the UI server so the 35=W snapshot
    # the UI server fetches during lifespan startup already contains this level.
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        s.send("D", {
            "11": "UI-BOOK-SNAP",
            "21": "1",
            "55": "AMZN",
            "54": "1",  # buy
            "40": "2",
            "44": "0.01",
            "38": "1",
            "60": now_str(),
        })
        recv_exec(s)
        s.logout()
        s.close()
    finally:
        release_session(comp_id)

    async def _run():
        async with websockets.connect(UI_URL) as ws:
            snap = await ws_recv_until(ws, lambda m: m["type"] == "snapshot")
            book = snap.get("book", {})
            all_bids = [
                level
                for sym_book in book.values()
                for level in sym_book.get("bids", [])
            ]
            assert all_bids, \
                f"Expected at least one bid level in book snapshot, got book={book}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_live_trade_survives_page_reload():
    """Trades that happen after server startup appear in trade_history on reconnect."""
    async def _run():
        # Connect WS1, place a crossing pair, wait for the fill md event.
        async with websockets.connect(UI_URL) as ws1:
            await ws_recv_until(ws1, lambda m: m["type"] == "snapshot")

            comp_id = claim_session()
            try:
                helper = FixSession(sender=comp_id)
                helper.connect()
                helper.logon()
                now = now_str()
                helper.send("D", {"11": "LT-BUY", "21": "1", "55": "AMZN", "54": "1",
                                   "40": "2", "44": "8888.00", "38": "1", "60": now})
                recv_exec(helper)
                helper.send("D", {"11": "LT-SELL", "21": "1", "55": "AMZN", "54": "2",
                                   "40": "2", "44": "8888.00", "38": "1", "60": now})
                recv_exec(helper)
                drain(helper, timeout=0.3)
                helper.logout()
                helper.close()
            finally:
                release_session(comp_id)

            # Wait until the trade md event reaches WS1 so we know trade_history is updated.
            await ws_recv_until(ws1, lambda m: m.get("type") == "md" and m.get("side") == ord('2'))

        # WS2 simulates a page reload — trade must be in the snapshot.
        async with websockets.connect(UI_URL) as ws2:
            snap = await ws_recv_until(ws2, lambda m: m["type"] == "snapshot")
            amzn = snap.get("trade_history", {}).get("AMZN", [])
            assert any(abs(e["value"] - 8888.0) < 1e-9 for e in amzn), \
                f"Expected AMZN trade @ 8888 in snapshot after reload, got {amzn}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_resting_order_in_exec_replay():
    """Resting order placed in the current session appears in exec_log for new WS clients."""
    async def _run():
        async with websockets.connect(UI_URL) as ws1:
            await ws_recv_until(ws1, lambda m: m["type"] == "snapshot")
            await ws1.send(json.dumps({
                "type": "new_order", "symbol": "AMZN",
                "side": "buy", "price": 0.01, "qty": 1,
            }))
            ack = await ws_recv_until(ws1, is_exec("0"))
            clordid = ack.get("11")
            assert clordid, "Expected ClOrdID in ExecType=0"

        # Page reload — exec_log replay must include the resting order
        async with websockets.connect(UI_URL) as ws2:
            replayed = await ws_recv_until(
                ws2,
                lambda m: m.get("type") == "exec" and m.get("11") == clordid,
            )
            assert replayed.get("150") == "0", \
                f"Expected ExecType=0 for resting order in exec replay, got {replayed.get('150')}"

    with ui_server_ctx():
        asyncio.run(_run())


def test_clordid_session_token_changes_on_restart():
    """Each UI server restart produces a distinct session token in ClOrdIDs.

    Without this, reusing seq_counter=0 after restart would recycle ClOrdIDs
    like S1-1, causing historical fills to overwrite resting orders in the blotter.
    """
    async def _place_and_get_clordid():
        async with websockets.connect(UI_URL) as ws:
            await ws_recv_until(ws, lambda m: m["type"] == "snapshot")
            await ws.send(json.dumps({
                "type": "new_order", "symbol": "AMZN",
                "side": "buy", "price": 0.01, "qty": 1,
            }))
            ack = await ws_recv_until(ws, is_exec("0"))
            return ack.get("11")  # format: {comp_id}-{session_ts}-{seq}

    with ui_server_ctx():
        clordid1 = asyncio.run(_place_and_get_clordid())

    # _session_ts uses millisecond precision, so even sub-second restarts produce distinct tokens.
    with ui_server_ctx():
        clordid2 = asyncio.run(_place_and_get_clordid())

    assert clordid1 and clordid2, "Expected ClOrdIDs from both sessions"
    parts1, parts2 = clordid1.split("-"), clordid2.split("-")
    assert len(parts1) == 3 and len(parts2) == 3, \
        f"Expected format comp-ts-seq, got {clordid1!r} / {clordid2!r}"
    assert parts1[1] != parts2[1], \
        f"Expected different session tokens across restarts, got {parts1[1]!r} == {parts2[1]!r}"


def test_snapshot_trade_timestamps_strictly_increasing():
    """trade_history timestamps must be strictly increasing — no two fills share a timestamp.

    _process_md_snapshot applies a monotonic guarantee because the DB stores
    timestamps at second resolution, so rapid fills would otherwise produce
    duplicate timestamps that lightweight-charts silently deduplicates.
    """
    async def _run():
        async with websockets.connect(UI_URL) as ws:
            snap = await ws_recv_until(ws, lambda m: m["type"] == "snapshot")
            for sym, points in snap.get("trade_history", {}).items():
                if len(points) < 2:
                    continue
                times = [p["time"] for p in points]
                for i in range(1, len(times)):
                    assert times[i] > times[i - 1], \
                        f"{sym} trade_history has non-monotonic timestamps at index {i}: " \
                        f"{times[i - 1]} >= {times[i]}"

    with ui_server_ctx():
        asyncio.run(_run())


TESTS = [
    ("UI → snapshot includes trade history from DB",                        test_snapshot_trade_history),
    ("UI → exec_log replayed to new WebSocket client",                      test_blotter_exec_replay),
    ("UI → new_order routes to FIX → ExecType=0 on WebSocket",              test_new_order_routes_to_fix),
    ("UI → cancel routes to FIX → ExecType=4 on WebSocket",                 test_cancel_routes_to_fix),
    ("UI → exec report fan-out to all WebSocket clients",                    test_multi_client_fanout),
    ("UI → order book populated in snapshot",                                test_book_populated_in_snapshot),
    ("UI → live trade appears in trade_history after page reload",           test_live_trade_survives_page_reload),
    ("UI → resting order present in exec_log replay on page reload",         test_resting_order_in_exec_replay),
    ("UI → ClOrdID session token changes across server restarts",            test_clordid_session_token_changes_on_restart),
    ("UI → trade_history timestamps strictly increasing in snapshot",        test_snapshot_trade_timestamps_strictly_increasing),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import time
    from helpers import run_module, FixSession, claim_session, release_session, recv_exec, drain, now_str

    def _setup():
        # Seed an AAPL fill so test_snapshot_trade_history has trade history in the DB.
        comp_id = claim_session()
        try:
            s = FixSession(sender=comp_id)
            s.connect()
            s.logon()
            now = now_str()
            s.send("D", {"11": "UI-SEED-BUY", "21": "1", "55": "AAPL", "54": "1",
                          "40": "2", "44": "100.00", "38": "1", "60": now})
            recv_exec(s)
            s.send("D", {"11": "UI-SEED-SELL", "21": "1", "55": "AAPL", "54": "2",
                          "40": "2", "44": "100.00", "38": "1", "60": now})
            recv_exec(s)
            drain(s, timeout=0.3)
            time.sleep(0.05)  # let persistence thread flush
            s.logout()
            s.close()
        finally:
            release_session(comp_id)

    run_module(TESTS, setup=_setup)
