"""Watchlist: GET/POST/DELETE /api/watchlist (docs/ui-api-contract.md §2).

``exec_watchlist`` is deliberately not env-overridable (default_config.py:38-41),
so the API layer keeps its own state file (``server/data/watchlist.json``) and
syncs the engine default ``DEFAULT_CONFIG["exec_watchlist"]`` after every write,
so an in-process daily runner would read the same list. Symbols are normalized
to Yahoo pipeline form (strip + uppercase); an empty watchlist is valid.
"""

import json

from fastapi import APIRouter, Request

from server import audit as audit_log, paths
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.default_config import DEFAULT_CONFIG

router = APIRouter(prefix="/api", tags=["watchlist"])


def _load_state() -> tuple[list[dict], str | None]:
    """(symbols, warning). A missing file is a fresh install, not an error; an
    unreadable one is recovered from the engine default, per contract §2."""
    path = paths.WATCHLIST_PATH
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        if not isinstance(symbols, list):
            raise ValueError("symbols must be a list")
        items = [{"symbol": str(entry["symbol"]), "added_at": str(entry.get("added_at"))} for entry in symbols]
        return items, None
    except (OSError, ValueError, KeyError, TypeError):
        fallback = [{"symbol": str(t), "added_at": utc_now_iso()} for t in DEFAULT_CONFIG.get("exec_watchlist", [])]
        return fallback, "watchlist state file was unreadable; recovered from engine defaults"


def _save_state(items: list[dict]) -> None:
    paths.ensure_data_dir()
    paths.WATCHLIST_PATH.write_text(
        json.dumps({"symbols": items}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    DEFAULT_CONFIG["exec_watchlist"] = [item["symbol"] for item in items]


def ensure_seeded() -> None:
    """Startup: give a fresh install the engine's default watchlist so the UI
    shows the same list a CLI run would."""
    if not paths.WATCHLIST_PATH.exists():
        _save_state([{"symbol": str(t), "added_at": utc_now_iso()} for t in DEFAULT_CONFIG.get("exec_watchlist", [])])


def current_symbols() -> list[str]:
    """The watchlist as the API layer tracks it — the source POST /api/daily
    defaults to (contract §5: "exec_watchlist hiện hành"; ``get_config`` holds
    a frozen copy, so the file state is the authoritative one)."""
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
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ApiError(400, "validation_error", '"symbol" must be a non-empty string')
    symbol = symbol.strip().upper()
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
