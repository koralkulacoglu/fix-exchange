# Configuration Reference

The exchange is configured via a QuickFIX-style INI file passed as the first argument:

```bash
./build/fix-exchange config/exchange.cfg
```

The file has a `[DEFAULT]` section (values inherited by all sessions), one or more `[SESSION]` sections, and an `[EXCHANGE]` section for exchange-specific settings.

## [DEFAULT] Settings

| Key | Example | Description |
|-----|---------|-------------|
| `ConnectionType` | `acceptor` | Must be `acceptor` for a server-side exchange. |
| `BeginString` | `FIX.4.2` | FIX protocol version. Only 4.2 is tested. |
| `DataDictionary` | `spec/FIX42.xml` | Path to the FIX message spec. A copy ships in `spec/`. |
| `FileStorePath` | `store` | Directory where QuickFIX persists sequence numbers. Created automatically. |
| `FileLogPath` | `log` | Directory where QuickFIX writes session and message logs. Created automatically. |
| `StartTime` | `00:00:00` | UTC time the session becomes active. `00:00:00` means always active. |
| `EndTime` | `00:00:00` | UTC time the session deactivates. `00:00:00` means always active. |
| `HeartBtInt` | `30` | Heartbeat interval in seconds. |
| `ResetOnLogon` | `Y` | Reset sequence numbers when a client logs on. Useful during development. Set to `N` in production for session continuity. |
| `ResetOnLogout` | `Y` | Reset sequence numbers on logout. Same tradeoff as above. |

## [SESSION] Settings

`[SESSION]` blocks are optional. Each one pre-registers a static client CompID that can connect without going through `CLAIM-SESSION`. For most use cases the session pool (`SessionPool` in `[EXCHANGE]`) is sufficient and no `[SESSION]` blocks are needed.

| Key | Example | Description |
|-----|---------|-------------|
| `SenderCompID` | `EXCHANGE` | The exchange's CompID. Clients must set `TargetCompID` to this value. |
| `TargetCompID` | `ALGO1` | The expected client CompID. A connecting client must send `SenderCompID=ALGO1`. |

### Session pool (recommended)

The session pool pre-allocates `N` anonymous slots (`S1`–`SN`) at startup. Clients call `CLAIM-SESSION` on the admin gateway to obtain a `SenderCompID`, connect to the FIX port using it, and return the slot with `RELEASE-SESSION` when done. Pool size is controlled by `SessionPool` in `[EXCHANGE]`.

## [EXCHANGE] Settings

The `[EXCHANGE]` section is not a standard QuickFIX section — it is parsed manually by `main.cpp` and controls exchange-specific behaviour.

| Key | Example | Default | Description |
|-----|---------|---------|-------------|
| `Symbols` | `AAPL,MSFT,GOOG,AMZN` | — | Comma-separated list of symbols to pre-register at startup. Orders for any other symbol are rejected with `ExecutionReport(Rejected)`. Additional symbols can be registered at runtime via the admin gateway. |
| `AdminPort` | `5002` | `5002` | TCP port for the plain-text admin gateway. |
| `MulticastGroup` | `239.1.1.1` | `239.1.1.1` | IPv4 multicast group address for the UDP market data feed. Must be in the locally-scoped range `239.0.0.0/8`. |
| `MulticastPort` | `5003` | `5003` | UDP port subscribers bind to when joining the multicast group. |
| `SessionPool` | `8` | `0` | Number of additional FIX session slots to pre-allocate at startup (named `S1`–`SN`). Clients claim a slot via `CLAIM-SESSION` on the admin gateway before connecting. `0` disables the pool. |
| `DatabasePath` | `store/exchange.db` | *(disabled)* | Path to the SQLite database file used for persistence. If set, resting orders, fills, cancels, and runtime symbol registrations are recorded. On restart the book is restored from this file. Omit to run without persistence (all state is lost on crash). The directory must exist; the file is created if absent. |
| `EngineCore` | `2` | *(unset)* | CPU core number to pin the matching engine thread to at startup (Linux only). If unset, the OS schedules the thread normally. |
| `PersistenceCore` | `3` | *(unset)* | CPU core number to pin the persistence thread to at startup (Linux only). Has no effect when `DatabasePath` is not set. |

## [RISK] Settings

The optional `[RISK]` section configures global pre-trade risk controls. All checks are applied in `FixGateway` before an order is submitted to the matching engine. A rejected order receives `ExecutionReport(ExecType=Rejected)` with the reason in tag 58. All limits default to `0` / `0.0` (disabled).

| Key | Example | Default | Description |
|-----|---------|---------|-------------|
| `MaxOrderQty` | `10000` | `0` | Maximum order quantity. Orders with `OrderQty` greater than this value are rejected. `0` disables the check. |
| `PriceCollarPct` | `5.0` | `0.0` | Directional price collar for limit orders. A buy order whose price exceeds the last trade price by more than N% is rejected; a sell order whose price is more than N% below the last trade price is rejected. The check is skipped if no trade has occurred yet for that symbol. `0.0` disables the check. |

### Admin gateway

The admin gateway listens on `AdminPort` and accepts plain-text commands over TCP. Each command is a single line terminated by `\n`; the exchange replies with a single line. See [docs/MESSAGES.md](MESSAGES.md) for the full admin command reference.

## Runtime Directories

| Path | Contents |
|------|----------|
| `store/` | Per-session sequence number state. Delete to reset sequence numbers between test runs. |
| `log/` | QuickFIX session logs (`*.log`) and message logs (`*.messages.current.log`). Useful for debugging raw FIX traffic. |

Both directories default to relative paths from the working directory where you launch the binary.

## Data Dictionary

`spec/FIX42.xml` is the canonical FIX 4.2 message specification from the QuickFIX project. QuickFIX validates every inbound and outbound message against this file. If you receive `Invalid message` session-level rejects, check that all required fields are present as defined in this XML.

The file should not need modification for standard FIX 4.2 usage.

## Example config

```ini
[DEFAULT]
ConnectionType=acceptor
BeginString=FIX.4.2
DataDictionary=spec/FIX42.xml
FileStorePath=store
FileLogPath=log
StartTime=00:00:00
EndTime=00:00:00
HeartBtInt=30
ResetOnLogon=Y
ResetOnLogout=Y
SocketAcceptPort=5001
SocketNodelay=Y

[EXCHANGE]
Symbols=AAPL,MSFT,GOOG,AMZN
AdminPort=5002
MulticastGroup=239.1.1.1
MulticastPort=5003
SessionPool=8

[RISK]
MaxOrderQty=10000
PriceCollarPct=5.0
```
