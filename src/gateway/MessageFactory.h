#pragma once
#include "engine/Order.h"
#include <quickfix/fix42/ExecutionReport.h>
#include <quickfix/fix42/MarketDataSnapshotFullRefresh.h>
#include <quickfix/fix42/OrderCancelReject.h>
#include <string>

namespace gateway {

enum class ExecType : char {
    New         = '0',
    PartFill    = '1',
    Fill        = '2',
    Canceled    = '4',
    Replaced    = '5',
    Rejected    = '8',
    OrderStatus = 'I',
};

inline FIX42::ExecutionReport make_exec_report(
    const engine::Order& order,
    ExecType exec_type,
    const engine::Fill* fill = nullptr)
{
    char et  = static_cast<char>(exec_type);
    int  cum = fill ? (order.qty - fill->leaves_qty) : 0;

    FIX42::ExecutionReport msg(
        FIX::OrderID(order.exchange_id),
        FIX::ExecID(fill ? fill->exec_id : order.exchange_id + "-ACK"),
        FIX::ExecTransType('0'),
        FIX::ExecType(et),
        FIX::OrdStatus(et),
        FIX::Symbol(order.symbol),
        FIX::Side(order.side),
        FIX::LeavesQty(fill ? fill->leaves_qty : order.qty),
        FIX::CumQty(cum),
        FIX::AvgPx(fill ? fill->price : 0.0)
    );

    msg.set(FIX::ClOrdID(order.clord_id));
    msg.set(FIX::OrderQty(order.qty));
    if (order.type == '2')
        msg.set(FIX::Price(order.price));
    if (fill)
        msg.set(FIX::LastShares(fill->qty));

    return msg;
}

inline FIX42::ExecutionReport make_exec_report(const engine::Fill& fill, ExecType exec_type)
{
    char et  = static_cast<char>(exec_type);
    int  cum = fill.order_qty - fill.leaves_qty;

    FIX42::ExecutionReport msg(
        FIX::OrderID(fill.exchange_id),
        FIX::ExecID(fill.exec_id),
        FIX::ExecTransType('0'),
        FIX::ExecType(et),
        FIX::OrdStatus(et),
        FIX::Symbol(fill.symbol),
        FIX::Side(fill.side),
        FIX::LeavesQty(fill.leaves_qty),
        FIX::CumQty(cum),
        FIX::AvgPx(fill.price)
    );
    msg.set(FIX::ClOrdID(fill.clord_id));
    msg.set(FIX::OrderQty(fill.order_qty));
    if (fill.order_type == '2')
        msg.set(FIX::Price(fill.limit_price));
    msg.set(FIX::LastShares(fill.qty));
    return msg;
}

inline FIX42::ExecutionReport make_tif_cancel_report(const engine::Order& order)
{
    char et  = static_cast<char>(ExecType::Canceled);
    int  cum = order.qty - order.leaves_qty;

    FIX42::ExecutionReport msg(
        FIX::OrderID(order.exchange_id),
        FIX::ExecID(order.exchange_id + "-TIF"),
        FIX::ExecTransType('0'),
        FIX::ExecType(et),
        FIX::OrdStatus(et),
        FIX::Symbol(order.symbol),
        FIX::Side(order.side),
        FIX::LeavesQty(0),
        FIX::CumQty(cum),
        FIX::AvgPx(0.0)
    );

    msg.set(FIX::ClOrdID(order.clord_id));
    msg.set(FIX::OrderQty(order.qty));
    if (order.type == '2')
        msg.set(FIX::Price(order.price));

    return msg;
}

inline FIX42::ExecutionReport make_order_status_report(const engine::Order& order)
{
    char ord_status = (order.leaves_qty < order.qty) ? '1' : '0';
    int  cum_qty    = order.qty - order.leaves_qty;

    FIX42::ExecutionReport msg(
        FIX::OrderID(order.exchange_id),
        FIX::ExecID(order.exchange_id + "-STATUS"),
        FIX::ExecTransType('0'),
        FIX::ExecType('I'),
        FIX::OrdStatus(ord_status),
        FIX::Symbol(order.symbol),
        FIX::Side(order.side),
        FIX::LeavesQty(order.leaves_qty),
        FIX::CumQty(cum_qty),
        FIX::AvgPx(0.0)
    );
    msg.set(FIX::ClOrdID(order.clord_id));
    msg.set(FIX::OrderQty(order.qty));
    if (order.type == '2')
        msg.set(FIX::Price(order.price));
    return msg;
}

inline FIX42::OrderCancelReject make_cancel_reject(
    const std::string& clord_id,
    const std::string& orig_clord_id,
    const std::string& exchange_id,
    const std::string& reason)
{
    FIX42::OrderCancelReject msg(
        FIX::OrderID(exchange_id),
        FIX::ClOrdID(clord_id),
        FIX::OrigClOrdID(orig_clord_id),
        FIX::OrdStatus('8'),
        FIX::CxlRejResponseTo('2')
    );
    msg.set(FIX::Text(reason));
    return msg;
}

inline FIX42::MarketDataSnapshotFullRefresh make_md_snapshot(
    const std::string& req_id,
    const engine::BookSnapshot& snap)
{
    FIX42::MarketDataSnapshotFullRefresh msg(FIX::Symbol(snap.symbol));
    msg.set(FIX::MDReqID(req_id));

    for (const auto& order : snap.orders) {
        FIX42::MarketDataSnapshotFullRefresh::NoMDEntries grp;
        grp.set(FIX::MDEntryType(order.side == '1' ? '0' : '1'));
        grp.set(FIX::MDEntryPx(order.price));
        grp.set(FIX::MDEntrySize(order.leaves_qty));
        grp.setField(278, order.exchange_id);
        msg.addGroup(grp);
    }
    return msg;
}

} // namespace gateway
