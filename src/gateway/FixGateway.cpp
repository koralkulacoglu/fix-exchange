#include "FixGateway.h"
#include "MessageFactory.h"
#include "market_data/MarketDataPublisher.h"
#include <quickfix/fix42/MarketDataRequest.h>
#include <quickfix/fix42/NewOrderSingle.h>
#include <quickfix/fix42/OrderCancelReplaceRequest.h>
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

void FixGateway::onMessage(const FIX42::OrderCancelReplaceRequest& msg, const FIX::SessionID& session_id) {
    FIX::ClOrdID     clOrdID;     msg.get(clOrdID);
    FIX::OrigClOrdID origClOrdID; msg.get(origClOrdID);
    FIX::Symbol      symbol;      msg.get(symbol);
    FIX::Side        side;        msg.get(side);
    FIX::OrdType     ordType;     msg.get(ordType);
    FIX::OrderQty    orderQty;    msg.get(orderQty);
    FIX::Price       price;       msg.get(price);

    engine::ReplaceRequest req;
    req.new_clord_id = clOrdID.getValue();
    req.old_clord_id = origClOrdID.getValue();
    req.client_id    = session_id.getSenderCompID().getValue();
    req.symbol       = symbol.getValue();
    req.side         = side.getValue();
    req.new_price    = price.getValue();
    req.new_qty      = static_cast<int>(orderQty.getValue());

    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        auto cit = clord_to_exchange_.find(req.old_clord_id);
        if (cit == clord_to_exchange_.end()) {
            auto reject = make_cancel_reject(req.new_clord_id, req.old_clord_id, "UNKNOWN", "Unknown order");
            FIX::Session::sendToTarget(reject, session_id);
            return;
        }
        req.orig_order_id = cit->second;

        auto oit = active_orders_.find(req.orig_order_id);
        if (oit != active_orders_.end()) {
            const auto& existing = oit->second;
            if (existing.symbol != req.symbol || existing.side != req.side) {
                auto reject = make_cancel_reject(req.new_clord_id, req.old_clord_id,
                                                 req.orig_order_id, "Cannot change symbol or side");
                FIX::Session::sendToTarget(reject, session_id);
                return;
            }
        }
    }

    engine_.replace(std::move(req));
}

void FixGateway::onReplace(const engine::ReplaceRequest& req, bool found, int new_leaves_qty) {
    FIX::SessionID session_id;
    engine::Order order;
    double order_old_price = 0.0;
    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        auto sit = order_sessions_.find(req.orig_order_id);
        auto oit = active_orders_.find(req.orig_order_id);
        if (sit == order_sessions_.end() || oit == active_orders_.end()) return;
        session_id = sit->second;

        if (!found) {
            auto reject = make_cancel_reject(req.new_clord_id, req.old_clord_id,
                                             req.orig_order_id, "Order not found in book");
            FIX::Session::sendToTarget(reject, session_id);
            return;
        }

        double old_price = oit->second.price;
        clord_to_exchange_.erase(req.old_clord_id);
        clord_to_exchange_.emplace(req.new_clord_id, req.orig_order_id);
        oit->second.clord_id   = req.new_clord_id;
        oit->second.price      = req.new_price;
        oit->second.qty        = req.new_qty;
        oit->second.leaves_qty = std::max(0, new_leaves_qty);
        order = oit->second;

        if (new_leaves_qty == 0) {
            order_sessions_.erase(sit);
            active_orders_.erase(oit);
        }
        order_old_price = old_price;
    }
    auto report = make_exec_report(order, ExecType::Replaced);
    report.set(FIX::OrigClOrdID(req.old_clord_id));
    FIX::Session::sendToTarget(report, session_id);
    publisher_.on_replace(req, new_leaves_qty, order_old_price);
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
        publisher_.on_cancel(order);
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

void FixGateway::onOrderRested(const engine::Order& order, int leaves_qty) {
    publisher_.on_new_order(order, leaves_qty);
}

void FixGateway::onMessage(const FIX42::MarketDataRequest& msg, const FIX::SessionID& session_id) {
    FIX::SubscriptionRequestType subType; msg.get(subType);
    bool subscribe = (subType.getValue() == '1');

    FIX::NoRelatedSym noSym; msg.get(noSym);
    std::vector<std::string> symbols;
    for (int i = 1; i <= noSym.getValue(); ++i) {
        FIX42::MarketDataRequest::NoRelatedSym grp;
        msg.getGroup(i, grp);
        FIX::Symbol sym; grp.get(sym);
        symbols.push_back(sym.getValue());
    }

    if (subscribe) {
        publisher_.subscribe(session_id, symbols);
        // Send immediate 35=W snapshot for each subscribed symbol
        engine_.requestSnapshot([session_id, symbols](std::vector<engine::BookSnapshot> snaps) {
            for (const auto& snap : snaps) {
                if (std::find(symbols.begin(), symbols.end(), snap.symbol) == symbols.end()) continue;
                if (snap.bids.empty() && snap.asks.empty()) continue;
                auto msg = gateway::make_market_data_snapshot(snap);
                FIX::Session::sendToTarget(msg, session_id);
            }
        });
    } else {
        publisher_.unsubscribe(session_id, symbols);
    }
}

} // namespace gateway
