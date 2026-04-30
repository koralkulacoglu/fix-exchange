#pragma once
#include "engine/Order.h"
#include <quickfix/SessionID.h>
#include <quickfix/fix42/MarketDataIncrementalRefresh.h>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace market_data {

class MarketDataPublisher {
public:
    void add_session(const FIX::SessionID& id);
    void remove_session(const FIX::SessionID& id);

    void subscribe(const FIX::SessionID& id, const std::vector<std::string>& symbols);
    void unsubscribe(const FIX::SessionID& id, const std::vector<std::string>& symbols);

    void on_fill(const engine::Fill& maker);
    void on_new_order(const engine::Order& order, int leaves_qty);
    void on_cancel(const engine::Order& order);
    void on_replace(const engine::ReplaceRequest& req, int new_leaves_qty, double old_price);

private:
    void send_to_symbol(const std::string& symbol, FIX42::MarketDataIncrementalRefresh& msg);

    std::mutex mutex_;
    std::unordered_map<std::string, std::set<FIX::SessionID>> symbol_subscribers_;
    std::map<FIX::SessionID, std::set<std::string>> session_symbols_;
};

} // namespace market_data
