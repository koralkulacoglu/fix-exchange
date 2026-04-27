#include "engine/MatchingEngine.h"
#include "gateway/FixGateway.h"
#include "market_data/MarketDataPublisher.h"

#include <quickfix/FileLog.h>
#include <quickfix/FileStore.h>
#include <quickfix/SessionSettings.h>
#include <quickfix/SocketAcceptor.h>

#include <chrono>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <thread>

static volatile std::sig_atomic_t g_stop = 0;

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: fix-exchange <config-file>\n";
        return 1;
    }

    try {
        market_data::MarketDataPublisher publisher;

        gateway::FixGateway* gw_ptr = nullptr;

        engine::MatchingEngine engine(
            [&](const engine::Fill& maker, const engine::Fill& taker) {
                if (gw_ptr) gw_ptr->onFill(maker, taker);
            },
            [&](const engine::CancelRequest& req, bool found) {
                if (gw_ptr) gw_ptr->onCancel(req, found);
            }
        );

        gateway::FixGateway gateway(engine, publisher);
        gw_ptr = &gateway;

        FIX::SessionSettings settings(argv[1]);
        FIX::FileStoreFactory store(settings);
        FIX::FileLogFactory   log(settings);
        FIX::SocketAcceptor   acceptor(gateway, store, settings, log);

        engine.start();
        acceptor.start();

        std::cout << "Exchange running on port 5001. Press Ctrl+C to stop.\n";

        std::signal(SIGINT,  [](int){ g_stop = 1; });
        std::signal(SIGTERM, [](int){ g_stop = 1; });
        while (!g_stop)
            std::this_thread::sleep_for(std::chrono::milliseconds(200));

        acceptor.stop();
        engine.stop();

    } catch (const std::exception& e) {
        std::cerr << "Fatal: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
