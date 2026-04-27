#!/usr/bin/env python3
"""
Interactive FIX 4.2 CLI client for fix-exchange.

Usage:
    # Start the exchange first:
    ./build/fix-exchange config/exchange.cfg

    # Then in another terminal:
    python3 tools/client.py [--host 127.0.0.1] [--port 5001]
"""

import argparse
import datetime
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from test_exchange import FixSession  # noqa: E402

# ---------------------------------------------------------------------------
# ExecType / OrdStatus / Side labels
# ---------------------------------------------------------------------------

EXEC_TYPE = {
    "0": "NEW",
    "1": "PART-FILL",
    "2": "FILLED",
    "4": "CANCELED",
    "8": "REJECTED",
}

SIDE = {"1": "BUY", "2": "SELL"}

HELP_TEXT = """\
Commands:
  buy  <SYMBOL> <QTY> @ <PRICE>   limit buy
  buy  <SYMBOL> <QTY> market      market buy
  sell <SYMBOL> <QTY> @ <PRICE>   limit sell
  sell <SYMBOL> <QTY> market      market sell
  cancel <ORDER-ID>               cancel a resting order (e.g. ORD-3)
  help                            show this message
  quit / exit                     disconnect and exit\
"""


# ---------------------------------------------------------------------------
# Extended session
# ---------------------------------------------------------------------------

class ClientSession(FixSession):
    def __init__(self, host, port):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((host, port))
        self._order_counter = 0
        self._cancel_counter = 0

    def _next_order_id(self):
        self._order_counter += 1
        return f"ORD-{self._order_counter}"

    def _now(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%S")

    def send_order(self, symbol, side, qty, price=None):
        ord_id = self._next_order_id()
        fields = {
            "11": ord_id,
            "21": "1",
            "55": symbol,
            "54": side,
            "40": "1" if price is None else "2",
            "38": str(qty),
            "60": self._now(),
        }
        if price is not None:
            fields["44"] = f"{price:.2f}"
        self.send("D", fields)
        return ord_id

    def send_cancel(self, orig_order_id, symbol, side, qty):
        self._cancel_counter += 1
        fields = {
            "41": orig_order_id,
            "11": f"{orig_order_id}-CXL{self._cancel_counter}",
            "55": symbol,
            "54": side,
            "38": str(qty),
            "60": self._now(),
        }
        self.send("F", fields)

    def recv_print(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            while True:
                msg = self.recv()
                msg_type = msg.get("35", "")

                if msg_type in ("0", "1", "2", "5"):
                    # Heartbeat, TestRequest, ResendRequest, Logout — skip
                    continue

                if msg_type == "8":
                    _print_exec(msg)

                elif msg_type == "X":
                    _print_mktdata(msg)

        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(5)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _print_exec(msg):
    clord  = msg.get("11", "?")
    symbol = msg.get("55", "?")
    side   = SIDE.get(msg.get("54", ""), msg.get("54", "?"))
    qty    = msg.get("38", "?")
    price  = msg.get("44", msg.get("6", "0"))
    status = EXEC_TYPE.get(msg.get("150", ""), msg.get("150", "?"))

    try:
        price_str = f"@ {float(price):>8.2f}"
    except (ValueError, TypeError):
        price_str = f"@ {'?':>8}"

    print(f"[EXEC]    {clord:<10}  {symbol:<6}  {side:<5}  {qty:>6}  {price_str}  {status}")


def _print_mktdata(msg):
    symbol = msg.get("55", "?")
    px     = msg.get("270", "?")
    sz     = msg.get("271", "?")
    try:
        print(f"[MKTDATA] {symbol:<6}  trade  {sz} @ {float(px):.2f}")
    except (ValueError, TypeError):
        print(f"[MKTDATA] {symbol}  trade  {sz} @ {px}")


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

def _parse(line):
    """Return (action, args_dict) or (None, error_string)."""
    tokens = line.strip().split()
    if not tokens:
        return None, None

    cmd = tokens[0].lower()

    if cmd in ("quit", "exit"):
        return "quit", {}

    if cmd == "help":
        return "help", {}

    if cmd == "cancel":
        if len(tokens) != 2:
            return None, "Usage: cancel <ORDER-ID>"
        return "cancel", {"orig_order_id": tokens[1]}

    if cmd in ("buy", "sell"):
        side = "1" if cmd == "buy" else "2"
        # buy AAPL 100 @ 150.00   or   buy AAPL 100 market
        if len(tokens) < 3:
            return None, f"Usage: {cmd} <SYMBOL> <QTY> @ <PRICE>  or  {cmd} <SYMBOL> <QTY> market"
        symbol = tokens[1].upper()
        try:
            qty = int(tokens[2])
        except ValueError:
            return None, f"Invalid quantity: {tokens[2]}"

        rest = tokens[3:]
        if not rest or rest[0].lower() == "market":
            return "order", {"symbol": symbol, "side": side, "qty": qty, "price": None}
        if rest[0] == "@" and len(rest) == 2:
            try:
                price = float(rest[1])
            except ValueError:
                return None, f"Invalid price: {rest[1]}"
            return "order", {"symbol": symbol, "side": side, "qty": qty, "price": price}
        return None, f"Usage: {cmd} <SYMBOL> <QTY> @ <PRICE>  or  {cmd} <SYMBOL> <QTY> market"

    return None, f"Unknown command '{cmd}'. Type 'help' for commands."


# ---------------------------------------------------------------------------
# Order book for cancel lookup
# ---------------------------------------------------------------------------

class OrderTracker:
    """Remembers open orders so 'cancel CLI-3' can fill in symbol/side/qty."""
    def __init__(self):
        self._orders = {}  # order_id → {symbol, side, qty}

    def track(self, order_id, symbol, side, qty):
        self._orders[order_id] = {"symbol": symbol, "side": side, "qty": qty}

    def get(self, order_id):
        return self._orders.get(order_id)

    def remove(self, order_id):
        self._orders.pop(order_id, None)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def repl(session, tracker):
    print("Type 'help' for commands.\n")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            line = "quit"

        action, args = _parse(line)

        if action is None:
            if args:
                print(f"  {args}")
            continue

        if action == "help":
            print(HELP_TEXT)
            continue

        if action == "quit":
            print("Disconnecting...")
            session.logout()
            break

        if action == "order":
            order_id = session.send_order(
                args["symbol"], args["side"], args["qty"], args["price"]
            )
            tracker.track(order_id, args["symbol"], args["side"], args["qty"])
            session.recv_print()

        if action == "cancel":
            oid = args["orig_order_id"]
            info = tracker.get(oid)
            if info is None:
                print(f"  Unknown order '{oid}'. Only orders placed in this session can be canceled.")
                continue
            session.send_cancel(oid, info["symbol"], info["side"], info["qty"])
            tracker.remove(oid)
            session.recv_print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="fix-exchange interactive client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    print(f"Connecting to {args.host}:{args.port} ...")
    try:
        session = ClientSession(args.host, args.port)
    except OSError as e:
        print(f"Connection failed: {e}")
        print("Is the exchange running?  ./build/fix-exchange config/exchange.cfg")
        sys.exit(1)

    session.logon()
    print("[CONNECTED] Logon accepted.")

    tracker = OrderTracker()
    repl(session, tracker)
    session.close()


if __name__ == "__main__":
    main()
