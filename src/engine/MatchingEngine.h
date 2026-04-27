#pragma once
#include "Order.h"
#include "OrderBook.h"
#include <condition_variable>
#include <functional>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <unordered_map>

namespace engine {

using FillCallback   = std::function<void(const Fill& maker, const Fill& taker)>;
using CancelCallback = std::function<void(const CancelRequest& req, bool found)>;

class MatchingEngine {
public:
    MatchingEngine(FillCallback on_fill, CancelCallback on_cancel);
    ~MatchingEngine();

    void start();
    void stop();

    void submit(Order order);
    void cancel(CancelRequest req);

private:
    struct WorkItem {
        enum Tag { ORDER, CANCEL } tag;
        Order order;
        CancelRequest cancel_req;
    };

    void run();
    OrderBook& book_for(const std::string& symbol);

    FillCallback   on_fill_;
    CancelCallback on_cancel_;
    std::unordered_map<std::string, OrderBook> books_;

    std::queue<WorkItem> queue_;
    std::mutex mutex_;
    std::condition_variable cv_;
    bool stop_{false};
    std::thread thread_;
};

} // namespace engine
