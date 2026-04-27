# fix-exchange

A single-process equity exchange written in C++. Clients connect over TCP using the FIX 4.2 protocol to submit orders and receive execution reports. A price-time priority matching engine runs on a dedicated thread and broadcasts fills as `MarketDataIncrementalRefresh` messages to all connected sessions.

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full component diagram and design decisions.

```
Clients (FIX 4.2 / TCP)
       │  NewOrderSingle (D)
       │  OrderCancelRequest (F)
       ▼
  FIX Gateway  ──────────────────────► Matching Engine
  (QuickFIX acceptor)   Order          (single thread)
       ▲                               │
       │  ExecutionReport (8)          │ Fill events
       │  MarketDataIncrementalRefresh ▼
       └──────────────────────── Market Data Publisher
```

---

## Dependencies

| Dependency  | Version | Install |
|-------------|---------|---------|
| g++ or clang++ | C++14+ | system |
| CMake | 3.20+ | system |
| QuickFIX | 1.14+ | `sudo apt install libquickfix-dev` |
| OpenSSL | any | usually pre-installed |

### One-time setup (Ubuntu / WSL2)

```bash
sudo apt install libquickfix-dev
```

---

## Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(nproc)
```

For a release build:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

The binary is placed at `build/fix-exchange`.

---

## Running

```bash
./build/fix-exchange config/exchange.cfg
```

The exchange starts a FIX acceptor on **port 5001** and logs session activity to `log/` and persists sequence numbers to `store/`. Both directories are created automatically on first run.

Stop with `Ctrl+C` or `SIGTERM`.

---

## Testing

An integration test script connects over raw TCP, sends FIX messages, and asserts on the responses. It requires only Python 3 — no additional libraries.

```bash
# Terminal 1 — start the exchange
./build/fix-exchange config/exchange.cfg

# Terminal 2 — run the tests
python3 tests/test_exchange.py
```

### What is tested

| Test | Description |
|------|-------------|
| Logon / Logout | Session establishment and clean teardown |
| NewOrderSingle → ExecReport(New) | Order acknowledgment |
| Order matching | Two crossing limit orders produce fill ExecReports for both parties and a MarketDataIncrementalRefresh broadcast |
| OrderCancelRequest | Resting order is canceled and an ExecReport(Canceled) is returned |

---

## Configuration

The config file is a standard QuickFIX acceptor config. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for a full reference.

Key settings in `config/exchange.cfg`:

```ini
[DEFAULT]
BeginString=FIX.4.2
DataDictionary=spec/FIX42.xml
FileStorePath=store
FileLogPath=log

[SESSION]
SenderCompID=EXCHANGE
TargetCompID=CLIENT
SocketAcceptPort=5001
```

To support multiple clients, add additional `[SESSION]` blocks with different `TargetCompID` values.

---

## FIX Message Reference

| Direction | MsgType | Tag | Purpose |
|-----------|---------|-----|---------|
| Client → Exchange | NewOrderSingle | D | Submit a limit or market order |
| Client → Exchange | OrderCancelRequest | F | Cancel a resting order |
| Exchange → Client | ExecutionReport | 8 | Ack, fill, or cancel confirm |
| Exchange → Client | MarketDataIncrementalRefresh | X | Last trade broadcast to all sessions |

### ExecutionReport ExecTypes

| ExecType (tag 150) | Value | Meaning |
|--------------------|-------|---------|
| New | `0` | Order accepted and resting |
| PartialFill | `1` | Partial fill, order still resting |
| Fill | `2` | Fully filled |
| Canceled | `4` | Cancel confirmed |

---

## Project Layout

```
fix-exchange/
├── CMakeLists.txt
├── config/
│   └── exchange.cfg          QuickFIX acceptor config
├── spec/
│   └── FIX42.xml             FIX 4.2 data dictionary (from QuickFIX)
├── src/
│   ├── main.cpp              Entry point — wires components, starts acceptor
│   ├── gateway/
│   │   ├── FixGateway.h/.cpp QuickFIX Application impl, message parsing
│   │   └── MessageFactory.h  Builds outbound FIX messages
│   ├── engine/
│   │   ├── Order.h           Order, Fill, CancelRequest structs
│   │   ├── OrderBook.h/.cpp  Price-time priority book per symbol
│   │   └── MatchingEngine.h/.cpp  Routes orders to books, engine thread
│   └── market_data/
│       └── MarketDataPublisher.h/.cpp  Broadcasts fills to all sessions
└── tests/
    └── test_exchange.py      Integration test suite (pure Python)
```

---

## Scope (v1)

In scope:
- FIX 4.2 session management via QuickFIX
- Limit and market orders
- Price-time priority matching
- ExecutionReports for new, fill, and cancel
- MarketDataIncrementalRefresh on fill

Out of scope (see ARCHITECTURE.md):
- Persistent order log / recovery
- Risk checks or pre-trade limits
- Order book snapshots
- TLS / authentication
- Stop, IOC, FOK order types
