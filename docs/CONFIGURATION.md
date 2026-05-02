# Configuration Reference

The exchange is configured via a QuickFIX-style INI file passed as the first argument:

```bash
./build/fix-exchange config/exchange.cfg
```

The file has a `[DEFAULT]` section (values inherited by all sessions), one or more `[SESSION]` sections, and an `[EXCHANGE]` section for exchange-specific settings.

---

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

---

## [SESSION] Settings

Each `[SESSION]` block represents one client connection the exchange will accept. All `[DEFAULT]` values are inherited and can be overridden per session.

| Key | Example | Description |
|-----|---------|-------------|
| `SenderCompID` | `EXCHANGE` | The exchange's CompID. Clients must set `TargetCompID` to this value. |
| `TargetCompID` | `CLIENT` | The expected client CompID. A connecting client must send `SenderCompID=CLIENT`. |
| `SocketAcceptPort` | `5001` | TCP port to listen on. |

### Multiple clients

The recommended way to support multiple simultaneous FIX clients is the session pool (`SessionPool` in `[EXCHANGE]`). At startup the exchange pre-allocates `N` anonymous session slots (`S1`–`SN`) and exposes `CLAIM-SESSION` / `RELEASE-SESSION` on the admin gateway so clients discover their `SenderCompID` at runtime.

Additional `[SESSION]` blocks with fixed `TargetCompID` values can still be added manually for static clients that use a known CompID.

---

## [EXCHANGE] Settings

The `[EXCHANGE]` section is not a standard QuickFIX section — it is parsed manually by `main.cpp` and controls exchange-specific behaviour.

| Key | Example | Default | Description |
|-----|---------|---------|-------------|
| `Symbols` | `AAPL,MSFT,GOOG,AMZN` | — | Comma-separated list of symbols to pre-register at startup. Orders for any other symbol are rejected with `ExecutionReport(Rejected)`. Additional symbols can be registered at runtime via the admin gateway. |
| `AdminPort` | `5002` | `5002` | TCP port for the plain-text admin gateway. |
| `MulticastGroup` | `239.1.1.1` | `239.1.1.1` | IPv4 multicast group address for the UDP market data feed. Must be in the locally-scoped range `239.0.0.0/8`. |
| `MulticastPort` | `5003` | `5003` | UDP port subscribers bind to when joining the multicast group. |
| `SessionPool` | `8` | `0` | Number of additional FIX session slots to pre-allocate at startup (named `S1`–`SN`). Clients claim a slot via `CLAIM-SESSION` on the admin gateway before connecting. `0` disables the pool. |

### Admin gateway

The admin gateway listens on `AdminPort` and accepts plain-text commands over TCP. Each command is a single line terminated by `\n`; the exchange replies with a single line. See [docs/MESSAGES.md](MESSAGES.md) for the full admin command reference.

---

## Runtime Directories

| Path | Contents |
|------|----------|
| `store/` | Per-session sequence number state. Delete to reset sequence numbers between test runs. |
| `log/` | QuickFIX session logs (`*.log`) and message logs (`*.messages.current.log`). Useful for debugging raw FIX traffic. |

Both directories default to relative paths from the working directory where you launch the binary.

---

## Data Dictionary

`spec/FIX42.xml` is the canonical FIX 4.2 message specification from the QuickFIX project. QuickFIX validates every inbound and outbound message against this file. If you receive `Invalid message` session-level rejects, check that all required fields are present as defined in this XML.

The file should not need modification for standard FIX 4.2 usage.

---

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

[SESSION]
SenderCompID=EXCHANGE
TargetCompID=CLIENT
SocketAcceptPort=5001

[EXCHANGE]
Symbols=AAPL,MSFT,GOOG,AMZN
AdminPort=5002
MulticastGroup=239.1.1.1
MulticastPort=5003
SessionPool=8
```
