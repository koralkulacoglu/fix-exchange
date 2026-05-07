#!/usr/bin/env python3
"""Shared test infrastructure: exchange lifecycle, FIX framing, helpers."""

import atexit
import datetime
import os
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

TARGET = "EXCHANGE"
SEP    = "\x01"

ADMIN_PORT = 5002

MD_MCAST_GROUP = "239.1.1.1"
MD_MCAST_PORT  = 5003
MD_FMT  = "<Q B B 8s d i 16s"
MD_SIZE = struct.calcsize(MD_FMT)

EVENT_NEW_ORDER       = 0
EVENT_CANCEL          = 1
EVENT_FILL_RESTING    = 2
EVENT_TRADE           = 3
EVENT_REPLACE_INPLACE = 4
EVENT_REPLACE_DELETE  = 5
EVENT_REPLACE_NEW     = 6

SIDE_BID   = ord('0')
SIDE_ASK   = ord('1')
SIDE_TRADE = ord('2')

_proc = None


# ---------------------------------------------------------------------------
# Exchange lifecycle
# ---------------------------------------------------------------------------

def start_exchange():
    global _proc
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
            _proc = proc
            return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("Exchange failed to start within 4 seconds")


def restart_exchange():
    global _proc
    if _proc is not None:
        _proc.terminate()
        try:
            _proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait()
        _proc = None
    time.sleep(0.2)
    start_exchange()


# ---------------------------------------------------------------------------
# FIX framing
# ---------------------------------------------------------------------------

def _checksum(data: str) -> str:
    return f"{sum(data.encode('ascii')) % 256:03d}"


def build_message(msg_type: str, seq: int, body_fields, sender: str) -> bytes:
    sending_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")
    header_body = (
        f"35={msg_type}{SEP}"
        f"49={sender}{SEP}"
        f"56={TARGET}{SEP}"
        f"34={seq}{SEP}"
        f"52={sending_time}{SEP}"
    )
    pairs = body_fields.items() if isinstance(body_fields, dict) else body_fields
    user_body = "".join(f"{k}={v}{SEP}" for k, v in pairs)
    body = header_body + user_body
    length = len(body.encode("ascii"))
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

    if fields.get("35") == "W":
        entries, cur = [], {}
        for pair in raw.decode("ascii", errors="replace").split("\x01"):
            if "=" not in pair:
                continue
            tag, _, val = pair.partition("=")
            if tag == "269":
                if cur:
                    entries.append(cur)
                cur = {"type": val}
            elif tag == "270" and cur:
                cur["price"] = float(val)
            elif tag == "271" and cur:
                cur["qty"] = int(float(val))
            elif tag == "278" and cur:
                cur["eid"] = val
            elif tag == "272" and cur:
                cur["date"] = val
            elif tag == "273" and cur:
                cur["time"] = val
        if cur:
            entries.append(cur)
        fields["md_entries"] = entries

    return fields


# ---------------------------------------------------------------------------
# FIX session
# ---------------------------------------------------------------------------

class FixSession:
    def __init__(self, sender: str):
        self.sender = sender
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.seq = 1
        self.buf = b""
        self.order_statuses = []

    def connect(self):
        self.sock.connect((HOST, PORT))

    def close(self):
        self.sock.close()

    def send(self, msg_type: str, body) -> None:
        msg = build_message(msg_type, self.seq, body, sender=self.sender)
        self.seq += 1
        self.sock.sendall(msg)

    def recv(self) -> dict:
        while True:
            if b"10=" in self.buf:
                end = self.buf.index(b"10=")
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
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(0.3)
        while True:
            try:
                msg = self.recv()
                if msg.get("35") == "8" and msg.get("150") in ("I", "2", "4"):
                    self.order_statuses.append(msg)
            except socket.timeout:
                break
        self.sock.settimeout(old_timeout)
        return resp

    def logout(self):
        self.send("5", {"58": "Normal logout"})
        try:
            self.recv()
        except socket.timeout:
            pass


# ---------------------------------------------------------------------------
# UDP market data listener
# ---------------------------------------------------------------------------

class UdpMdListener:
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
# Admin helpers
# ---------------------------------------------------------------------------

def admin_send(command: str) -> str:
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


def claim_session() -> str:
    resp = admin_send("CLAIM-SESSION")
    assert resp.startswith("OK "), f"CLAIM-SESSION failed: {resp!r}"
    return resp.split()[1]


def release_session(comp_id: str) -> None:
    admin_send(f"RELEASE-SESSION {comp_id}")


# ---------------------------------------------------------------------------
# FIX test helpers
# ---------------------------------------------------------------------------

def recv_exec(session: FixSession) -> dict:
    while True:
        msg = session.recv()
        if msg.get("35") == "8":
            return msg


def drain(session: FixSession, timeout=0.4) -> list:
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


def now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")


def run_module(tests: list, setup=None) -> None:
    """Start a fresh exchange and run a single module's TESTS list standalone."""
    import sys
    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    try:
        os.remove("store/exchange.db")
    except FileNotFoundError:
        pass
    start_exchange()
    if setup:
        setup()
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  {PASS}  {name}")
        except Exception as e:
            print(f"  {FAIL}  {name}: {e}")
            failures.append(name)
    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests passed.")


def recv_md_snapshot(session: FixSession, timeout=2.0) -> dict | None:
    old = session.sock.gettimeout()
    session.sock.settimeout(timeout)
    try:
        while True:
            msg = session.recv()
            if msg.get("35") == "W":
                return msg
    except socket.timeout:
        return None
    finally:
        session.sock.settimeout(old)
