#pragma once
#include "engine/Order.h"
#include <sqlite3.h>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

namespace persistence {

struct PersistenceEvent {
    enum Type { RESTED, FILL, TAKER_FILL, CANCEL, REPLACE, SYMBOL } type;
    engine::Order          order;       // RESTED
    int                    leaves_qty{0}; // RESTED, REPLACE
    engine::Fill           fill;        // FILL
    engine::ReplaceRequest req;         // REPLACE
    std::string            str_val;     // CANCEL: exchange_id; SYMBOL: symbol name
};

class PersistenceLayer {
public:
    explicit PersistenceLayer(const std::string& path, int core = -1);
    ~PersistenceLayer();

    // Recovery reads — single-threaded, called before engine.start()
    std::vector<std::string>   loadSymbols();
    std::vector<engine::Order> loadRestingOrders();
    int                        loadMaxOrderSeq();

    // History reads — safe to call concurrently under WAL mode
    struct HistoricalFill {
        char exec_type;  // '2' = fill, '4' = cancel
        std::string exchange_id, clord_id, symbol;
        char side;
        double price;
        int qty;
        long long ts;
    };
    struct HistoricalTrade {
        double price;
        int qty;
        long long ts;
    };
    std::vector<HistoricalFill>  loadHistoricalFills(const std::string& client_id);
    std::vector<HistoricalTrade> loadHistoricalTrades(const std::string& symbol, int limit = 500);

    // Non-blocking enqueue — called from engine/admin threads
    void push(PersistenceEvent evt);

private:
    sqlite3*                     db_{nullptr};
    std::queue<PersistenceEvent> queue_;
    std::mutex                   mutex_;
    std::condition_variable      cv_;
    std::thread                  thread_;
    bool                         stop_{false};
    int                          core_{-1};

    void initSchema();
    void run();
    void flush(std::vector<PersistenceEvent>& batch);
    void applyEvent(const PersistenceEvent& evt);
    void exec(const char* sql);
};

} // namespace persistence
