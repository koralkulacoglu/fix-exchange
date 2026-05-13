#pragma once
#include "Order.h"
#include <absl/container/btree_map.h>
#include <absl/container/flat_hash_map.h>
#include <functional>
#include <list>
#include <string>

namespace engine {

using FillCallback = std::function<void(const Fill& maker, const Fill& taker)>;

class OrderBook {
public:
    explicit OrderBook(std::string symbol, FillCallback on_fill);

    int  add(Order order);   // returns leaves_qty after matching
    void restore(Order order); // insert directly without matching (crash recovery)
    bool cancel(const std::string& order_id);
    int  replace(const std::string& order_id, double new_price, int new_qty); // returns new leaves_qty, -1 if not found
    int  available_to_fill(const Order& order) const;

    std::vector<BookLevel> getBids() const;
    std::vector<BookLevel> getAsks() const;
    std::vector<Order> getOrders() const;

private:
    void try_match(Order& aggressor);
    template<typename BookSide>
    void match_against(Order& aggressor, BookSide& opposite, bool is_buy);
    Fill make_fill(const Order& order, double price, int qty, int leaves) const;

    std::string symbol_;
    FillCallback on_fill_;
    absl::btree_map<double, std::list<Order>, std::greater<double>> bids_;
    absl::btree_map<double, std::list<Order>> asks_;
    absl::flat_hash_map<std::string, std::list<Order>::iterator> order_index_;
    long long exec_seq_{0};
};

} // namespace engine
