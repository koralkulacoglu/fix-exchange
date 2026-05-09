#pragma once
#include "Order.h"
#include "OrderBook.h"
#include "RingBuffer.h"
#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace engine {

using FillCallback      = std::function<void(const Fill& maker, const Fill& taker)>;
using CancelCallback    = std::function<void(const CancelRequest& req, bool found)>;
using TIFCancelCallback = std::function<void(const Order& order)>;
using SnapshotCallback  = std::function<void(std::vector<BookSnapshot>)>;
using ReplaceCallback   = std::function<void(const ReplaceRequest& req, bool found, int new_leaves_qty)>;
using RestingCallback   = std::function<void(const Order& order, int leaves_qty)>;

class MatchingEngine {
public:
    MatchingEngine(FillCallback on_fill, CancelCallback on_cancel,
                   TIFCancelCallback on_tif_cancel = {},
                   ReplaceCallback on_replace = {},
                   RestingCallback on_order_rested = {},
                   std::vector<std::string> symbols = {});
    ~MatchingEngine();

    void start();
    void stop();

    void submit(Order order);
    void cancel(CancelRequest req);
    void replace(ReplaceRequest req);
    void requestSnapshot(SnapshotCallback cb);

    // Returns false if symbol is already registered or fails validation.
    bool registerSymbol(const std::string& symbol);
    bool isValidSymbol(const std::string& symbol) const;

    // Insert a previously-resting order directly into the book without matching.
    // Must be called single-threaded before start().
    void restoreOrder(const Order& order);

private:
    struct WorkItem {
        enum Tag { ORDER, CANCEL, SNAPSHOT, REPLACE } tag;
        Order order;
        CancelRequest cancel_req;
        ReplaceRequest replace_req;
        SnapshotCallback snapshot_cb;
    };
    static constexpr size_t kQueueSize = 4096;

    void run();
    OrderBook& book_for(const std::string& symbol);

    FillCallback      on_fill_;
    CancelCallback    on_cancel_;
    TIFCancelCallback on_tif_cancel_;
    ReplaceCallback   on_replace_;
    RestingCallback   on_order_rested_;
    std::unordered_map<std::string, OrderBook> books_;

    mutable std::mutex symbols_mutex_;
    std::unordered_set<std::string> valid_symbols_;

    RingBuffer<WorkItem, kQueueSize> queue_;
    std::mutex idle_mutex_;
    std::condition_variable idle_cv_;
    std::atomic<bool> stop_{false};
    std::thread thread_;
};

} // namespace engine
