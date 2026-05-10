# Performance History

A log of performance-focused changes made to the exchange — what was changed, why, and which commit/issue it came from.

## Changes

- **O(1) order cancel** (`f32a47f`, closes #8)  
  Replaced `std::deque<Order>` per price level with `std::list<Order>` + an `order_index_` map (exchange_id → list iterator). Cancel went from a full O(n) scan-and-rebuild of the deque to three O(1) pointer operations.

- **TCP_NODELAY / Nagle fix** (`e116b08`, v1.3.1, closes #12)  
  A fill generates two back-to-back `ExecReport(Fill)` messages on the same connection. Without `TCP_NODELAY`, Nagle held the second packet until the first was ACK'd; the receiver's delayed-ACK timer held that ACK for up to 40 ms — a classic 40 ms stall. Setting `SocketNodelay=Y` eliminated it. **match p50: 41 ms → 598 µs.**

- **SPSC lock-free ring buffer** (`e702d6d`, v1.11.1, closes #5)  
  Replaced the `std::queue<WorkItem> + std::mutex + std::condition_variable` work queue with a 4096-slot SPSC ring buffer. The hot path (submit, cancel, replace) now does two atomic loads and one store instead of two mutex acquisitions and a heap allocation per message.

- **Split `orders_mutex_` / fill hot-path** (`7f8defa`, v1.11.2, closes #35)  
  Added `order_qty`, `order_type`, and `limit_price` to `Fill` so `onFill` can build the `ExecutionReport` without touching `active_orders_`. Split the single `orders_mutex_` into `routing_mutex_` (session routing + clord map) and `orders_mutex_` (active orders only), reducing critical section length on the fill callback path.
