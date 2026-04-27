#pragma once
#include "Order.h"
#include <functional>
#include <map>
#include <queue>
#include <string>

namespace engine {

using FillCallback = std::function<void(const Fill& maker, const Fill& taker)>;

class OrderBook {
public:
    explicit OrderBook(std::string symbol, FillCallback on_fill);

    void add(Order order);
    bool cancel(const std::string& order_id);

private:
    void try_match(Order& aggressor);
    template<typename BookSide>
    void match_against(Order& aggressor, BookSide& opposite, bool is_buy);
    Fill make_fill(const Order& order, double price, int qty, int leaves) const;

    std::string symbol_;
    FillCallback on_fill_;
    std::map<double, std::queue<Order>, std::greater<double>> bids_;
    std::map<double, std::queue<Order>> asks_;
    long long exec_seq_{0};
};

} // namespace engine
