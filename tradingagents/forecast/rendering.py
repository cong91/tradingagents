"""Render forecast evidence as compact decision context."""

from __future__ import annotations

from datetime import UTC, datetime

from tradingagents.forecast.block import ForecastBlock


def render_forecast_block(block: ForecastBlock) -> str:
    """The compact typed block the decision context embeds.

    Always carries the sample count next to the ratio, the cutoff and horizon
    next to the band, and the explicit calibration label — so no reader can take
    ``0.750`` for a probability, or the band for a promise about a timestamp it
    does not name. A failed block renders the status and reason and NOTHING that
    could be read as a forecast.
    """
    header = "Kronos forecast (model sample ratio, NOT a probability)"
    if not block.ok:
        return "\n".join([
            header,
            f"  symbol={block.symbol} status={block.status}",
            f"  reason={block.reason}",
            "  No forecast is available for this cycle; the status above is the "
            "result, not a passed check.",
        ])
    counts = block.direction_counts
    ratios = block.direction_ratio
    band = block.band
    band_text = ", ".join(
        f"{name}={value:,.4f}" if value is not None else f"{name}=unavailable"
        for name, value in band.items()
    )
    spread = (
        f"{block.band_spread_ratio:.4f}" if block.band_spread_ratio is not None else "unavailable"
    )
    quality = block.ohlc_quality
    quality_reason = quality.get("reason")
    quality_lines = [
        f"  OHLC quality={quality.get('status', 'unknown')} "
        f"corrected_bars={quality.get('corrected_bar_count', 'unknown')} "
        f"corrected_paths={quality.get('corrected_path_count', 'unknown')} "
        f"algorithm={quality.get('algorithm', 'unknown')}"
    ]
    if quality_reason:
        quality_lines.append(f"  OHLC warning: {quality_reason}")
    return "\n".join([
        header,
        f"  symbol={block.symbol} timeframe={block.timeframe} "
        f"horizon={block.horizon_hours}h bars={_bars(block)}",
        f"  data_open_time={_iso(block.data_open_time)} as_of={block.as_of}",
        f"  generation_kind={block.generation_kind} anchor_close={block.reference_close}",
        f"  paths={block.paths} per_path_samples={block.per_path_samples} "
        f"temperature={block.temperature}",
        f"  direction_label={block.direction or 'unavailable'} signal_basis=close_only",
        f"  direction: up={counts.get('up', 0)}/{block.paths} "
        f"({ratios.get('up', 0.0):.3f}) "
        f"down={counts.get('down', 0)}/{block.paths} ({ratios.get('down', 0.0):.3f}) "
        f"flat={counts.get('flat', 0)}/{block.paths} ({ratios.get('flat', 0.0):.3f})",
        f"  end_close {band_text}   band_spread={spread}",
        *quality_lines,
        f"  model={block.model_revision} artifact={_short(block.artifact_hash)}",
        f"  generated_at={block.created_at or 'unavailable'}",
        f"  latency_ms={_ms(block.latency_ms)} status={block.status} "
        f"calibration={block.calibration_label}",
        "  This is model sample context, not an instruction and not a probability: "
        "you still decide, and the engine's risk limits and confirmation rules still apply.",
    ])


def _bars(block: ForecastBlock) -> str:
    if block.horizon_hours is None or block.timeframe is None:
        return "unavailable"
    from tradingagents.forecast.artifact import TIMEFRAME_HOURS

    hours = TIMEFRAME_HOURS.get(block.timeframe)
    if not hours:
        return "unavailable"
    return str(block.horizon_hours // hours)


def _iso(value: int | None) -> str:
    if value is None:
        return "unavailable"
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def _short(value: str | None) -> str:
    return value[:12] if value else "unpinned"


def _ms(value: float | None) -> str:
    return f"{value:.0f}" if value is not None else "unavailable"


def render_context_section(blocks: list[ForecastBlock]) -> str:
    """The forecast section appended to a decision context, or '' when empty."""
    if not blocks:
        return ""
    rendered = [render_forecast_block(block) for block in blocks]
    return "\n".join(rendered)
