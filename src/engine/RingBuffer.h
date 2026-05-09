#pragma once
#include <array>
#include <atomic>
#include <cstddef>

// Single-producer / single-consumer lock-free ring buffer.
// N must be a power of 2. head_ is owned by the producer, tail_ by the consumer.
template<typename T, size_t N>
class RingBuffer {
    static_assert((N & (N - 1)) == 0, "N must be a power of 2");

    std::array<T, N> buf_;
    alignas(64) std::atomic<size_t> head_{0};
    alignas(64) std::atomic<size_t> tail_{0};

public:
    bool push(T&& item) {
        const size_t h    = head_.load(std::memory_order_relaxed);
        const size_t next = (h + 1) & (N - 1);
        if (next == tail_.load(std::memory_order_acquire))
            return false;
        buf_[h] = std::move(item);
        head_.store(next, std::memory_order_release);
        return true;
    }

    bool pop(T& item) {
        const size_t t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire))
            return false;
        item = std::move(buf_[t]);
        tail_.store((t + 1) & (N - 1), std::memory_order_release);
        return true;
    }

    bool empty() const {
        return tail_.load(std::memory_order_acquire) ==
               head_.load(std::memory_order_acquire);
    }
};
