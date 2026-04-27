#include "OrderBook.h"
#include <algorithm>
#include <stdexcept>

namespace engine {

OrderBook::OrderBook(std::string symbol, FillCallback on_fill)
    : symbol_(std::move(symbol)), on_fill_(std::move(on_fill)) {}

void OrderBook::add(Order order) {
    order.leaves_qty = order.qty;
    try_match(order);
    if (order.leaves_qty > 0 && order.type == '2') {
        if (order.side == '1')
            bids_[order.price].push(order);
        else
            asks_[order.price].push(order);
    }
}

bool OrderBook::cancel(const std::string& order_id) {
    auto scan = [&](auto& levels) {
        for (auto& kv : levels) {
            auto& q = kv.second;
            std::queue<Order> kept;
            bool found = false;
            while (!q.empty()) {
                if (!found && q.front().exchange_id == order_id)
                    found = true;
                else
                    kept.push(q.front());
                q.pop();
            }
            q = std::move(kept);
            if (found) return true;
        }
        return false;
    };
    return scan(bids_) || scan(asks_);
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
        Fill maker = make_fill(resting,   best_price, fill_qty, resting.leaves_qty);
        on_fill_(maker, taker);

        if (resting.leaves_qty == 0)
            q.pop();
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

Fill OrderBook::make_fill(const Order& order, double price, int qty, int leaves) const {
    return Fill{
        symbol_ + "-" + std::to_string(++const_cast<OrderBook*>(this)->exec_seq_),
        order.exchange_id,
        order.client_id,
        symbol_,
        order.side,
        price,
        qty,
        leaves
    };
}

} // namespace engine
