#!/usr/bin/env python3
"""
Integration tests for fix-exchange.

Connects to the exchange over a raw TCP socket, sends FIX 4.2 messages,
and asserts on the responses. No external FIX library required.

Usage:
    # In one terminal:
    ./build/fix-exchange config/exchange.cfg

    # In another terminal:
    python3 tests/test_exchange.py
"""

import socket
import time
import datetime
import sys

HOST = "127.0.0.1"
PORT = 5001
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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print(f"\nConnecting to {HOST}:{PORT} …")
    # Quick connectivity check
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(3)
    try:
        probe.connect((HOST, PORT))
        probe.close()
    except OSError:
        print(f"ERROR: Cannot connect to {HOST}:{PORT}. Is the exchange running?")
        print("  Start it with:  ./build/fix-exchange config/exchange.cfg")
        sys.exit(1)

    print("Running FIX integration tests …\n")
    run("Logon / Logout", test_logon_logout)
    run("NewOrderSingle → ExecReport(New)", test_new_order_ack)
    run("Matching → two Fill ExecReports + MarketDataRefresh", test_order_match_fills)
    run("OrderCancelRequest → session stays alive", test_order_cancel)
    run("Unknown symbol → ExecReport(Rejected)", test_unknown_symbol_rejected)
    run("Admin REGISTER → new symbol accepted", test_admin_register_symbol)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    main()
