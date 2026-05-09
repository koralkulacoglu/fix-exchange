#!/usr/bin/env python3
"""
Generate historical trend charts from bench/results.db.

    python3 bench/plot_history.py [options]

Options:
    --db PATH   path to results.db   (default: bench/results.db)
    --out DIR   chart output dir     (default: bench/bench_results)
    --kind KIND which internal latency kind to overlay
                  ack_total | cancel_total | fill_total  (default: ack_total)
"""

import argparse
import os
import sqlite3

COLORS    = ["#4c72b0", "#dd8452", "#55a868", "#c44e52"]
SCENARIOS = ["add", "cancel", "match", "mixed"]
DB_PATH   = "bench/results.db"

# Internal kind to use per scenario when --kind is not overridden
_SCENARIO_KIND = {
    "add":    "ack_total",
    "cancel": "cancel_total",
    "match":  "fill_total",
    "mixed":  "cancel_total",
}


def _pct(values: list, p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _is_exact_tag(version: str) -> bool:
    """Return True if version looks like a plain tag (no -N-gHASH suffix)."""
    import re
    return bool(re.match(r'^v\d+\.\d+', version)) and '-' not in version.split('v', 1)[-1].replace('.', '', 2)


def load_data(db_path: str) -> tuple:
    """
    Load per-scenario per-version stats from the DB.
    Only exact tag versions are included (untagged commits between releases skipped).
    Returns (data, version_order) where:
      data = {scenario: {version: {rtt_p50, rtt_p99, int_p50, ops_sec}}}
      version_order = list of versions in first-seen order
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    runs = con.execute(
        "SELECT id, git_version, scenario, run_at FROM bench_runs ORDER BY run_at"
    ).fetchall()

    data: dict = {}
    version_order: list = []

    for run in runs:
        ver = run["git_version"]
        if not _is_exact_tag(ver):
            continue  # skip untagged commits

        sc  = run["scenario"]
        rid = run["id"]

        if ver not in version_order:
            version_order.append(ver)

        rtt_vals = [r[0] for r in con.execute(
            "SELECT value_us FROM bench_samples WHERE run_id=? AND kind='rtt'", (rid,)
        ).fetchall()]

        int_kind = _SCENARIO_KIND.get(sc, "ack_total")
        int_vals = [r[0] for r in con.execute(
            "SELECT value_us FROM bench_samples WHERE run_id=? AND kind=?", (rid, int_kind)
        ).fetchall()]

        mean = sum(rtt_vals) / len(rtt_vals) if rtt_vals else 0
        entry = {
            "rtt_p50": _pct(rtt_vals, 50),
            "rtt_p99": _pct(rtt_vals, 99),
            "int_p50": _pct(int_vals, 50) if int_vals else None,
            "ops_sec": 1e6 / mean if mean else 0,
        }
        data.setdefault(sc, {})[ver] = entry

    con.close()
    return data, version_order


def generate_charts(db_path: str, out_dir: str) -> None:
    """Generate trend_p50.png and trend_ops.png from db_path into out_dir."""
    if not os.path.exists(db_path):
        print(f"  Skipping trend charts: {db_path} not found")
        return

    data, version_order = load_data(db_path)
    if not data:
        print("  No tagged versions in DB yet — trend charts skipped")
        return

    print(f"  {len(version_order)} version(s) in history: {', '.join(version_order)}")
    _plot_trend_p50(data, version_order, out_dir)
    _plot_trend_ops(data, version_order, out_dir)


def _plot_trend_p50(data: dict, version_order: list, out_dir: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping trend charts")
        return

    os.makedirs(out_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))

    for sc, color in zip(SCENARIOS, COLORS):
        if sc not in data:
            continue
        sc_data  = data[sc]
        versions = [v for v in version_order if v in sc_data]
        rtt_p50  = [sc_data[v]["rtt_p50"] for v in versions]
        int_p50  = [sc_data[v]["int_p50"] for v in versions]

        ax.plot(versions, rtt_p50, marker="o", linewidth=2, color=color,
                label=f"{sc} RTT")
        if any(v is not None for v in int_p50):
            vals = [v if v is not None else float("nan") for v in int_p50]
            ax.plot(versions, vals, marker="s", linewidth=1.5, linestyle="--",
                    color=color, alpha=0.6, label=f"{sc} internal")

    ax.set_xlabel("Version")
    ax.set_ylabel("p50 Latency (µs)")
    ax.set_title("fix-exchange — p50 Latency by Version")
    ax.legend(fontsize=8, ncol=2)
    ax.grid(True, alpha=0.25)
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()
    path = os.path.join(out_dir, "trend_p50.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved {path}")


def _plot_trend_ops(data: dict, version_order: list, out_dir: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    os.makedirs(out_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))

    for sc, color in zip(SCENARIOS, COLORS):
        if sc not in data:
            continue
        sc_data  = data[sc]
        versions = [v for v in version_order if v in sc_data]
        ops      = [sc_data[v]["ops_sec"] for v in versions]
        ax.plot(versions, ops, marker="o", linewidth=2, color=color, label=sc)

    ax.set_xlabel("Version")
    ax.set_ylabel("ops / sec")
    ax.set_title("fix-exchange — Throughput by Version")
    ax.legend()
    ax.grid(True, alpha=0.25)
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()
    path = os.path.join(out_dir, "trend_ops.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db",  default=DB_PATH,            help="path to results.db")
    ap.add_argument("--out", default="bench/bench_results", metavar="DIR")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} not found.")
        print("       Run 'python3 bench/bench.py --save' first.")
        return

    print(f"Reading {args.db} …")
    data, version_order = load_data(args.db)

    if not data:
        print("No exact-tag versions found. Run --save on a tagged commit first.")
        return

    print(f"Found {sum(len(v) for v in data.values())} run(s) across "
          f"{len(version_order)} version(s): {', '.join(version_order)}")
    _plot_trend_p50(data, version_order, args.out)
    _plot_trend_ops(data, version_order, args.out)


if __name__ == "__main__":
    main()
