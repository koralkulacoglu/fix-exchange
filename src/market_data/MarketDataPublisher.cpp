#include "MarketDataPublisher.h"
#include <cstring>
#include <stdexcept>
#include <unistd.h>

namespace market_data {

static void fill_fixed(char* dst, size_t n, const std::string& src) {
    std::memset(dst, 0, n);
    std::memcpy(dst, src.data(), std::min(src.size(), n));
}

MarketDataPublisher::MarketDataPublisher(const std::string& group, uint16_t port) {
    sock_fd_ = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (sock_fd_ < 0)
        throw std::runtime_error("MarketDataPublisher: socket() failed");

    int ttl = 1;
    ::setsockopt(sock_fd_, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));

    dest_.sin_family = AF_INET;
    dest_.sin_port   = htons(port);
    ::inet_pton(AF_INET, group.c_str(), &dest_.sin_addr);
}

MarketDataPublisher::~MarketDataPublisher() {
    if (sock_fd_ >= 0)
        ::close(sock_fd_);
}

void MarketDataPublisher::send(MdPacket& pkt) {
    pkt.seq = ++seq_;
    ::sendto(sock_fd_, &pkt, sizeof(pkt), 0,
             reinterpret_cast<sockaddr*>(&dest_), sizeof(dest_));
}

void MarketDataPublisher::on_new_order(const engine::Order& order, int leaves_qty) {
    MdPacket pkt{};
    pkt.event_type = static_cast<uint8_t>(EventType::NewOrder);
    pkt.side       = (order.side == '1') ? '0' : '1';
    fill_fixed(pkt.symbol, sizeof(pkt.symbol), order.symbol);
    pkt.price = order.price;
    pkt.qty   = leaves_qty;
    fill_fixed(pkt.exchange_id, sizeof(pkt.exchange_id), order.exchange_id);
    send(pkt);
}

void MarketDataPublisher::on_cancel(const engine::Order& order) {
    MdPacket pkt{};
    pkt.event_type = static_cast<uint8_t>(EventType::Cancel);
    pkt.side       = (order.side == '1') ? '0' : '1';
    fill_fixed(pkt.symbol, sizeof(pkt.symbol), order.symbol);
    pkt.price = order.price;
    pkt.qty   = 0;
    fill_fixed(pkt.exchange_id, sizeof(pkt.exchange_id), order.exchange_id);
    send(pkt);
}

void MarketDataPublisher::on_fill(const engine::Fill& maker) {
    // Packet 1: resting side update
    MdPacket p1{};
    p1.event_type = static_cast<uint8_t>(EventType::FillResting);
    p1.side       = (maker.side == '1') ? '0' : '1';
    fill_fixed(p1.symbol, sizeof(p1.symbol), maker.symbol);
    p1.price = maker.price;
    p1.qty   = maker.leaves_qty;
    fill_fixed(p1.exchange_id, sizeof(p1.exchange_id), maker.exchange_id);
    send(p1);

    // Packet 2: trade print
    MdPacket p2{};
    p2.event_type = static_cast<uint8_t>(EventType::Trade);
    p2.side       = '2';
    fill_fixed(p2.symbol, sizeof(p2.symbol), maker.symbol);
    p2.price = maker.price;
    p2.qty   = maker.qty;
    fill_fixed(p2.exchange_id, sizeof(p2.exchange_id), maker.exchange_id);
    send(p2);
}

void MarketDataPublisher::on_replace(const engine::ReplaceRequest& req,
                                     int new_leaves_qty, double old_price) {
    char side = (req.side == '1') ? '0' : '1';

    if (old_price == req.new_price) {
        MdPacket pkt{};
        pkt.event_type = static_cast<uint8_t>(EventType::ReplaceInPlace);
        pkt.side       = side;
        fill_fixed(pkt.symbol, sizeof(pkt.symbol), req.symbol);
        pkt.price = req.new_price;
        pkt.qty   = new_leaves_qty;
        fill_fixed(pkt.exchange_id, sizeof(pkt.exchange_id), req.orig_order_id);
        send(pkt);
    } else {
        MdPacket del{};
        del.event_type = static_cast<uint8_t>(EventType::ReplaceDelete);
        del.side       = side;
        fill_fixed(del.symbol, sizeof(del.symbol), req.symbol);
        del.price = old_price;
        del.qty   = 0;
        fill_fixed(del.exchange_id, sizeof(del.exchange_id), req.orig_order_id);
        send(del);

        MdPacket add{};
        add.event_type = static_cast<uint8_t>(EventType::ReplaceNew);
        add.side       = side;
        fill_fixed(add.symbol, sizeof(add.symbol), req.symbol);
        add.price = req.new_price;
        add.qty   = new_leaves_qty;
        fill_fixed(add.exchange_id, sizeof(add.exchange_id), req.orig_order_id);
        send(add);
    }
}

} // namespace market_data
