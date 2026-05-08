#include "FixGateway.h"
#include "MessageFactory.h"
#include "market_data/MarketDataPublisher.h"
#include <quickfix/fix42/NewOrderSingle.h>
#include <quickfix/fix42/OrderCancelReplaceRequest.h>
#include <quickfix/fix42/OrderCancelRequest.h>
#include <quickfix/Fields.h>
#include <cmath>
#include <ctime>
#include <iostream>
#include <limits>

namespace gateway {

FixGateway::FixGateway(engine::MatchingEngine& engine,
                       market_data::MarketDataPublisher& publisher,
                       persistence::PersistenceLayer* persistence,
                       risk::RiskConfig risk_cfg)
    : engine_(engine), publisher_(publisher), persistence_(persistence),
      risk_(risk_cfg) {}

void FixGateway::restoreOrders(const std::vector<engine::Order>& orders, int max_seq) {
    std::lock_guard<std::mutex> lock(orders_mutex_);
    for (const auto& o : orders) {
        active_orders_.emplace(o.exchange_id, o);
        clord_to_exchange_.emplace(o.clord_id, o.exchange_id);
        // order_sessions_ left empty — onLogon re-binds when clients reconnect
    }
    if (max_seq > 0) order_seq_.store(max_seq);
}

static FIX42::ExecutionReport make_historical_fill_report(
    const persistence::PersistenceLayer::HistoricalFill& f)
{
    bool is_fill = (f.exec_type == '2');
    FIX42::ExecutionReport msg(
        FIX::OrderID(f.exchange_id),
        FIX::ExecID(f.exchange_id + "-HIST"),
        FIX::ExecTransType('0'),
        FIX::ExecType(f.exec_type),
        FIX::OrdStatus(f.exec_type),
        FIX::Symbol(f.symbol),
        FIX::Side(f.side),
        FIX::LeavesQty(0),
        FIX::CumQty(is_fill ? f.qty : 0),
        FIX::AvgPx(is_fill ? f.price : 0.0)
    );
    msg.set(FIX::ClOrdID(f.clord_id));
    msg.set(FIX::OrderQty(f.qty));
    if (f.price > 0.0)
        msg.set(FIX::Price(f.price));
    if (is_fill)
        msg.set(FIX::LastShares(f.qty));
    return msg;
}

void FixGateway::onLogon(const FIX::SessionID& id) {
    std::cout << "Logon: " << id << "\n";
    std::string client_id = id.getTargetCompID().getValue();
    std::vector<engine::Order> client_orders;
    {
        std::lock_guard<std::mutex> lock(orders_mutex_);
        for (auto& [exch_id, order] : active_orders_) {
            if (order.client_id == client_id) {
                client_orders.push_back(order);
                // Re-bind any restored orders (no session yet) to this reconnecting client
                if (order_sessions_.count(exch_id) == 0)
                    order_sessions_.emplace(exch_id, id);
            }
        }
    }
    for (const auto& order : client_orders) {
        auto msg = make_order_status_report(order);
        FIX::Session::sendToTarget(msg, id);
    }
    if (persistence_) {
        for (const auto& f : persistence_->loadHistoricalFills(client_id)) {
            auto report = make_historical_fill_report(f);
            FIX::Session::sendToTarget(report, id);
        }
    }
}

void FixGateway::onLogout(const FIX::SessionID& id) {
    std::cout << "Logout: " << id << "\n";
}

void FixGateway::fromApp(const FIX::Message& msg, const FIX::SessionID& id)
    throw(FIX::FieldNotFound, FIX::IncorrectDataFormat,
          FIX::IncorrectTagValue, FIX::UnsupportedMessageType) {
    crack(msg, id);
}

void FixGateway::onMessage(const FIX42::NewOrderSingle& msg, const FIX::SessionID& session_id) {
    engine::Order order;
    order.client_id = session_id.getTargetCompID().getValue();

    FIX::ClOrdID  clOrdID;  msg.get(clOrdID);
    FIX::Symbol   symbol;   msg.get(symbol);
    FIX::Side     side;     msg.get(side);
    FIX::OrdType  ordType;  msg.get(ordType);
    FIX::OrderQty orderQty; msg.get(orderQty);

    double raw_qty = orderQty.getValue();
    order.clord_id    = clOrdID.getValue();
    order.exchange_id = "EXCH-" + std::to_string(++order_seq_);
    order.symbol      = symbol.getValue();
    order.side        = side.getValue();
    order.type        = ordType.getValue();

    if (raw_qty <= 0 || raw_qty > std::numeric_limits<int>::max()) {
        auto reject = make_exec_report(order, ExecType::Rejected);
        reject.set(FIX::Text("Invalid order qty"));
        FIX::Session::sendToTarget(reject, session_id);
        return;
    }

    order.qty        = static_cast<int>(raw_qty);
    order.leaves_qty = order.qty;

    if (order.type == '2') {
        FIX::Price price; msg.get(price);
        double p = price.getValue();
        if (p <= 0.0 || !std::isfinite(p)) {
            auto reject = make_exec_report(order, ExecType::Rejected);
            reject.set(FIX::Text("Invalid price"));
            FIX::Session::sendToTarget(reject, session_id);
            return;
        }
        order.price = p;
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

    auto risk_reason = risk_.check(order.symbol, order.type, order.side, order.qty, order.price);
    if (!risk_reason.empty()) {
        auto reject = make_exec_report(order, ExecType::Rejected);
        reject.set(FIX::Text(risk_reason));
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
    req.client_id = session_id.getTargetCompID().getValue();

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
    req.client_id    = session_id.getTargetCompID().getValue();
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
    if (persistence_) {
        persistence::PersistenceEvent evt;
        evt.type       = persistence::PersistenceEvent::REPLACE;
        evt.req        = req;
        evt.leaves_qty = std::max(0, new_leaves_qty);
        persistence_->push(std::move(evt));
    }
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
        if (persistence_) {
            persistence::PersistenceEvent evt;
            evt.type    = persistence::PersistenceEvent::CANCEL;
            evt.str_val = req.orig_order_id;
            evt.order   = order;
            persistence_->push(std::move(evt));
        }
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
            auto oit = active_orders_.find(fill.exchange_id);
            if (oit != active_orders_.end()) {
                order = oit->second;
                if (fill.leaves_qty == 0) {
                    order_sessions_.erase(sit);
                    clord_to_exchange_.erase(oit->second.clord_id);
                    active_orders_.erase(oit);
                } else {
                    oit->second.leaves_qty = fill.leaves_qty;
                }
            } else if (fill.leaves_qty == 0) {
                order_sessions_.erase(sit);
            }
        }
        order.leaves_qty = fill.leaves_qty;

        ExecType et = (fill.leaves_qty == 0) ? ExecType::Fill : ExecType::PartFill;
        auto report = make_exec_report(order, et, &fill);
        FIX::Session::sendToTarget(report, session_id);
    };

    send_fill(maker);
    send_fill(taker);
    risk_.on_trade(maker.symbol, maker.price);
    publisher_.on_fill(maker);
    if (persistence_) {
        persistence::PersistenceEvent maker_evt;
        maker_evt.type = persistence::PersistenceEvent::FILL;
        maker_evt.fill = maker;
        persistence_->push(std::move(maker_evt));

        persistence::PersistenceEvent taker_evt;
        taker_evt.type = persistence::PersistenceEvent::TAKER_FILL;
        taker_evt.fill = taker;
        persistence_->push(std::move(taker_evt));
    }
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
    if (persistence_) {
        persistence::PersistenceEvent evt;
        evt.type       = persistence::PersistenceEvent::RESTED;
        evt.order      = order;
        evt.leaves_qty = leaves_qty;
        persistence_->push(std::move(evt));
    }
}


void FixGateway::onMessage(const FIX42::MarketDataRequest& msg,
                           const FIX::SessionID& session_id)
{
    FIX::MDReqID reqId; msg.get(reqId);
    std::string req_id = reqId.getValue();

    std::unordered_set<std::string> requested;
    FIX::NoRelatedSym numSyms; msg.get(numSyms);
    for (int i = 1; i <= numSyms; ++i) {
        FIX42::MarketDataRequest::NoRelatedSym grp;
        msg.getGroup(i, grp);
        FIX::Symbol sym; grp.get(sym);
        requested.insert(sym.getValue());
    }

    engine_.requestSnapshot(
        [this, req_id, session_id, requested](std::vector<engine::BookSnapshot> snaps) {
            for (const auto& snap : snaps) {
                if (!requested.empty() && !requested.count(snap.symbol))
                    continue;
                auto reply = make_md_snapshot(req_id, snap);
                if (persistence_) {
                    for (const auto& t : persistence_->loadHistoricalTrades(snap.symbol)) {
                        FIX42::MarketDataSnapshotFullRefresh::NoMDEntries grp;
                        grp.set(FIX::MDEntryType('2'));
                        grp.set(FIX::MDEntryPx(t.price));
                        grp.set(FIX::MDEntrySize(t.qty));
                        time_t secs = t.ts / 1'000'000'000LL;
                        int    ms   = (t.ts % 1'000'000'000LL) / 1'000'000LL;
                        struct tm utc {};
                        gmtime_r(&secs, &utc);
                        char date_buf[9], time_buf[13];
                        strftime(date_buf, sizeof(date_buf), "%Y%m%d", &utc);
                        snprintf(time_buf, sizeof(time_buf), "%02d:%02d:%02d.%03d",
                                 utc.tm_hour, utc.tm_min, utc.tm_sec, ms);
                        grp.setField(272, date_buf);
                        grp.setField(273, time_buf);
                        reply.addGroup(grp);
                    }
                }
                FIX::Session::sendToTarget(reply, session_id);
            }
        });
}

} // namespace gateway
