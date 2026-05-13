#!/usr/bin/env python3
"""
Latency / throughput benchmarking harness for fix-exchange.

    python3 bench/bench.py [options]

Options:
    --host HOST        exchange host           (default: 127.0.0.1)
    --port PORT        FIX acceptor port       (default: 5001)
    --admin-port PORT  admin gateway port      (default: 5002)
    --count N             iterations/scenario     (default: 10000)
    --scenario NAME       add|cancel|match|mixed|all  (default: all)
    --out DIR             chart output directory  (default: bench/bench_results)
    --no-spawn            connect to a running exchange instead of starting one
    --save                persist results to bench/results.db
    --version-override V  override git describe version stored in DB
"""

import argparse
import atexit
import datetime
import os
import socket
import sqlite3
import subprocess
import sys
import time

EXCHANGE_BIN = "./build/fix-exchange"
EXCHANGE_CFG = "config/exchange.cfg"
TARGET       = "EXCHANGE"
SEP          = "\x01"

# ── FIX framing ────────────────────────────────────────────────────────────────

def _now_fix() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%S")

def _checksum(data: str) -> str:
    return f"{sum(data.encode('ascii')) % 256:03d}"

def _build(msg_type: str, seq: int, sender: str, fields: dict) -> bytes:
    header = (
        f"35={msg_type}{SEP}49={sender}{SEP}56={TARGET}{SEP}"
        f"34={seq}{SEP}52={_now_fix()}{SEP}"
    )
    body = header + "".join(f"{k}={v}{SEP}" for k, v in fields.items())
    prefix = f"8=FIX.4.2{SEP}9={len(body.encode('ascii'))}{SEP}"
    raw = prefix + body
    return (raw + f"10={_checksum(raw)}{SEP}").encode("ascii")

def _parse(raw: bytes) -> dict:
    out = {}
    for pair in raw.decode("ascii", errors="replace").split("\x01"):
        if "=" in pair:
            tag, _, val = pair.partition("=")
            out[tag] = val
    return out


class FixSession:
    def __init__(self, host: str, port: int, sender: str):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.host, self.port = host, port
        self.sender = sender
        self.seq = 1
        self.buf = b""

    def connect(self):
        self.sock.connect((self.host, self.port))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, msg_type: str, fields: dict) -> None:
        self.sock.sendall(_build(msg_type, self.seq, self.sender, fields))
        self.seq += 1

    def send_timed(self, msg_type: str, fields: dict) -> int:
        """Send and return perf_counter_ns timestamp after sendall."""
        msg = _build(msg_type, self.seq, self.sender, fields)
        self.seq += 1
        self.sock.sendall(msg)
        return time.perf_counter_ns()

    def recv(self) -> dict:
        msg_bytes, ts = self._recv_raw()
        return _parse(msg_bytes)

    def _recv_raw(self) -> tuple:
        """Return (raw_bytes, perf_counter_ns) timestamped before parsing."""
        while True:
            if b"10=" in self.buf:
                end = self.buf.index(b"10=")
                try:
                    soh = self.buf.index(b"\x01", end)
                except ValueError:
                    pass  # checksum field not fully received yet; read more data
                else:
                    msg_bytes, self.buf = self.buf[:soh + 1], self.buf[soh + 1:]
                    ts = time.perf_counter_ns()
                    return msg_bytes, ts
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("exchange closed connection")
            self.buf += chunk

    def recv_timed(self) -> tuple:
        """Return (parsed_dict, perf_counter_ns) — timestamp before parse."""
        msg_bytes, ts = self._recv_raw()
        return _parse(msg_bytes), ts

    def logon(self):
        self.send("A", {"98": "0", "108": "30"})
        resp = self.recv()
        if resp.get("35") != "A":
            raise RuntimeError(f"Logon failed: {resp}")
        old = self.sock.gettimeout()
        self.sock.settimeout(0.3)
        while True:
            try:
                self.recv()
            except socket.timeout:
                break
        self.sock.settimeout(old)

    def logout(self):
        self.send("5", {"58": "done"})
        old = self.sock.gettimeout()
        self.sock.settimeout(0.5)
        try:
            self.recv()
        except (socket.timeout, ConnectionError):
            pass
        self.sock.settimeout(old)


def _wait_exec(s: FixSession, exec_type=None) -> tuple:
    """Read ExecReports until one matches exec_type (150), skipping others."""
    while True:
        msg, ts = s.recv_timed()
        if msg.get("35") == "8":
            if exec_type is None or msg.get("150") == exec_type:
                return msg, ts


# ── Admin helpers ───────────────────────────────────────────────────────────────

def claim_session(host: str, port: int) -> str:
    resp = _admin_cmd(host, port, "CLAIM-SESSION")
    if not resp.startswith("OK "):
        raise RuntimeError(f"CLAIM-SESSION failed: {resp!r}")
    return resp[3:].strip()

def release_session(host: str, port: int, comp_id: str) -> None:
    _admin_cmd(host, port, f"RELEASE-SESSION {comp_id}")

def _admin_cmd(host: str, port: int, cmd: str) -> str:
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall((cmd + "\n").encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if cmd == "STATS":
                if b"END\n" in data:
                    break
            else:
                if b"\n" in data:
                    break
        return data.decode()

def reset_stats(host: str, port: int) -> None:
    _admin_cmd(host, port, "RESET-STATS")

def fetch_stats(host: str, port: int) -> dict:
    """Query STATS and return dict of kind -> list[float] in microseconds."""
    raw = _admin_cmd(host, port, "STATS")
    result: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "END":
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        key, values_str = parts
        try:
            values = [int(v) / 1000.0 for v in values_str.split(",") if v]
        except ValueError:
            continue
        result[key] = values
    return result


# ── Scenarios ──────────────────────────────────────────────────────────────────

WARMUP_RATIO = 0.1  # fraction of count run before recording; minimum 50 iterations


def scenario_add(s: FixSession, count: int, symbol: str, reset_cb=None) -> list:
    """RTT: NewOrderSingle send → ExecReport(New). Uses non-crossing buy prices."""
    warmup = max(50, int(count * WARMUP_RATIO))
    for i in range(warmup):
        s.send("D", {
            "11": f"WU-ADD-{i}",
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": "0.01", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")
    if reset_cb:
        reset_cb()
    latencies = []
    for i in range(count):
        t0 = s.send_timed("D", {
            "11": f"ADD-{i}",
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": f"{1 + (i % 10) * 0.01:.2f}", "38": "1", "60": _now_fix(),
        })
        _, t1 = _wait_exec(s, "0")
        latencies.append((t1 - t0) / 1000)
    return latencies


def scenario_cancel(s: FixSession, count: int, symbol: str, reset_cb=None) -> list:
    """RTT: OrderCancelRequest send → ExecReport(Canceled)."""
    warmup = max(50, int(count * WARMUP_RATIO))
    for i in range(warmup):
        clord = f"WU-CXL-{i}"
        s.send("D", {
            "11": clord,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": "0.01", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")
        s.send("F", {
            "41": clord, "11": f"{clord}-CXL",
            "55": symbol, "54": "1", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "4")
    if reset_cb:
        reset_cb()
    latencies = []
    for i in range(count):
        clord = f"CXL-{i}"
        s.send("D", {
            "11": clord,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": f"{2 + (i % 10) * 0.01:.2f}", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")
        t0 = s.send_timed("F", {
            "41": clord, "11": f"{clord}-CXL",
            "55": symbol, "54": "1", "38": "1", "60": _now_fix(),
        })
        _, t1 = _wait_exec(s, "4")
        latencies.append((t1 - t0) / 1000)
    return latencies


def scenario_match(s: FixSession, count: int, symbol: str, reset_cb=None) -> list:
    """RTT: aggressive-buy send → Fill ExecReport (taker side)."""
    warmup = max(50, int(count * WARMUP_RATIO))
    for i in range(warmup):
        s.send("D", {
            "11": f"WU-MSELL-{i}",
            "21": "1", "55": symbol, "54": "2", "40": "2",
            "44": "100.00", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")
        mbuy = f"WU-MBUY-{i}"
        s.send("D", {
            "11": mbuy,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": "100.00", "38": "1", "60": _now_fix(),
        })
        while True:
            msg, _ = s.recv_timed()
            if msg.get("35") == "8" and msg.get("150") in ("1", "2") and msg.get("11") == mbuy:
                break
    if reset_cb:
        reset_cb()
    latencies = []
    for i in range(count):
        s.send("D", {
            "11": f"MSELL-{i}",
            "21": "1", "55": symbol, "54": "2", "40": "2",
            "44": "100.00", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")

        mbuy = f"MBUY-{i}"
        t0 = s.send_timed("D", {
            "11": mbuy,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": "100.00", "38": "1", "60": _now_fix(),
        })
        while True:
            msg, t1 = s.recv_timed()
            if (msg.get("35") == "8"
                    and msg.get("150") in ("1", "2")
                    and msg.get("11") == mbuy):
                break
        latencies.append((t1 - t0) / 1000)
    return latencies


def scenario_mixed(s: FixSession, count: int, symbol: str, reset_cb=None) -> list:
    """Cancel latency under a populated book (N/2 resting orders pre-loaded)."""
    warmup = max(50, int(count * WARMUP_RATIO))
    for i in range(warmup):
        clord = f"WU-MIX-{i}"
        s.send("D", {
            "11": clord,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": "0.01", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "0")
        s.send("F", {
            "41": clord, "11": f"{clord}-CXL",
            "55": symbol, "54": "1", "38": "1", "60": _now_fix(),
        })
        _wait_exec(s, "4")
    if reset_cb:
        reset_cb()
    half = max(count // 2, 1)
    clords = [f"MIX-{i}" for i in range(half)]
    for i, clord in enumerate(clords):
        s.send("D", {
            "11": clord,
            "21": "1", "55": symbol, "54": "1", "40": "2",
            "44": f"{3 + i * 0.01:.2f}", "38": "1", "60": _now_fix(),
        })
    for _ in range(half):
        _wait_exec(s, "0")

    latencies = []
    for clord in clords:
        t0 = s.send_timed("F", {
            "41": clord, "11": f"{clord}-CXL",
            "55": symbol, "54": "1", "38": "1", "60": _now_fix(),
        })
        _, t1 = _wait_exec(s, "4")
        latencies.append((t1 - t0) / 1000)
    return latencies


# ── Statistics ─────────────────────────────────────────────────────────────────

def _pct(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

def compute_stats(latencies: list) -> dict:
    n    = len(latencies)
    mean = sum(latencies) / n if n else 0
    return {
        "n":       n,
        "mean":    mean,
        "p50":     _pct(latencies, 50),
        "p95":     _pct(latencies, 95),
        "p99":     _pct(latencies, 99),
        "max":     max(latencies) if latencies else 0,
        "ops_sec": 1e6 / mean if mean else 0,
    }


# ── Display ────────────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    return f"{v / 1000:.2f} ms" if v >= 1000 else f"{v:.1f} µs"

try:
    from rich.console import Console as _Console
    from rich.table import Table as _Table
    from rich import box as _rbox
    _RICH = True
except ImportError:
    _RICH = False


def print_results(results: dict) -> None:
    if _RICH:
        _rich_table(results)
        _rich_histograms(results)
    else:
        _plain_table(results)

def _rich_table(results: dict) -> None:
    console = _Console()
    t = _Table(box=_rbox.ROUNDED, header_style="bold cyan", show_header=True)
    cols = [
        ("scenario",    "left"),
        ("n",           "right"),
        ("rtt p50",     "right"),
        ("rtt p99",     "right"),
        ("int p50",     "right"),
        ("int p99",     "right"),
        ("queue p50",   "right"),
        ("ops/sec",     "right"),
    ]
    for col, just in cols:
        t.add_column(col, justify=just, style="bold" if col == "scenario" else "")
    for name, r in results.items():
        rtt  = r["rtt_stats"]
        int_ = r["int_stats"]
        q    = r["queue_stats"]
        t.add_row(
            name, str(rtt["n"]),
            _fmt(rtt["p50"]), _fmt(rtt["p99"]),
            _fmt(int_["p50"]) if int_["n"] else "—",
            _fmt(int_["p99"]) if int_["n"] else "—",
            _fmt(q["p50"])   if q["n"]   else "—",
            f"{rtt['ops_sec']:,.0f}",
        )
    console.print()
    console.print(t)

def _rich_histograms(results: dict) -> None:
    console = _Console()
    BAR = 32
    for name, r in results.items():
        data = r["rtt"]
        if not data:
            continue
        lo, hi = min(data), max(data)
        if lo == hi:
            continue
        BINS = 8
        w = (hi - lo) / BINS
        counts = [0] * BINS
        for v in data:
            counts[min(int((v - lo) / w), BINS - 1)] += 1
        total  = len(data)
        max_c  = max(counts)
        console.print(f"\n  [bold]{name}[/bold] — RTT latency distribution")
        for i, c in enumerate(counts):
            b0, b1 = lo + i * w, lo + (i + 1) * w
            bar = "█" * (int(c / max_c * BAR) if max_c else 0)
            console.print(
                f"  {_fmt(b0):>9} – {_fmt(b1):<9} "
                f"[green]{bar:<{BAR}}[/green]  {c / total * 100:4.1f}%"
            )
    console.print()

def _plain_table(results: dict) -> None:
    header = (f"{'scenario':<12} {'n':>6} {'rtt p50':>10} {'rtt p99':>10} "
              f"{'int p50':>10} {'int p99':>10} {'queue p50':>10} {'ops/sec':>10}")
    print(f"\n{header}\n{'-' * len(header)}")
    for name, r in results.items():
        rtt  = r["rtt_stats"]
        int_ = r["int_stats"]
        q    = r["queue_stats"]
        print(
            f"{name:<12} {rtt['n']:>6} {_fmt(rtt['p50']):>10} {_fmt(rtt['p99']):>10} "
            f"{_fmt(int_['p50']) if int_['n'] else '—':>10} "
            f"{_fmt(int_['p99']) if int_['n'] else '—':>10} "
            f"{_fmt(q['p50']) if q['n'] else '—':>10} "
            f"{rtt['ops_sec']:>10,.0f}"
        )
    print()


# ── Charts ─────────────────────────────────────────────────────────────────────

COLORS = ["#4c72b0", "#dd8452", "#55a868", "#c44e52"]

def save_charts(results: dict, out_dir: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib not installed — skipping chart generation")
        return

    os.makedirs(out_dir, exist_ok=True)
    names = list(results.keys())

    # RTT latency CDF
    fig, ax = plt.subplots(figsize=(8, 5))
    for (name, r), color in zip(results.items(), COLORS):
        data = sorted(r["rtt"])
        if not data:
            continue
        y = [(i + 1) / len(data) * 100 for i in range(len(data))]
        ax.plot(data, y, label=name, linewidth=2, color=color)
    for p in (50, 95, 99):
        ax.axhline(p, color="grey", linestyle="--", linewidth=0.8, alpha=0.5)
        ax.text(ax.get_xlim()[1] * 0.99, p + 1, f"p{p}",
                ha="right", fontsize=8, color="grey")
    ax.set_xlabel("Latency (µs)")
    ax.set_ylabel("Percentile")
    ax.set_title("fix-exchange — Round-Trip Latency CDF")
    ax.legend()
    ax.grid(True, alpha=0.25)
    ax.set_xlim(left=0)
    ax.set_ylim(0, 101)
    fig.tight_layout()
    path = os.path.join(out_dir, "latency_cdf.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved {path}")

    # Throughput bar chart
    ops = [results[n]["rtt_stats"]["ops_sec"] for n in names]
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(names, ops, color=COLORS[:len(names)])
    ax.bar_label(bars, labels=[f"{v:,.0f}" for v in ops], padding=4, fontsize=9)
    ax.set_ylabel("ops / sec")
    ax.set_title("fix-exchange — Throughput by Scenario")
    ax.set_ylim(0, max(ops) * 1.25 if ops else 1)
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    path = os.path.join(out_dir, "throughput.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved {path}")

    # Internal vs RTT comparison
    x = np.arange(len(names))
    width = 0.25
    rtt_p50   = [results[n]["rtt_stats"]["p50"]   for n in names]
    int_p50   = [results[n]["int_stats"]["p50"]   if results[n]["int_stats"]["n"] else 0 for n in names]
    queue_p50 = [results[n]["queue_stats"]["p50"] if results[n]["queue_stats"]["n"] else 0 for n in names]

    fig, ax = plt.subplots(figsize=(9, 5))
    b1 = ax.bar(x - width, rtt_p50,   width, label="RTT p50",        color="#4c72b0")
    b2 = ax.bar(x,          int_p50,   width, label="Internal p50",   color="#55a868")
    b3 = ax.bar(x + width,  queue_p50, width, label="Queue wait p50", color="#dd8452")
    ax.bar_label(b1, labels=[_fmt(v) for v in rtt_p50],   padding=3, fontsize=8)
    ax.bar_label(b2, labels=[_fmt(v) if v else "—" for v in int_p50],   padding=3, fontsize=8)
    ax.bar_label(b3, labels=[_fmt(v) if v else "—" for v in queue_p50], padding=3, fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.set_ylabel("Latency (µs)")
    ax.set_title("fix-exchange — RTT vs Internal vs Queue Wait (p50)")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    path = os.path.join(out_dir, "internal_vs_rtt.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved {path}")


# ── Exchange bootstrap ──────────────────────────────────────────────────────────

def start_exchange(host: str, port: int) -> None:
    proc = subprocess.Popen(
        ["chrt", "-o", "0", EXCHANGE_BIN, EXCHANGE_CFG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(lambda: (proc.kill(), proc.wait()))
    for _ in range(20):
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("exchange failed to start within 4 s")


# ── Historical storage ──────────────────────────────────────────────────────────

DB_PATH = "bench/results.db"

def save_results(results: dict, version_override: str = None) -> None:
    if version_override:
        git_version = version_override
    else:
        git_version = subprocess.check_output(
            ["git", "describe", "--tags", "--always"], stderr=subprocess.DEVNULL
        ).decode().strip()
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
    ).decode().strip()
    run_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bench_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT    NOT NULL,
            git_version TEXT    NOT NULL,
            git_commit  TEXT    NOT NULL,
            build_type  TEXT    NOT NULL,
            scenario    TEXT    NOT NULL,
            n           INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bench_samples (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id   INTEGER NOT NULL REFERENCES bench_runs(id),
            kind     TEXT    NOT NULL,
            value_us REAL    NOT NULL
        )
    """)
    con.commit()

    # Delete any existing data for this version so re-runs overwrite cleanly
    existing = con.execute(
        "SELECT id FROM bench_runs WHERE git_version=?", (git_version,)
    ).fetchall()
    if existing:
        ids = [str(r[0]) for r in existing]
        con.execute(f"DELETE FROM bench_samples WHERE run_id IN ({','.join(ids)})")
        con.execute("DELETE FROM bench_runs WHERE git_version=?", (git_version,))
        con.commit()
        print(f"  Replaced existing results for {git_version}")

    for name, r in results.items():
        cur = con.execute(
            "INSERT INTO bench_runs (run_at, git_version, git_commit, build_type, scenario, n) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_at, git_version, git_commit, "Release", name, r["rtt_stats"]["n"])
        )
        run_id = cur.lastrowid
        rows = []
        for v in r["rtt"]:
            rows.append((run_id, "rtt", v))
        for v in r.get("ack_total", []):
            rows.append((run_id, "ack_total", v))
        for v in r.get("ack_queue", []):
            rows.append((run_id, "ack_queue", v))
        for v in r.get("cancel_total", []):
            rows.append((run_id, "cancel_total", v))
        for v in r.get("cancel_queue", []):
            rows.append((run_id, "cancel_queue", v))
        for v in r.get("fill_total", []):
            rows.append((run_id, "fill_total", v))
        for v in r.get("fill_queue", []):
            rows.append((run_id, "fill_queue", v))
        con.executemany(
            "INSERT INTO bench_samples (run_id, kind, value_us) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    print(f"  Results saved to {DB_PATH} (version={git_version})")


# ── Main ───────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "add":    (scenario_add,    "AAPL"),
    "cancel": (scenario_cancel, "MSFT"),
    "match":  (scenario_match,  "GOOG"),
    "mixed":  (scenario_mixed,  "AMZN"),
}

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host",       default="127.0.0.1")
    ap.add_argument("--port",       type=int, default=5001)
    ap.add_argument("--admin-port", type=int, default=5002, dest="admin_port")
    ap.add_argument("--count",            type=int, default=10000)
    ap.add_argument("--scenario",         default="all",
                    choices=[*SCENARIOS, "all"])
    ap.add_argument("--out",              default="bench/bench_results", metavar="DIR")
    ap.add_argument("--no-spawn",         action="store_true",
                    help="connect to an already-running exchange")
    ap.add_argument("--save",             action="store_true",
                    help="persist results to bench/results.db")
    ap.add_argument("--version-override", default=None, metavar="VERSION",
                    dest="version_override",
                    help="version string stored in DB instead of git describe (for rebaselining)")
    args = ap.parse_args()

    to_run = list(SCENARIOS.items()) if args.scenario == "all" \
             else [(args.scenario, SCENARIOS[args.scenario])]

    if not args.no_spawn:
        print("Starting exchange …")
        start_exchange(args.host, args.port)

    results = {}
    for name, (fn, symbol) in to_run:
        print(f"  running {name!r} × {args.count} …", end=" ", flush=True)
        comp_id = claim_session(args.host, args.admin_port)
        s = FixSession(args.host, args.port, sender=comp_id)
        s.connect()
        s.logon()

        reset_stats(args.host, args.admin_port)
        latencies = fn(s, args.count, symbol,
                       reset_cb=lambda: reset_stats(args.host, args.admin_port))
        raw_stats = fetch_stats(args.host, args.admin_port)

        s.logout()
        s.close()
        release_session(args.host, args.admin_port, comp_id)

        rtt_st = compute_stats(latencies)
        # Pick the most relevant internal track per scenario:
        #   match  → fill path (taker arrival → Fill ExecReport send)
        #   cancel/mixed → cancel path (cancel arrival → Canceled ExecReport send)
        #   add    → ack path (New ack, sent before engine queue; queue_wait=0 by design)
        if raw_stats.get("FILL_TOTAL_NS"):
            int_data   = raw_stats["FILL_TOTAL_NS"]
            queue_data = raw_stats["FILL_QUEUE_NS"]
        elif raw_stats.get("CANCEL_TOTAL_NS"):
            int_data   = raw_stats["CANCEL_TOTAL_NS"]
            queue_data = raw_stats["CANCEL_QUEUE_NS"]
        else:
            int_data   = raw_stats.get("ACK_TOTAL_NS", [])
            queue_data = raw_stats.get("ACK_QUEUE_NS", [])

        results[name] = {
            "rtt":           latencies,
            "rtt_stats":     rtt_st,
            "int_stats":     compute_stats(int_data),
            "queue_stats":   compute_stats(queue_data),
            "ack_total":     raw_stats.get("ACK_TOTAL_NS", []),
            "ack_queue":     raw_stats.get("ACK_QUEUE_NS", []),
            "cancel_total":  raw_stats.get("CANCEL_TOTAL_NS", []),
            "cancel_queue":  raw_stats.get("CANCEL_QUEUE_NS", []),
            "fill_total":    raw_stats.get("FILL_TOTAL_NS", []),
            "fill_queue":    raw_stats.get("FILL_QUEUE_NS", []),
        }
        rtt_p50 = _fmt(rtt_st["p50"])
        int_p50 = _fmt(compute_stats(int_data)["p50"]) if int_data else "—"
        print(f"rtt p50={rtt_p50}  internal p50={int_p50}  ops/sec={rtt_st['ops_sec']:,.0f}")

    print_results(results)

    print("Saving charts …")
    save_charts(results, args.out)

    if args.save:
        save_results(results, version_override=args.version_override)

if __name__ == "__main__":
    main()
