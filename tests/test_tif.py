import socket
from helpers import (
    FixSession, claim_session, release_session,
    recv_exec, drain, now_str,
)


def test_ioc_no_fill():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        s.send("D", {
            "11": "ORD-IOC-NOFILL",
            "21": "1",
            "55": "AMZN",
            "54": "1",
            "40": "2",
            "44": "100.00",
            "38": "50",
            "59": "3",
            "60": now_str(),
        })

        ack = s.recv()
        assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"

        cancel = s.recv()
        assert cancel.get("35")  == "8", f"Expected ExecutionReport, got {cancel.get('35')}"
        assert cancel.get("150") == "4", f"Expected ExecType=Canceled(4), got {cancel.get('150')}"
        assert cancel.get("39")  == "4", f"Expected OrdStatus=Canceled(4), got {cancel.get('39')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_ioc_partial_fill():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        now = now_str()

        s.send("D", {
            "11": "ORD-IOC-SELL",
            "21": "1",
            "55": "AMZN",
            "54": "2",
            "40": "2",
            "44": "200.00",
            "38": "50",
            "60": now,
        })
        ack_sell = s.recv()
        assert ack_sell.get("150") == "0", "Expected New ack for resting sell"

        s.send("D", {
            "11": "ORD-IOC-BUY",
            "21": "1",
            "55": "AMZN",
            "54": "1",
            "40": "2",
            "44": "210.00",
            "38": "100",
            "59": "3",
            "60": now,
        })
        ack_buy = s.recv()
        assert ack_buy.get("150") == "0", "Expected New ack for IOC buy"

        fill_reports   = []
        cancel_reports = []
        for _ in range(4):
            try:
                msg = s.recv()
                if msg.get("35") != "8":
                    continue
                if msg.get("150") == "4":
                    cancel_reports.append(msg)
                else:
                    fill_reports.append(msg)
            except socket.timeout:
                break

        assert len(fill_reports) == 2, \
            f"Expected 2 fill ExecReports, got {len(fill_reports)}"
        for r in fill_reports:
            assert r.get("150") in ("1", "2"), \
                f"Expected PartFill or Fill, got {r.get('150')}"
        assert len(cancel_reports) == 1, \
            f"Expected 1 Canceled for IOC remainder, got {len(cancel_reports)}"
        assert cancel_reports[0].get("11") == "ORD-IOC-BUY", \
            "Canceled should be for the IOC buy"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_fok_insufficient():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        now = now_str()

        s.send("D", {
            "11": "ORD-FOK-SELL",
            "21": "1",
            "55": "AMZN",
            "54": "2",
            "40": "2",
            "44": "200.00",
            "38": "50",
            "60": now,
        })
        ack_sell = s.recv()
        assert ack_sell.get("150") == "0", "Expected New ack for resting sell"

        s.send("D", {
            "11": "ORD-FOK-BUY",
            "21": "1",
            "55": "AMZN",
            "54": "1",
            "40": "2",
            "44": "210.00",
            "38": "100",
            "59": "4",
            "60": now,
        })
        ack_fok = s.recv()
        assert ack_fok.get("150") == "0", "Expected New ack for FOK buy"

        cancel = s.recv()
        assert cancel.get("35")  == "8", f"Expected ExecutionReport, got {cancel.get('35')}"
        assert cancel.get("150") == "4", f"Expected ExecType=Canceled(4), got {cancel.get('150')}"
        assert cancel.get("11")  == "ORD-FOK-BUY", "Canceled should be for the FOK buy"

        s.send("F", {
            "41": "ORD-FOK-SELL",
            "11": "ORD-FOK-SELL-CXL",
            "55": "AMZN",
            "54": "2",
            "38": "50",
            "60": now,
        })
        confirm = s.recv()
        assert confirm.get("150") == "4", \
            f"Expected Canceled for resting sell (book unchanged), got {confirm.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_fok_full_fill():
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        now = now_str()

        s.send("D", {
            "11": "ORD-FOK-FULL-SELL",
            "21": "1",
            "55": "AMZN",
            "54": "2",
            "40": "2",
            "44": "200.00",
            "38": "100",
            "60": now,
        })
        ack_sell = s.recv()
        assert ack_sell.get("150") == "0", "Expected New ack for resting sell"

        s.send("D", {
            "11": "ORD-FOK-FULL-BUY",
            "21": "1",
            "55": "AMZN",
            "54": "1",
            "40": "2",
            "44": "210.00",
            "38": "100",
            "59": "4",
            "60": now,
        })
        ack_fok = s.recv()
        assert ack_fok.get("150") == "0", "Expected New ack for FOK buy"

        fill_reports   = []
        cancel_reports = []
        for _ in range(3):
            try:
                msg = s.recv()
                if msg.get("35") != "8":
                    continue
                if msg.get("150") == "4":
                    cancel_reports.append(msg)
                else:
                    fill_reports.append(msg)
            except socket.timeout:
                break

        assert len(fill_reports) == 2, \
            f"Expected 2 Fill ExecReports, got {len(fill_reports)}"
        for r in fill_reports:
            assert r.get("150") in ("1", "2"), \
                f"Expected PartFill or Fill ExecType, got {r.get('150')}"
        assert len(cancel_reports) == 0, \
            f"Expected no Canceled for FOK full fill, got {len(cancel_reports)}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


TESTS = [
    ("IOC order → no fill → Canceled",                      test_ioc_no_fill),
    ("IOC order → partial fill → PartFill + Canceled",      test_ioc_partial_fill),
    ("FOK order → insufficient qty → Canceled, book unchanged", test_fok_insufficient),
    ("FOK order → full qty available → Fill, no Canceled",  test_fok_full_fill),
]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers import run_module
    run_module(TESTS)
