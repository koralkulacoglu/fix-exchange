#include "FixGateway.h"
#include "MessageFactory.h"
#include "market_data/MarketDataPublisher.h"
#include <quickfix/fix42/NewOrderSingle.h>
#include <quickfix/fix42/OrderCancelRequest.h>
#include <quickfix/Fields.h>
#include <iostream>

namespace gateway {

FixGateway::FixGateway(engine::MatchingEngine& engine,
                       market_data::MarketDataPublisher& publisher)
    : engine_(engine), publisher_(publisher) {}

void FixGateway::onLogon(const FIX::SessionID& id) {
    std::cout << "Logon: " << id << "\n";
    publisher_.add_session(id);

    std::string client_id = id.getSenderCompID().getValue();
    std::vector<engine::Order> client_orders;
    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        for (const auto& [exch_id, order] : active_orders_)
            if (order.client_id == client_id)
                client_orders.push_back(order);
    }
    for (const auto& order : client_orders) {
        auto msg = make_order_status_report(order);
        FIX::Session::sendToTarget(msg, id);
    }

    engine_.requestSnapshot([id](std::vector<engine::BookSnapshot> snaps) {
        for (const auto& snap : snaps) {
            if (snap.bids.empty() && snap.asks.empty()) continue;
            auto msg = gateway::make_market_data_snapshot(snap);
            FIX::Session::sendToTarget(msg, id);
        }
    });
}

void FixGateway::onLogout(const FIX::SessionID& id) {
    std::cout << "Logout: " << id << "\n";
    publisher_.remove_session(id);
}

void FixGateway::fromApp(const FIX::Message& msg, const FIX::SessionID& id)
    throw(FIX::FieldNotFound, FIX::IncorrectDataFormat,
          FIX::IncorrectTagValue, FIX::UnsupportedMessageType) {
    crack(msg, id);
}

void FixGateway::onMessage(const FIX42::NewOrderSingle& msg, const FIX::SessionID& session_id) {
    engine::Order order;
    order.client_id = session_id.getSenderCompID().getValue();

    FIX::ClOrdID  clOrdID;  msg.get(clOrdID);
    FIX::Symbol   symbol;   msg.get(symbol);
    FIX::Side     side;     msg.get(side);
    FIX::OrdType  ordType;  msg.get(ordType);
    FIX::OrderQty orderQty; msg.get(orderQty);

    order.clord_id    = clOrdID.getValue();
    order.exchange_id = "EXCH-" + std::to_string(++order_seq_);
    order.symbol      = symbol.getValue();
    order.side        = side.getValue();
    order.type        = ordType.getValue();
    order.qty         = static_cast<int>(orderQty.getValue());
    order.leaves_qty  = order.qty;

    if (order.type == '2') {
        FIX::Price price; msg.get(price);
        order.price = price.getValue();
    } else {
        order.price = 0.0;
    }

    if (msg.isSetField(FIX::FIELD::TimeInForce)) {
        FIX::TimeInForce tif; msg.get(tif);
        char v = tif.getValue();
        if (v == '3' || v == '4') order.tif = v;
    }

    if (!engine_.isValidSymbol(order.symbol)) {
        auto reject = make_exec_report(order, ExecType::Rejected);
        reject.set(FIX::Text("Unknown symbol"));
        FIX::Session::sendToTarget(reject, session_id);
        return;
    }

    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        order_sessions_.emplace(order.exchange_id, session_id);
        active_orders_.emplace(order.exchange_id, order);
        clord_to_exchange_.emplace(order.clord_id, order.exchange_id);
    }

    auto ack = make_exec_report(order, ExecType::New);
    FIX::Session::sendToTarget(ack, session_id);

    engine_.submit(std::move(order));
}

void FixGateway::onMessage(const FIX42::OrderCancelRequest& msg, const FIX::SessionID& session_id) {
    engine::CancelRequest req;
    req.client_id = session_id.getSenderCompID().getValue();

    FIX::OrigClOrdID origClOrdID; msg.get(origClOrdID);
    FIX::Symbol      symbol;      msg.get(symbol);
    req.symbol = symbol.getValue();

    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        auto it = clord_to_exchange_.find(origClOrdID.getValue());
        if (it == clord_to_exchange_.end()) return;
        req.orig_order_id = it->second; // resolve ClOrdID → exchange_id
    }

    engine_.cancel(std::move(req));
}

void FixGateway::onCancel(const engine::CancelRequest& req, bool found) {
    FIX::SessionID session_id;
    engine::Order order;
    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        auto sit = order_sessions_.find(req.orig_order_id);
        auto oit = active_orders_.find(req.orig_order_id);
        if (sit == order_sessions_.end() || oit == active_orders_.end()) return;
        session_id = sit->second;
        order      = oit->second;
        if (found) {
            order_sessions_.erase(sit);
            active_orders_.erase(oit);
            clord_to_exchange_.erase(order.clord_id);
        }
    }
    if (found) {
        auto report = make_exec_report(order, ExecType::Canceled);
        FIX::Session::sendToTarget(report, session_id);
    }
}

void FixGateway::onFill(const engine::Fill& maker, const engine::Fill& taker) {
    auto send_fill = [this](const engine::Fill& fill) {
        FIX::SessionID session_id;
        engine::Order order;
        {
            std::lock_guard<std::mutex> lock(orders_mutex_);
            auto sit = order_sessions_.find(fill.exchange_id);
            if (sit == order_sessions_.end()) return;
            session_id = sit->second;
            if (fill.leaves_qty == 0)
                order_sessions_.erase(sit);
            auto oit = active_orders_.find(fill.exchange_id);
            if (oit != active_orders_.end())
                order = oit->second;
        }
        order.leaves_qty = fill.leaves_qty;

        ExecType et = (fill.leaves_qty == 0) ? ExecType::Fill : ExecType::PartFill;
        auto report = make_exec_report(order, et, &fill);
        FIX::Session::sendToTarget(report, session_id);
    };

    send_fill(maker);
    send_fill(taker);
    publisher_.on_fill(maker);
}

void FixGateway::onTIFCancel(const engine::Order& order) {
    FIX::SessionID session_id;
    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        auto sit = order_sessions_.find(order.exchange_id);
        if (sit == order_sessions_.end()) return;
        session_id = sit->second;
        order_sessions_.erase(sit);
        active_orders_.erase(order.exchange_id);
        clord_to_exchange_.erase(order.clord_id);
    }
    auto report = make_tif_cancel_report(order);
    FIX::Session::sendToTarget(report, session_id);
}

} // namespace gateway
