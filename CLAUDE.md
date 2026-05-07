# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(nproc)
```

Binary is placed at `build/fix-exchange`.

## Python environment

A single venv lives at `.venv/`. Always use it for any Python work in this repo:

```bash
. .venv/bin/activate          # activate
pip install -r requirements.txt  # add deps (then deactivate when done)
```

Never install into system Python.

## Running tests

The test suite spawns and tears down the exchange process itself — no manual server start required:

```bash
python3 tests/run_all.py        # primary runner (30 tests)
python3 tests/test_exchange.py  # backward-compat shim, same result
```

The binary must be built first. Tests connect over raw TCP on port 5001 using hand-rolled FIX framing (no Python FIX library). UI server tests additionally start a uvicorn subprocess on port 18080 and use `websockets` to connect.

Test files are split by theme under `tests/`:

| File | Coverage |
|---|---|
| `helpers.py` | Shared infra: FIX framing, `FixSession`, `UdpMdListener`, exchange lifecycle |
| `test_session.py` | Logon/logout, order status replay, session pool |
| `test_orders.py` | NewOrderSingle, matching, cancel, symbol validation, admin REGISTER |
| `test_tif.py` | IOC and FOK time-in-force semantics |
| `test_replace.py` | OrderCancelReplaceRequest — qty, price, error cases |
| `test_market_data.py` | UDP multicast events, market data snapshot trade history |
| `test_persistence.py` | Crash recovery — resting orders, fills, symbols |
| `test_ui_server.py` | WebSocket API — snapshot, exec replay, order routing, fan-out |

## Running the benchmark

```bash
python3 bench/bench.py                  # all scenarios, 500 iterations each
python3 bench/bench.py --scenario add   # single scenario
python3 bench/bench.py --no-spawn       # connect to an already-running exchange
```

## Running the exchange manually

```bash
./build/fix-exchange config/exchange.cfg
```

FIX acceptor on port 5001. Admin gateway on port 5002. Logs go to `log/`, sequence number state to `store/`. Delete `store/` between test runs to reset sequence numbers.

## Architecture

Three threads, fixed topology:

- **QuickFIX thread** — runs the FIX acceptor, calls into `FixGateway` callbacks
- **Engine thread** — the only thread that touches order books; `MatchingEngine::run()` drains a `std::queue<WorkItem>` via `std::mutex + std::condition_variable`
- **Admin thread** — `AdminGateway` accepts plain-TCP connections for runtime commands (e.g. `REGISTER <symbol>`)

`FixGateway` submits work to the engine via `engine_.submit()` / `engine_.cancel()` (locks the queue). Fills flow back via callbacks (`on_fill_`, `on_cancel_`) invoked **on the engine thread** — `FixGateway::onFill` and `onCancel` must therefore be thread-safe.

`MarketDataPublisher` holds a single UDP multicast socket and an atomic sequence counter. It serializes each book event into a 46-byte `MdPacket` and calls `sendto()` — no subscriber list, no mutex. It is called exclusively from the engine thread.

## Order ID duality

Every order carries two IDs:
- `clord_id` — FIX tag 11, client-assigned, used for cancel references
- `exchange_id` — exchange-assigned (`EXCH-<seq>`), used as the stable internal key

`FixGateway` maintains three maps under `orders_mutex_`: `order_sessions_` (exchange_id → SessionID), `active_orders_` (exchange_id → Order), and `clord_to_exchange_` (clord_id → exchange_id). Cancel requests arrive with a `ClOrdID` and must be resolved to an `exchange_id` before forwarding to the engine.

## Symbol registry

Symbols are loaded at startup from the `[EXCHANGE]` section of the config file (not a standard QuickFIX section — parsed manually in `main.cpp`):

```ini
[EXCHANGE]
Symbols=AAPL,MSFT,GOOG,AMZN
AdminPort=5002
MulticastGroup=239.1.1.1
MulticastPort=5003
```

`MatchingEngine` validates incoming orders against `valid_symbols_` (guarded by `symbols_mutex_`, separate from the work-queue mutex). Orders for unknown symbols are rejected with `ExecutionReport(Rejected)` in the gateway before they reach the engine. New symbols can be registered at runtime via the admin gateway (`REGISTER <symbol>`).

## Issues

Open issues are tracked on GitHub:

- Issue list: https://github.com/koralkulacoglu/fix-exchange/issues
- Project board: https://github.com/users/koralkulacoglu/projects/1/views/1

Use the `mcp__github__list_issues` tool (owner: `koralkulacoglu`, repo: `fix-exchange`) to fetch issues programmatically.

## Docs

Update docs when a change is significant enough that someone reading them would be misled — new config keys, protocol changes, architectural shifts. Skip updates for small internal changes that don't affect how anyone builds, runs, or integrates with the exchange. Keep docs concise; don't pad them.

- `README.md` — intro, build, run, test instructions
- `docs/ARCHITECTURE.md` — component diagram, threading model, data flow
- `docs/CONFIGURATION.md` — `[EXCHANGE]` config keys
- `docs/MESSAGES.md` — FIX message reference, UDP wire format

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `perf:`, `refactor:`, `test:`, `docs:`, `chore:`. Keep the subject line (first line) under 72 characters. Do not add `Co-Authored-By` or any Claude Code attribution to commit messages. When the changes close a GitHub issue, add `Closes #N` at the end of the commit body.

## Key files

- [src/engine/Order.h](src/engine/Order.h) — `Order`, `Fill`, `CancelRequest` structs; change here when adding order fields (e.g. `tif`)
- [src/engine/OrderBook.cpp](src/engine/OrderBook.cpp) — matching logic lives in `try_match` / `match_against`
- [src/gateway/FixGateway.cpp](src/gateway/FixGateway.cpp) — FIX parsing, order ID lifecycle, fill/cancel callbacks
- [src/gateway/MessageFactory.h](src/gateway/MessageFactory.h) — builds all outbound FIX messages; keep FIX field construction here
- [src/market_data/MarketDataEvent.h](src/market_data/MarketDataEvent.h) — `MdPacket` struct and `EventType` enum; change here when modifying the UDP wire format
- [src/market_data/MarketDataPublisher.h/.cpp](src/market_data/MarketDataPublisher.cpp) — UDP multicast publisher; one `sendto()` per book event
- [src/main.cpp](src/main.cpp) — wires all components; reads `[EXCHANGE]` config section
