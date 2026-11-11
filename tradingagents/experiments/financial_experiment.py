"""Financial experiment CLI: imported candles through the real simulation.

Plan §3.2 acceptance needs a *command a user can run* that produces the
financial part — equity curve, drawdown, net after costs/funding, exit-reason
distribution — not only a unit test. This module is that entry point:

    .venv\\Scripts\\python.exe -m tradingagents.experiments.financial_experiment \\
        --candles history.json --fixture tests/fixtures/futures_cycles_replay.json \\
        --holdout-start 2026-08-01 --horizon-days 1 --seed 7 --repeat 3

It wires the pieces that already exist and nothing more:

- ``candles.load_candle_file`` validates the imported OHLCV series (offline);
- ``baselines.rule_source`` builds the proposal source from the REAL
  production confirmation rule over the recorded cycles;
- ``walkforward.split_walkforward`` produces the train/holdout split with its
  leakage assertion;
- ``runner.run_experiment`` runs the simulation and writes manifest, per-repeat
  rows and summary under ``TRADINGAGENTS_RESULTS_DIR``.

Two honesty boundaries are enforced here, not in prose:

- the run is labelled ``replay_of_recording`` and every recorded-only variant
  states that the model/agent was NOT re-run, so an uplift of an unexecuted
  knob is never claimed;
- the financial report is emitted with its coverage. A series too short for
  the requested horizon, a missing funding series, or a variant that never
  trades are reported as such — a zero is never substituted for unknown.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tradingagents.execution.risk import RiskLimits

from tradingagents.experiments import artifacts
from tradingagents.experiments.baselines import no_trade_source, rule_source
from tradingagents.experiments.candles import (
    CandleImportError,
    load_candle_file,
    parse_as_of,
)
from tradingagents.experiments.runner import (
    DEFAULT_METRIC,
    VariantSpec,
    build_manifest,
    run_experiment,
)
from tradingagents.experiments.walkforward import (
    CLOSE_TIME_KEY,
    OPEN_TIME_KEY,
    holdout_start_ms_from_iso,
    split_walkforward,
)

_DAY_MS = 86_400_000


def _recorded_rows(fixture_path: Path | None) -> list[dict[str, Any]]:
    from server.futures_replay import FIXTURE_PATH

    path = fixture_path if fixture_path is not None else FIXTURE_PATH
    if not path.is_file():
        raise CandleImportError(f"recorded-cycle fixture not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise CandleImportError(f"{path} must be a JSON list of futures_cycles rows")
    return [row for row in payload if isinstance(row, dict)]


def _variants(rule_name: str, recorded: list[dict[str, Any]]) -> list[VariantSpec]:
    from server.futures_replay import RULES

    if rule_name not in RULES:
        raise CandleImportError(
            f"unknown rule {rule_name!r}; available: {sorted(RULES)}"
        )
    rule = RULES[rule_name]
    return [
        VariantSpec(name="no_trade", kind="no_trade", factory=lambda: no_trade_source),
        VariantSpec(
            name=rule_name, kind="rule",
            factory=lambda: rule_source(rule, recorded),
        ),
    ]


def financial_replay(
    *,
    candles_path: Path,
    fixture_path: Path | None,
    rule_name: str,
    holdout_start: str,
    horizon_days: int,
    embargo_days: int,
    seed: int,
    repeat: int,
    initial_cash: float,
    quote_currency: str,
    as_of: str | None,
    results_dir: str | Path | None,
    symbol: str | None = None,
    timeframe: str | None = None,
    derivatives_exposure_pct: float | None = None,
    register_run: bool = False,
) -> dict[str, Any]:
    """Run the financial replay and return the summary the CLI prints."""
    imported = load_candle_file(
        candles_path, symbol=symbol, timeframe=timeframe, as_of=parse_as_of(as_of),
    )
    recorded = _recorded_rows(fixture_path)

    # The split works on dated rows, so project the imported series into the
    # same open/close-ms shape the walkforward expects.
    rows = [
        {OPEN_TIME_KEY: candle.open_time, CLOSE_TIME_KEY: candle.close_time}
        for candle in imported.candles
    ]
    holdout_start_ms = holdout_start_ms_from_iso(holdout_start)
    split = split_walkforward(
        rows, holdout_start_ms=holdout_start_ms,
        horizon_days=horizon_days, embargo_days=embargo_days,
    )
    holdout_opens = {row[OPEN_TIME_KEY] for row in split.holdout}
    holdout_candles = [c for c in imported.candles if c.open_time in holdout_opens]
    if not holdout_candles:
        raise CandleImportError(
            "the holdout window holds no imported candle: widen the series or move "
            "--holdout-start inside the imported range"
        )

    # Same limits the live derivatives executor uses. A bare RiskLimits() leaves
    # max_derivatives_exposure_pct at 0, which blocks every derivatives entry, so
    # the run would report a flat zero that says nothing about the rule.
    overrides = (
        {"risk_max_derivatives_exposure_pct": derivatives_exposure_pct}
        if derivatives_exposure_pct is not None else None
    )
    risk_limits = runtime_risk_limits(overrides)
    manifest_note = None
    if risk_limits.max_derivatives_exposure_pct <= 0:
        manifest_note = (
            "risk_max_derivatives_exposure_pct is 0, so the runtime blocks every "
            "derivatives entry and this run cannot open a position; pass "
            "--derivatives-exposure-pct to model a specific cap"
        )

    variants = _variants(rule_name, recorded)
    run_id = f"financial_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{seed}"
    first_open = datetime.fromtimestamp(min(holdout_opens) / 1000, UTC)
    last_open = datetime.fromtimestamp(max(holdout_opens) / 1000, UTC)
    manifest = build_manifest(
        run_id=run_id,
        hypothesis=(
            f"does the {rule_name} confirmation rule's simulated net after costs differ "
            "from holding cash, on imported closed candles"
        ),
        # A real ISO range: the registry parses this with the walkforward date
        # helper, so a synthetic string like "<date>:+1d" is not acceptable.
        window=f"{first_open.date().isoformat()}:{last_open.date().isoformat()}",
        holdout_start=holdout_start,
        horizon_days=horizon_days,
        embargo_days=embargo_days,
        seed=seed,
        repeat=repeat,
        variants=[spec.name for spec in variants],
        data_source=(
            f"imported candles {imported.source} ({imported.symbol} "
            f"{imported.timeframe}, venue={imported.venue or 'unstated'}); "
            f"holdout {len(holdout_candles)} bars"
        ),
        config={"initial_cash": initial_cash, "quote_currency": quote_currency},
        criteria={"metric": DEFAULT_METRIC},
    )
    # The provenance the plan requires: a recorded-only run must never imply it
    # re-ran the model, and the source series must be identifiable.
    manifest["kind"] = "replay_of_recording"
    manifest["replay"] = {
        "model_rerun": False,
        "note": (
            "proposal sources are the recorded cycles plus the real confirmation "
            "rule; no agent/LLM was re-run, so no uplift of an unexecuted knob is "
            "claimed by this run"
        ),
        "candles": {
            "source": imported.source, "symbol": imported.symbol,
            "timeframe": imported.timeframe, "venue": imported.venue,
            "count": len(imported.candles),
            "first_open_ms": imported.candles[0].open_time,
            "last_close_ms": imported.candles[-1].close_time,
        },
        "holdout_candles": len(holdout_candles),
        "purged_rows": len(split.purged_rows),
        "risk_limits": {
            "source": "runtime config (mirrors DerivativesBridge._build_risk_guard)",
            "max_derivatives_exposure_pct": risk_limits.max_derivatives_exposure_pct,
            "max_derivatives_leverage": risk_limits.max_derivatives_leverage,
            "research_override": derivatives_exposure_pct is not None,
        },
    }
    if manifest_note is not None:
        manifest["replay"]["note_blocking"] = manifest_note

    summary = run_experiment(
        results_dir=results_dir, manifest=manifest, split=split,
        variants=variants, candles_holdout=holdout_candles,
        risk_limits=risk_limits,
        initial_cash=initial_cash, quote_currency=quote_currency,
    )
    summary["candle_coverage"] = {
        "imported": len(imported.candles),
        "holdout": len(holdout_candles),
        "purged": len(split.purged_rows),
        "first_holdout_open_ms": min(holdout_opens),
        "last_holdout_open_ms": max(holdout_opens),
    }
    # run_experiment already persisted the summary; carry the coverage into the
    # stored copy so the artifact and the CLI print agree. The digest comes from
    # the LOCKED manifest — a run_experiment re-write would otherwise emit the
    # pre-lock dict with a null digest.
    directory = artifacts.run_dir(results_dir or _default_results_dir(), run_id)
    summary["manifest_digest"] = artifacts.load_manifest(directory).get("manifest_digest")
    artifacts.write_summary(directory, summary)
    if register_run:
        summary["registry"] = _register_run(directory)
    return summary


def _register_run(directory: Path) -> dict[str, Any]:
    """Publish the run into the experiment registry so the API/brief can read it.

    Off by default: the registry is PostgreSQL state, so a run only registers
    when the operator asks. It REFUSES to report success when the database is
    unreachable — a silent failure here would leave ``GET /api/experiments``
    and the brief's comparison section permanently ``unavailable`` while the
    CLI claimed the run was recorded.
    """
    from server import experiment_store

    manifest = artifacts.load_manifest(directory)
    try:
        run_id = experiment_store.register_run(manifest, artifact_dir=str(directory))
        rows = artifacts.load_results(directory)
        counts = experiment_store.record_results(run_id, rows)
        stored = artifacts.load_summary(directory)
        for comparison in stored.get("comparisons") or []:
            if isinstance(comparison, dict):
                experiment_store.record_comparison(run_id, comparison)
    except Exception as exc:  # noqa: BLE001 - surface the real reason, never a fake success
        raise CandleImportError(
            f"the run was written to {directory} but the experiment registry could not "
            f"be updated ({type(exc).__name__}: {exc}); set TRADINGAGENTS_DATABASE_URL to a "
            "reachable database or drop --register-run"
        ) from exc
    return {"registered": True, "run_id": run_id, "results": counts}


def _default_results_dir() -> Path:
    from tradingagents.default_config import DEFAULT_CONFIG

    return Path(DEFAULT_CONFIG.get("results_dir") or Path.home() / ".tradingagents" / "logs")


def runtime_risk_limits(config: dict[str, Any] | None = None) -> RiskLimits:
    """The RiskLimits the live derivatives executor builds from config.

    Mirrors ``DerivativesBridge._build_risk_guard`` field for field so a
    simulation is judged by the SAME limits as the runtime, rather than the
    bare ``RiskLimits()`` default. That default sets
    ``max_derivatives_exposure_pct = 0``, which blocks every derivatives
    entry — a simulation built on it could never trade and its zero result
    would say nothing about the rule.

    This reads config only; it never writes config, env, or the engine
    settings store, so no runtime risk limit is changed by a research run.
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.execution.risk import RiskLimits

    source = dict(DEFAULT_CONFIG)
    if config:
        source.update(config)
    return RiskLimits(
        max_daily_loss_pct=float(source["risk_max_daily_loss_pct"]),
        max_position_pct_per_asset=float(source["risk_max_position_pct_per_asset"]),
        max_total_exposure_pct=float(source["risk_max_total_exposure_pct"]),
        max_consecutive_loss_count=int(source["risk_max_consecutive_loss_count"]),
        max_derivatives_leverage=float(source["risk_max_derivatives_leverage"]),
        max_derivatives_exposure_pct=float(source["risk_max_derivatives_exposure_pct"]),
        max_derivatives_exposure_hard_pct=float(source["risk_max_derivatives_exposure_hard_pct"]),
        risk_derivatives_override_min_r=float(source["risk_derivatives_override_min_r"]),
        risk_derivatives_override_max_risk_fraction=float(
            source["risk_derivatives_override_max_risk_fraction"]
        ),
        risk_derivatives_override_max_per_day=int(
            source["risk_derivatives_override_max_per_day"]
        ),
        max_open_heat_pct=float(source["risk_max_open_heat_pct"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.experiments.financial_experiment",
        description="Financial replay experiment over imported closed candles (offline, no LLM).",
    )
    parser.add_argument("--candles", required=True,
                        help="imported OHLCV file (JSON or CSV) of closed candles")
    parser.add_argument("--fixture", default=None,
                        help="recorded futures_cycles JSON (default: the shipped fixture)")
    parser.add_argument("--rule", default="baseline",
                        help="confirmation rule from server.futures_replay.RULES")
    parser.add_argument("--holdout-start", required=True,
                        help="holdout boundary date, YYYY-MM-DD (leakage is asserted)")
    parser.add_argument("--horizon-days", type=int, default=1)
    parser.add_argument("--embargo-days", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--initial-cash", type=float, default=10000.0)
    parser.add_argument("--quote-currency", default="USDT")
    parser.add_argument("--as-of", default=None,
                        help="ISO-8601 cutoff; drops candles not closed by then")
    parser.add_argument("--symbol", default=None, help="override the file's symbol")
    parser.add_argument("--timeframe", default=None, help="override the file's timeframe")
    parser.add_argument("--derivatives-exposure-pct", type=float, default=None,
                        help="research override for the derivatives notional cap; the runtime "
                             "default is 0, which blocks every derivatives entry. This changes "
                             "only this simulation, never runtime config")
    parser.add_argument("--register-run", action="store_true",
                        help="publish the run into the PostgreSQL experiment registry so "
                             "GET /api/experiments and the brief can read it; fails loudly "
                             "when the database is unreachable (default: off)")
    parser.add_argument("--results-dir", default=None,
                        help="artifact root (default TRADINGAGENTS_RESULTS_DIR, never the repo)")
    args = parser.parse_args(argv)
    try:
        summary = financial_replay(
            candles_path=Path(args.candles),
            fixture_path=Path(args.fixture) if args.fixture else None,
            rule_name=args.rule,
            holdout_start=args.holdout_start,
            horizon_days=args.horizon_days,
            embargo_days=args.embargo_days,
            seed=args.seed,
            repeat=args.repeat,
            initial_cash=args.initial_cash,
            quote_currency=args.quote_currency,
            as_of=args.as_of,
            results_dir=args.results_dir,
            symbol=args.symbol,
            timeframe=args.timeframe,
            derivatives_exposure_pct=args.derivatives_exposure_pct,
            register_run=args.register_run,
        )
    except (CandleImportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
