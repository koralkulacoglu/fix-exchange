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
    order.client_id  = session_id.getSenderCompID().getValue();

    FIX::ClOrdID  clOrdID;  msg.get(clOrdID);
    FIX::Symbol   symbol;   msg.get(symbol);
    FIX::Side     side;     msg.get(side);
    FIX::OrdType  ordType;  msg.get(ordType);
    FIX::OrderQty orderQty; msg.get(orderQty);

    order.order_id   = clOrdID.getValue();
    order.symbol     = symbol.getValue();
    order.side       = side.getValue();
    order.type       = ordType.getValue();
    order.qty        = static_cast<int>(orderQty.getValue());
    order.leaves_qty = order.qty;

    if (order.type == '2') {
        FIX::Price price; msg.get(price);
        order.price = price.getValue();
    } else {
        order.price = 0.0;
    }

    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        order_sessions_.emplace(order.order_id, session_id);
        active_orders_.emplace(order.order_id, order);
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

    req.orig_order_id = origClOrdID.getValue();
    req.symbol        = symbol.getValue();

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
        {
            std::lock_guard<std::mutex> lock(orders_mutex_);
            auto it = order_sessions_.find(fill.order_id);
            if (it == order_sessions_.end()) return;
            session_id = it->second;
            if (fill.leaves_qty == 0)
                order_sessions_.erase(it);
        }

        engine::Order dummy;
        dummy.order_id   = fill.order_id;
        dummy.client_id  = fill.client_id;
        dummy.symbol     = fill.symbol;
        dummy.side       = fill.side;
        dummy.type       = '2';
        dummy.price      = fill.price;
        dummy.qty        = fill.qty + fill.leaves_qty;
        dummy.leaves_qty = fill.leaves_qty;

        ExecType et = (fill.leaves_qty == 0) ? ExecType::Fill : ExecType::PartFill;
        auto report = make_exec_report(dummy, et, &fill);
        FIX::Session::sendToTarget(report, session_id);
    };

    send_fill(maker);
    send_fill(taker);
    publisher_.on_fill(maker);
}

} // namespace gateway
