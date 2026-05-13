#include "OrderBook.h"
#include <algorithm>
#include <stdexcept>

namespace engine {

OrderBook::OrderBook(std::string symbol, FillCallback on_fill)
    : symbol_(std::move(symbol)), on_fill_(std::move(on_fill)) {
    order_index_.reserve(16384);
}

void OrderBook::restore(Order order) {
    if (order.side == '1') {
        auto& lst = bids_[order.price];
        lst.push_back(order);
        order_index_[order.exchange_id] = std::prev(lst.end());
    } else {
        auto& lst = asks_[order.price];
        lst.push_back(order);
        order_index_[order.exchange_id] = std::prev(lst.end());
    }
}

int OrderBook::add(Order order) {
    order.leaves_qty = order.qty;
    try_match(order);
    if (order.leaves_qty > 0 && order.type == '2' && order.tif != '3') {
        if (order.side == '1') {
            auto& lst = bids_[order.price];
            lst.push_back(order);
            order_index_[order.exchange_id] = std::prev(lst.end());
        } else {
            auto& lst = asks_[order.price];
            lst.push_back(order);
            order_index_[order.exchange_id] = std::prev(lst.end());
        }
    }
    return order.leaves_qty;
}

bool OrderBook::cancel(const std::string& order_id) {
    auto idx_it = order_index_.find(order_id);
    if (idx_it == order_index_.end()) return false;
    auto list_it = idx_it->second;
    double price = list_it->price;
    char side = list_it->side;
    order_index_.erase(idx_it);
    if (side == '1') {
        auto& lst = bids_[price];
        lst.erase(list_it);
        if (lst.empty()) bids_.erase(price);
    } else {
        auto& lst = asks_[price];
        lst.erase(list_it);
        if (lst.empty()) asks_.erase(price);
    }
    return true;
}

int OrderBook::replace(const std::string& order_id, double new_price, int new_qty) {
    auto idx_it = order_index_.find(order_id);
    if (idx_it == order_index_.end()) return -1;

    auto list_it = idx_it->second;

    if (new_price == list_it->price && new_qty < list_it->leaves_qty) {
        list_it->qty = new_qty;
        list_it->leaves_qty = new_qty;
        return new_qty;
    }

    Order order = *list_it;
    double old_price = order.price;
    char side = order.side;
    order_index_.erase(idx_it);
    if (side == '1') {
        auto& lst = bids_[old_price];
        lst.erase(list_it);
        if (lst.empty()) bids_.erase(old_price);
    } else {
        auto& lst = asks_[old_price];
        lst.erase(list_it);
        if (lst.empty()) asks_.erase(old_price);
    }

    order.price = new_price;
    order.qty = new_qty;
    return add(order);
}

template<typename BookSide>
void OrderBook::match_against(Order& aggressor, BookSide& opposite, bool is_buy) {
    while (aggressor.leaves_qty > 0 && !opposite.empty()) {
        auto it = opposite.begin();
        double best_price = it->first;

        bool crosses = (aggressor.type == '1') ||
                       (is_buy ? aggressor.price >= best_price
                               : aggressor.price <= best_price);
        if (!crosses) break;

        auto& q = it->second;
        Order& resting = q.front();
        int fill_qty = std::min(aggressor.leaves_qty, resting.leaves_qty);

        aggressor.leaves_qty -= fill_qty;
        resting.leaves_qty   -= fill_qty;

        Fill taker = make_fill(aggressor, best_price, fill_qty, aggressor.leaves_qty);
        taker.arrival_ns  = aggressor.arrival_ns;
        taker.dequeue_ns  = aggressor.dequeue_ns;
        Fill maker = make_fill(resting,   best_price, fill_qty, resting.leaves_qty);
        on_fill_(maker, taker);

        if (resting.leaves_qty == 0) {
            order_index_.erase(resting.exchange_id);
            q.pop_front();
        }
        if (q.empty())
            opposite.erase(it);
    }
}

void OrderBook::try_match(Order& aggressor) {
    if (aggressor.side == '1')
        match_against(aggressor, asks_, true);
    else
        match_against(aggressor, bids_, false);
}

int OrderBook::available_to_fill(const Order& order) const {
    int total = 0;
    if (order.side == '1') {
        for (auto& kv : asks_) {
            if (order.type == '2' && kv.first > order.price) break;
            for (auto& o : kv.second) total += o.leaves_qty;
        }
    } else {
        for (auto& kv : bids_) {
            if (order.type == '2' && kv.first < order.price) break;
            for (auto& o : kv.second) total += o.leaves_qty;
        }
    }
    return total;
}

std::vector<BookLevel> OrderBook::getBids() const {
    std::vector<BookLevel> out;
    for (const auto& [price, orders] : bids_) {
        int total = 0;
        for (const auto& o : orders) total += o.leaves_qty;
        if (total > 0) out.push_back({price, total});
    }
    return out;
}

std::vector<BookLevel> OrderBook::getAsks() const {
    std::vector<BookLevel> out;
    for (const auto& [price, orders] : asks_) {
        int total = 0;
        for (const auto& o : orders) total += o.leaves_qty;
        if (total > 0) out.push_back({price, total});
    }
    return out;
}

std::vector<Order> OrderBook::getOrders() const {
    std::vector<Order> out;
    for (const auto& [price, orders] : bids_)
        for (const auto& o : orders) out.push_back(o);
    for (const auto& [price, orders] : asks_)
        for (const auto& o : orders) out.push_back(o);
    return out;
}

Fill OrderBook::make_fill(const Order& order, double price, int qty, int leaves) const {
    return Fill{
        symbol_ + "-" + std::to_string(++const_cast<OrderBook*>(this)->exec_seq_),
        order.clord_id,
        order.exchange_id,
        order.client_id,
        symbol_,
        order.side,
        price,
        qty,
        leaves,
        order.qty,    // order_qty
        order.type,   // order_type
        order.price,  // limit_price
    };
}

} // namespace engine
