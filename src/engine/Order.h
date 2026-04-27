#pragma once
#include <string>

namespace engine {

struct Order {
    std::string order_id;
    std::string client_id;  // FIX SenderCompID
    std::string symbol;
    char side;              // '1' buy, '2' sell
    char type;              // '1' market, '2' limit
    double price;
    int qty;
    int leaves_qty;
};

struct CancelRequest {
    std::string orig_order_id;
    std::string client_id;
    std::string symbol;
};

struct Fill {
    std::string exec_id;
    std::string order_id;
    std::string client_id;
    std::string symbol;
    char side;
    double price;
    int qty;
    int leaves_qty;
};

} // namespace engine
