#pragma once
#include "engine/MatchingEngine.h"
#include "engine/Order.h"
#include <quickfix/Application.h>
#include <quickfix/MessageCracker.h>
#include <quickfix/Session.h>
#include <quickfix/SessionID.h>
#include <quickfix/fix42/ExecutionReport.h>
#include <quickfix/fix42/MarketDataRequest.h>
#include <quickfix/fix42/NewOrderSingle.h>
#include <quickfix/fix42/OrderCancelReplaceRequest.h>
#include <quickfix/fix42/OrderCancelRequest.h>
#include <atomic>
#include <mutex>
#include <set>

namespace market_data { class MarketDataPublisher; }

namespace gateway {

class FixGateway : public FIX::Application, public FIX::MessageCracker {
public:
    FixGateway(engine::MatchingEngine& engine,
               market_data::MarketDataPublisher& publisher);

    void onFill(const engine::Fill& maker, const engine::Fill& taker);
    void onCancel(const engine::CancelRequest& req, bool found);
    void onReplace(const engine::ReplaceRequest& req, bool found, int new_leaves_qty);
    void onTIFCancel(const engine::Order& order);
    void onOrderRested(const engine::Order& order, int leaves_qty);

    // FIX::Application interface
    void onCreate(const FIX::SessionID&) override {}
    void onLogon(const FIX::SessionID& id) override;
    void onLogout(const FIX::SessionID& id) override;
    void toAdmin(FIX::Message&, const FIX::SessionID&) override {}
    void fromAdmin(const FIX::Message&, const FIX::SessionID&)
        throw(FIX::FieldNotFound, FIX::IncorrectDataFormat,
              FIX::IncorrectTagValue, FIX::RejectLogon) override {}
    void toApp(FIX::Message&, const FIX::SessionID&)
        throw(FIX::DoNotSend) override {}
    void fromApp(const FIX::Message& msg, const FIX::SessionID& id)
        throw(FIX::FieldNotFound, FIX::IncorrectDataFormat,
              FIX::IncorrectTagValue, FIX::UnsupportedMessageType) override;

private:
    void onMessage(const FIX42::NewOrderSingle& msg, const FIX::SessionID& id);
    void onMessage(const FIX42::OrderCancelRequest& msg, const FIX::SessionID& id);
    void onMessage(const FIX42::OrderCancelReplaceRequest& msg, const FIX::SessionID& id);
    void onMessage(const FIX42::MarketDataRequest& msg, const FIX::SessionID& id);

    engine::MatchingEngine& engine_;
    market_data::MarketDataPublisher& publisher_;

    std::atomic<int> order_seq_{0};

    std::mutex orders_mutex_;
    std::unordered_map<std::string, FIX::SessionID>   order_sessions_;   // exchange_id → session
    std::unordered_map<std::string, engine::Order>    active_orders_;    // exchange_id → order
    std::unordered_map<std::string, std::string>      clord_to_exchange_; // clord_id → exchange_id
};

} // namespace gateway
