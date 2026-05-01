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
import struct
import subprocess
import sys
import threading
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
        self.order_statuses = [] # ExecType=I reports buffered during logon

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
        self.order_statuses = []
        self.send("A", {"98": "0", "108": "30"})
        resp = self.recv()
        assert resp.get("35") == "A", f"Expected Logon, got: {resp}"
        # Drain ExecType=I order status reports that arrive right after logon.
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(0.3)
        while True:
            try:
                msg = self.recv()
                if msg.get("35") == "8" and msg.get("150") == "I":
                    self.order_statuses.append(msg)
            except socket.timeout:
                break
        self.sock.settimeout(old_timeout)
        return resp

    def logout(self):
        self.send("5", {"58": "Normal logout"})
        # drain — exchange may echo a logout or heartbeat
        try:
            self.recv()
        except socket.timeout:
            pass


# ---------------------------------------------------------------------------
# UDP multicast market data listener
# ---------------------------------------------------------------------------

MD_MCAST_GROUP = "239.1.1.1"
MD_MCAST_PORT  = 5003
MD_FMT  = "<Q B B 8s d i 16s"
MD_SIZE = struct.calcsize(MD_FMT)

EVENT_NEW_ORDER        = 0
EVENT_CANCEL           = 1
EVENT_FILL_RESTING     = 2
EVENT_TRADE            = 3
EVENT_REPLACE_INPLACE  = 4
EVENT_REPLACE_DELETE   = 5
EVENT_REPLACE_NEW      = 6

SIDE_BID   = ord('0')
SIDE_ASK   = ord('1')
SIDE_TRADE = ord('2')


class UdpMdListener:
    """Joins the multicast group and collects UDP market-data packets in a background thread."""
    def __init__(self, group=MD_MCAST_GROUP, port=MD_MCAST_PORT):
        self.group   = group
        self.port    = port
        self.packets = []
        self._sock   = None
        self._thread = None
        self._stop   = threading.Event()

    def start(self):
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", self.port))
        mreq = struct.pack("4sL", socket.inet_aton(self.group), socket.INADDR_ANY)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                data = self._sock.recv(256)
                if len(data) == MD_SIZE:
                    seq, event_type, side, symbol_b, price, qty, exch_b = struct.unpack(MD_FMT, data)
                    self.packets.append({
                        "seq":         seq,
                        "event_type":  event_type,
                        "side":        side,
                        "symbol":      symbol_b.rstrip(b"\x00").decode("ascii"),
                        "price":       price,
                        "qty":         qty,
                        "exchange_id": exch_b.rstrip(b"\x00").decode("ascii"),
                    })
            except socket.timeout:
                pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()

    def wait_for(self, count, timeout=2.0):
        deadline = time.time() + timeout
        while len(self.packets) < count and time.time() < deadline:
            time.sleep(0.05)
        return self.packets


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


def recv_exec(session):
    """Receive the next ExecutionReport, discarding market data and snapshots."""
    while True:
        msg = session.recv()
        if msg.get("35") == "8":
            return msg


def drain(session, timeout=0.4):
    """Collect all ExecutionReports until timeout."""
    exec_reports = []
    old = session.sock.gettimeout()
    session.sock.settimeout(timeout)
    while True:
        try:
            msg = session.recv()
            if msg.get("35") == "8":
                exec_reports.append(msg)
        except socket.timeout:
            break
    session.sock.settimeout(old)
    return exec_reports


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
    ack_buy = recv_exec(s)
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
    ack_sell = recv_exec(s)
    assert ack_sell.get("150") == "0", "Sell ack should be ExecType=New"

    reports = drain(s)
    assert len(reports) == 2, f"Expected 2 fill ExecReports, got {len(reports)}"
    for r in reports:
        assert r.get("150") in ("1", "2"), f"Expected PartFill or Fill ExecType, got {r.get('150')}"

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



def test_order_status_on_reconnect():
    s = FixSession()
    s.connect()
    s.logon()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")

    # Place a resting limit buy on MSFT (MSFT book is empty at this point)
    s.send("D", {
        "11": "STATUS-BID",
        "21": "1",
        "55": "MSFT",
        "54": "1",
        "40": "2",
        "44": "250.00",
        "38": "75",
        "60": now,
    })
    assert s.recv().get("150") == "0", "Expected New ack"

    s.logout()
    s.close()

    # Reconnect — order statuses are buffered by logon() into s2.order_statuses
    s2 = FixSession()
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



def now_str():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")


def test_udp_md_new_resting_order():
    listener = UdpMdListener()
    listener.start()

    s = FixSession()
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
    assert len(pkts) >= 1, f"Expected >= 1 NewOrder UDP packet for AAPL, got {listener.packets}"
    pkt = pkts[0]
    assert pkt["side"] == SIDE_BID, f"Expected SIDE_BID, got {pkt['side']}"
    assert abs(pkt["price"] - 99.0) < 1e-9, f"Expected price 99.0, got {pkt['price']}"
    assert pkt["qty"] == 10, f"Expected qty 10, got {pkt['qty']}"


def test_udp_md_cancel():
    listener = UdpMdListener()
    listener.start()

    s = FixSession()
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
    assert len(pkts) >= 1, f"Expected >= 1 Cancel UDP packet for AAPL, got {listener.packets}"
    assert pkts[0]["qty"] == 0, f"Expected qty=0 for cancel, got {pkts[0]['qty']}"


def test_udp_md_fill():
    listener = UdpMdListener()
    listener.start()

    s = FixSession()
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

    pkts = [p for p in listener.packets if p["symbol"] == "GOOG"]
    fill_rest = [p for p in pkts if p["event_type"] == EVENT_FILL_RESTING]
    trades    = [p for p in pkts if p["event_type"] == EVENT_TRADE]
    assert len(fill_rest) >= 1, f"Expected >= 1 FillResting packet, got {pkts}"
    assert len(trades) >= 1,    f"Expected >= 1 Trade packet, got {pkts}"
    assert trades[0]["side"] == SIDE_TRADE, f"Expected SIDE_TRADE, got {trades[0]['side']}"
    assert abs(trades[0]["price"] - 500.0) < 1e-9, f"Expected price 500.0, got {trades[0]['price']}"


def test_replace_qty_reduction():
    s = FixSession()
    s.connect()
    s.logon()

    # Place a resting limit buy
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

    # Replace: same price, smaller qty (should preserve queue priority)
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
    assert resp.get("35") == "8",   f"Expected ExecutionReport, got {resp.get('35')}"
    assert resp.get("150") == "5",  f"Expected ExecType=Replaced(5), got {resp.get('150')}"
    assert resp.get("11") == "RPL-QTY-NEW",  f"Expected new ClOrdID, got {resp.get('11')}"
    assert resp.get("41") == "RPL-QTY-ORIG", f"Expected OrigClOrdID, got {resp.get('41')}"
    assert resp.get("151") == "60", f"Expected LeavesQty=60, got {resp.get('151')}"

    s.logout()
    s.close()


def test_replace_price_change():
    s = FixSession()
    s.connect()
    s.logon()

    # Place a resting limit sell
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

    # Replace: change price (loses queue priority, re-inserted at new level)
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
    assert resp.get("35") == "8",  f"Expected ExecutionReport, got {resp.get('35')}"
    assert resp.get("150") == "5", f"Expected ExecType=Replaced(5), got {resp.get('150')}"
    assert resp.get("44") == "210", f"Expected Price=210, got {resp.get('44')}"

    s.logout()
    s.close()


def test_replace_unknown_order():
    s = FixSession()
    s.connect()
    s.logon()

    # Send replace for a ClOrdID that was never submitted
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


def test_replace_symbol_change_rejected():
    s = FixSession()
    s.connect()
    s.logon()

    # Place a resting order for AAPL
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

    # Try to replace changing the symbol — should be rejected
    s.send("G", {
        "11": "RPL-SYM-NEW",
        "41": "RPL-SYM-ORIG",
        "21": "1",
        "55": "MSFT",           # different symbol
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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("\nStarting exchange …")
    start_exchange()
    print("Running FIX integration tests …\n")
    run("Logon / Logout", test_logon_logout)
    run("NewOrderSingle → ExecReport(New)", test_new_order_ack)
    run("Matching → two Fill ExecReports", test_order_match_fills)
    run("OrderCancelRequest → session stays alive", test_order_cancel)
    run("Unknown symbol → ExecReport(Rejected)", test_unknown_symbol_rejected)
    run("Admin REGISTER → new symbol accepted", test_admin_register_symbol)
    run("IOC order → no fill → Canceled", test_ioc_no_fill)
    run("IOC order → partial fill → PartFill + Canceled", test_ioc_partial_fill)
    run("FOK order → insufficient qty → Canceled, book unchanged", test_fok_insufficient)
    run("FOK order → full qty available → Fill, no Canceled", test_fok_full_fill)
    run("ExecType=I order status replay on reconnect", test_order_status_on_reconnect)
    run("UDP multicast → NewOrder packet on resting bid", test_udp_md_new_resting_order)
    run("UDP multicast → Cancel packet on order cancel", test_udp_md_cancel)
    run("UDP multicast → FillResting + Trade packets on match", test_udp_md_fill)
    run("OrderCancelReplaceRequest → same price qty reduction", test_replace_qty_reduction)
    run("OrderCancelReplaceRequest → price change", test_replace_price_change)
    run("OrderCancelReplaceRequest → unknown order → OrderCancelReject", test_replace_unknown_order)
    run("OrderCancelReplaceRequest → symbol change → OrderCancelReject", test_replace_symbol_change_rejected)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    main()
