#!/usr/bin/env python3
"""
Integration tests for fix-exchange.

Spawns a fresh exchange process, runs all tests against it, and tears it
down on exit. Requires the binary to be built first:

    cmake --build build

Then run:

    python3 tests/test_exchange.py
"""

import atexit
import datetime
import socket
import subprocess
import sys
import time

HOST = "127.0.0.1"
PORT = 5001

EXCHANGE_BIN = "./build/fix-exchange"
EXCHANGE_CFG = "config/exchange.cfg"


def start_exchange():
    proc = subprocess.Popen(
        [EXCHANGE_BIN, EXCHANGE_CFG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    def _cleanup():
        proc.kill()
        proc.wait()
    atexit.register(_cleanup)
    for _ in range(20):
        try:
            socket.create_connection((HOST, PORT), timeout=0.5).close()
            return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("Exchange failed to start within 4 seconds")
SENDER = "CLIENT"
TARGET = "EXCHANGE"
SEP = "\x01"


# ---------------------------------------------------------------------------
# FIX framing helpers
# ---------------------------------------------------------------------------

def _body_length(fields: str) -> int:
    return len(fields.encode("ascii"))


def _checksum(data: str) -> str:
    return f"{sum(data.encode('ascii')) % 256:03d}"


def build_message(msg_type: str, seq: int, body_fields: dict) -> bytes:
    sending_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")
    header_body = (
        f"35={msg_type}{SEP}"
        f"49={SENDER}{SEP}"
        f"56={TARGET}{SEP}"
        f"34={seq}{SEP}"
        f"52={sending_time}{SEP}"
    )
    user_body = "".join(f"{k}={v}{SEP}" for k, v in body_fields.items())
    body = header_body + user_body
    length = _body_length(body)
    prefix = f"8=FIX.4.2{SEP}9={length}{SEP}"
    raw = prefix + body
    raw += f"10={_checksum(raw)}{SEP}"
    return raw.encode("ascii")


def parse_fields(raw: bytes) -> dict:
    fields = {}
    for pair in raw.decode("ascii", errors="replace").split("\x01"):
        if "=" in pair:
            tag, _, val = pair.partition("=")
            fields[tag] = val
    return fields


class FixSession:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.seq = 1
        self.buf = b""

    def connect(self):
        self.sock.connect((HOST, PORT))

    def close(self):
        self.sock.close()

    def send(self, msg_type: str, body: dict) -> None:
        msg = build_message(msg_type, self.seq, body)
        self.seq += 1
        self.sock.sendall(msg)

    def recv(self) -> dict:
        """Read one complete FIX message from the socket."""
        while True:
            # A FIX message ends with the checksum field 10=NNN\x01
            if b"10=" in self.buf:
                end = self.buf.index(b"10=")
                # find the SOH after the checksum value
                soh = self.buf.index(b"\x01", end)
                msg = self.buf[: soh + 1]
                self.buf = self.buf[soh + 1:]
                return parse_fields(msg)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Exchange closed connection")
            self.buf += chunk

    def logon(self):
        self.send("A", {"98": "0", "108": "30"})
        resp = self.recv()
        assert resp.get("35") == "A", f"Expected Logon, got: {resp}"
        return resp

    def logout(self):
        self.send("5", {"58": "Normal logout"})
        # drain — exchange may echo a logout or heartbeat
        try:
            self.recv()
        except socket.timeout:
            pass


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
failures = []


def run(name, fn):
    try:
        fn()
        print(f"  {PASS}  {name}")
    except Exception as e:
        print(f"  {FAIL}  {name}: {e}")
        failures.append(name)


def test_logon_logout():
    s = FixSession()
    s.connect()
    s.logon()
    s.logout()
    s.close()


def test_new_order_ack():
    s = FixSession()
    s.connect()
    s.logon()

    s.send("D", {
        "11": "ORD-001",
        "21": "1",
        "55": "AAPL",
        "54": "1",       # buy
        "40": "2",       # limit
        "44": "150.00",
        "38": "100",
        "60": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S"),
    })

    # Expect New ack (ExecType=0)
    resp = s.recv()
    assert resp.get("35") == "8", f"Expected ExecutionReport, got {resp.get('35')}"
    assert resp.get("150") == "0", f"Expected ExecType=New(0), got {resp.get('150')}"
    assert resp.get("39") == "0", f"Expected OrdStatus=New(0), got {resp.get('39')}"

    s.logout()
    s.close()


def test_order_match_fills():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Buy 100 MSFT @ 300
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
    ack_buy = s.recv()
    assert ack_buy.get("150") == "0", "Buy ack should be ExecType=New"

    # Sell 100 MSFT @ 300 (crosses the resting buy)
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
    ack_sell = s.recv()
    assert ack_sell.get("150") == "0", "Sell ack should be ExecType=New"

    # Collect next two reports — should be fills for maker and taker
    # Plus a MarketDataIncrementalRefresh (35=X)
    reports = []
    market_data = []
    for _ in range(3):
        try:
            msg = s.recv()
            if msg.get("35") == "8":
                reports.append(msg)
            elif msg.get("35") == "X":
                market_data.append(msg)
        except socket.timeout:
            break

    assert len(reports) == 2, f"Expected 2 fill ExecReports, got {len(reports)}"
    for r in reports:
        assert r.get("150") in ("1", "2"), f"Expected PartFill or Fill ExecType, got {r.get('150')}"

    assert len(market_data) == 1, f"Expected 1 MarketDataIncrementalRefresh, got {len(market_data)}"

    s.logout()
    s.close()


def test_order_cancel():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Place a resting limit buy that won't match
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

    # Cancel it
    s.send("F", {
        "41": "ORD-CXLTEST",
        "11": "ORD-CXLTEST-CXL",
        "55": "GOOG",
        "54": "1",
        "38": "50",
        "60": now,
    })

    # Expect ExecReport(Canceled) — ExecType=4, OrdStatus=4
    confirm = s.recv()
    assert confirm.get("35") == "8", f"Expected ExecutionReport, got {confirm.get('35')}"
    assert confirm.get("150") == "4", f"Expected ExecType=Canceled(4), got {confirm.get('150')}"
    assert confirm.get("39") == "4", f"Expected OrdStatus=Canceled(4), got {confirm.get('39')}"

    s.logout()
    s.close()


ADMIN_PORT = 5002


def admin_send(command: str) -> str:
    """Send a single command to the admin port and return the response line."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(("127.0.0.1", ADMIN_PORT))
    sock.sendall((command + "\n").encode("ascii"))
    resp = b""
    while b"\n" not in resp:
        chunk = sock.recv(256)
        if not chunk:
            break
        resp += chunk
    sock.close()
    return resp.decode("ascii").strip()


def test_unknown_symbol_rejected():
    s = FixSession()
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
        "60": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S"),
    })

    resp = s.recv()
    assert resp.get("35") == "8", f"Expected ExecutionReport, got {resp.get('35')}"
    assert resp.get("150") == "8", f"Expected ExecType=Rejected(8), got {resp.get('150')}"
    assert resp.get("39") == "8", f"Expected OrdStatus=Rejected(8), got {resp.get('39')}"
    assert "Unknown symbol" in resp.get("58", ""), f"Expected reject reason in tag 58, got {resp.get('58')}"

    s.logout()
    s.close()


def test_admin_register_symbol():
    # Register a new symbol via admin port
    resp = admin_send("REGISTER TSLA")
    assert resp == "OK", f"Expected OK from admin, got: {resp!r}"

    # Attempting to register the same symbol again should fail
    resp2 = admin_send("REGISTER TSLA")
    assert resp2.startswith("ERROR"), f"Expected ERROR on duplicate, got: {resp2!r}"

    # Now an order for TSLA should be accepted
    s = FixSession()
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
        "60": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S"),
    })

    ack = s.recv()
    assert ack.get("150") == "0", f"Expected ExecType=New(0) for TSLA order, got {ack.get('150')}"

    s.logout()
    s.close()


def test_ioc_no_fill():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    s.send("D", {
        "11": "ORD-IOC-NOFILL",
        "21": "1",
        "55": "AMZN",
        "54": "1",
        "40": "2",
        "44": "100.00",
        "38": "50",
        "59": "3",       # IOC
        "60": now,
    })

    ack = s.recv()
    assert ack.get("150") == "0", f"Expected New ack, got {ack.get('150')}"

    cancel = s.recv()
    assert cancel.get("35") == "8", f"Expected ExecutionReport, got {cancel.get('35')}"
    assert cancel.get("150") == "4", f"Expected ExecType=Canceled(4), got {cancel.get('150')}"
    assert cancel.get("39") == "4", f"Expected OrdStatus=Canceled(4), got {cancel.get('39')}"

    s.logout()
    s.close()


def test_ioc_partial_fill():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Resting sell: 50 @ $200 (GTC)
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

    # IOC buy: 100 @ $210 — fills 50, remaining 50 canceled
    s.send("D", {
        "11": "ORD-IOC-BUY",
        "21": "1",
        "55": "AMZN",
        "54": "1",
        "40": "2",
        "44": "210.00",
        "38": "100",
        "59": "3",       # IOC
        "60": now,
    })
    ack_buy = s.recv()
    assert ack_buy.get("150") == "0", "Expected New ack for IOC buy"

    # Collect: 2 fill ExecReports (maker + taker) and 1 Canceled for IOC remainder
    fill_reports = []
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

    assert len(fill_reports) == 2, f"Expected 2 fill ExecReports, got {len(fill_reports)}"
    for r in fill_reports:
        assert r.get("150") in ("1", "2"), f"Expected PartFill or Fill, got {r.get('150')}"
    assert len(cancel_reports) == 1, f"Expected 1 Canceled for IOC remainder, got {len(cancel_reports)}"
    assert cancel_reports[0].get("11") == "ORD-IOC-BUY", "Canceled should be for the IOC buy"

    s.logout()
    s.close()


def test_fok_insufficient():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Resting sell: 50 @ $200 (GTC) — not enough to fill a 100-lot FOK
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

    # FOK buy: 100 @ $210 — only 50 available, rejected outright
    s.send("D", {
        "11": "ORD-FOK-BUY",
        "21": "1",
        "55": "AMZN",
        "54": "1",
        "40": "2",
        "44": "210.00",
        "38": "100",
        "59": "4",       # FOK
        "60": now,
    })
    ack_fok = s.recv()
    assert ack_fok.get("150") == "0", "Expected New ack for FOK buy"

    cancel = s.recv()
    assert cancel.get("35") == "8", f"Expected ExecutionReport, got {cancel.get('35')}"
    assert cancel.get("150") == "4", f"Expected ExecType=Canceled(4), got {cancel.get('150')}"
    assert cancel.get("11") == "ORD-FOK-BUY", "Canceled should be for the FOK buy"

    # Book should be untouched: resting sell must still be cancelable
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


def test_fok_full_fill():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Resting sell: 100 @ $200 (GTC) — exactly enough for the FOK
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

    # FOK buy: 100 @ $210 — full qty available, should fully fill, no Canceled
    s.send("D", {
        "11": "ORD-FOK-FULL-BUY",
        "21": "1",
        "55": "AMZN",
        "54": "1",
        "40": "2",
        "44": "210.00",
        "38": "100",
        "59": "4",       # FOK
        "60": now,
    })
    ack_fok = s.recv()
    assert ack_fok.get("150") == "0", "Expected New ack for FOK buy"

    fill_reports = []
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

    assert len(fill_reports) == 2, f"Expected 2 Fill ExecReports, got {len(fill_reports)}"
    for r in fill_reports:
        assert r.get("150") in ("1", "2"), f"Expected PartFill or Fill ExecType, got {r.get('150')}"
    assert len(cancel_reports) == 0, f"Expected no Canceled for FOK full fill, got {len(cancel_reports)}"

    s.logout()
    s.close()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("\nStarting exchange …")
    start_exchange()
    print("Running FIX integration tests …\n")
    run("Logon / Logout", test_logon_logout)
    run("NewOrderSingle → ExecReport(New)", test_new_order_ack)
    run("Matching → two Fill ExecReports + MarketDataRefresh", test_order_match_fills)
    run("OrderCancelRequest → session stays alive", test_order_cancel)
    run("Unknown symbol → ExecReport(Rejected)", test_unknown_symbol_rejected)
    run("Admin REGISTER → new symbol accepted", test_admin_register_symbol)
    run("IOC order → no fill → Canceled", test_ioc_no_fill)
    run("IOC order → partial fill → PartFill + Canceled", test_ioc_partial_fill)
    run("FOK order → insufficient qty → Canceled, book unchanged", test_fok_insufficient)
    run("FOK order → full qty available → Fill, no Canceled", test_fok_full_fill)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    main()
