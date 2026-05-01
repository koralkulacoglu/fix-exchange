#!/usr/bin/env python3
"""
Latency / throughput benchmarking harness for fix-exchange.

    python3 tests/bench.py [options]

Options:
    --host HOST       exchange host          (default: 127.0.0.1)
    --port PORT       FIX acceptor port      (default: 5001)
    --count N         iterations/scenario    (default: 500)
    --scenario NAME   add|cancel|match|mixed|all  (default: all)
    --out DIR         chart output directory (default: docs/bench_results)
    --no-spawn        connect to a running exchange instead of starting one
"""

import argparse
import atexit
import datetime
import os
import socket
import subprocess
import sys
import time

EXCHANGE_BIN = "./build/fix-exchange"
EXCHANGE_CFG = "config/exchange.cfg"
SENDER       = "CLIENT"
TARGET       = "EXCHANGE"
SEP          = "\x01"

# ── FIX framing ────────────────────────────────────────────────────────────────

def _now_fix() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%S")

def _checksum(data: str) -> str:
    return f"{sum(data.encode('ascii')) % 256:03d}"

def _build(msg_type: str, seq: int, fields: dict) -> bytes:
    header = (
        f"35={msg_type}{SEP}49={SENDER}{SEP}56={TARGET}{SEP}"
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
    def __init__(self, host: str, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.host, self.port = host, port
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
        self.sock.sendall(_build(msg_type, self.seq, fields))
        self.seq += 1

    def send_timed(self, msg_type: str, fields: dict) -> int:
        """Send and return perf_counter_ns timestamp after sendall."""
        msg = _build(msg_type, self.seq, fields)
        self.seq += 1
        self.sock.sendall(msg)
        return time.perf_counter_ns()

    def recv(self) -> dict:
        while True:
            if b"10=" in self.buf:
                end = self.buf.index(b"10=")
                soh = self.buf.index(b"\x01", end)
                msg, self.buf = self.buf[:soh + 1], self.buf[soh + 1:]
                return _parse(msg)
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("exchange closed connection")
            self.buf += chunk

    def recv_timed(self) -> tuple:
        msg = self.recv()
        return msg, time.perf_counter_ns()

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


# ── Scenarios ──────────────────────────────────────────────────────────────────

def scenario_add(s: FixSession, count: int, symbol: str) -> list:
    """RTT: NewOrderSingle send → ExecReport(New). Uses non-crossing buy prices."""
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


def scenario_cancel(s: FixSession, count: int, symbol: str) -> list:
    """RTT: OrderCancelRequest send → ExecReport(Canceled)."""
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


def scenario_match(s: FixSession, count: int, symbol: str) -> list:
    """RTT: aggressive-buy send → Fill ExecReport (taker side)."""
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

        old = s.sock.gettimeout()
        s.sock.settimeout(0.05)
        try:
            s.recv()
        except socket.timeout:
            pass
        s.sock.settimeout(old)
    return latencies


def scenario_mixed(s: FixSession, count: int, symbol: str) -> list:
    """Cancel latency under a populated book (N/2 resting orders pre-loaded)."""
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
    for col, just in [
        ("scenario", "left"), ("n", "right"), ("p50", "right"),
        ("p95", "right"), ("p99", "right"), ("max", "right"), ("ops/sec", "right"),
    ]:
        t.add_column(col, justify=just, style="bold" if col == "scenario" else "")
    for name, r in results.items():
        st = r["stats"]
        t.add_row(
            name, str(st["n"]),
            _fmt(st["p50"]), _fmt(st["p95"]), _fmt(st["p99"]), _fmt(st["max"]),
            f"{st['ops_sec']:,.0f}",
        )
    console.print()
    console.print(t)

def _rich_histograms(results: dict) -> None:
    console = _Console()
    BAR = 32
    for name, r in results.items():
        data = r["latencies"]
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
        console.print(f"\n  [bold]{name}[/bold] — latency distribution")
        for i, c in enumerate(counts):
            b0, b1 = lo + i * w, lo + (i + 1) * w
            bar = "█" * (int(c / max_c * BAR) if max_c else 0)
            console.print(
                f"  {_fmt(b0):>9} – {_fmt(b1):<9} "
                f"[green]{bar:<{BAR}}[/green]  {c / total * 100:4.1f}%"
            )
    console.print()

def _plain_table(results: dict) -> None:
    header = f"{'scenario':<12} {'n':>6} {'p50':>10} {'p95':>10} {'p99':>10} {'max':>10} {'ops/sec':>10}"
    print(f"\n{header}\n{'-' * len(header)}")
    for name, r in results.items():
        st = r["stats"]
        print(
            f"{name:<12} {st['n']:>6} {_fmt(st['p50']):>10} {_fmt(st['p95']):>10} "
            f"{_fmt(st['p99']):>10} {_fmt(st['max']):>10} {st['ops_sec']:>10,.0f}"
        )
    print()


# ── Charts ─────────────────────────────────────────────────────────────────────

COLORS = ["#4c72b0", "#dd8452", "#55a868", "#c44e52"]

def save_charts(results: dict, out_dir: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping chart generation")
        return

    os.makedirs(out_dir, exist_ok=True)
    names = list(results.keys())

    # Latency CDF
    fig, ax = plt.subplots(figsize=(8, 5))
    for (name, r), color in zip(results.items(), COLORS):
        data = sorted(r["latencies"])
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
    ops = [results[n]["stats"]["ops_sec"] for n in names]
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


# ── Exchange bootstrap ──────────────────────────────────────────────────────────

def start_exchange(host: str, port: int) -> None:
    proc = subprocess.Popen(
        [EXCHANGE_BIN, EXCHANGE_CFG],
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
    ap.add_argument("--host",      default="127.0.0.1")
    ap.add_argument("--port",      type=int, default=5001)
    ap.add_argument("--count",     type=int, default=500)
    ap.add_argument("--scenario",  default="all",
                    choices=[*SCENARIOS, "all"])
    ap.add_argument("--out",       default="docs/bench_results",
                    metavar="DIR")
    ap.add_argument("--no-spawn",  action="store_true",
                    help="connect to an already-running exchange")
    args = ap.parse_args()

    to_run = list(SCENARIOS.items()) if args.scenario == "all" \
             else [(args.scenario, SCENARIOS[args.scenario])]

    if not args.no_spawn:
        print("Starting exchange …")
        start_exchange(args.host, args.port)

    results = {}
    for name, (fn, symbol) in to_run:
        print(f"  running {name!r} × {args.count} …", end=" ", flush=True)
        s = FixSession(args.host, args.port)
        s.connect()
        s.logon()
        latencies = fn(s, args.count, symbol)
        s.logout()
        s.close()
        st = compute_stats(latencies)
        results[name] = {"latencies": latencies, "stats": st}
        print(f"p50={_fmt(st['p50'])}  p99={_fmt(st['p99'])}  ops/sec={st['ops_sec']:,.0f}")

    print_results(results)

    print("Saving charts …")
    save_charts(results, args.out)

if __name__ == "__main__":
    main()
