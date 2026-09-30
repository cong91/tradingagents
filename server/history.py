"""History & backtest: GET /api/history and GET /api/backtest (§7).

Both read-only, built on the engine's own decision-log parser
(``TradingMemoryLog._parse_entry``) so the API can never drift from what runs
write. ``/api/history`` joins each entry with its on-disk run artifacts under
``results_dir``; ``/api/backtest`` summarizes the per-run decision logs of
backtest sweeps via ``tradingagents.backtest.summarize``.
"""

from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Query

from server.contract import ApiError, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.decision_log import TradingMemoryLog

router = APIRouter(prefix="/api", tags=["history"])

_EXCERPT_CHARS = 500


def _parse_log(path: Path) -> tuple[list[dict], str | None]:
    """(entries, warning) — engine parsing, plus a warning when blocks were dropped."""
    if not path.is_file():
        return [], None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"decision log unreadable: {exc}"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    entries: list[dict] = []
    dropped = 0
    for block in text.split(TradingMemoryLog._SEPARATOR):
        block = block.strip()
        if not block:
            continue
        parsed = log._parse_entry(block)
        if parsed is None:
            dropped += 1
        else:
            entries.append(parsed)
    return entries, (f"{dropped} log entries could not be parsed" if dropped else None)


def _history_item(entry: dict, config: dict) -> dict:
    results_dir = Path(str(config.get("results_dir", "")))
    safe = safe_ticker_component(entry["ticker"])
    state_log = results_dir / safe / "TradingAgentsStrategy_logs" / f"full_states_log_{entry['date']}.json"
    reports_dir = results_dir / safe / entry["date"] / "reports"
    saved_report = None
    if results_dir.is_dir():
        candidates = sorted(results_dir.glob(f"reports/{safe}_*/complete_report.md"))
        saved_report = str(candidates[-1]) if candidates else None
    return {
        "date": entry["date"],
        "ticker": entry["ticker"],
        "rating": entry["rating"],
        "pending": entry["pending"],
        "raw": entry["raw"],
        "alpha": entry["alpha"],
        "holding": entry["holding"],
        "resolved": entry["resolved"],
        "decision_excerpt": (entry.get("decision") or "")[:_EXCERPT_CHARS],
        "reflection": entry.get("reflection") or None,
        "run_artifacts": {
            "state_log": str(state_log) if state_log.is_file() else None,
            "reports_dir": str(reports_dir) if reports_dir.is_dir() else None,
            "saved_report": saved_report,
        },
    }


@router.get("/history")
def get_history(
    ticker: str | None = Query(None),
    pending_only: bool = Query(False),
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
):
    config = get_config()
    entries, warning = _parse_log(Path(str(config.get("memory_log_path", ""))).expanduser())
    matching = [
        entry for entry in entries
        if (ticker is None or entry["ticker"] == ticker)
        and (not pending_only or entry["pending"])
    ]
    matching.sort(key=lambda entry: entry["date"], reverse=True)  # newest first
    items = [_history_item(entry, config) for entry in matching[offset:offset + limit]]
    payload = {
        "generated_at": utc_now_iso(),
        "items": items,
        "total": len(matching),
        "by_ticker": dict(Counter(entry["ticker"] for entry in matching)),
    }
    if warning:
        payload["warning"] = warning
    return payload


def _backtest_summary(run_id: str, log_path: Path) -> dict:
    if not log_path.is_file():
        # Every cell of the sweep failed and wrote no log (backtest.py:180-181).
        return {
            "run_id": run_id, "log_path": str(log_path), "entries": 0,
            "resolved": 0, "pending": 0, "unscored": 0, "holding": "", "by_rating": {},
            "warning": "run wrote no decision log",
        }
    from tradingagents.backtest import summarize  # lazy: pulls the graph import chain

    summary = summarize(str(log_path))
    return {
        "run_id": run_id,
        "log_path": str(log_path),
        "entries": summary.resolved + summary.pending + summary.unscored,
        "resolved": summary.resolved,
        "pending": summary.pending,
        "unscored": summary.unscored,
        "holding": summary.holding,
        "by_rating": {
            rating: {"count": score.count, "hit_rate": score.hit_rate, "mean_alpha": score.mean_alpha}
            for rating, score in summary.by_rating.items()
        },
    }


@router.get("/backtest")
def get_backtest(run_id: str | None = Query(None)):
    config = get_config()
    base = Path(str(config.get("results_dir", ""))) / "backtest"
    if run_id is not None:
        run_dir = base / run_id
        if not run_dir.is_dir():
            raise ApiError(404, "not_found", f"backtest run {run_id} not found")
        payload = _backtest_summary(run_id, run_dir / "trading_memory.md")
        entries, warning = _parse_log(run_dir / "trading_memory.md")
        entries.sort(key=lambda entry: entry["date"], reverse=True)
        payload["entries"] = [_history_item(entry, config) for entry in entries]
        if warning:
            payload["warning"] = warning
        return payload
    if not base.is_dir():
        return {"generated_at": utc_now_iso(), "runs": []}
    runs = [
        _backtest_summary(directory.name, directory / "trading_memory.md")
        for directory in sorted(base.iterdir()) if directory.is_dir()
    ]
    return {"generated_at": utc_now_iso(), "runs": runs}
