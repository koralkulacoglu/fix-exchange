#include "admin/AdminGateway.h"
#include "engine/MatchingEngine.h"
#include "gateway/FixGateway.h"
#include "market_data/MarketDataPublisher.h"
#include "persistence/PersistenceLayer.h"
#include "risk/RiskEngine.h"

#include <quickfix/FileLog.h>
#include <quickfix/FileStore.h>
#include <quickfix/SessionSettings.h>
#include <quickfix/SocketAcceptor.h>

#include <chrono>
#include <csignal>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static volatile std::sig_atomic_t g_stop = 0;

// Parse a simple ini file for a named section's key.
static std::string read_cfg_value(const std::string& path,
                                   const std::string& section,
                                   const std::string& key) {
    std::ifstream f(path);
    bool in_section = false;
    std::string line;
    while (std::getline(f, line)) {
        if (line == section) { in_section = true; continue; }
        if (!line.empty() && line[0] == '[') { in_section = false; continue; }
        if (!in_section) continue;
        auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        if (line.substr(0, eq) == key)
            return line.substr(eq + 1);
    }
    return {};
}

static std::string read_exchange_value(const std::string& path, const std::string& key) {
    return read_cfg_value(path, "[EXCHANGE]", key);
}

static std::vector<std::string> parse_symbols(const std::string& path) {
    std::string val = read_exchange_value(path, "Symbols");
    std::vector<std::string> result;
    std::stringstream ss(val);
    std::string tok;
    while (std::getline(ss, tok, ','))
        if (!tok.empty()) result.push_back(tok);
    return result;
}

static int parse_admin_port(const std::string& path) {
    std::string val = read_exchange_value(path, "AdminPort");
    return val.empty() ? 5002 : std::stoi(val);
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: fix-exchange <config-file>\n";
        return 1;
    }

    try {
        std::string config_path(argv[1]);
        auto symbols   = parse_symbols(config_path);
        int  admin_port = parse_admin_port(config_path);

        std::string mcast_group = read_exchange_value(config_path, "MulticastGroup");
        if (mcast_group.empty()) mcast_group = "239.1.1.1";
        std::string mcast_port_str = read_exchange_value(config_path, "MulticastPort");
        uint16_t mcast_port = mcast_port_str.empty()
            ? 5003 : static_cast<uint16_t>(std::stoi(mcast_port_str));
        market_data::MarketDataPublisher publisher(mcast_group, mcast_port);

        std::string db_path = read_exchange_value(config_path, "DatabasePath");
        std::unique_ptr<persistence::PersistenceLayer> persistence;
        if (!db_path.empty())
            persistence = std::unique_ptr<persistence::PersistenceLayer>(
                new persistence::PersistenceLayer(db_path));

        gateway::FixGateway* gw_ptr = nullptr;

        engine::MatchingEngine engine(
            [&](const engine::Fill& maker, const engine::Fill& taker) {
                if (gw_ptr) gw_ptr->onFill(maker, taker);
            },
            [&](const engine::CancelRequest& req, bool found) {
                if (gw_ptr) gw_ptr->onCancel(req, found);
            },
            [&](const engine::Order& o) {
                if (gw_ptr) gw_ptr->onTIFCancel(o);
            },
            [&](const engine::ReplaceRequest& req, bool found, int leaves) {
                if (gw_ptr) gw_ptr->onReplace(req, found, leaves);
            },
            [&](const engine::Order& o, int leaves) {
                if (gw_ptr) gw_ptr->onOrderRested(o, leaves);
            },
            symbols
        );

        risk::RiskConfig risk_cfg;
        auto mqo = read_cfg_value(config_path, "[RISK]", "MaxOrderQty");
        if (!mqo.empty()) risk_cfg.max_order_qty = std::stoi(mqo);
        auto pcp = read_cfg_value(config_path, "[RISK]", "PriceCollarPct");
        if (!pcp.empty()) risk_cfg.price_collar_pct = std::stod(pcp);

        gateway::FixGateway gateway(engine, publisher, persistence.get(), risk_cfg);
        gw_ptr = &gateway;

        FIX::SessionSettings settings(argv[1]);

        std::string pool_str = read_exchange_value(config_path, "SessionPool");
        int pool_size = pool_str.empty() ? 0 : std::stoi(pool_str);
        std::vector<std::string> pool_ids;
        for (int i = 1; i <= pool_size; ++i) {
            std::string comp_id = "S" + std::to_string(i);
            pool_ids.push_back(comp_id);
            FIX::SessionID id("FIX.4.2", "EXCHANGE", comp_id);
            settings.set(id, settings.get());
        }

        FIX::FileStoreFactory store(settings);
        FIX::FileLogFactory   log(settings);
        FIX::SocketAcceptor   acceptor(gateway, store, settings, log);

        admin::AdminGateway admin_gw(engine, admin_port, pool_ids, persistence.get());

        // Crash recovery: restore symbols and resting orders before accepting connections
        if (persistence) {
            for (const auto& sym : persistence->loadSymbols())
                engine.registerSymbol(sym);
            int max_seq = persistence->loadMaxOrderSeq();
            auto orders = persistence->loadRestingOrders();
            gateway.restoreOrders(orders, max_seq);
            for (const auto& order : orders)
                engine.restoreOrder(order);
            if (!orders.empty())
                std::cout << "Recovered " << orders.size() << " resting order(s) from DB\n";
        }

        engine.start();
        admin_gw.start();
        acceptor.start();

        std::cout << "Exchange running on port 5001. Press Ctrl+C to stop.\n";

        std::signal(SIGINT,  [](int){ g_stop = 1; });
        std::signal(SIGTERM, [](int){ g_stop = 1; });
        while (!g_stop)
            std::this_thread::sleep_for(std::chrono::milliseconds(200));

        acceptor.stop();
        admin_gw.stop();
        engine.stop();

    } catch (const std::exception& e) {
        std::cerr << "Fatal: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
