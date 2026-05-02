#include "AdminGateway.h"
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cstring>
#include <iostream>
#include <string>

namespace admin {

AdminGateway::AdminGateway(engine::MatchingEngine& engine, int port,
                           std::vector<std::string> session_pool)
    : engine_(engine), port_(port),
      pool_(std::move(session_pool)),
      available_(pool_.begin(), pool_.end()) {}

AdminGateway::~AdminGateway() { stop(); }

void AdminGateway::start() {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        std::cerr << "AdminGateway: socket() failed\n";
        return;
    }

    int opt = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port        = htons(static_cast<uint16_t>(port_));

    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        std::cerr << "AdminGateway: bind() failed on port " << port_ << "\n";
        ::close(listen_fd_);
        listen_fd_ = -1;
        return;
    }

    ::listen(listen_fd_, 4);
    std::cout << "Admin interface listening on 127.0.0.1:" << port_ << "\n";
    thread_ = std::thread(&AdminGateway::run, this);
}

void AdminGateway::stop() {
    stop_ = true;
    if (listen_fd_ >= 0) {
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (thread_.joinable())
        thread_.join();
}

void AdminGateway::run() {
    while (!stop_) {
        int client_fd = ::accept(listen_fd_, nullptr, nullptr);
        if (client_fd < 0) {
            if (!stop_)
                std::cerr << "AdminGateway: accept() failed\n";
            break;
        }
        handle_client(client_fd);
        ::close(client_fd);
    }
}

void AdminGateway::handle_client(int fd) {
    std::string buf;
    char tmp[256];

    while (true) {
        ssize_t n = ::recv(fd, tmp, sizeof(tmp) - 1, 0);
        if (n <= 0) break;
        tmp[n] = '\0';
        buf += tmp;

        std::string::size_type pos;
        while ((pos = buf.find('\n')) != std::string::npos) {
            std::string line = buf.substr(0, pos);
            buf.erase(0, pos + 1);

            if (!line.empty() && line.back() == '\r')
                line.pop_back();

            std::string response;
            if (line.rfind("REGISTER ", 0) == 0) {
                std::string sym = line.substr(9);
                if (engine_.registerSymbol(sym))
                    response = "OK\n";
                else
                    response = "ERROR: symbol already registered or invalid (alphanumeric, 1-8 chars)\n";
            } else if (line == "CLAIM-SESSION") {
                if (available_.empty()) {
                    response = "ERROR: no sessions available\n";
                } else {
                    auto it = available_.begin();
                    response = "OK " + *it + "\n";
                    available_.erase(it);
                }
            } else if (line.rfind("RELEASE-SESSION ", 0) == 0) {
                std::string comp_id = line.substr(16);
                bool known = false;
                for (const auto& id : pool_)
                    if (id == comp_id) { known = true; break; }
                if (!known)
                    response = "ERROR: unknown session " + comp_id + "\n";
                else {
                    available_.insert(comp_id);
                    response = "OK\n";
                }
            } else if (line == "HELP") {
                response = "Commands: REGISTER <SYMBOL> | CLAIM-SESSION | RELEASE-SESSION <ID>\n";
            } else {
                response = "ERROR: unknown command\n";
            }

            ::send(fd, response.c_str(), response.size(), 0);
        }
    }
}

} // namespace admin
