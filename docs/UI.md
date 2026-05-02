# Trading UI

A browser-based trading interface that bridges the exchange over WebSocket. Each browser tab gets an independent FIX session, shares a live order book fed from the UDP multicast stream, and can place and cancel orders in real time.

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

Open the respective URL in a browser.

---

## Architecture

```
Browser  ←→  WebSocket (/ws)  ←→  AsyncFixSession (per server)  ←→  exchange :5001
                  FastAPI app
                       ↕
            asyncio UDP datagram endpoint  ←→  multicast 239.1.1.1:5003
```

- **One FIX session per server process.** On startup the backend calls `CLAIM-SESSION` on the admin gateway to obtain a pool slot (e.g. `S1`), connects to the exchange, and holds that session for its entire lifetime. The session is released on shutdown. All WebSocket connections to the same server share this session.
- **Single shared UDP listener.** One asyncio datagram endpoint joins the multicast group and fans out parsed `MdPacket` events to all connected clients. The order book is maintained in memory on the backend and snapshotted to new clients on connect.
- **Exec log.** All execution reports received from the exchange are accumulated in memory. New WebSocket connections replay the full log so the blotter survives page reloads.
- **No external FIX library.** Hand-rolled framing adapted from `tests/test_exchange.py`.

---

## UI layout

```
┌──────────────────────────────────────────────────┐
│  fix-exchange  [AAPL] [MSFT] [GOOG] [AMZN]   ●  │
├──────────────────┬───────────────────────────────┤
│  ORDER BOOK      │  ORDER ENTRY                  │
│  151.00    50    │  Qty: ____   Price: ____       │
│  150.50   200    │  [  Buy  ]  [  Sell  ]        │
│  ─ spread 0.25 ─ │                               │
│  150.25   100    │  BLOTTER                      │
│  149.75   300    │  #  Sym  Side  Qty  Px  Status │
│                  │  1  AAPL BUY  100  150  New   │
├──────────────────┴───────────────────────────────┤
│  TRADES  AAPL 150.25 ×50  MSFT 300.00 ×100       │
└──────────────────────────────────────────────────┘
```

- **Symbol tabs** — switch between symbols; the order book updates live.
- **Order book** — top 10 bids (green) and asks (red) with proportional depth bars. Spread shown between sides.
- **Order entry** — enter qty and price, click Buy or Sell. Orders appear in the blotter immediately on ack.
- **Blotter** — all orders submitted in this session with current status. Click `×` to cancel a resting order.
- **Trade tape** — last 12 trades across all symbols, updated in real time from market data.
- **Status dot** — green when the WebSocket is connected, red otherwise. Reconnects automatically.

---

## WebSocket protocol

All messages are JSON. The browser connects to `ws://<host>:8080/ws`.

### Backend → browser

| `type` | Description |
|--------|-------------|
| `snapshot` | Sent on connect. Contains `symbols` (list), `book` (top-10 bids/asks per symbol as `[[price, qty], ...]`), and `orders` (list of resting orders as `{eid, symbol, side, price, qty}`). |
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
