#include "MarketDataPublisher.h"
#include "gateway/MessageFactory.h"
#include <quickfix/Session.h>

namespace market_data {

void MarketDataPublisher::add_session(const FIX::SessionID& id) {
    std::lock_guard<std::mutex> lock(mutex_);
    session_symbols_.emplace(id, std::set<std::string>{});
}

void MarketDataPublisher::remove_session(const FIX::SessionID& id) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = session_symbols_.find(id);
    if (it == session_symbols_.end()) return;
    for (const auto& sym : it->second)
        symbol_subscribers_[sym].erase(id);
    session_symbols_.erase(it);
}

void MarketDataPublisher::subscribe(const FIX::SessionID& id,
                                    const std::vector<std::string>& symbols) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& sym : symbols) {
        symbol_subscribers_[sym].insert(id);
        session_symbols_[id].insert(sym);
    }
}

void MarketDataPublisher::unsubscribe(const FIX::SessionID& id,
                                      const std::vector<std::string>& symbols) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& sym : symbols) {
        symbol_subscribers_[sym].erase(id);
        session_symbols_[id].erase(sym);
    }
}

void MarketDataPublisher::send_to_symbol(const std::string& symbol,
                                         FIX42::MarketDataIncrementalRefresh& msg) {
    auto it = symbol_subscribers_.find(symbol);
    if (it == symbol_subscribers_.end()) return;
    for (const auto& id : it->second)
        FIX::Session::sendToTarget(msg, id);
}

void MarketDataPublisher::on_fill(const engine::Fill& maker) {
    char book_side = (maker.side == '1') ? '0' : '1'; // buy maker → bid entry
    char action    = (maker.leaves_qty == 0) ? '2' : '1'; // Delete or Change

    FIX42::MarketDataIncrementalRefresh msg;

    // Resting side update (Change or Delete)
    {
        FIX42::MarketDataIncrementalRefresh::NoMDEntries g;
        g.set(FIX::MDUpdateAction(action));
        g.set(FIX::MDEntryType(book_side));
        g.set(FIX::Symbol(maker.symbol));
        g.set(FIX::MDEntryPx(maker.price));
        g.set(FIX::MDEntrySize(maker.leaves_qty));
        g.set(FIX::MDEntryID(maker.exchange_id));
        msg.addGroup(g);
    }
    // Trade entry
    {
        FIX42::MarketDataIncrementalRefresh::NoMDEntries g;
        g.set(FIX::MDUpdateAction('0')); // New
        g.set(FIX::MDEntryType('2'));    // Trade
        g.set(FIX::Symbol(maker.symbol));
        g.set(FIX::MDEntryPx(maker.price));
        g.set(FIX::MDEntrySize(maker.qty));
        msg.addGroup(g);
    }

    std::lock_guard<std::mutex> lock(mutex_);
    send_to_symbol(maker.symbol, msg);
}

void MarketDataPublisher::on_new_order(const engine::Order& order, int leaves_qty) {
    char book_side = (order.side == '1') ? '0' : '1';
    auto msg = gateway::make_md_increment('0', book_side, order.symbol,
                                          order.price, leaves_qty, order.exchange_id);
    std::lock_guard<std::mutex> lock(mutex_);
    send_to_symbol(order.symbol, msg);
}

void MarketDataPublisher::on_cancel(const engine::Order& order) {
    char book_side = (order.side == '1') ? '0' : '1';
    auto msg = gateway::make_md_increment('2', book_side, order.symbol,
                                          order.price, 0, order.exchange_id);
    std::lock_guard<std::mutex> lock(mutex_);
    send_to_symbol(order.symbol, msg);
}

void MarketDataPublisher::on_replace(const engine::ReplaceRequest& req,
                                     int new_leaves_qty, double old_price) {
    char book_side = (req.side == '1') ? '0' : '1';
    std::lock_guard<std::mutex> lock(mutex_);

    if (old_price == req.new_price) {
        // In-place qty reduction — Change entry
        auto msg = gateway::make_md_increment('1', book_side, req.symbol,
                                              req.new_price, new_leaves_qty,
                                              req.orig_order_id);
        send_to_symbol(req.symbol, msg);
    } else {
        // Price change — Delete old + New at new price
        FIX42::MarketDataIncrementalRefresh msg;
        {
            FIX42::MarketDataIncrementalRefresh::NoMDEntries g;
            g.set(FIX::MDUpdateAction('2')); // Delete
            g.set(FIX::MDEntryType(book_side));
            g.set(FIX::Symbol(req.symbol));
            g.set(FIX::MDEntryPx(old_price));
            g.set(FIX::MDEntrySize(0));
            g.set(FIX::MDEntryID(req.orig_order_id));
            msg.addGroup(g);
        }
        {
            FIX42::MarketDataIncrementalRefresh::NoMDEntries g;
            g.set(FIX::MDUpdateAction('0')); // New
            g.set(FIX::MDEntryType(book_side));
            g.set(FIX::Symbol(req.symbol));
            g.set(FIX::MDEntryPx(req.new_price));
            g.set(FIX::MDEntrySize(new_leaves_qty));
            g.set(FIX::MDEntryID(req.orig_order_id));
            msg.addGroup(g);
        }
        send_to_symbol(req.symbol, msg);
    }
}

} // namespace market_data
