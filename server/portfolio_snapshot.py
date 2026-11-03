"""Coordinate display-only portfolio reads and validate their account binding."""

from collections.abc import Callable
from typing import Any

from server import performance_history_cache, snapshot_coordinator
from server.contract import utc_now_iso


def _snapshot_config(config: dict) -> dict:
    return {key: value for key, value in config.items() if key.startswith(("exec_", "risk_"))}


def _unavailable(config: dict, reason: str, stale_after: int) -> dict:
    return {
        "generated_at": utc_now_iso(),
        "currency": str(config.get("exec_quote_currency") or "USDT").strip().upper() or None,
        "cash": None,
        "equity_est": None,
        "positions": [],
        "positions_source": "unavailable",
        "realized_pnl": None,
        "halted_today": None,
        "warnings": [f"display snapshot unavailable: {reason}"],
        "stale_after": stale_after,
        "futures": {
            "equity": None,
            "wallet_balance": None,
            "available_balance": None,
            "unrealized_pnl": None,
            "venue_profile": None,
            "equity_currency": None,
            "wallet_balance_currency": None,
            "available_balance_currency": None,
            "unrealized_pnl_currency": None,
            "account_scope": "derivatives",
            "positions": [],
            "positions_read": {
                "status": "unavailable", "skipped": None, "issues": [], "observed_at": None,
            },
            "status": "unavailable",
            "error": None,
        },
        "snapshot": {
            **snapshot_coordinator.status(None),
            "status": "unavailable",
            "freshness": "unknown",
            "error": reason,
            "atomic": False,
        },
    }


def read_portfolio(
    config: dict,
    *,
    build_bridge: Callable,
    get_config: Callable,
    load_portfolio: Callable,
    stale_after: int,
) -> dict:
    """Read once with one bridge, discarding results if its binding changes."""
    original_config = _snapshot_config(config)
    if config.get("exec_live") is not True:
        body = load_portfolio(config)
        if _snapshot_config(get_config()) != original_config:
            return _unavailable(config, "account_identity_changed", stale_after)
        return body

    sync = snapshot_coordinator.enabled()
    try:
        bridge = build_bridge(config)
        live = bridge.effective_mode() == "live"
        client_method = getattr(bridge, "client", None)
        client = client_method() if live and callable(client_method) else None
        context = (
            snapshot_coordinator.credential_context(bridge, client)
            if client is not None and hasattr(bridge, "config") and hasattr(bridge, "exchange_id")
            else None
        )
    except Exception:
        if sync:
            return _unavailable(config, "account_read_unavailable", stale_after)
        raise
    if sync and live and context is None:
        return _unavailable(config, "account_identity_unavailable", stale_after)

    def binding_matches() -> bool:
        try:
            current_config = get_config()
            if _snapshot_config(current_config) != original_config:
                return False
            if snapshot_coordinator.enabled() != sync:
                return False
            current_bridge = build_bridge(current_config)
            if (bridge.effective_mode() == "live") != live:
                return False
            if (current_bridge.effective_mode() == "live") != live:
                return False
            if context is None:
                return True
            current_client = current_bridge.client()
            return (
                bridge.client() is client
                and snapshot_coordinator.credential_context(bridge, client) == context
                and snapshot_coordinator.credential_context(current_bridge, current_client) == context
            )
        except Exception:
            return False

    def load() -> Any:
        if not binding_matches():
            raise snapshot_coordinator.SnapshotUnavailable("account_identity_changed")
        try:
            body = load_portfolio(config, bridge=bridge)
        except Exception as exc:
            if not binding_matches():
                raise snapshot_coordinator.SnapshotUnavailable("account_identity_changed") from exc
            raise
        if not binding_matches():
            raise snapshot_coordinator.SnapshotUnavailable("account_identity_changed")
        if sync and performance_history_cache.is_rate_limited():
            body["_snapshot_no_cache"] = True
        return body

    if not sync or not live:
        try:
            return load()
        except snapshot_coordinator.SnapshotUnavailable as exc:
            return _unavailable(config, str(exc), stale_after)

    try:
        body, metadata = snapshot_coordinator.read(
            context,
            "portfolio",
            {"venue": str(bridge.exchange_id).lower(), "scope": "spot+derivatives",
             "config": original_config},
            load,
        )
    except snapshot_coordinator.SnapshotUnavailable as exc:
        return _unavailable(config, str(exc), stale_after)
    except Exception:
        return _unavailable(config, "account_read_failed", stale_after)
    if not binding_matches():
        return _unavailable(config, "account_identity_changed", stale_after)
    body = body.copy()
    body.pop("_snapshot_no_cache", None)
    body["snapshot"] = {**metadata, "atomic": False}
    return body
