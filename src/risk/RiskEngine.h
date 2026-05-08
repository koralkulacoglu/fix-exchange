#pragma once
#include <mutex>
#include <string>
#include <unordered_map>

namespace risk {

struct RiskConfig {
    int    max_order_qty    {0};    // 0 = disabled
    double price_collar_pct {0.0}; // 0.0 = disabled
};

class RiskEngine {
public:
    explicit RiskEngine(const RiskConfig& cfg);

    // Public entry point — calls all individual checks, returns first failure.
    // Returns "" if all pass. Called from QuickFIX thread.
    std::string check(const std::string& symbol, char type, char side,
                      int qty, double price) const;

    // Called from engine thread (onFill) to track last trade price.
    void on_trade(const std::string& symbol, double price);

private:
    std::string check_qty(int qty) const;
    std::string check_price(const std::string& symbol, char side, double price) const;

    RiskConfig cfg_;
    mutable std::mutex mutex_;
    std::unordered_map<std::string, double> last_price_;
};

} // namespace risk
