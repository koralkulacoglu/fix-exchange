# Performance History

A log of performance-focused changes made to the exchange — what was changed, why, and which issue it came from. Entries are in reverse chronological order (most recent first).

## Changes

- **Engine thread SCHED_FIFO scheduling** (#65)  
  The engine thread now calls `pthread_setschedparam(SCHED_FIFO, priority=1)` at startup alongside the existing CPU affinity call. This prevents the OS from preempting the engine mid-match under normal load, eliminating the class of p99/p999 latency spikes caused by scheduler interference on the engine core. Requires `CAP_SYS_NICE`; degrades gracefully otherwise. Bench launch updated from `chrt -o 0` to `chrt -f 1`.

- **Release build: target ISA, LTO, and loop hardening** (#62)  
  Release builds now specify `-march=icelake-server` (targeting the c6i.metal deployment host), enabling AVX-512 and Ice Lake-specific instruction selection. Added `-funroll-loops` and `-fno-semantic-interposition`, and enabled full LTO (`INTERPROCEDURAL_OPTIMIZATION`) so the compiler can inline and optimize across translation unit boundaries at link time. Debug builds are unaffected.

- **Pre-reserve flat_hash_maps at startup** (#55, #57)  
  `order_index_` in `OrderBook` and the three routing maps in `FixGateway` (`order_sessions_`, `clord_to_exchange_`, `active_orders_`) are reserved to 16384 entries at construction. Pays the page-fault cost at startup before any client connects, eliminating rehash spikes on the matching and fill callback paths.

- **abseil btree_map + flat_hash_map** (#43)  
  Replaced `std::map` price-level maps in `OrderBook` with `absl::btree_map`, which packs multiple keys per B-tree node and reduces pointer hops and TLB pressure on every match. Replaced all `std::unordered_map` instances (`order_index_`, `books_`, gateway routing maps, risk engine) with `absl::flat_hash_map`, which uses open addressing in a flat array instead of separate-chaining with per-bucket pointers. Most visible on the `mixed` scenario (large resting book + cancels).

- **O(1) order cancel** (#8)  
  Replaced `std::deque<Order>` per price level with `std::list<Order>` + an `order_index_` map (exchange_id → list iterator). Cancel went from a full O(n) scan-and-rebuild of the deque to three O(1) pointer operations.

- **TCP_NODELAY / Nagle fix** (#12)  
  A fill generates two back-to-back `ExecReport(Fill)` messages on the same connection. Without `TCP_NODELAY`, Nagle held the second packet until the first was ACK'd; the receiver's delayed-ACK timer held that ACK for up to 40 ms — a classic 40 ms stall. Setting `SocketNodelay=Y` eliminated it. **match p50: 41 ms → 598 µs.**

- **SPSC lock-free ring buffer** (#5)  
  Replaced the `std::queue<WorkItem> + std::mutex + std::condition_variable` work queue with a 4096-slot SPSC ring buffer. The hot path (submit, cancel, replace) now does two atomic loads and one store instead of two mutex acquisitions and a heap allocation per message.

- **Split `orders_mutex_` / fill hot-path** (#35)  
  Added `order_qty`, `order_type`, and `limit_price` to `Fill` so `onFill` can build the `ExecutionReport` without touching `active_orders_`. Split the single `orders_mutex_` into `routing_mutex_` (session routing + clord map) and `orders_mutex_` (active orders only), reducing critical section length on the fill callback path.

- **Engine and persistence thread CPU affinity** (#36)  
  Added opt-in pinning of the matching engine and persistence threads to dedicated CPU cores via `EngineCore` and `PersistenceCore` config keys. When set, `pthread_setaffinity_np` is called at thread startup, preventing OS migrations and cache invalidation under load. Expected gain: reduced p99 and max latency in the `match` scenario. Most visible on a busy or multi-tenant machine.
