import time
from helpers import (
    FixSession, UdpMdListener, claim_session, release_session,
    recv_exec, recv_md_snapshot, drain, now_str,
    EVENT_NEW_ORDER, EVENT_CANCEL, EVENT_FILL_RESTING, EVENT_TRADE,
    SIDE_BID, SIDE_TRADE,
)


def test_udp_md_new_resting_order():
    comp_id = claim_session()
    listener = UdpMdListener()
    listener.start()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "UDP-BID-1",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "99.00",
            "38": "10",
            "60": now_str(),
        })
        ack = recv_exec(s)
        assert ack.get("150") == "0", "Expected New ack"

        listener.wait_for(1, timeout=2.0)
        listener.stop()
        s.logout()
        s.close()

        pkts = [p for p in listener.packets
                if p["event_type"] == EVENT_NEW_ORDER and p["symbol"] == "AAPL"]
        assert len(pkts) >= 1, \
            f"Expected >= 1 NewOrder UDP packet for AAPL, got {listener.packets}"
        pkt = pkts[0]
        assert pkt["side"]            == SIDE_BID, f"Expected SIDE_BID, got {pkt['side']}"
        assert abs(pkt["price"] - 99.0) < 1e-9,   f"Expected price 99.0, got {pkt['price']}"
        assert pkt["qty"]             == 10,       f"Expected qty 10, got {pkt['qty']}"
    finally:
        release_session(comp_id)


def test_udp_md_cancel():
    comp_id = claim_session()
    listener = UdpMdListener()
    listener.start()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()

        s.send("D", {
            "11": "UDP-CXL-ORD",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "88.00",
            "38": "5",
            "60": now,
        })
        recv_exec(s)

        time.sleep(0.2)

        s.send("F", {
            "41": "UDP-CXL-ORD",
            "11": "UDP-CXL-ORD-CXL",
            "55": "AAPL",
            "54": "1",
            "38": "5",
            "60": now,
        })
        recv_exec(s)

        listener.wait_for(2, timeout=2.0)
        listener.stop()
        s.logout()
        s.close()

        pkts = [p for p in listener.packets
                if p["event_type"] == EVENT_CANCEL and p["symbol"] == "AAPL"]
        assert len(pkts) >= 1, \
            f"Expected >= 1 Cancel UDP packet for AAPL, got {listener.packets}"
        assert pkts[0]["qty"] == 0, f"Expected qty=0 for cancel, got {pkts[0]['qty']}"
    finally:
        release_session(comp_id)


def test_udp_md_fill():
    comp_id = claim_session()
    listener = UdpMdListener()
    listener.start()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()

        s.send("D", {
            "11": "UDP-FILL-BUY",
            "21": "1",
            "55": "GOOG",
            "54": "1",
            "40": "2",
            "44": "500.00",
            "38": "100",
            "60": now,
        })
        recv_exec(s)
        time.sleep(0.1)

        s.send("D", {
            "11": "UDP-FILL-SELL",
            "21": "1",
            "55": "GOOG",
            "54": "2",
            "40": "2",
            "44": "500.00",
            "38": "100",
            "60": now,
        })
        recv_exec(s)

        listener.wait_for(3, timeout=2.0)
        drain(s)
        listener.stop()
        s.logout()
        s.close()

        pkts      = [p for p in listener.packets if p["symbol"] == "GOOG"]
        fill_rest = [p for p in pkts if p["event_type"] == EVENT_FILL_RESTING]
        trades    = [p for p in pkts if p["event_type"] == EVENT_TRADE]
        assert len(fill_rest) >= 1, f"Expected >= 1 FillResting packet, got {pkts}"
        assert len(trades)    >= 1, f"Expected >= 1 Trade packet, got {pkts}"
        assert trades[0]["side"] == SIDE_TRADE, \
            f"Expected SIDE_TRADE, got {trades[0]['side']}"
        assert abs(trades[0]["price"] - 500.0) < 1e-9, \
            f"Expected price 500.0, got {trades[0]['price']}"
    finally:
        release_session(comp_id)


def test_md_snapshot_contains_trade_history():
    comp_id = claim_session()
    trade_price = 210.00
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()

        s.send("D", {
            "11": "HIST-MD-BUY",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": str(trade_price),
            "38": "3",
            "60": now,
        })
        recv_exec(s)

        s.send("D", {
            "11": "HIST-MD-SELL",
            "21": "1",
            "55": "AAPL",
            "54": "2",
            "40": "2",
            "44": str(trade_price),
            "38": "3",
            "60": now,
        })
        recv_exec(s)
        drain(s, timeout=0.5)
        time.sleep(0.05)

        s.send("V", [
            ("262", "HIST-MD-1"), ("263", "0"), ("264", "0"),
            ("267", "1"), ("269", "2"),
            ("146", "1"), ("55", "AAPL"),
        ])
        snap = recv_md_snapshot(s)
        assert snap is not None, "Expected 35=W MarketDataSnapshotFullRefresh response"

        trade_entries = [e for e in snap.get("md_entries", []) if e.get("type") == "2"]
        assert len(trade_entries) >= 1, \
            f"Expected at least one type-2 (trade) entry in 35=W, got {len(trade_entries)}"

        prices = {e.get("price") for e in trade_entries}
        assert trade_price in prices, \
            f"Expected trade price {trade_price} in snapshot entries, got {prices}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


TESTS = [
    ("UDP multicast → NewOrder packet on resting bid",           test_udp_md_new_resting_order),
    ("UDP multicast → Cancel packet on order cancel",            test_udp_md_cancel),
    ("UDP multicast → FillResting + Trade packets on match",     test_udp_md_fill),
    ("Persistence → 35=W snapshot includes trade history entries", test_md_snapshot_contains_trade_history),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module
    run_module(TESTS)
