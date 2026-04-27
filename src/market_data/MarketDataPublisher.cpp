#include "MarketDataPublisher.h"
#include "gateway/MessageFactory.h"
#include <quickfix/Session.h>

namespace market_data {

void MarketDataPublisher::add_session(const FIX::SessionID& id) {
    std::lock_guard<std::mutex> lock(mutex_);
    sessions_.insert(id);
}

void MarketDataPublisher::remove_session(const FIX::SessionID& id) {
    std::lock_guard<std::mutex> lock(mutex_);
    sessions_.erase(id);
}

void MarketDataPublisher::on_fill(const engine::Fill& fill) {
    auto msg = gateway::make_market_data_refresh(fill);
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& id : sessions_)
        FIX::Session::sendToTarget(msg, id);
}

} // namespace market_data
