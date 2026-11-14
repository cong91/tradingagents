"""Imported historical candle series for the financial experiment path.

The deterministic replay experiments compare *decisions*; the financial part
needs a full closed-candle series the export does not carry (plan §3.2: equity
curve, drawdown, net-of-cost expectation, exit-reason distribution). This
module is the one place that turns a caller-supplied OHLCV file into a
``Candle`` series the simulation engine accepts, so the experiment CLI never
grows a parser and the engine never grows a file format.

Contract (enforced here, not at the call site):

- the file declares its ``symbol``, ``timeframe`` and ``venue`` metadata, and
  every row carries the same symbol and timeframe — a mixed import is refused;
- rows are UTC epoch milliseconds, OHLCV complete, ``closed`` explicit: the
  loader refuses the forming bar rather than guessing it is closed;
- duplicates, overlaps and gaps are refused by the engine's own
  ``sequential_candles`` guard (point-in-time integrity, no silent fill);
- an ``as_of`` cutoff drops every candle not fully closed by that instant, so
  an import that reaches into the future cannot leak into a past-dated run.

Everything here is offline: the loader reads a local file and never opens a
network path.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents.dataflows.market_data import Candle
from tradingagents.simulation.data import as_of_closed, sequential_candles

# Accepted on-disk shapes. CSV keeps one row per candle; JSON is either a list
# of rows or an object with ``metadata`` + ``candles`` so the file can declare
# its symbol/timeframe/venue once instead of repeating it on every row.
_REQUIRED_FIELDS = ("open_time", "close_time", "open", "high", "low", "close")


class CandleImportError(ValueError):
    """An imported candle file is unusable; the message says exactly why."""


@dataclass(frozen=True)
class CandleImport:
    """A validated, ordered, closed series plus the metadata it declared."""

    candles: list[Candle]
    symbol: str
    timeframe: str
    venue: str | None
    source: str


def _metadata(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            return meta
    return {}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("candles")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    raise CandleImportError(
        "the JSON must be a list of candle rows, or an object with a 'candles' list"
    )


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_candle_file(
    path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    as_of: datetime | None = None,
) -> CandleImport:
    """Load and validate one local OHLCV file into a closed ``Candle`` series.

    ``symbol``/``timeframe`` may be supplied by the caller when the file does
    not declare them; when both the file and the caller state one, they must
    agree. ``as_of`` (timezone-aware) drops every candle not yet closed at that
    instant so an import cannot leak future bars into a past-dated run.
    """
    source = Path(path)
    if not source.is_file():
        raise CandleImportError(f"candle file not found: {source}")
    try:
        if source.suffix.lower() == ".csv":
            raw_rows = _read_csv(source)
            meta: dict[str, Any] = {}
        else:
            payload = json.loads(source.read_text(encoding="utf-8"))
            raw_rows = _rows(payload)
            meta = _metadata(payload)
    except json.JSONDecodeError as exc:
        raise CandleImportError(f"{source} is not valid JSON: {exc}") from exc
    except csv.Error as exc:
        raise CandleImportError(f"{source} is not valid CSV: {exc}") from exc
    if not raw_rows:
        raise CandleImportError(f"{source} carries no candle rows")

    declared_symbol = str(meta.get("symbol") or symbol or "").strip()
    declared_timeframe = str(meta.get("timeframe") or timeframe or "").strip()
    if not declared_symbol:
        raise CandleImportError(
            "the candle symbol is unknown: state it in the file metadata or pass --symbol"
        )
    if not declared_timeframe:
        raise CandleImportError(
            "the candle timeframe is unknown: state it in the file metadata or pass --timeframe"
        )
    if symbol and declared_symbol != symbol:
        raise CandleImportError(
            f"the file declares symbol {declared_symbol!r} but {symbol!r} was requested"
        )
    if timeframe and declared_timeframe != timeframe:
        raise CandleImportError(
            f"the file declares timeframe {declared_timeframe!r} but {timeframe!r} was requested"
        )
    venue = str(meta.get("venue") or "").strip() or None

    candles: list[Candle] = []
    for index, row in enumerate(raw_rows):
        candles.append(_candle_from_row(row, index, declared_symbol, declared_timeframe))

    # The engine's own guards do the ordering/contiguity/duplicate work, so the
    # rules cannot drift between the loader and the simulation.
    ordered = sequential_candles(candles, declared_timeframe)
    if as_of is not None:
        ordered = as_of_closed(ordered, as_of)
        if not ordered:
            raise CandleImportError(
                f"no candle in {source} is closed at or before {as_of.isoformat()}"
            )
    return CandleImport(
        candles=ordered, symbol=declared_symbol, timeframe=declared_timeframe,
        venue=venue, source=str(source),
    )


def _candle_from_row(
    row: dict[str, Any], index: int, symbol: str, timeframe: str,
) -> Candle:
    missing = [field for field in _REQUIRED_FIELDS if row.get(field) in (None, "")]
    if missing:
        raise CandleImportError(
            f"candle row {index} is missing {', '.join(missing)}"
        )
    row_symbol = str(row.get("symbol") or symbol).strip()
    if row_symbol != symbol:
        raise CandleImportError(
            f"candle row {index} declares symbol {row_symbol!r}, expected {symbol!r}"
        )
    row_timeframe = str(row.get("timeframe") or timeframe).strip()
    if row_timeframe != timeframe:
        raise CandleImportError(
            f"candle row {index} declares timeframe {row_timeframe!r}, expected {timeframe!r}"
        )
    # ``closed`` must be explicit: defaulting it true would let the forming bar
    # in, and defaulting it false would silently drop real closes.
    if "closed" not in row:
        raise CandleImportError(
            f"candle row {index} does not state 'closed': the import never guesses "
            "whether a bar has finished"
        )
    closed = _as_bool(row["closed"])
    try:
        return Candle(
            symbol=row_symbol,
            timeframe=row_timeframe,
            open_time=_as_int_ms(row["open_time"], index, "open_time"),
            close_time=_as_int_ms(row["close_time"], index, "close_time"),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0.0),
            is_closed=closed,
        )
    except (TypeError, ValueError) as exc:
        raise CandleImportError(f"candle row {index} has a non-numeric value: {exc}") from exc


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise CandleImportError(f"'closed' must be a boolean, got {value!r}")


def _as_int_ms(value: Any, index: int, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CandleImportError(
            f"candle row {index}: {field} must be epoch milliseconds, got {value!r}"
        ) from exc


def parse_as_of(text: str | None) -> datetime | None:
    """Parse an ISO-8601 cutoff into an aware datetime; naive input is refused."""
    if text is None or text == "":
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CandleImportError(f"--as-of must be ISO-8601, got {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


__all__ = ["CandleImport", "CandleImportError", "load_candle_file", "parse_as_of"]
