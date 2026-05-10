#include "MatchingEngine.h"
#include <algorithm>
#include <cctype>
#include <chrono>
#ifdef __linux__
#include <pthread.h>
#include <sched.h>
#endif

namespace engine {

MatchingEngine::MatchingEngine(FillCallback on_fill, CancelCallback on_cancel,
                               TIFCancelCallback on_tif_cancel,
                               ReplaceCallback on_replace,
                               RestingCallback on_order_rested,
                               std::vector<std::string> symbols)
    : on_fill_(std::move(on_fill)), on_cancel_(std::move(on_cancel)),
      on_tif_cancel_(std::move(on_tif_cancel)), on_replace_(std::move(on_replace)),
      on_order_rested_(std::move(on_order_rested)) {
    for (const auto& sym : symbols) {
        valid_symbols_.insert(sym);
        books_.emplace(sym, OrderBook(sym, on_fill_));
    }
}

bool MatchingEngine::registerSymbol(const std::string& symbol) {
    if (symbol.empty() || symbol.size() > 8)
        return false;
    for (char c : symbol)
        if (!std::isalnum(static_cast<unsigned char>(c)))
            return false;

    std::lock_guard<std::mutex> lock(symbols_mutex_);
    if (valid_symbols_.count(symbol))
        return false;
    valid_symbols_.insert(symbol);
    books_.emplace(symbol, OrderBook(symbol, on_fill_));
    return true;
}

void MatchingEngine::restoreOrder(const Order& order) {
    auto it = books_.find(order.symbol);
    if (it != books_.end())
        it->second.restore(order);
}

bool MatchingEngine::isValidSymbol(const std::string& symbol) const {
    std::lock_guard<std::mutex> lock(symbols_mutex_);
    return valid_symbols_.count(symbol) > 0;
}

MatchingEngine::~MatchingEngine() { stop(); }

void MatchingEngine::start(int core) {
    core_ = core;
    thread_ = std::thread(&MatchingEngine::run, this);
}

void MatchingEngine::stop() {
    stop_.store(true, std::memory_order_release);
    idle_cv_.notify_one();
    if (thread_.joinable())
        thread_.join();
}

void MatchingEngine::submit(Order order) {
    WorkItem item;
    item.tag   = WorkItem::ORDER;
    item.order = std::move(order);
    while (!queue_.push(std::move(item)))
        std::this_thread::yield();
    idle_cv_.notify_one();
}

void MatchingEngine::requestSnapshot(SnapshotCallback cb) {
    WorkItem item;
    item.tag         = WorkItem::SNAPSHOT;
    item.snapshot_cb = std::move(cb);
    while (!queue_.push(std::move(item)))
        std::this_thread::yield();
    idle_cv_.notify_one();
}

void MatchingEngine::replace(ReplaceRequest req) {
    WorkItem item;
    item.tag         = WorkItem::REPLACE;
    item.replace_req = std::move(req);
    while (!queue_.push(std::move(item)))
        std::this_thread::yield();
    idle_cv_.notify_one();
}

void MatchingEngine::cancel(CancelRequest req) {
    WorkItem item;
    item.tag        = WorkItem::CANCEL;
    item.cancel_req = std::move(req);
    while (!queue_.push(std::move(item)))
        std::this_thread::yield();
    idle_cv_.notify_one();
}

void MatchingEngine::run() {
#ifdef __linux__
    if (core_ >= 0) {
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        CPU_SET(core_, &cpuset);
        pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset);
    }
#endif
    while (true) {
        WorkItem item;
        if (!queue_.pop(item)) {
            std::unique_lock<std::mutex> lock(idle_mutex_);
            idle_cv_.wait(lock, [this] {
                return stop_.load(std::memory_order_acquire) || !queue_.empty();
            });
            if (stop_.load(std::memory_order_acquire) && queue_.empty()) break;
            continue;
        }

        auto dequeue_ns = std::chrono::steady_clock::now().time_since_epoch().count();

        if (item.tag == WorkItem::ORDER) {
            Order order = std::move(item.order);
            order.dequeue_ns = dequeue_ns;
            auto& book = book_for(order.symbol);
            if (order.tif == '4') {
                if (book.available_to_fill(order) < order.qty) {
                    order.leaves_qty = order.qty;
                    if (on_tif_cancel_) on_tif_cancel_(order);
                } else {
                    book.add(order);
                }
            } else {
                int leaves = book.add(order);
                if (order.tif == '3' && leaves > 0) {
                    order.leaves_qty = leaves;
                    if (on_tif_cancel_) on_tif_cancel_(order);
                } else if (leaves > 0 && order.type == '2') {
                    if (on_order_rested_) on_order_rested_(order, leaves);
                }
            }
        } else if (item.tag == WorkItem::CANCEL) {
            item.cancel_req.dequeue_ns = dequeue_ns;
            bool found = book_for(item.cancel_req.symbol).cancel(item.cancel_req.orig_order_id);
            on_cancel_(item.cancel_req, found);
        } else if (item.tag == WorkItem::REPLACE) {
            const auto& req = item.replace_req;
            int result = book_for(req.symbol).replace(req.orig_order_id, req.new_price, req.new_qty);
            if (on_replace_) on_replace_(req, result >= 0, result);
        } else {
            std::vector<BookSnapshot> snaps;
            snaps.reserve(books_.size());
            for (auto& [sym, book] : books_) {
                BookSnapshot s;
                s.symbol = sym;
                s.bids   = book.getBids();
                s.asks   = book.getAsks();
                s.orders = book.getOrders();
                snaps.push_back(std::move(s));
            }
            item.snapshot_cb(std::move(snaps));
        }
    }
}

OrderBook& MatchingEngine::book_for(const std::string& symbol) {
    std::lock_guard<std::mutex> lock(symbols_mutex_);
    auto it = books_.find(symbol);
    return it->second; // symbol is always pre-allocated; validated upstream
}

} // namespace engine
