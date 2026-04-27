#pragma once
#include "engine/Order.h"
#include <quickfix/SessionID.h>
#include <mutex>
#include <set>

namespace market_data {

class MarketDataPublisher {
public:
    void add_session(const FIX::SessionID& id);
    void remove_session(const FIX::SessionID& id);
    void on_fill(const engine::Fill& fill);

private:
    std::mutex mutex_;
    std::set<FIX::SessionID> sessions_;
};

} // namespace market_data
