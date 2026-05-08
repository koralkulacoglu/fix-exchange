#include "RiskEngine.h"
#include <cmath>
#include <iomanip>
#include <sstream>

namespace risk {

RiskEngine::RiskEngine(const RiskConfig& cfg) : cfg_(cfg) {}

std::string RiskEngine::check_qty(int qty) const {
    if (cfg_.max_order_qty > 0 && qty > cfg_.max_order_qty) {
        std::ostringstream ss;
        ss << "Order qty " << qty << " exceeds max " << cfg_.max_order_qty;
        return ss.str();
    }
    return {};
}

std::string RiskEngine::check_price(const std::string& symbol, char side, double price) const {
    if (price == 0.0 || cfg_.price_collar_pct <= 0.0) return {};
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = last_price_.find(symbol);
    if (it == last_price_.end()) return {};
    double last  = it->second;
    double limit = last * cfg_.price_collar_pct / 100.0;
    // Directional: buys are risky when too far above last; sells when too far below.
    bool breached = (side == '1') ? (price > last + limit) : (price < last - limit);
    if (breached) {
        std::ostringstream ss;
        ss << std::fixed << std::setprecision(2);
        ss << "Price " << price << " deviates more than " << cfg_.price_collar_pct
           << "% from last trade " << last;
        return ss.str();
    }
    return {};
}

std::string RiskEngine::check(const std::string& symbol, char type, char side,
                              int qty, double price) const {
    auto r1 = check_qty(qty);
    if (!r1.empty()) return r1;
    auto r2 = check_price(symbol, side, type == '2' ? price : 0.0);
    if (!r2.empty()) return r2;
    return {};
}

void RiskEngine::on_trade(const std::string& symbol, double price) {
    std::lock_guard<std::mutex> lock(mutex_);
    last_price_[symbol] = price;
}

} // namespace risk
