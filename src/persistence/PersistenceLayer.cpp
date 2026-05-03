#include "PersistenceLayer.h"
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <sys/stat.h>

namespace persistence {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static long long now_ns() {
    using namespace std::chrono;
    return static_cast<long long>(
        duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count());
}

static void bind_text(sqlite3_stmt* s, int col, const std::string& v) {
    sqlite3_bind_text(s, col, v.c_str(), -1, SQLITE_TRANSIENT);
}

static void bind_char(sqlite3_stmt* s, int col, char c) {
    char buf[2] = {c, '\0'};
    sqlite3_bind_text(s, col, buf, 1, SQLITE_TRANSIENT);
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

PersistenceLayer::PersistenceLayer(const std::string& path) {
    // Create parent directory if needed
    auto slash = path.rfind('/');
    if (slash != std::string::npos) {
        std::string dir = path.substr(0, slash);
        ::mkdir(dir.c_str(), 0755);
    }

    if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK) {
        std::string err = sqlite3_errmsg(db_);
        sqlite3_close(db_);
        db_ = nullptr;
        throw std::runtime_error("PersistenceLayer: cannot open DB: " + err);
    }
    initSchema();
    thread_ = std::thread(&PersistenceLayer::run, this);
}

PersistenceLayer::~PersistenceLayer() {
    {
        std::lock_guard<std::mutex> lk(mutex_);
        stop_ = true;
    }
    cv_.notify_one();
    if (thread_.joinable())
        thread_.join();
    sqlite3_close(db_);
}

void PersistenceLayer::push(PersistenceEvent evt) {
    {
        std::lock_guard<std::mutex> lk(mutex_);
        queue_.push(std::move(evt));
    }
    cv_.notify_one();
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

void PersistenceLayer::exec(const char* sql) {
    char* err = nullptr;
    if (sqlite3_exec(db_, sql, nullptr, nullptr, &err) != SQLITE_OK) {
        std::string msg = err ? err : "unknown error";
        sqlite3_free(err);
        throw std::runtime_error(std::string("SQLite: ") + msg);
    }
}

void PersistenceLayer::initSchema() {
    exec("PRAGMA journal_mode=WAL");
    exec("PRAGMA synchronous=NORMAL");
    exec("CREATE TABLE IF NOT EXISTS resting_orders ("
         "  exchange_id TEXT PRIMARY KEY,"
         "  clord_id    TEXT NOT NULL,"
         "  client_id   TEXT NOT NULL,"
         "  symbol      TEXT NOT NULL,"
         "  side        TEXT NOT NULL,"
         "  price       REAL NOT NULL,"
         "  leaves_qty  INTEGER NOT NULL"
         ")");
    exec("CREATE TABLE IF NOT EXISTS events ("
         "  seq         INTEGER PRIMARY KEY AUTOINCREMENT,"
         "  ts          INTEGER NOT NULL,"
         "  type        TEXT NOT NULL,"
         "  exchange_id TEXT NOT NULL,"
         "  clord_id    TEXT,"
         "  client_id   TEXT,"
         "  symbol      TEXT,"
         "  side        TEXT,"
         "  price       REAL,"
         "  qty         INTEGER,"
         "  leaves_qty  INTEGER"
         ")");
    exec("CREATE TABLE IF NOT EXISTS symbols ("
         "  symbol TEXT PRIMARY KEY"
         ")");
}

// ---------------------------------------------------------------------------
// Persistence thread
// ---------------------------------------------------------------------------

void PersistenceLayer::run() {
    while (true) {
        std::vector<PersistenceEvent> batch;
        {
            std::unique_lock<std::mutex> lk(mutex_);
            cv_.wait_for(lk, std::chrono::milliseconds(5),
                         [this]{ return stop_ || !queue_.empty(); });
            while (!queue_.empty()) {
                batch.push_back(std::move(queue_.front()));
                queue_.pop();
            }
            if (stop_ && batch.empty()) break;
        }
        if (!batch.empty())
            flush(batch);
    }
}

void PersistenceLayer::flush(std::vector<PersistenceEvent>& batch) {
    exec("BEGIN");
    for (const auto& evt : batch)
        applyEvent(evt);
    exec("COMMIT");
}

void PersistenceLayer::applyEvent(const PersistenceEvent& evt) {
    sqlite3_stmt* s = nullptr;
    long long ts = now_ns();

    switch (evt.type) {

    case PersistenceEvent::RESTED: {
        const auto& o = evt.order;
        sqlite3_prepare_v2(db_,
            "INSERT OR REPLACE INTO resting_orders"
            "(exchange_id,clord_id,client_id,symbol,side,price,leaves_qty)"
            " VALUES(?,?,?,?,?,?,?)", -1, &s, nullptr);
        bind_text(s,1,o.exchange_id); bind_text(s,2,o.clord_id);
        bind_text(s,3,o.client_id);   bind_text(s,4,o.symbol);
        bind_char(s,5,o.side);
        sqlite3_bind_double(s,6,o.price);
        sqlite3_bind_int(s,7,evt.leaves_qty);
        sqlite3_step(s); sqlite3_finalize(s); s = nullptr;

        sqlite3_prepare_v2(db_,
            "INSERT INTO events(ts,type,exchange_id,clord_id,client_id,symbol,side,price,qty,leaves_qty)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", -1, &s, nullptr);
        sqlite3_bind_int64(s,1,ts);
        sqlite3_bind_text(s,2,"rested",-1,SQLITE_STATIC);
        bind_text(s,3,o.exchange_id); bind_text(s,4,o.clord_id);
        bind_text(s,5,o.client_id);   bind_text(s,6,o.symbol);
        bind_char(s,7,o.side);
        sqlite3_bind_double(s,8,o.price);
        sqlite3_bind_int(s,9,o.qty);
        sqlite3_bind_int(s,10,evt.leaves_qty);
        sqlite3_step(s); sqlite3_finalize(s);
        break;
    }

    case PersistenceEvent::FILL: {
        const auto& f = evt.fill;
        if (f.leaves_qty > 0) {
            sqlite3_prepare_v2(db_,
                "UPDATE resting_orders SET leaves_qty=? WHERE exchange_id=?",
                -1, &s, nullptr);
            sqlite3_bind_int(s,1,f.leaves_qty);
            bind_text(s,2,f.exchange_id);
        } else {
            sqlite3_prepare_v2(db_,
                "DELETE FROM resting_orders WHERE exchange_id=?",
                -1, &s, nullptr);
            bind_text(s,1,f.exchange_id);
        }
        sqlite3_step(s); sqlite3_finalize(s); s = nullptr;

        sqlite3_prepare_v2(db_,
            "INSERT INTO events(ts,type,exchange_id,client_id,symbol,side,price,qty,leaves_qty)"
            " VALUES(?,?,?,?,?,?,?,?,?)", -1, &s, nullptr);
        sqlite3_bind_int64(s,1,ts);
        sqlite3_bind_text(s,2,"fill",-1,SQLITE_STATIC);
        bind_text(s,3,f.exchange_id); bind_text(s,4,f.client_id);
        bind_text(s,5,f.symbol);      bind_char(s,6,f.side);
        sqlite3_bind_double(s,7,f.price);
        sqlite3_bind_int(s,8,f.qty);
        sqlite3_bind_int(s,9,f.leaves_qty);
        sqlite3_step(s); sqlite3_finalize(s);
        break;
    }

    case PersistenceEvent::CANCEL: {
        sqlite3_prepare_v2(db_,
            "DELETE FROM resting_orders WHERE exchange_id=?",
            -1, &s, nullptr);
        bind_text(s,1,evt.str_val);
        sqlite3_step(s); sqlite3_finalize(s); s = nullptr;

        sqlite3_prepare_v2(db_,
            "INSERT INTO events(ts,type,exchange_id) VALUES(?,?,?)",
            -1, &s, nullptr);
        sqlite3_bind_int64(s,1,ts);
        sqlite3_bind_text(s,2,"cancel",-1,SQLITE_STATIC);
        bind_text(s,3,evt.str_val);
        sqlite3_step(s); sqlite3_finalize(s);
        break;
    }

    case PersistenceEvent::REPLACE: {
        const auto& req = evt.req;
        if (evt.leaves_qty > 0) {
            sqlite3_prepare_v2(db_,
                "UPDATE resting_orders SET clord_id=?,price=?,leaves_qty=?"
                " WHERE exchange_id=?", -1, &s, nullptr);
            bind_text(s,1,req.new_clord_id);
            sqlite3_bind_double(s,2,req.new_price);
            sqlite3_bind_int(s,3,evt.leaves_qty);
            bind_text(s,4,req.orig_order_id);
        } else {
            sqlite3_prepare_v2(db_,
                "DELETE FROM resting_orders WHERE exchange_id=?",
                -1, &s, nullptr);
            bind_text(s,1,req.orig_order_id);
        }
        sqlite3_step(s); sqlite3_finalize(s); s = nullptr;

        sqlite3_prepare_v2(db_,
            "INSERT INTO events(ts,type,exchange_id,clord_id,price,qty,leaves_qty)"
            " VALUES(?,?,?,?,?,?,?)", -1, &s, nullptr);
        sqlite3_bind_int64(s,1,ts);
        sqlite3_bind_text(s,2,"replace",-1,SQLITE_STATIC);
        bind_text(s,3,req.orig_order_id);
        bind_text(s,4,req.new_clord_id);
        sqlite3_bind_double(s,5,req.new_price);
        sqlite3_bind_int(s,6,req.new_qty);
        sqlite3_bind_int(s,7,evt.leaves_qty);
        sqlite3_step(s); sqlite3_finalize(s);
        break;
    }

    case PersistenceEvent::SYMBOL: {
        sqlite3_prepare_v2(db_,
            "INSERT OR IGNORE INTO symbols(symbol) VALUES(?)",
            -1, &s, nullptr);
        bind_text(s,1,evt.str_val);
        sqlite3_step(s); sqlite3_finalize(s);
        break;
    }
    }
}

// ---------------------------------------------------------------------------
// Recovery reads
// ---------------------------------------------------------------------------

std::vector<std::string> PersistenceLayer::loadSymbols() {
    std::vector<std::string> result;
    sqlite3_stmt* s = nullptr;
    sqlite3_prepare_v2(db_, "SELECT symbol FROM symbols", -1, &s, nullptr);
    while (sqlite3_step(s) == SQLITE_ROW)
        result.emplace_back(
            reinterpret_cast<const char*>(sqlite3_column_text(s, 0)));
    sqlite3_finalize(s);
    return result;
}

std::vector<engine::Order> PersistenceLayer::loadRestingOrders() {
    std::vector<engine::Order> result;
    sqlite3_stmt* s = nullptr;
    sqlite3_prepare_v2(db_,
        "SELECT exchange_id,clord_id,client_id,symbol,side,price,leaves_qty"
        " FROM resting_orders",
        -1, &s, nullptr);
    while (sqlite3_step(s) == SQLITE_ROW) {
        engine::Order o;
        o.exchange_id = reinterpret_cast<const char*>(sqlite3_column_text(s, 0));
        o.clord_id    = reinterpret_cast<const char*>(sqlite3_column_text(s, 1));
        o.client_id   = reinterpret_cast<const char*>(sqlite3_column_text(s, 2));
        o.symbol      = reinterpret_cast<const char*>(sqlite3_column_text(s, 3));
        const char* side = reinterpret_cast<const char*>(sqlite3_column_text(s, 4));
        o.side        = side ? side[0] : '1';
        o.type        = '2'; // all resting orders are limit orders
        o.price       = sqlite3_column_double(s, 5);
        o.qty         = sqlite3_column_int(s, 6);
        o.leaves_qty  = o.qty;
        result.push_back(std::move(o));
    }
    sqlite3_finalize(s);
    return result;
}

int PersistenceLayer::loadMaxOrderSeq() {
    sqlite3_stmt* s = nullptr;
    sqlite3_prepare_v2(db_,
        "SELECT MAX(CAST(SUBSTR(exchange_id,6) AS INTEGER)) FROM resting_orders",
        -1, &s, nullptr);
    int result = 0;
    if (sqlite3_step(s) == SQLITE_ROW &&
        sqlite3_column_type(s, 0) != SQLITE_NULL)
        result = sqlite3_column_int(s, 0);
    sqlite3_finalize(s);
    return result;
}

} // namespace persistence
