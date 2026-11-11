"""Optional Kronos provider: the pinned model as a shadow observation source.

This is the thin shadow-side adapter over the neutral core. The pinning, the
isolated interpreter and the bounded inference live in ``tradingagents.forecast``
(``artifact``/``sampler``) because the decision path reads the same model and may
not import the observation layer. What stays here is the translation between the
two roles: one request in, one attempt out, with the pinning re-checked and the
artifact hash recorded on the row.

A failure is an answer. Missing interpreter, missing weights, a hash mismatch, a
crash, a non-JSON result or a deadline overrun all come back as
``unavailable``/``error``/``timeout`` with the reason — never as a forecast and
never as an exception that reaches a caller.

Each path is one draw kept separate (that is what makes the per-path statistics
meaningful); the ratio reported over N paths is a sample ratio, not a calibrated
probability, and the caller stores that label.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradingagents.forecast.artifact import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PATHS,
    DEFAULT_PROFILE,
    MAX_HORIZON_BARS,
    MAX_LOOKBACK_BARS,
    MAX_PATHS,
    MIN_LOOKBACK_BARS,
    MIN_PATHS,
    MODEL_REPO,
    MODEL_REVISION,
    PINNED_SOURCE_HASHES,
    PINNED_WEIGHT_HASHES,
    SOURCE_REVISION,
    TIMEFRAME_HOURS,
    TOKENIZER_REPO,
    TOKENIZER_REVISION,
    artifact_digest_from,
    isolated_model_dir,
    isolated_python,
    isolated_source_dir,
    profile_for,
    verify_artifacts,
)
from tradingagents.forecast.contract import (
    CALIBRATION_SAMPLE_RATIO,
    DISTRIBUTION_MODEL,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ForecastAttempt,
    ForecastRequest,
    PathSummary,
    direction_of,
)
from tradingagents.forecast.sampler import (
    KronosSampler,
    SamplerError,
    SamplerSettings,
    percentile,
)

# The shadow worker observes on 1h or 4h series; the bounds are the core's.
_TIMEFRAME_HOURS = TIMEFRAME_HOURS


@dataclass(frozen=True)
class KronosSettings:
    """The knobs that must be identical between two comparable observations."""

    profile: str = DEFAULT_PROFILE
    paths: int = DEFAULT_PATHS
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    temperature: float = 1.0
    top_p: float = 0.9
    contexts: tuple[str, ...] = ()

    def as_params(self) -> dict[str, Any]:
        profile = profile_for(self.profile)
        return {
            "profile": self.profile,
            "paths": self.paths,
            "lookback_bars": self.lookback_bars,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_context": profile.max_context,
        }

    def sampler_settings(self) -> SamplerSettings:
        return SamplerSettings(
            profile=self.profile, paths=max(MIN_PATHS, self.paths),
            temperature=self.temperature, top_p=self.top_p,
        )


class KronosProvider:
    """The pinned Kronos model behind the common provider interface."""

    provider_id = "kronos"
    distribution_kind = DISTRIBUTION_MODEL
    calibration_label = CALIBRATION_SAMPLE_RATIO
    source_revision = SOURCE_REVISION

    def __init__(
        self,
        *,
        deadline_seconds: float = 120.0,
        settings: KronosSettings | None = None,
        python: Path | None = None,
        source_dir: Path | None = None,
        model_dir: Path | None = None,
    ) -> None:
        self.deadline_seconds = deadline_seconds
        self.settings = settings or KronosSettings()
        self._python = python
        self._source_dir = source_dir
        self._model_dir = model_dir
        # Nothing has been verified yet, so nothing is claimed: the digest of
        # the hashes ACTUALLY checked is set by ``unavailable_reason()`` when
        # verification succeeds, before any row that records it exists.
        self.artifact_hash: str | None = None

    @property
    def model_revision(self) -> str:
        profile = profile_for(self.settings.profile)
        return (
            f"{profile.model_repo}@{profile.model_revision[:12]}+"
            f"{profile.tokenizer_repo}@{profile.tokenizer_revision[:12]}"
        )

    # -- availability -------------------------------------------------------

    def unavailable_reason(self) -> str | None:
        """Why this provider cannot run here, or None when it can."""
        python = self._python or isolated_python()
        if not python.exists():
            return (
                "the isolated Kronos interpreter is absent "
                f"({python}); install it outside the repo (plan §6.1)"
            )
        problem, verified = verify_artifacts(
            self._source_dir or isolated_source_dir(),
            self._model_dir or isolated_model_dir(),
            self.settings.profile,
        )
        if problem is None:
            # The recorded hash must be the one ACTUALLY checked on disk, not
            # merely the expected pins — an expected hash is not a checked one.
            self.artifact_hash = artifact_digest_from(verified, self.settings.profile)
        return problem

    # -- forecasting --------------------------------------------------------

    def forecast(self, request: ForecastRequest) -> ForecastAttempt:
        reason = self.unavailable_reason()
        if reason is not None:
            return ForecastAttempt(status=STATUS_UNAVAILABLE, reason=reason)
        hours = _TIMEFRAME_HOURS.get(request.timeframe)
        if hours is None:
            return ForecastAttempt(
                status=STATUS_ERROR,
                reason=f"Kronos adapter supports 1h/4h series, got {request.timeframe!r}",
            )
        if request.horizon_hours % hours:
            return ForecastAttempt(
                status=STATUS_ERROR,
                reason=(
                    f"horizon {request.horizon_hours}h is not a whole number of "
                    f"{request.timeframe} bars"
                ),
            )
        pred_len = request.horizon_hours // hours
        if pred_len > MAX_HORIZON_BARS:
            return ForecastAttempt(
                status=STATUS_ERROR,
                reason=f"horizon of {pred_len} bars exceeds the {MAX_HORIZON_BARS}-bar bound",
            )
        candles = list(request.candles)[-self.settings.lookback_bars:]
        if len(candles) < MIN_LOOKBACK_BARS:
            return ForecastAttempt(
                status=STATUS_UNAVAILABLE,
                reason=(
                    f"only {len(candles)} closed candles available; Kronos needs at "
                    f"least {MIN_LOOKBACK_BARS} and this adapter keeps the last "
                    f"{self.settings.lookback_bars}"
                ),
            )
        settings = self.settings.sampler_settings()
        settings = SamplerSettings(
            profile=settings.profile,
            paths=settings.paths, temperature=settings.temperature, top_p=settings.top_p,
            chunk_deadline_seconds=self.deadline_seconds,
        )
        sampler = KronosSampler(
            python=self._python or isolated_python(),
            source_dir=self._source_dir or isolated_source_dir(),
            model_dir=self._model_dir or isolated_model_dir(),
            settings=settings,
        )
        try:
            sampled = sampler.sample([
                (request.symbol, request.timeframe, request.horizon_hours,
                 tuple(candles), request.seed),
            ])
        except SamplerError as exc:
            return ForecastAttempt(status=STATUS_UNAVAILABLE, reason=str(exc))
        finally:
            sampler.close()
        if not sampled:
            return ForecastAttempt(status=STATUS_ERROR, reason="Kronos returned no result")
        entry = sampled[0]
        if entry.status != STATUS_OK:
            return ForecastAttempt(status=entry.status, reason=entry.reason)
        return _attempt_from_paths(request, entry.paths, entry.duration_ms)


def _attempt_from_paths(
    request: ForecastRequest, raw_paths: tuple[dict[str, Any], ...], duration_ms: float | None,
) -> ForecastAttempt:
    """Aggregate independent paths into one attempt; never invent a path."""
    if not raw_paths:
        return ForecastAttempt(status=STATUS_ERROR, reason="Kronos returned no paths")
    reference = request.reference_close
    summaries: list[PathSummary] = []
    for raw in raw_paths:
        try:
            closes = [float(value) for value in raw["closes"]]
            highs = [float(value) for value in raw["highs"]]
            lows = [float(value) for value in raw["lows"]]
        except (KeyError, TypeError, ValueError) as exc:
            return ForecastAttempt(status=STATUS_ERROR, reason=f"a Kronos path is malformed: {exc}")
        if not closes:
            return ForecastAttempt(status=STATUS_ERROR, reason="a Kronos path is empty")
        summaries.append(PathSummary(
            end_close=closes[-1],
            path_low=min(lows or closes),
            path_high=max(highs or closes),
            direction=direction_of(reference, closes[-1]),
            closes=tuple(closes),
            highs=tuple(highs),
            lows=tuple(lows),
        ))
    return ForecastAttempt(
        status=STATUS_OK,
        paths=tuple(summaries),
        direction=_majority_direction(summaries),
        duration_ms=duration_ms,
        samples=summarise_paths(reference, summaries),
        params={
            **request.params,
            "paths": len(summaries),
            "temperature": None,
            "profile": request.params.get("profile", DEFAULT_PROFILE),
        },
    )


def _majority_direction(paths: list[PathSummary] | tuple[PathSummary, ...]) -> str:
    """The direction most sampled paths ended in; ties resolve to ``flat``."""
    counts = {"up": 0, "down": 0, "flat": 0}
    for path in paths:
        counts[path.direction] += 1
    top = max(counts.values())
    leaders = [name for name, count in counts.items() if count == top]
    if len(leaders) != 1:
        return "flat"
    return leaders[0]


def summarise_paths(reference: float, paths: list[PathSummary] | tuple[PathSummary, ...]) -> dict[str, Any]:
    """Per-path aggregates: direction counts, end-close band, dispersion."""
    ends = [path.end_close for path in paths]
    counts = {"up": 0, "down": 0, "flat": 0}
    for path in paths:
        counts[path.direction] += 1
    total = len(ends)
    median = percentile(ends, 0.5)
    mean = sum(ends) / total
    variance = sum((value - mean) ** 2 for value in ends) / total
    return {
        "sample_count": total,
        "per_path_sample_count": 1,
        "direction_counts": counts,
        "direction_ratio": {name: count / total for name, count in counts.items()},
        "end_close": {
            "reference": reference,
            "p10": percentile(ends, 0.10),
            "p50": median,
            "p90": percentile(ends, 0.90),
            "min": min(ends),
            "max": max(ends),
        },
        "band": {"low": percentile(ends, 0.10), "high": percentile(ends, 0.90)},
        "dispersion": {
            "stdev": variance ** 0.5,
            "spread_ratio": (max(ends) - min(ends)) / reference if reference else None,
        },
    }


__all__ = [
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_PATHS",
    "MAX_LOOKBACK_BARS",
    "MAX_PATHS",
    "KronosProvider",
    "KronosSettings",
    "MODEL_REPO",
    "MODEL_REVISION",
    "PINNED_SOURCE_HASHES",
    "PINNED_WEIGHT_HASHES",
    "SOURCE_REVISION",
    "TOKENIZER_REPO",
    "TOKENIZER_REVISION",
    "isolated_model_dir",
    "isolated_python",
    "isolated_source_dir",
    "summarise_paths",
    "verify_artifacts",
]
