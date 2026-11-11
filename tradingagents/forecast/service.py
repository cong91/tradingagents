"""Orchestrate verified Kronos inference into a decision forecast block."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import (
    MODEL_REPO,
    MODEL_REVISION,
    TIMEFRAME_HOURS,
    TOKENIZER_REPO,
    TOKENIZER_REVISION,
)
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.cache import ForecastCache, cache_key, content_digest
from tradingagents.forecast.contract import (
    CALIBRATION_SAMPLE_RATIO,
    DISTRIBUTION_MODEL,
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    direction_of,
)
from tradingagents.forecast.inputs import series_problem
from tradingagents.forecast.sampler import KronosSampler, SampledSymbol, SamplerSettings, percentile
from tradingagents.forecast.settings import (
    MODE_FILTER,
    MODES,
    ForecastSettings,
    settings_from_config,
)

logger = logging.getLogger(__name__)


FILTER_MODE_REFUSAL = (
    "a hard direction gate is not stable enough to cut candidates: at the merge "
    "scale (10 symbols x 10 seeds, identical candles, only the seed changed) the "
    "verdict held for 2/10 symbols at T=1.0 and 3/10 at T=0.5, both below the "
    "8/10 bar. Kronos therefore contributes CONTEXT to the decision, never a veto."
)


class _Sampler(Protocol):
    def sample(self, requests: list[tuple[str, str, int, tuple[Candle, ...], int]]) -> list[SampledSymbol]: ...


def _directions(paths: list[dict[str, Any]], reference: float) -> tuple[dict[str, int], dict[str, float]]:
    counts = {"up": 0, "down": 0, "flat": 0}
    for path in paths:
        closes = path["closes"]
        counts[direction_of(reference, closes[-1])] += 1
    total = sum(counts.values()) or 1
    return counts, {name: count / total for name, count in counts.items()}


def _majority(counts: dict[str, int]) -> str:
    top = max(counts.values())
    leaders = [name for name, count in counts.items() if count == top]
    return leaders[0] if len(leaders) == 1 else "flat"


def _paths_problem(
    paths: tuple[dict[str, Any], ...], *, requested_paths: int, horizon_bars: int,
) -> str | None:
    """The service-side acceptance check, before anything is labelled ``ok``.

    The sampler validates the child's answer; this re-checks at the seam the
    decision path reads, so a malformed or short result can never become a
    ``status=ok`` block even through a caller-supplied sampler.
    """
    if horizon_bars < 1:
        return "the configured horizon does not resolve to at least one bar"
    if len(paths) != requested_paths:
        return (
            f"the sample carries {len(paths)} path(s) for a {requested_paths}-path request; "
            "a request is satisfied only by the exact number of independent draws"
        )
    for index, path in enumerate(paths):
        for name in ("closes", "highs", "lows"):
            values = path.get(name)
            if not isinstance(values, list) or len(values) != horizon_bars:
                return (
                    f"path {index} {name} does not cover the {horizon_bars}-bar horizon "
                    f"(got {len(values) if isinstance(values, list) else type(values).__name__})"
                )
            for value in values:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return f"path {index} {name} carries a non-numeric value"
                if not math.isfinite(value) or value <= 0:
                    return f"path {index} {name} carries a non-finite or non-positive price {value!r}"
        highs, lows = path["highs"], path["lows"]
        if any(high < low for high, low in zip(highs, lows, strict=True)):
            return f"path {index} has a bar whose high is below its low"
    return None


class KronosForecastService:
    """Turn closed candles into decision-grade blocks, bounded and cacheable."""

    provider_id = "kronos"
    distribution_kind = DISTRIBUTION_MODEL
    calibration_label = CALIBRATION_SAMPLE_RATIO
    model_revision = f"{MODEL_REPO}@{MODEL_REVISION[:12]}+{TOKENIZER_REPO}@{TOKENIZER_REVISION[:12]}"

    def __init__(
        self,
        *,
        sampler: _Sampler | None = None,
        settings: ForecastSettings | None = None,
        artifact_hash: str | None = None,
        artifact_problem: str | None = None,
        venue: str = "",
        cache: ForecastCache | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.settings = settings or ForecastSettings()
        self._sampler = sampler
        self._created_at = created_at
        self.artifact_hash = artifact_hash
        # The readiness outcome of the pinned-artifact preflight. A non-None
        # problem means the source/weights on disk were NOT the pinned ones (or
        # absent), and every forecast refuses before the child is spawned.
        self.artifact_problem = artifact_problem
        # The venue/contract scope of this service's inputs; part of the cache
        # identity because the same contract string is not the same market on
        # two venues.
        self.venue = venue
        self.cache = cache if cache is not None else ForecastCache()

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any], **kwargs: Any) -> KronosForecastService:
        """Build the service the operator configured; no model is loaded here.

        The pinned source and weights are VERIFIED by content before any child
        process exists — the same memoized ``verify_artifacts`` the shadow
        consumer runs — so an unavailable environment or a hash mismatch
        surfaces as ``unavailable`` BEFORE spawning instead of loading
        something the pins never described. The block's ``artifact_hash`` is
        the digest of the hashes that were actually checked; when verification
        failed, it stays None (an expected hash is never labelled as checked).
        """
        from tradingagents.forecast.artifact import (
            artifact_digest_from,
            isolated_model_dir,
            isolated_python,
            isolated_source_dir,
            verify_artifacts,
        )

        settings = settings_from_config(config)
        source_dir, model_dir = isolated_source_dir(), isolated_model_dir()
        problem, verified = verify_artifacts(source_dir, model_dir)
        # Consumed here either way: a caller's budget must never leak into the
        # constructor as an unexpected keyword when verification failed.
        deadline = kwargs.pop("chunk_deadline_seconds", None)
        sampler = None
        if problem is None:
            sampler_settings = settings.sampler_settings()
            if deadline is not None:
                sampler_settings = SamplerSettings(
                    paths=sampler_settings.paths, temperature=sampler_settings.temperature,
                    top_p=sampler_settings.top_p, max_batch_rows=sampler_settings.max_batch_rows,
                    chunk_deadline_seconds=min(sampler_settings.chunk_deadline_seconds, float(deadline)),
                )
            sampler = KronosSampler(
                python=isolated_python(), source_dir=source_dir,
                model_dir=model_dir, settings=sampler_settings,
            )
        venue = str(config.get("exec_exchange_id") or "")
        return cls(
            sampler=sampler, settings=settings,
            artifact_hash=artifact_digest_from(verified) if problem is None else None,
            artifact_problem=problem, venue=venue, **kwargs,
        )

    def close(self) -> None:
        """Stop the sampler's child, if one was ever started.

        Ownership is scoped: whoever builds the service (``forecast_for_cycle``
        builds one per candidate) closes it on success, on error and on
        timeout, so no model child outlives its owner.
        """
        sampler = self._sampler
        if sampler is not None and hasattr(sampler, "close"):
            sampler.close()

    # -- forecasting --------------------------------------------------------

    def _now(self) -> datetime:
        return self._created_at or datetime.now(UTC)

    def _first_realized_close(self, series: CandleSeries) -> datetime | None:
        step_hours = TIMEFRAME_HOURS.get(self.settings.timeframe)
        if not step_hours:
            return None
        return series.as_of.astimezone(UTC) + timedelta(hours=step_hours)

    def forecast(self, series: CandleSeries, *, seed: int) -> ForecastBlock:
        """One symbol's block, from the cache when the identity matches."""
        settings = self.settings
        symbol = series.symbol
        if not settings.enabled:
            return _failure(symbol, STATUS_UNAVAILABLE, "active Kronos forecasting is disabled")
        if settings.mode == MODE_FILTER:
            return _failure(symbol, STATUS_UNAVAILABLE, FILTER_MODE_REFUSAL)
        if settings.mode not in MODES:
            return _failure(symbol, STATUS_UNAVAILABLE, f"unknown Kronos mode {settings.mode!r}")
        problem = settings.settings_problem()
        if problem is not None:
            return _failure(symbol, STATUS_UNAVAILABLE, problem)
        if self.artifact_problem is not None:
            # The pinned artifacts on disk were absent or not what the pins
            # describe: refuse BEFORE any child imports or loads them.
            return _failure(symbol, STATUS_UNAVAILABLE, self.artifact_problem)
        problem = series_problem(
            series, settings.timeframe, lookback_bars=settings.lookback_bars,
        )
        if problem is not None:
            return _failure(symbol, STATUS_UNAVAILABLE, problem, stale=True)

        created = self._now()
        # A decision whose first predicted bar has already closed is a
        # historical (research/replay) decision: nothing created earlier can
        # PROVE it existed at the decision timestamp, so the cache is bypassed
        # entirely and the draw below is labelled retrospective.
        first_realized_close = self._first_realized_close(series)
        decision_is_historical = (
            first_realized_close is not None and created > first_realized_close
        )
        key = None if decision_is_historical else self._key(series, seed)
        if key is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        if self._sampler is None:
            return _failure(
                symbol, STATUS_UNAVAILABLE, "no Kronos sampler is configured for this process",
            )
        window = series.candles[-settings.lookback_bars:]
        try:
            sampled = self._sampler.sample([
                (symbol, settings.timeframe, settings.horizon_hours, window, seed),
            ])
        except Exception as exc:  # a sampler bug is a status, never a crashed cycle
            logger.warning("kronos forecast raised for %s: %r", symbol, exc)
            return _failure(symbol, STATUS_ERROR, f"sampler raised {type(exc).__name__}: {exc}")
        if not sampled:
            return _failure(symbol, STATUS_ERROR, "the sampler returned no result for this symbol")
        block = self._block_from(series, sampled[0], seed=seed)
        if block.ok and key is not None:
            self.cache.put(key, block)
        return block

    def _key(self, series: CandleSeries, seed: int) -> str:
        return cache_key(
            symbol=series.symbol, timeframe=self.settings.timeframe,
            data_open_time=series.candles[-1].open_time, settings=self.settings,
            model_revision=self.model_revision, artifact_hash=self.artifact_hash, seed=seed,
            venue=self.venue,
            cutoff_ms=int(series.as_of.astimezone(UTC).timestamp() * 1000),
            data_digest=content_digest(series.candles[-self.settings.lookback_bars:]),
        )

    def _block_from(self, series: CandleSeries, sampled: SampledSymbol, *, seed: int) -> ForecastBlock:
        settings = self.settings
        if sampled.status != STATUS_OK:
            return _failure(
                series.symbol, sampled.status, sampled.reason or "no reason reported",
            )
        if sampled.symbol != series.symbol:
            return _failure(
                series.symbol, STATUS_ERROR,
                f"the sampler returned paths for {sampled.symbol!r}, not the requested "
                f"{series.symbol!r}",
            )
        # Re-validated HERE, at the seam the decision path reads, before any
        # block is labelled ok: a one-path answer to a 16-path request, a short
        # horizon or a non-finite price is an error, never a small sample.
        problem = _paths_problem(
            sampled.paths, requested_paths=settings.paths,
            horizon_bars=settings.horizon_hours // TIMEFRAME_HOURS.get(settings.timeframe, 0),
        )
        if problem is not None:
            return _failure(series.symbol, STATUS_ERROR, problem)
        paths = [dict(path) for path in sampled.paths]
        reference = series.candles[-1].close
        counts, ratios = _directions(paths, reference)
        ends = [float(path["closes"][-1]) for path in paths]
        low = percentile(ends, 0.10)
        high = percentile(ends, 0.90)
        spread = (high - low) / reference if reference else None
        # The band is the model's own stated uncertainty. A band wider than the
        # configured bound is reported as ``unclear`` rather than dropped: the
        # caller still sees the numbers, and the label says the model is not
        # committing. The bound is uncalibrated and is recorded as such.
        direction = _majority(counts)
        if spread is not None and spread > settings.max_band_spread_ratio:
            direction = "unclear"
        created = self._now()
        # ``forward`` is a TIMESTAMP claim: the draws happened before the first
        # predicted bar closed. It is not proof the forecast existed at the
        # decision timestamp and never evidence of out-of-sample skill or alpha.
        first_realized_close = self._first_realized_close(series)
        return ForecastBlock(
            symbol=series.symbol,
            status=STATUS_OK,
            direction=direction,
            direction_counts=counts,
            direction_ratio=ratios,
            band={"low": low, "p50": percentile(ends, 0.5), "high": high},
            band_spread_ratio=spread,
            reference_close=reference,
            data_open_time=series.candles[-1].open_time,
            as_of=series.as_of.isoformat(),
            horizon_hours=settings.horizon_hours,
            timeframe=settings.timeframe,
            paths=len(paths),
            per_path_samples=1,
            generation_kind=(
                GENERATION_FORWARD
                if first_realized_close is not None and created <= first_realized_close
                else GENERATION_RETROSPECTIVE
            ),
            temperature=settings.temperature,
            model_revision=self.model_revision,
            artifact_hash=self.artifact_hash,
            latency_ms=sampled.duration_ms,
            created_at=created.isoformat(),
        )


def _failure(
    symbol: str, status: str, reason: str, *, stale: bool = False,
) -> ForecastBlock:
    return ForecastBlock(
        symbol=symbol, status=status,
        reason=f"stale: {reason}" if stale else reason,
    )
