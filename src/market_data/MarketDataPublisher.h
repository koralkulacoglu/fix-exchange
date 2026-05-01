#pragma once
#include "MarketDataEvent.h"
#include "engine/Order.h"
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <atomic>
#include <string>

namespace market_data {

class MarketDataPublisher {
public:
    MarketDataPublisher(const std::string& group, uint16_t port);
    ~MarketDataPublisher();

    void on_fill(const engine::Fill& maker);
    void on_new_order(const engine::Order& order, int leaves_qty);
    void on_cancel(const engine::Order& order);
    void on_replace(const engine::ReplaceRequest& req, int new_leaves_qty, double old_price);

private:
    void send(MdPacket& pkt);

    int sock_fd_{-1};
    sockaddr_in dest_{};
    std::atomic<uint64_t> seq_{0};
};

} // namespace market_data
