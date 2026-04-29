#include "admin/AdminGateway.h"
#include "engine/MatchingEngine.h"
#include "gateway/FixGateway.h"
#include "market_data/MarketDataPublisher.h"

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

// Parse a simple ini file for [EXCHANGE] section values.
static std::string read_exchange_value(const std::string& path, const std::string& key) {
    std::ifstream f(path);
    bool in_section = false;
    std::string line;
    while (std::getline(f, line)) {
        if (line == "[EXCHANGE]") { in_section = true; continue; }
        if (!line.empty() && line[0] == '[') { in_section = false; continue; }
        if (!in_section) continue;
        auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        if (line.substr(0, eq) == key)
            return line.substr(eq + 1);
    }
    return {};
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

        market_data::MarketDataPublisher publisher;

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
            symbols
        );

        gateway::FixGateway gateway(engine, publisher);
        gw_ptr = &gateway;

        admin::AdminGateway admin_gw(engine, admin_port);

        FIX::SessionSettings settings(argv[1]);
        FIX::FileStoreFactory store(settings);
        FIX::FileLogFactory   log(settings);
        FIX::SocketAcceptor   acceptor(gateway, store, settings, log);

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
