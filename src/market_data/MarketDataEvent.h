#pragma once
#include <cstdint>

namespace market_data {

enum class EventType : uint8_t {
    NewOrder       = 0,
    Cancel         = 1,
    FillResting    = 2,
    Trade          = 3,
    ReplaceInPlace = 4,
    ReplaceDelete  = 5,
    ReplaceNew     = 6,
};

#pragma pack(push, 1)
struct MdPacket {
    uint64_t seq;
    uint8_t  event_type;
    uint8_t  side;          // '0'=bid, '1'=ask, '2'=trade
    char     symbol[8];
    double   price;
    int32_t  qty;
    char     exchange_id[16];
};
#pragma pack(pop)

} // namespace market_data
