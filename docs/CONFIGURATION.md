# Configuration Reference

The exchange is configured via a QuickFIX-style INI file passed as the first argument:

```bash
./build/fix-exchange config/exchange.cfg
```

The file has one `[DEFAULT]` section (values inherited by all sessions) and one or more `[SESSION]` sections.

---

## [DEFAULT] Settings

| Key | Example | Description |
|-----|---------|-------------|
| `ConnectionType` | `acceptor` | Must be `acceptor` for a server-side exchange. |
| `BeginString` | `FIX.4.2` | FIX protocol version. Only 4.2 is tested. |
| `DataDictionary` | `spec/FIX42.xml` | Path to the FIX message spec. The file ships with the QuickFIX source; a copy is included in `spec/`. |
| `FileStorePath` | `store` | Directory where QuickFIX persists sequence numbers for session recovery. Created automatically. |
| `FileLogPath` | `log` | Directory where QuickFIX writes session and message logs. Created automatically. |
| `StartTime` | `00:00:00` | UTC time the session becomes active. `00:00:00` means always active. |
| `EndTime` | `00:00:00` | UTC time the session deactivates. `00:00:00` means always active. |
| `HeartBtInt` | `30` | Heartbeat interval in seconds. |
| `ResetOnLogon` | `Y` | Reset sequence numbers when a client logs on. Useful during development. Set to `N` in production if you want session continuity across reconnects. |
| `ResetOnLogout` | `Y` | Reset sequence numbers on logout. Same tradeoff as above. |

---

## [SESSION] Settings

Each `[SESSION]` block represents one client connection the exchange will accept. All `[DEFAULT]` values are inherited and can be overridden per session.

| Key | Example | Description |
|-----|---------|-------------|
| `SenderCompID` | `EXCHANGE` | The exchange's CompID. Clients must set `TargetCompID` to this value. |
| `TargetCompID` | `CLIENT` | The expected client CompID. A client connecting must send `SenderCompID=CLIENT`. |
| `SocketAcceptPort` | `5001` | TCP port to listen on. Can differ per session. |

### Multiple clients

To accept more than one client simultaneously, add additional `[SESSION]` blocks with different `TargetCompID` values (they can share the same port):

```ini
[SESSION]
SenderCompID=EXCHANGE
TargetCompID=CLIENT_A
SocketAcceptPort=5001

[SESSION]
SenderCompID=EXCHANGE
TargetCompID=CLIENT_B
SocketAcceptPort=5001
```

Each client must use its own `SenderCompID` when connecting (matching the `TargetCompID` configured above).

---

## Runtime Directories

| Path | Contents |
|------|----------|
| `store/` | Per-session sequence number state. Delete to reset sequence numbers between test runs. |
| `log/` | QuickFIX session logs (`*.log`) and message logs (`*.messages.current.log`). Useful for debugging raw FIX traffic. |

Both directories default to relative paths, resolved from the working directory where you launch the binary. Use absolute paths if you run the exchange from a different directory.

---

## Data Dictionary

`spec/FIX42.xml` is the canonical FIX 4.2 message specification from [quickfixengine.org](http://www.quickfixengine.org). QuickFIX validates every inbound and outbound message against this file. If you receive `Invalid message` session-level rejects, check whether all required fields are present as defined in this XML.

The file was downloaded from the QuickFIX GitHub repository and should not need modification for standard FIX 4.2 usage.
