#include "MatchingEngine.h"

namespace engine {

MatchingEngine::MatchingEngine(FillCallback on_fill, CancelCallback on_cancel)
    : on_fill_(std::move(on_fill)), on_cancel_(std::move(on_cancel)) {}

MatchingEngine::~MatchingEngine() { stop(); }

void MatchingEngine::start() {
    thread_ = std::thread(&MatchingEngine::run, this);
}

void MatchingEngine::stop() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
    }
    cv_.notify_one();
    if (thread_.joinable())
        thread_.join();
}

void MatchingEngine::submit(Order order) {
    WorkItem item;
    item.tag   = WorkItem::ORDER;
    item.order = std::move(order);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.push(std::move(item));
    }
    cv_.notify_one();
}

void MatchingEngine::cancel(CancelRequest req) {
    WorkItem item;
    item.tag        = WorkItem::CANCEL;
    item.cancel_req = std::move(req);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.push(std::move(item));
    }
    cv_.notify_one();
}

void MatchingEngine::run() {
    while (true) {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this]{ return stop_ || !queue_.empty(); });
        if (stop_ && queue_.empty()) break;

        WorkItem item = std::move(queue_.front());
        queue_.pop();
        lock.unlock();

        if (item.tag == WorkItem::ORDER) {
            book_for(item.order.symbol).add(std::move(item.order));
        } else {
            bool found = book_for(item.cancel_req.symbol).cancel(item.cancel_req.orig_order_id);
            on_cancel_(item.cancel_req, found);
        }
    }
}

OrderBook& MatchingEngine::book_for(const std::string& symbol) {
    auto it = books_.find(symbol);
    if (it == books_.end()) {
        books_.emplace(symbol, OrderBook(symbol, on_fill_));
        return books_.at(symbol);
    }
    return it->second;
}

} // namespace engine
