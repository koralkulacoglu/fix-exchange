#pragma once
#include <chrono>
#include <cstdint>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace gateway {

struct LatencySample {
    int64_t total_ns;
    int64_t queue_wait_ns;
};

class LatencyStats {
public:
    explicit LatencyStats(std::size_t capacity = 2000) {
        samples_.reserve(capacity);
        capacity_ = capacity;
    }

    void record(int64_t total_ns, int64_t queue_wait_ns) {
        std::lock_guard<std::mutex> lock(mu_);
        if (samples_.size() < capacity_)
            samples_.push_back({total_ns, queue_wait_ns});
    }

    void reset() {
        std::lock_guard<std::mutex> lock(mu_);
        samples_.clear();
    }

    // Returns two lines of comma-separated nanosecond integers:
    //   "<prefix>_TOTAL_NS <v1>,<v2>,...\n"
    //   "<prefix>_QUEUE_NS <v1>,<v2>,...\n"
    // Empty if no samples recorded.
    std::string serialize(const std::string& prefix) const {
        std::lock_guard<std::mutex> lock(mu_);
        if (samples_.empty()) return "";

        std::ostringstream total_ss, queue_ss;
        for (std::size_t i = 0; i < samples_.size(); ++i) {
            if (i) { total_ss << ','; queue_ss << ','; }
            total_ss << samples_[i].total_ns;
            queue_ss << samples_[i].queue_wait_ns;
        }
        return prefix + "_TOTAL_NS " + total_ss.str() + "\n"
             + prefix + "_QUEUE_NS " + queue_ss.str() + "\n";
    }

private:
    mutable std::mutex mu_;
    std::vector<LatencySample> samples_;
    std::size_t capacity_;
};

} // namespace gateway
