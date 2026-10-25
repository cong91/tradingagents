"""Watchlist: GET/POST/DELETE /api/watchlist (docs/ui-api-contract.md §2).

``exec_watchlist`` is deliberately not env-overridable (default_config.py:38-41),
so the API layer keeps its own state file (``server/data/watchlist.json``) and
syncs the engine default ``DEFAULT_CONFIG["exec_watchlist"]`` after every write,
so an in-process daily runner would read the same list. Symbols are normalized
to Yahoo pipeline form (strip + uppercase); an empty watchlist is valid.
"""

import re

from fastapi import APIRouter, Request

from server import audit as audit_log, control_store
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.default_config import DEFAULT_CONFIG

router = APIRouter(prefix="/api", tags=["watchlist"])

_SYMBOL_RE = re.compile(r"^[A-Z0-9._=+^\-]+$")


def _validate_symbol(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "validation_error", '"symbol" must be a non-empty string')
    symbol = raw.strip().upper()
    if "," in symbol or not _SYMBOL_RE.fullmatch(symbol):
        raise ApiError(
            400,
            "validation_error",
            '"symbol" must contain exactly one ticker, not a comma-separated list',
        )
    return symbol


def _load_state() -> tuple[list[dict], str | None]:
    """(symbols, warning). An empty table on a fresh install is normal state."""
    items = control_store.load_watchlist()
    return items, None


def _save_state(items: list[dict]) -> None:
    control_store.replace_watchlist(items)
    DEFAULT_CONFIG["exec_watchlist"] = [item["symbol"] for item in items]


def ensure_seeded() -> None:
    """Startup: give a fresh install the engine's default watchlist so the UI
    shows the same list a CLI run would."""
    if not control_store.load_watchlist():
        _save_state([
            {"symbol": str(t), "added_at": utc_now_iso()}
            for t in DEFAULT_CONFIG.get("exec_watchlist", [])
        ])


def current_symbols() -> list[str]:
    """The watchlist as the API layer tracks it — the source POST /api/daily
    defaults to (contract §5: "exec_watchlist hiện hành"; ``get_config`` holds
    a frozen copy, so the database state is the authoritative one)."""
    items, _warning = _load_state()
    return [item["symbol"] for item in items]


def _payload(items: list[dict], warning: str | None = None) -> dict:
    payload = {"generated_at": utc_now_iso(), "symbols": items}
    if warning:
        payload["warning"] = warning
    return payload


@router.get("/watchlist")
def get_watchlist():
    items, warning = _load_state()
    return _payload(items, warning)


@router.post("/watchlist", status_code=201)
async def add_watchlist_symbol(request: Request):
    body = await json_body(request)
    symbol = _validate_symbol(body.get("symbol"))
    items, warning = _load_state()
    if any(item["symbol"].upper() == symbol for item in items):
        raise ApiError(409, "conflict", f"symbol {symbol} is already on the watchlist", {"symbol": symbol})
    items.append({"symbol": symbol, "added_at": utc_now_iso()})
    _save_state(items)
    audit_log.record("watchlist_added", symbol=symbol)
    return _payload(items, warning)


@router.delete("/watchlist/{symbol}")
def remove_watchlist_symbol(symbol: str):
    normalized = symbol.strip().upper()
    items, warning = _load_state()
    remaining = [item for item in items if item["symbol"].upper() != normalized]
    if len(remaining) == len(items):
        raise ApiError(404, "not_found", f"symbol {normalized} is not on the watchlist")
    _save_state(remaining)
    audit_log.record("watchlist_removed", symbol=normalized)
    return _payload(remaining, warning)
