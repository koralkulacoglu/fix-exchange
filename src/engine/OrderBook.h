#pragma once
#include "Order.h"
#include <functional>
#include <list>
#include <map>
#include <string>
#include <unordered_map>

namespace engine {

using FillCallback = std::function<void(const Fill& maker, const Fill& taker)>;

class OrderBook {
public:
    explicit OrderBook(std::string symbol, FillCallback on_fill);

    int  add(Order order);   // returns leaves_qty after matching
    bool cancel(const std::string& order_id);
    int  available_to_fill(const Order& order) const;

    std::vector<BookLevel> getBids() const;
    std::vector<BookLevel> getAsks() const;

private:
    void try_match(Order& aggressor);
    template<typename BookSide>
    void match_against(Order& aggressor, BookSide& opposite, bool is_buy);
    Fill make_fill(const Order& order, double price, int qty, int leaves) const;

    std::string symbol_;
    FillCallback on_fill_;
    std::map<double, std::list<Order>, std::greater<double>> bids_;
    std::map<double, std::list<Order>> asks_;
    std::unordered_map<std::string, std::list<Order>::iterator> order_index_;
    long long exec_seq_{0};
};

} // namespace engine
