from helpers import (
    FixSession, claim_session, release_session, now_str,
)


def test_replace_qty_reduction():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "RPL-QTY-ORIG",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "120.00",
            "38": "100",
            "60": now_str(),
        })
        ack = s.recv()
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"

        s.send("G", {
            "11": "RPL-QTY-NEW",
            "41": "RPL-QTY-ORIG",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "120.00",
            "38": "60",
            "60": now_str(),
        })
        resp = s.recv()
        assert resp.get("35")   == "8",            f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150")  == "5",            f"Expected ExecType=Replaced(5), got {resp.get('150')}"
        assert resp.get("11")   == "RPL-QTY-NEW",  f"Expected new ClOrdID, got {resp.get('11')}"
        assert resp.get("41")   == "RPL-QTY-ORIG", f"Expected OrigClOrdID, got {resp.get('41')}"
        assert resp.get("151")  == "60",           f"Expected LeavesQty=60, got {resp.get('151')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_replace_price_change():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "RPL-PX-ORIG",
            "21": "1",
            "55": "AAPL",
            "54": "2",
            "40": "2",
            "44": "200.00",
            "38": "50",
            "60": now_str(),
        })
        ack = s.recv()
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"

        s.send("G", {
            "11": "RPL-PX-NEW",
            "41": "RPL-PX-ORIG",
            "21": "1",
            "55": "AAPL",
            "54": "2",
            "40": "2",
            "44": "210.00",
            "38": "50",
            "60": now_str(),
        })
        resp = s.recv()
        assert resp.get("35")  == "8",  f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150") == "5",  f"Expected ExecType=Replaced(5), got {resp.get('150')}"
        assert resp.get("44")  == "210", f"Expected Price=210, got {resp.get('44')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_replace_unknown_order():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("G", {
            "11": "RPL-UNK-NEW",
            "41": "RPL-UNK-GHOST",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "100.00",
            "38": "10",
            "60": now_str(),
        })
        resp = s.recv()
        assert resp.get("35") == "9", f"Expected OrderCancelReject(9), got {resp.get('35')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_replace_symbol_change_rejected():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "RPL-SYM-ORIG",
            "21": "1",
            "55": "AAPL",
            "54": "1",
            "40": "2",
            "44": "130.00",
            "38": "25",
            "60": now_str(),
        })
        ack = s.recv()
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"

        s.send("G", {
            "11": "RPL-SYM-NEW",
            "41": "RPL-SYM-ORIG",
            "21": "1",
            "55": "MSFT",
            "54": "1",
            "40": "2",
            "44": "130.00",
            "38": "25",
            "60": now_str(),
        })
        resp = s.recv()
        assert resp.get("35") == "9", f"Expected OrderCancelReject(9), got {resp.get('35')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


TESTS = [
    ("OrderCancelReplaceRequest → same price qty reduction",        test_replace_qty_reduction),
    ("OrderCancelReplaceRequest → price change",                    test_replace_price_change),
    ("OrderCancelReplaceRequest → unknown order → OrderCancelReject", test_replace_unknown_order),
    ("OrderCancelReplaceRequest → symbol change → OrderCancelReject", test_replace_symbol_change_rejected),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module
    run_module(TESTS)
