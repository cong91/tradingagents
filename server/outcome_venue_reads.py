"""Read-only venue history for outcome-ledger attribution, one window at a time.

One concern: for a given logical account, symbol and half-open time window,
return the venue's confirmed fills and funding income with the identity guards
the ledger requires — the account observed now must be the account the ledger
attributes to, and it must not change during the read. Builds the derivatives
executor the same way bracket_monitor does. Fills come from the bounded
trade-history reader; funding income from the venue income reader. Both are
reads; nothing is sent.
"""

from __future__ import annotations

from tradingagents.dataflows.config import get_config
from tradingagents.outcomes.netting import VENUE_SEMANTICS_BINANCE


def read_venue_window(account_id: str, symbol: str, since_ms: int, until_ms: int, *,
                      expected_venue: str | None = None) -> dict:
    """Read confirmed fills + funding income for the account's own window.

    Every failure mode returns an ``unavailable`` window with a named warning,
    never a partial guess: venue mismatch, identity mismatch, endpoint
    mismatch, or an identity that changed mid-read all refuse the whole read.
    """
    from server.futures_trade_history import read_futures_trade_history
    from tradingagents.execution import futures_identity
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.execution.exchange_client import venue_key

    config = get_config()
    executor = DerivativesExecutor(config=config)
    bridge = executor._bridge  # the bridge is the executor's venue reader (not a public API)
    selected_venue = venue_key(bridge)
    if expected_venue is not None and selected_venue != expected_venue:
        return {"status": "unavailable", "fills": [], "funding": None,
                "warnings": ["venue_mismatch_or_unknown"]}
    observed = futures_identity.read_futures_account_identity(bridge)
    if (observed is None or observed.account_type not in {"futures", "usds_futures"}
            or f"{observed.exchange}:{observed.profile}:{observed.identity_hash}" != account_id):
        return {"status": "unavailable", "fills": [], "funding": None,
                "warnings": ["account_identity_mismatch_or_unknown"]}
    client = bridge.client()
    credentials = (getattr(client, "apiKey", None), getattr(client, "secret", None))
    ccxt_symbol = symbol
    history = read_futures_trade_history(bridge, [ccxt_symbol], since_ms, until_ms)
    funding: list[dict] | None = None
    if history["status"] in ("ok", "partial"):
        from server.futures_trade_history import _endpoint_matches, _profile
        from tradingagents.execution.futures_funding import fetch_funding_income

        profile = _profile(bridge)
        if profile != observed.profile or not _endpoint_matches(client, observed.profile):
            return {"status": "unavailable", "fills": [], "funding": None,
                    "warnings": ["funding_venue_endpoint_mismatch"]}
        rows = fetch_funding_income(client, ccxt_symbol, since_ms)
        if rows is not None:
            funding = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                info = row.get("info") if isinstance(row.get("info"), dict) else {}
                if (row.get("symbol") != symbol or info.get("incomeType") != "FUNDING_FEE"
                        or type(row.get("timestamp")) is not int
                        or not since_ms <= row["timestamp"] < until_ms):
                    continue
                currency = info.get("asset") or row.get("code") or info.get("incomeAsset")
                amount = info.get("income") if info.get("income") is not None else row.get("amount")
                event_id = row.get("id") if row.get("id") is not None else info.get("tranId")
                if not isinstance(currency, str) or not currency or amount is None or event_id is None:
                    continue
                funding.append({
                    "id": str(event_id),
                    "timestamp": row.get("timestamp"),
                    "amount": amount,
                    "currency": currency,
                    "venue_semantics": VENUE_SEMANTICS_BINANCE,
                })
    if (bridge.client() is not client or venue_key(bridge) != selected_venue
            or (getattr(client, "apiKey", None), getattr(client, "secret", None)) != credentials
            or futures_identity.read_futures_account_identity(bridge) != observed):
        return {"status": "unavailable", "fills": [], "funding": None,
                "warnings": ["account_identity_changed_during_read"]}
    return {
        "status": history["status"],
        "venue_key": selected_venue,
        "fills": list(history["fills"]),
        "warnings": [*history["warnings"], "funding_history_unverified"],
        "start_at": history.get("start_at"),
        "end_at": history.get("end_at"),
        "funding": funding,
    }
