import time
from helpers import (
    FixSession, claim_session, release_session,
    recv_exec, drain, now_str, admin_send,
    restart_exchange, crash_exchange,
)


def test_persistence_resting_survives_restart():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        s.send("D", {
            "11": "PERSIST-REST-1",
            "21": "1",
            "55": "TSLA",
            "54": "2",
            "40": "2",
            "44": "300.00",
            "38": "5",
            "60": now_str(),
        })
        ack = recv_exec(s)
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"
        s.logout()
        s.close()
    finally:
        release_session(comp_id)

    time.sleep(0.05)
    restart_exchange()

    comp_id2 = claim_session()
    try:
        s2 = FixSession(sender=comp_id2)
        s2.connect()
        s2.logon()
        s2.send("D", {
            "11": "PERSIST-REST-2",
            "21": "1",
            "55": "TSLA",
            "54": "1",
            "40": "2",
            "44": "300.00",
            "38": "5",
            "60": now_str(),
        })
        ack2 = recv_exec(s2)
        assert ack2.get("150") == "0", f"Expected New ack, got {ack2.get('150')}"
        fills = drain(s2, timeout=0.5)
        assert len(fills) >= 1, \
            "Expected fill from restored resting sell, got none — resting order not recovered"
        s2.logout()
        s2.close()
    finally:
        release_session(comp_id2)


def test_persistence_cancelled_not_restored():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()
        s.send("D", {
            "11": "PERSIST-CXL-1",
            "21": "1",
            "55": "TSLA",
            "54": "2",
            "40": "2",
            "44": "400.00",
            "38": "5",
            "60": now,
        })
        ack = recv_exec(s)
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"
        s.send("F", {
            "41": "PERSIST-CXL-1",
            "11": "PERSIST-CXL-1-C",
            "55": "TSLA",
            "54": "2",
            "38": "5",
            "60": now,
        })
        cxl = recv_exec(s)
        assert cxl.get("150") == "4", f"Expected Canceled, got {cxl.get('150')}"
        s.logout()
        s.close()
    finally:
        release_session(comp_id)

    time.sleep(0.05)
    restart_exchange()

    comp_id2 = claim_session()
    try:
        s2 = FixSession(sender=comp_id2)
        s2.connect()
        s2.logon()
        s2.send("D", {
            "11": "PERSIST-CXL-2",
            "21": "1",
            "55": "TSLA",
            "54": "1",
            "40": "2",
            "44": "400.00",
            "38": "5",
            "60": now_str(),
        })
        ack2 = recv_exec(s2)
        assert ack2.get("150") == "0", f"Expected New ack, got {ack2.get('150')}"
        fills = drain(s2, timeout=0.3)
        assert len(fills) == 0, \
            f"Expected no fills — cancelled order must not be in recovered book, got {len(fills)} fill(s)"
        s2.send("F", {
            "41": "PERSIST-CXL-2",
            "11": "PERSIST-CXL-2-C",
            "55": "TSLA",
            "54": "1",
            "38": "5",
            "60": now_str(),
        })
        recv_exec(s2)
        s2.logout()
        s2.close()
    finally:
        release_session(comp_id2)


def test_persistence_fill_history_on_reconnect():
    comp_id = claim_session()
    buy_clord = "HIST-BUY-1"
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()
        s.send("D", {
            "11": buy_clord,
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "175.00",
            "38": "5",
            "60": now,
        })
        ack_buy = recv_exec(s)
        assert ack_buy.get("150") == "0", f"Expected New ack, got {ack_buy.get('150')}"

        s.send("D", {
            "11": "HIST-SELL-1",
            "21": "1",
            "55": "AAPL",
            "54": "2",
            "40": "2",
            "44": "175.00",
            "38": "5",
            "60": now,
        })
        ack_sell = recv_exec(s)
        assert ack_sell.get("150") == "0", f"Expected New ack, got {ack_sell.get('150')}"
        fills = drain(s, timeout=0.5)
        assert len(fills) >= 1, "Expected fill reports from the crossing orders"
        s.logout()
        s.close()
    finally:
        release_session(comp_id)

    time.sleep(0.1)
    restart_exchange()

    comp_id2 = claim_session()
    try:
        s2 = FixSession(sender=comp_id2)
        s2.connect()
        s2.logon()
        fill_reports = [m for m in s2.order_statuses if m.get("150") == "2"]
        assert len(fill_reports) >= 2, \
            f"Expected historical fill replay for both sides, got {len(fill_reports)} ExecType=2 reports"
        clord_ids = {m.get("11") for m in fill_reports}
        assert buy_clord in clord_ids, \
            f"Expected maker fill for {buy_clord!r} in history, got {clord_ids}"
        assert "HIST-SELL-1" in clord_ids, \
            f"Expected taker fill for HIST-SELL-1 in history, got {clord_ids}"
        s2.logout()
        s2.close()
    finally:
        release_session(comp_id2)


def test_persistence_symbol_survives_restart():
    admin_send("REGISTER NFLX")
    time.sleep(0.05)
    restart_exchange()

    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        s.send("D", {
            "11": "PERSIST-SYM-1",
            "21": "1",
            "55": "NFLX",
            "54": "1",
            "40": "2",
            "44": "500.00",
            "38": "1",
            "60": now_str(),
        })
        ack = recv_exec(s)
        assert ack.get("150") == "0", \
            f"Expected NFLX order accepted after restart (ExecType=New), got {ack.get('150')} — symbol not persisted"
        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_persistence_filled_order_not_restored_after_crash():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        now = now_str()
        s.send("D", {
            "11": "CRASH-SELL-1",
            "21": "1",
            "55": "AAPL",
            "54": "2",
            "40": "2",
            "44": "175.00",
            "38": "3",
            "60": now,
        })
        ack = recv_exec(s)
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"
        s.send("D", {
            "11": "CRASH-BUY-1",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "175.00",
            "38": "3",
            "60": now,
        })
        recv_exec(s)  # New ack
        fills = drain(s, timeout=0.5)
        assert len(fills) >= 1, "Expected fill from crossing orders"
        s.close()
    finally:
        release_session(comp_id)

    # Hard kill immediately — under the old async code the 5ms flush would not
    # have completed; the fix ensures the fill is already in the DB at this point.
    crash_exchange()

    comp_id2 = claim_session()
    try:
        s2 = FixSession(sender=comp_id2)
        s2.connect()
        s2.logon()
        s2.send("D", {
            "11": "CRASH-CHECK-1",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "175.00",
            "38": "3",
            "60": now_str(),
        })
        recv_exec(s2)  # New ack
        fills = drain(s2, timeout=0.3)
        assert len(fills) == 0, \
            f"Filled order must not reappear in book after crash, got {len(fills)} fill(s)"
        s2.send("F", {
            "41": "CRASH-CHECK-1",
            "11": "CRASH-CHECK-1-C",
            "55": "AAPL",
            "54": "1",
            "38": "3",
            "60": now_str(),
        })
        recv_exec(s2)
        s2.logout()
        s2.close()
    finally:
        release_session(comp_id2)


TESTS = [
    ("Persistence → resting order survives exchange restart",           test_persistence_resting_survives_restart),
    ("Persistence → cancelled order not restored after restart",        test_persistence_cancelled_not_restored),
    ("Persistence → historical fills replay as ExecType=2 on reconnect", test_persistence_fill_history_on_reconnect),
    ("Persistence → runtime-registered symbol survives restart",        test_persistence_symbol_survives_restart),
    ("Persistence → filled order not restored after hard crash",        test_persistence_filled_order_not_restored_after_crash),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module, admin_send

    def _setup():
        # TSLA is normally registered by test_orders; register it here for standalone runs.
        try:
            admin_send("REGISTER TSLA")
        except Exception:
            pass  # already registered is fine

    run_module(TESTS, setup=_setup)
