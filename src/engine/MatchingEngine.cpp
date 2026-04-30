#include "MatchingEngine.h"
#include <algorithm>
#include <cctype>

namespace engine {

MatchingEngine::MatchingEngine(FillCallback on_fill, CancelCallback on_cancel,
                               TIFCancelCallback on_tif_cancel,
                               ReplaceCallback on_replace,
                               std::vector<std::string> symbols)
    : on_fill_(std::move(on_fill)), on_cancel_(std::move(on_cancel)),
      on_tif_cancel_(std::move(on_tif_cancel)), on_replace_(std::move(on_replace)) {
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

bool MatchingEngine::isValidSymbol(const std::string& symbol) const {
    std::lock_guard<std::mutex> lock(symbols_mutex_);
    return valid_symbols_.count(symbol) > 0;
}

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

void MatchingEngine::requestSnapshot(SnapshotCallback cb) {
    WorkItem item;
    item.tag         = WorkItem::SNAPSHOT;
    item.snapshot_cb = std::move(cb);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.push(std::move(item));
    }
    cv_.notify_one();
}

void MatchingEngine::replace(ReplaceRequest req) {
    WorkItem item;
    item.tag         = WorkItem::REPLACE;
    item.replace_req = std::move(req);
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
            Order order = std::move(item.order);
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
                }
            }
        } else if (item.tag == WorkItem::CANCEL) {
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
