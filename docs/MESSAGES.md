# Message Reference

## FIX Messages (TCP, port 5001)

| Direction | MsgType | Tag | Purpose |
|-----------|---------|-----|---------|
| Client → Exchange | NewOrderSingle | D | Submit a limit or market order |
| Client → Exchange | OrderCancelRequest | F | Cancel a resting order |
| Client → Exchange | OrderCancelReplaceRequest | G | Modify qty or price of a resting order |
| Exchange → Client | ExecutionReport | 8 | Ack, fill, cancel confirm, or order status |

### ExecutionReport ExecTypes (tag 150)

| Value | Meaning |
|-------|---------|
| `0` | New — order accepted and resting |
| `1` | PartialFill — partial fill, order still resting |
| `2` | Fill — fully filled; also sent on reconnect to replay historical fills |
| `4` | Canceled — cancel confirmed; also sent on reconnect to replay historical cancels |
| `5` | Replaced — replace confirmed (qty or price modified) |
| `8` | Rejected — order rejected (e.g. unknown symbol) |
| `I` | OrderStatus — open/resting order replayed on reconnect |

## UDP Market Data (multicast, default 239.1.1.1:5003)

Market data is published as 46-byte binary packets to a UDP multicast group. Any number of subscribers can receive the feed by joining the group — no FIX session or subscription message required. There is no recovery channel; use `seq` to detect gaps.

### Packet layout (`MdPacket`)

Packed struct, little-endian. Defined in `src/market_data/MarketDataEvent.h`.

| Field | Type | Size | Notes |
|-------|------|------|-------|
| `seq` | uint64 | 8 | Monotonically increasing; gap detection |
| `event_type` | uint8 | 1 | See table below |
| `side` | uint8 | 1 | `'0'`=bid, `'1'`=ask, `'2'`=trade |
| `symbol` | char[8] | 8 | NUL-padded |
| `price` | double | 8 | IEEE 754 |
| `qty` | int32 | 4 | leaves_qty for book events; fill qty for Trade |
| `exchange_id` | char[16] | 16 | NUL-padded |

### Event types

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `NewOrder` | Limit order rested on the book |
| 1 | `Cancel` | Resting order removed |
| 2 | `FillResting` | Resting side updated by a fill (qty = remaining; 0 = fully consumed) |
| 3 | `Trade` | Trade print (qty = filled quantity) |
| 4 | `ReplaceInPlace` | Qty-only reduction at same price |
| 5 | `ReplaceDelete` | First packet of a price-change replace — removes old price level |
| 6 | `ReplaceNew` | Second packet of a price-change replace — adds at new price level |

## Admin Gateway (TCP, port 5002)

Plain-text, line-oriented protocol. Each command is a single `\n`-terminated line; the exchange replies with a single line. Connect with netcat or any TCP client — no FIX session required.

| Command | Response | Description |
|---------|----------|-------------|
| `REGISTER <symbol>` | `OK` or `ERROR: ...` | Register a new trading symbol at runtime. Must be 1–8 alphanumeric characters and not already registered. |
| `CLAIM-SESSION` | `OK <CompID>` or `ERROR: no sessions available` | Claim a free session slot from the pool. Use the returned `CompID` as `SenderCompID` when opening a FIX connection on port 5001. |
| `RELEASE-SESSION <CompID>` | `OK` or `ERROR: unknown session <CompID>` | Return a claimed slot to the pool. With `ResetOnLogout=Y` the slot resets automatically on the next claim regardless. |
| `STATS` | serialized latency samples | Returns raw nanosecond latency samples for the current session; consumed by `bench/bench.py` to compute percentiles. |
| `RESET-STATS` | `OK` | Clears all latency counters. |
| `HELP` | command list | List available commands. |

### Multi-client connect flow

```bash
# 1. Claim a session slot
CompID=$(echo "CLAIM-SESSION" | nc 127.0.0.1 5002 | awk '{print $2}')
# CompID is now e.g. "S3"

# 2. Connect to FIX acceptor using that CompID as SenderCompID
#    (SenderCompID=S3, TargetCompID=EXCHANGE)

# 3. When done, release the slot
echo "RELEASE-SESSION $CompID" | nc 127.0.0.1 5002
```

The pool size is set by `SessionPool` in `config/exchange.cfg` (see [CONFIGURATION.md](CONFIGURATION.md)).

### Python subscriber snippet

```python
import socket, struct

MD_FMT  = "<Q B B 8s d i 16s"
MD_SIZE = struct.calcsize(MD_FMT)  # 46

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", 5003))
mreq = struct.pack("4sL", socket.inet_aton("239.1.1.1"), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

while True:
    data = sock.recv(256)
    seq, event_type, side, symbol_b, price, qty, exch_b = struct.unpack(MD_FMT, data)
    print(seq, event_type, symbol_b.rstrip(b"\x00").decode(), price, qty)
```
