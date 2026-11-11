"""Content-addressed forecast identities and bounded in-memory reuse."""

from __future__ import annotations

import hashlib
import json

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.contract import GENERATION_RETROSPECTIVE
from tradingagents.forecast.settings import ForecastSettings


def content_digest(candles: tuple[Candle, ...]) -> str:
    """A digest of the candle CONTENT actually fed to the model.

    ``data_open_time`` identifies WHICH bars were used, not what they said: a
    corrected close at the same timestamp is a different input and must never
    share a cache entry with the uncorrected one.
    """
    rows = [
        [candle.open, candle.high, candle.low, candle.close, candle.volume,
         candle.open_time, candle.close_time, candle.symbol, candle.timeframe]
        for candle in candles
    ]
    material = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cache_key(
    *,
    symbol: str,
    timeframe: str,
    data_open_time: int,
    settings: ForecastSettings,
    model_revision: str,
    artifact_hash: str | None,
    seed: int,
    venue: str = "",
    cutoff_ms: int | None = None,
    data_digest: str | None = None,
) -> str:
    """The identity of one forecast: same key means the same numbers.

    The identity is complete, because a key collision serves wrong numbers:

    - ``symbol`` is the venue's own contract name, and ``venue`` scopes it (the
      same contract string on two venues is not the same market);
    - ``data_open_time`` is the last CLOSED candle's open time and
      ``cutoff_ms`` the decision moment — a new bar or a new boundary forms a
      new key, so a cache can never serve a forecast built on candles the
      decision had not seen;
    - ``data_digest`` is the OHLCV content of the window, so a corrected price
      at the same timestamp misses;
    - ``settings.as_params()`` carries every knob that changes what the numbers
      mean (paths, temperature, top_p, horizon, lookback, the band-spread
      bound), with ``mode`` alongside;
    - ``model_revision`` and ``artifact_hash`` pin which model and which
      verified artifacts drew the numbers, and ``seed`` pins the draws.
    """
    material = json.dumps({
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "data_open_time": int(data_open_time),
        "cutoff_ms": None if cutoff_ms is None else int(cutoff_ms),
        "data_digest": data_digest,
        "params": settings.as_params(),
        "profile": settings.profile,
        "mode": settings.mode,
        "model_revision": model_revision,
        "artifact_hash": artifact_hash,
        "seed": int(seed),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ForecastCache:
    """In-memory reuse for one process, keyed on the forecast's full identity.

    Deliberately small and non-persistent: a cached block is only valid for the
    closed candle it was drawn from, so it ages out as soon as a bar closes, and
    the value of persisting it is outweighed by the risk of serving a stale one.
    The bounds keep it from growing without limit.

    A ``retrospective`` entry is never stored or served: it was produced after
    its own outcome realized, so replaying it into a live decision would be
    look-ahead. The SERVICE additionally bypasses the cache entirely for a
    decision whose first predicted bar has already closed — no entry drawn
    earlier can prove it existed at that decision's timestamp.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self._entries: dict[str, ForecastBlock] = {}
        self._max_entries = max(1, int(max_entries))

    def get(self, key: str) -> ForecastBlock | None:
        return self._entries.get(key)

    def put(self, key: str, block: ForecastBlock) -> None:
        if block.generation_kind == GENERATION_RETROSPECTIVE:
            return  # an entry that cannot be served forward is not stored
        if len(self._entries) >= self._max_entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = block

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
