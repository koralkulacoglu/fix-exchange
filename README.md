# fix-exchange

A single-process equity exchange written in C++. Clients connect over TCP using the FIX 4.2 protocol to submit orders and receive execution reports and market data. A price-time priority matching engine runs on a dedicated thread.

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full component diagram and design decisions.

```mermaid
flowchart LR
    Clients["Clients\n(FIX 4.2 / TCP\nport 5001)"]
    Admin["Admin Client\n(plain TCP\nport 5002)"]
    GW["FIX Gateway\n(QuickFIX acceptor)"]
    ME["Matching Engine\n(single thread)"]
    MDP["Market Data\nPublisher"]

    Clients -->|"D / F"| GW
    GW -->|"8 / W"| Clients
    GW -->|"Order / Cancel\nSnapshotRequest"| ME
    ME -->|"Fill events"| GW
    ME -->|"Fill events"| MDP
    MDP -->|"X"| Clients
    Admin -->|"REGISTER"| ME
```

---

## Dependencies

| Dependency | Version | Install |
|------------|---------|---------|
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

The exchange starts a FIX acceptor on **port 5001** and an admin gateway on **port 5002**. Session logs go to `log/` and sequence number state to `store/`. Both directories are created automatically on first run.

Stop with `Ctrl+C` or `SIGTERM`.

To reset sequence numbers between runs, delete `store/`:

```bash
rm -rf store/
```

---

## Testing

The test suite manages the exchange process itself — no manual server start required:

```bash
python3 tests/test_exchange.py
```

The binary must be built first. Tests connect over raw TCP on port 5001 using hand-rolled FIX framing with no external Python libraries.

### What is tested

| Test | Description |
|------|-------------|
| Logon / Logout | Session establishment and clean teardown |
| NewOrderSingle → ExecReport(New) | Order acknowledgment |
| Order matching | Two crossing limit orders produce fill ExecReports and a MarketDataIncrementalRefresh |
| OrderCancelRequest | Resting order cancelled, ExecReport(Canceled) returned |
| Unknown symbol rejected | Orders for unregistered symbols get ExecReport(Rejected) |
| Admin REGISTER | New symbol registered at runtime via admin port, then accepted |
| IOC — no fill | IOC order with no liquidity is immediately cancelled |
| IOC — partial fill | IOC order fills available qty, remainder cancelled |
| FOK — insufficient qty | FOK order rejected outright if full qty unavailable |
| FOK — full fill | FOK order executes completely when full qty available |
| Snapshot on logon | 35=W snapshot delivered per symbol on client logon |
| Order status on reconnect | ExecType=I reports replayed for client's open orders on reconnect |

---

## Configuration

The config file is a QuickFIX acceptor config extended with an `[EXCHANGE]` section. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for a full reference.

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

[EXCHANGE]
Symbols=AAPL,MSFT,GOOG,AMZN
AdminPort=5002
```

---

## FIX Message Reference

| Direction | MsgType | Tag | Purpose |
|-----------|---------|-----|---------|
| Client → Exchange | NewOrderSingle | D | Submit a limit or market order |
| Client → Exchange | OrderCancelRequest | F | Cancel a resting order |
| Exchange → Client | ExecutionReport | 8 | Ack, fill, cancel confirm, or order status |
| Exchange → Client | MarketDataSnapshotFullRefresh | W | Full book depth on client logon |
| Exchange → Client | MarketDataIncrementalRefresh | X | Last trade broadcast to all sessions |

### ExecutionReport ExecTypes (tag 150)

| Value | Meaning |
|-------|---------|
| `0` | New — order accepted and resting |
| `1` | PartialFill — partial fill, order still resting |
| `2` | Fill — fully filled |
| `4` | Canceled — cancel confirmed |
| `8` | Rejected — order rejected (e.g. unknown symbol) |
| `I` | OrderStatus — open order replayed on reconnect |

---

## Project Layout

```
fix-exchange/
├── CMakeLists.txt
├── config/
│   └── exchange.cfg              QuickFIX + exchange config
├── spec/
│   └── FIX42.xml                 FIX 4.2 data dictionary
├── src/
│   ├── main.cpp                  Entry point — wires components
│   ├── admin/
│   │   └── AdminGateway.h/.cpp   Plain-TCP admin command interface
│   ├── gateway/
│   │   ├── FixGateway.h/.cpp     QuickFIX Application, message parsing
│   │   └── MessageFactory.h      Builds all outbound FIX messages
│   ├── engine/
│   │   ├── Order.h               Order, Fill, CancelRequest, BookSnapshot structs
│   │   ├── OrderBook.h/.cpp      Price-time priority book per symbol
│   │   └── MatchingEngine.h/.cpp Routes orders to books, engine thread
│   └── market_data/
│       └── MarketDataPublisher.h/.cpp  Broadcasts fills to all sessions
└── tests/
    └── test_exchange.py          Integration test suite (pure Python 3)
```
