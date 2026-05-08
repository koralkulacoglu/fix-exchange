#!/usr/bin/env python3
"""
Master test runner.

Usage:
    python3 tests/run_all.py

Cleans the exchange DB, starts the exchange subprocess, then runs every test
module in order.  Each module exports TESTS = [(description, fn), ...].
"""

import os
import sys

# Ensure the tests/ directory is on the path so sibling modules resolve.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import start_exchange

import test_session
import test_orders
import test_tif
import test_replace
import test_market_data
import test_persistence
import test_ui_server
import test_risk

MODULES = [
    # test_orders must precede test_session: the multiclient session test
    # leaves a resting MSFT buy @ 300 that would contaminate test_order_match_fills.
    test_orders,
    test_session,
    test_tif,
    test_replace,
    test_market_data,
    test_persistence,
    test_ui_server,
    test_risk,
]

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def run(name, fn, failures):
    try:
        fn()
        print(f"  {PASS}  {name}")
    except Exception as e:
        print(f"  {FAIL}  {name}: {e}")
        failures.append(name)


def main():
    try:
        os.remove("store/exchange.db")
    except FileNotFoundError:
        pass

    print("\nStarting exchange …")
    start_exchange()
    print("Running integration tests …\n")

    failures = []
    for module in MODULES:
        for name, fn in module.TESTS:
            run(name, fn, failures)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    main()
