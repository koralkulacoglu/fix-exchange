# Trading UI

A browser-based trading interface. Each server process claims one FIX session and serves the UI on a port. Place and cancel limit orders, watch the live order book and price chart, and monitor fills in the blotter.

---

## Requirements

Python 3.9+ and a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The exchange must be running before starting the UI.

---

## Running

```bash
python3 ui/main.py
# → Starting fix-exchange UI at http://localhost:8080
```

By default reads `config/exchange.cfg` from the project root. Pass an alternative path as the first argument:

```bash
python3 ui/main.py /path/to/exchange.cfg
```

To run two independent clients on the same machine:

```bash
python3 ui/main.py              # claims S1, serves http://localhost:8080
python3 ui/main.py --port 8081  # claims S2, serves http://localhost:8081
```

---

## Architecture

```
Browser  ←→  WebSocket (/ws)  ←→  AsyncFixSession (per server)  ←→  exchange :5001
                  FastAPI app
                       ↕
            asyncio UDP datagram endpoint  ←→  multicast 239.1.1.1:5003
```

- **One FIX session per server process.** On startup the backend calls `CLAIM-SESSION` on the admin gateway to obtain a pool slot (e.g. `S1`), connects to the exchange, and holds that session for its entire lifetime. The session is released on shutdown.
- **Single shared UDP listener.** One asyncio datagram endpoint joins the multicast group and fans out parsed `MdPacket` events to all connected clients. The order book is maintained in memory on the backend and snapshotted to new clients on connect.
- **Exec log.** All execution reports received from the exchange are accumulated in memory. New WebSocket connections replay the full log so the blotter survives page reloads. Historical fills and cancels are also seeded from the exchange on logon (ExecType=2/4 replay via the persistence layer), so the blotter is populated even after a server restart.
- **Trade history.** Per-symbol trade history is loaded from the exchange at startup via `MarketDataRequest` (35=V) and kept current as live UDP trade packets arrive. Survives page reloads within the same server session; repopulated from the exchange on the next server start.
- **No external FIX library.** Hand-rolled framing shared with `tests/helpers.py`.

---

## WebSocket protocol

All messages are JSON. The browser connects to `ws://<host>:<port>/ws`.

### Backend → browser

| `type` | Description |
|--------|-------------|
| `snapshot` | Sent on connect. Contains `symbols` (list), `book` (top-10 bids/asks per symbol as `[[price, qty], ...]`), `orders` (list of resting orders as `{eid, symbol, side, price, qty}`), and `trade_history` (map of symbol → `[{time, value}, ...]` for the price chart). |
| `md` | Incremental market data update. Fields: `evt`, `side`, `symbol`, `price`, `qty`, `seq`. See [MESSAGES.md](MESSAGES.md) for event type values. |
| `exec` | ExecutionReport forwarded from the exchange. FIX tag numbers as string keys (`"35"`, `"150"`, `"11"`, etc.). |

### Browser → backend

| `type` | Fields | Description |
|--------|--------|-------------|
| `new_order` | `symbol`, `side` (`"buy"`/`"sell"`), `qty`, `price` | Submit a limit order. |
| `cancel` | `orig_clord_id`, `symbol`, `side`, `qty` | Cancel a resting order. `orig_clord_id` comes from the `"11"` tag of the New ack. |

---

## Files

| Path | Description |
|------|-------------|
| `ui/main.py` | FastAPI app: WebSocket endpoint, UDP listener, static file serving, config reading |
| `ui/fix_client.py` | `AsyncFixSession` — async FIX framing over `asyncio.open_connection` |
| `requirements.txt` | `fastapi`, `uvicorn[standard]` |
| `ui/static/index.html` | Single-page trading UI (plain HTML + CSS + JS, no build step) |
