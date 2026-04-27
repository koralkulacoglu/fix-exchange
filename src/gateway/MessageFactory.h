#pragma once
#include "engine/Order.h"
#include <quickfix/fix42/ExecutionReport.h>
#include <quickfix/fix42/MarketDataIncrementalRefresh.h>
#include <string>

namespace gateway {

enum class ExecType : char {
    New      = '0',
    PartFill = '1',
    Fill     = '2',
    Canceled = '4',
    Rejected = '8',
};

inline FIX42::ExecutionReport make_exec_report(
    const engine::Order& order,
    ExecType exec_type,
    const engine::Fill* fill = nullptr)
{
    char et  = static_cast<char>(exec_type);
    int  cum = fill ? (order.qty - fill->leaves_qty) : 0;

    FIX42::ExecutionReport msg(
        FIX::OrderID(order.order_id),
        FIX::ExecID(fill ? fill->exec_id : order.order_id + "-ACK"),
        FIX::ExecTransType('0'),
        FIX::ExecType(et),
        FIX::OrdStatus(et),
        FIX::Symbol(order.symbol),
        FIX::Side(order.side),
        FIX::LeavesQty(fill ? fill->leaves_qty : order.qty),
        FIX::CumQty(cum),
        FIX::AvgPx(fill ? fill->price : 0.0)
    );

    msg.set(FIX::ClOrdID(order.order_id));
    msg.set(FIX::OrderQty(order.qty));
    if (order.type == '2')
        msg.set(FIX::Price(order.price));
    if (fill)
        msg.set(FIX::LastShares(fill->qty));

    return msg;
}

inline FIX42::MarketDataIncrementalRefresh make_market_data_refresh(
    const engine::Fill& fill)
{
    FIX42::MarketDataIncrementalRefresh msg;
    FIX42::MarketDataIncrementalRefresh::NoMDEntries group;
    group.set(FIX::MDUpdateAction('0'));
    group.set(FIX::MDEntryType('2'));
    group.set(FIX::Symbol(fill.symbol));
    group.set(FIX::MDEntryPx(fill.price));
    group.set(FIX::MDEntrySize(fill.qty));
    msg.addGroup(group);
    return msg;
}

} // namespace gateway
