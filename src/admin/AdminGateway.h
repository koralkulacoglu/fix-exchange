#pragma once
#include "engine/MatchingEngine.h"
#include <atomic>
#include <thread>

namespace admin {

class AdminGateway {
public:
    AdminGateway(engine::MatchingEngine& engine, int port);
    ~AdminGateway();

    void start();
    void stop();

private:
    void run();
    void handle_client(int fd);

    engine::MatchingEngine& engine_;
    int port_;
    int listen_fd_{-1};
    std::atomic<bool> stop_{false};
    std::thread thread_;
};

} // namespace admin
