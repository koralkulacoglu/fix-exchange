from helpers import (
    FixSession, claim_session, release_session,
    now_str, admin_send,
)


def _send_limit(s, clord_id, symbol, side, price, qty):
    s.send("D", {
        "11": clord_id,
        "21": "1",
        "55": symbol,
        "54": side,
        "40": "2",
        "44": str(price),
        "38": str(qty),
        "60": now_str(),
    })
    return s.recv()


def _establish_last_price(s, symbol, price):
    """Match a 1-lot at the given price to seed last_price for the symbol."""
    _send_limit(s, "SETUP-SELL", symbol, "2", price, 1)  # resting sell → New ack
    _send_limit(s, "SETUP-BUY",  symbol, "1", price, 1)  # crosses → BUY New ack
    s.recv()  # maker fill (SELL)
    s.recv()  # taker fill (BUY)


def test_max_qty_rejected():
    """Order qty above MaxOrderQty=10000 is rejected."""
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        resp = _send_limit(s, "ORD-1", "AAPL", "1", 150.00, 10001)
        assert resp.get("35")  == "8", f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150") == "8", f"Expected ExecType=Rejected, got {resp.get('150')}"
        assert "exceeds max" in resp.get("58", ""), \
            f"Expected rejection reason in tag 58, got {resp.get('58')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_max_qty_at_limit_accepted():
    """Order qty exactly at MaxOrderQty=10000 is accepted."""
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        resp = _send_limit(s, "ORD-1", "AAPL", "1", 150.00, 10000)
        assert resp.get("150") == "0", \
            f"Expected ExecType=New for qty=10000 (at limit), got {resp.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_collar_no_last_price_passes():
    """Collar check is skipped when no trade has occurred for the symbol."""
    admin_send("REGISTER RCNL")
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        # Extreme price — would breach any collar, but no last price exists yet
        resp = _send_limit(s, "ORD-1", "RCNL", "1", 999.00, 1)
        assert resp.get("150") == "0", \
            f"Expected New (no last price for collar), got ExecType={resp.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_collar_within_passes():
    """Limit order within PriceCollarPct=50% of last trade price is accepted."""
    admin_send("REGISTER RCWI")
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        _establish_last_price(s, "RCWI", 100.00)

        # 130.00 is 30% from 100.00 — within the 50% collar
        resp = _send_limit(s, "ORD-1", "RCWI", "1", 130.00, 1)
        assert resp.get("150") == "0", \
            f"Expected New for price within collar, got ExecType={resp.get('150')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


def test_collar_outside_rejected():
    """Limit order beyond PriceCollarPct=50% of last trade price is rejected."""
    admin_send("REGISTER RCOT")
    comp_id = claim_session()
    try:
        s = FixSession(sender=comp_id)
        s.connect()
        s.logon()

        _establish_last_price(s, "RCOT", 100.00)

        # 160.00 is 60% from 100.00 — outside the 50% collar
        resp = _send_limit(s, "ORD-1", "RCOT", "1", 160.00, 1)
        assert resp.get("35")  == "8", f"Expected ExecutionReport, got {resp.get('35')}"
        assert resp.get("150") == "8", \
            f"Expected ExecType=Rejected for price outside collar, got {resp.get('150')}"
        assert "deviates" in resp.get("58", ""), \
            f"Expected collar rejection reason in tag 58, got {resp.get('58')}"

        s.logout()
        s.close()
    finally:
        release_session(comp_id)


TESTS = [
    ("Risk → max order qty rejected",                        test_max_qty_rejected),
    ("Risk → order at qty limit accepted",                   test_max_qty_at_limit_accepted),
    ("Risk → price collar skipped with no last trade price", test_collar_no_last_price_passes),
    ("Risk → price collar passes for order within range",    test_collar_within_passes),
    ("Risk → price collar rejects order outside range",      test_collar_outside_rejected),
]
