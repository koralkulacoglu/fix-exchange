import threading
from helpers import (
    FixSession, claim_session, release_session, now_str,
)


def test_logon_logout():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()
        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_order_status_on_reconnect():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "STATUS-BID",
            "21": "1",
            "55": "MSFT",
            "54": "1",
            "40": "2",
            "44": "250.00",
            "38": "75",
            "60": now_str(),
        })
        assert s.recv().get("150") == "0", "Expected New ack"

        s.logout()
        s.close()

        s2 = FixSession(sender=comp_id)
        s2.connect()
        s2.logon()

        clord_ids = {m.get("11") for m in s2.order_statuses}
        assert "STATUS-BID" in clord_ids, \
            f"Expected order status for STATUS-BID on reconnect, got: {clord_ids}"

        status = next(m for m in s2.order_statuses if m.get("11") == "STATUS-BID")
        assert status.get("150") == "I",   f"Expected ExecType=I, got {status.get('150')}"
        assert status.get("39")  == "0",   f"Expected OrdStatus=New(0), got {status.get('39')}"
        assert status.get("151") == "75",  f"Expected LeavesQty=75, got {status.get('151')}"
        assert status.get("14")  == "0",   f"Expected CumQty=0, got {status.get('14')}"

        s2.logout()
        s2.close()
    finally:
        release_session(comp_id)


def test_pool_session_multiclient():
    c1 = claim_session()
    c2 = claim_session()
    results = {}
    errors  = {}

    def run_client(comp_id, symbol, price):
        try:
            s = FixSession(sender=comp_id)
            s.connect()
            s.logon()
            s.send("D", {
                "11": f"ORD-{comp_id}",
                "21": "1",
                "55": symbol,
                "54": "1",
                "40": "2",
                "44": price,
                "38": "1",
                "60": now_str(),
            })
            msg = s.recv()
            results[comp_id] = msg.get("150")
            s.logout()
            s.close()
        except Exception as e:
            errors[comp_id] = str(e)

    t1 = threading.Thread(target=run_client, args=(c1, "AAPL", "150.00"))
    t2 = threading.Thread(target=run_client, args=(c2, "MSFT", "300.00"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    release_session(c1)
    release_session(c2)

    assert not errors, f"Client errors: {errors}"
    assert results.get(c1) == "0", f"{c1} expected ExecType=New(0), got {results.get(c1)}"
    assert results.get(c2) == "0", f"{c2} expected ExecType=New(0), got {results.get(c2)}"


TESTS = [
    ("Logon / Logout",                                      test_logon_logout),
    ("ExecType=I order status replay on reconnect",         test_order_status_on_reconnect),
    ("Pool sessions → two concurrent FIX clients",          test_pool_session_multiclient),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module
    run_module(TESTS)
