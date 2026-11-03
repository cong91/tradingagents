"""Safety checks for the performance API's audit-derived data."""

from server.performance import compute_performance


def test_audit_entries_are_not_presented_as_confirmed_current_positions():
    events = [
        {"action": "sell", "phase": "execute", "mode": "live", "market": "derivatives",
         "ticker": "UNI-USD", "ccxt_symbol": "UNI/USDT:USDT", "qty": 38,
         "price_est": 7.047, "timestamp": "2026-10-22T16:09:35Z"},
        {"action": "time_stop_closed", "phase": "execute", "mode": "live",
         "market": "derivatives", "ticker": "UNI/USDT:USDT", "qty": 38,
         "price_est": 7.057, "timestamp": "2026-10-31T01:31:51Z"},
    ]
    result = compute_performance(events)

    assert result["open_positions"] is None
    assert result["summary"] is None
    assert result["recent_orders"][0]["action"] == "time_stop_closed"


def test_simulated_and_live_entries_never_create_confirmed_win_statistics():
    events = [
        {"action": "buy", "phase": "execute", "mode": "dry", "ticker": "BTC-USD",
         "qty": 1, "price_est": 100},
        {"action": "sell", "phase": "execute", "mode": "live", "ticker": "BTC-USD",
         "qty": 1, "price_est": 110},
    ]
    result = compute_performance(events)

    assert result["summary"] is None
    assert result["open_positions"] is None
