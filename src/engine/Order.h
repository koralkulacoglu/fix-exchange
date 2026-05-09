#pragma once
#include <string>
#include <vector>

namespace engine {

struct Order {
    std::string clord_id;      // FIX tag 11 — client-assigned reference
    std::string exchange_id;   // FIX tag 37 — exchange-assigned, e.g. "EXCH-1"
    std::string client_id;     // FIX SenderCompID
    std::string symbol;
    char side;                 // '1' buy, '2' sell
    char type;                 // '1' market, '2' limit
    double price;
    int qty;
    int leaves_qty;
    char tif{'0'};             // '0'=GTC (default), '3'=IOC, '4'=FOK (FIX tag 59)
    int64_t arrival_ns{0};    // steady_clock ns at FixGateway::onMessage() entry
    int64_t dequeue_ns{0};    // steady_clock ns when engine thread dequeues the order
};

struct CancelRequest {
    std::string orig_order_id; // exchange_id of the order to cancel (resolved by gateway)
    std::string client_id;
    std::string symbol;
    int64_t arrival_ns{0};    // steady_clock ns at FixGateway::onMessage(OrderCancelRequest) entry
    int64_t dequeue_ns{0};    // steady_clock ns when engine thread dequeues the request
};

struct ReplaceRequest {
    std::string orig_order_id; // exchange_id being replaced
    std::string old_clord_id;  // previous ClOrdID (for map cleanup)
    std::string new_clord_id;  // new ClOrdID from 35=G tag 11
    std::string client_id;
    std::string symbol;
    char side;
    double new_price;
    int new_qty;
};

struct Fill {
    std::string exec_id;
    std::string clord_id;      // FIX tag 11 of the filled order
    std::string exchange_id;   // exchange_id of the filled order
    std::string client_id;
    std::string symbol;
    char side;
    double price;              // execution price
    int qty;                   // fill quantity
    int leaves_qty;
    int    order_qty{0};       // original order quantity
    char   order_type{'0'};    // '1' market, '2' limit
    double limit_price{0.0};   // limit price (meaningful when order_type == '2')
    int64_t arrival_ns{0};    // copied from Order::arrival_ns (taker only; 0 for maker)
    int64_t dequeue_ns{0};    // copied from Order::dequeue_ns (taker only; 0 for maker)
};

struct BookLevel {
    double price;
    int qty;
};

struct BookSnapshot {
    std::string symbol;
    std::vector<BookLevel> bids;  // price-descending
    std::vector<BookLevel> asks;  // price-ascending
    std::vector<Order> orders;    // individual resting orders
};

} // namespace engine
