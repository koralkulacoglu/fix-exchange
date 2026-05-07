from helpers import (
    FixSession, claim_session, release_session,
    recv_exec, drain, now_str, admin_send,
)


def test_new_order_ack():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "ORD-001",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "150.00",
            "38": "100",
            "60": now_str(),
        })

        resp = s.recv()
        assert resp.get("35")  == "8", f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150") == "0", f"Expected ExecType=New(0), got {resp.get('150')}"
        assert resp.get("39")  == "0", f"Expected OrdStatus=New(0), got {resp.get('39')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_order_match_fills():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        now = now_str()

        s.send("D", {
            "11": "ORD-BUY",
            "21": "1",
            "55": "MSFT",
            "54": "1",
            "40": "2",
            "44": "300.00",
            "38": "100",
            "60": now,
        })
        ack_buy = recv_exec(s)
        assert ack_buy.get("150") == "0", "Buy ack should be ExecType=New"

        s.send("D", {
            "11": "ORD-SELL",
            "21": "1",
            "55": "MSFT",
            "54": "2",
            "40": "2",
            "44": "300.00",
            "38": "100",
            "60": now,
        })
        ack_sell = recv_exec(s)
        assert ack_sell.get("150") == "0", "Sell ack should be ExecType=New"

        reports = drain(s)
        assert len(reports) == 2, f"Expected 2 fill ExecReports, got {len(reports)}"
        for r in reports:
            assert r.get("150") in ("1", "2"), f"Expected PartFill or Fill ExecType, got {r.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_order_cancel():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        now = now_str()

        s.send("D", {
            "11": "ORD-CXLTEST",
            "21": "1",
            "55": "GOOG",
            "54": "1",
            "40": "2",
            "44": "100.00",
            "38": "50",
            "60": now,
        })
        ack = s.recv()
        assert ack.get("150") == "0", "Expected New ack before cancel"

        s.send("F", {
            "41": "ORD-CXLTEST",
            "11": "ORD-CXLTEST-CXL",
            "55": "GOOG",
            "54": "1",
            "38": "50",
            "60": now,
        })

        confirm = s.recv()
        assert confirm.get("35")  == "8", f"Expected ExecutionReport, got {confirm.get('35')}"
        assert confirm.get("150") == "4", f"Expected ExecType=Canceled(4), got {confirm.get('150')}"
        assert confirm.get("39")  == "4", f"Expected OrdStatus=Canceled(4), got {confirm.get('39')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_unknown_symbol_rejected():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "ORD-FAKE",
            "21": "1",
            "55": "FAKE",
            "54": "1",
            "40": "2",
            "44": "10.00",
            "38": "1",
            "60": now_str(),
        })

        resp = s.recv()
        assert resp.get("35")  == "8", f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150") == "8", f"Expected ExecType=Rejected(8), got {resp.get('150')}"
        assert resp.get("39")  == "8", f"Expected OrdStatus=Rejected(8), got {resp.get('39')}"
        assert "Unknown symbol" in resp.get("58", ""), \
            f"Expected reject reason in tag 58, got {resp.get('58')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_admin_register_symbol():
    resp = admin_send("REGISTER TSLA")
    assert resp == "OK", f"Expected OK from admin, got: {resp!r}"

    resp2 = admin_send("REGISTER TSLA")
    assert resp2.startswith("ERROR"), f"Expected ERROR on duplicate, got: {resp2!r}"

    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "ORD-TSLA",
            "21": "1",
            "55": "TSLA",
            "54": "1",
            "40": "2",
            "44": "200.00",
            "38": "10",
            "60": now_str(),
        })

        ack = s.recv()
        assert ack.get("150") == "0", \
            f"Expected ExecType=New(0) for TSLA order, got {ack.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


TESTS = [
    ("NewOrderSingle → ExecReport(New)",            test_new_order_ack),
    ("Matching → two Fill ExecReports",             test_order_match_fills),
    ("OrderCancelRequest → session stays alive",    test_order_cancel),
    ("Unknown symbol → ExecReport(Rejected)",       test_unknown_symbol_rejected),
    ("Admin REGISTER → new symbol accepted",        test_admin_register_symbol),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module
    run_module(TESTS)
