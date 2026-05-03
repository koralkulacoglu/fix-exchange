#pragma once
#include "engine/MatchingEngine.h"
#include "persistence/PersistenceLayer.h"
#include <atomic>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace admin {

class AdminGateway {
public:
    AdminGateway(engine::MatchingEngine& engine, int port,
                 std::vector<std::string> session_pool = {},
                 persistence::PersistenceLayer* persistence = nullptr);
    ~AdminGateway();

    void start();
    void stop();

private:
    void run();
    void handle_client(int fd);

    engine::MatchingEngine&         engine_;
    persistence::PersistenceLayer*  persistence_;
    int port_;
    int listen_fd_{-1};
    std::atomic<bool> stop_{false};
    std::thread thread_;

    std::vector<std::string> pool_;
    std::set<std::string>    available_;
};

} // namespace admin
