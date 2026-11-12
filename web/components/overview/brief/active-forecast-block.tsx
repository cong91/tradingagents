"use client";

/**
 * The ACTIVE Kronos forecast section. Kept separate from the operating brief
 * orchestration so forecast-specific rendering stays one responsibility.
 *
 * Forecast numbers are model sample ratios, not probabilities. OHLC quality is
 * descriptive only: direction remains close-only, and unknown quality never
 * becomes a claim that candles were corrected or usable for risk levels.
 */

import {
  formatDateTime,
  formatMoney,
  formatNumber,
} from "@/lib/format";
import { useT } from "@/lib/i18n";

import { BriefDetails, BriefSection, ScalarRow } from "./brief-section";
import {
  asNumber,
  inScope,
  type BriefActiveForecast,
  type BriefPayload,
  type OhlcQuality,
  type Scope,
} from "./contract";

type ActiveForecastBlockProps = {
  data: BriefPayload;
  scope: Scope;
};

function num(value: unknown): number | null {
  return asNumber(value);
}

function barsOf(forecast: BriefActiveForecast["forecast"]): string | null {
  const hours = forecast?.horizon_hours;
  const timeframe = forecast?.timeframe ?? null;
  const step = timeframe ? /^(\d+)h$/.exec(timeframe) : null;
  if (hours == null || !step) return null;
  return String(Math.floor(hours / Number(step[1])));
}

function iso(epochMs: unknown): string | null {
  const value = num(epochMs);
  if (value === null) return null;
  return formatDateTime(new Date(value).toISOString());
}

function directionText(forecast: BriefActiveForecast["forecast"]): string | null {
  const counts = forecast?.direction_counts ?? null;
  const ratios = forecast?.direction_ratio ?? null;
  const paths = num(forecast?.paths);
  if (!counts || Object.keys(counts).length === 0) return null;
  const denominator = paths !== null ? String(paths) : null;
  return (["up", "down", "flat"] as const)
    .map((key) => {
      const count = num(counts[key]);
      const ratio = ratios ? num(ratios[key]) : null;
      if (count === null) return null;
      const head = denominator ? `${key} ${count}/${denominator}` : `${key} ${count}`;
      return ratio !== null ? `${head} (${formatNumber(ratio, { maximumFractionDigits: 3 })})` : head;
    })
    .filter((part): part is string => part !== null)
    .join(" · ") || null;
}

function ohlcStatusText(quality: OhlcQuality | null, labels: ReturnType<typeof useT>["brief"]["activeForecast"]): string | null {
  if (!quality) return null;
  if (quality.status === "consistent") return labels.ohlcQuality.consistent;
  if (quality.status === "corrected") return labels.ohlcQuality.corrected;
  return labels.ohlcQuality.unknown;
}

function correctionMethodText(
  quality: OhlcQuality | null,
  labels: ReturnType<typeof useT>["brief"]["activeForecast"]
): string | null {
  if (!quality) return null;
  if (quality.status === "consistent") return labels.ohlcQuality.rawMethod;
  if (quality.status !== "corrected") return null;
  if (quality.algorithm === "ohlc_wick_bounds_v1" && quality.version === 1) {
    return labels.ohlcQuality.correctedMethod;
  }
  return labels.ohlcQuality.unknown;
}

function changedFieldsText(
  quality: OhlcQuality | null,
  labels: ReturnType<typeof useT>["brief"]["activeForecast"]
): string | null {
  const fields = quality?.changed_field_counts;
  if (!fields) return null;
  const high = num(fields.highs);
  const low = num(fields.lows);
  if (high === null && low === null) return null;
  return labels.ohlcQuality.changedFields(
    high === null ? "—" : String(high),
    low === null ? "—" : String(low)
  );
}

function absoluteDeltaText(
  quality: OhlcQuality | null,
  currency: string | null,
  labels: ReturnType<typeof useT>["brief"]["activeForecast"]
): string | null {
  const deltas = quality?.absolute_delta_by_field;
  if (!deltas) return null;
  const value = (field: "high" | "low") => {
    const total = num(deltas[field === "high" ? "highs" : "lows"]?.total);
    const max = num(deltas[field === "high" ? "highs" : "lows"]?.max);
    if (total === null && max === null) return null;
    return labels.ohlcQuality.deltaField(
      field,
      total === null ? "—" : formatMoney(total, currency),
      max === null ? "—" : formatMoney(max, currency)
    );
  };
  const parts = [value("high"), value("low")].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/**
 * The active Kronos forecast of the most recent recorded cycle. Compact by
 * default; the numbers live behind a native <details>. A failed block carries
 * no numbers, so nothing is printed that could be read as a forecast, and no
 * LLM-call saving or profit is claimed.
 */
export function ActiveForecastBlock({ data, scope }: ActiveForecastBlockProps) {
  const t = useT();
  const a = t.brief.activeForecast;
  const block = data.active_forecast;
  const forecast = block.forecast ?? null;

  // The active forecast covers the derivatives path only; in Spot scope this
  // block says so instead of pretending to cover Spot.
  if (!inScope(scope, "futures")) {
    return (
      <BriefSection title={a.title}>
        <p className="text-sm text-muted-foreground">{t.brief.scope.futuresOnly}</p>
      </BriefSection>
    );
  }

  const stateLine = (): string | null => {
    switch (block.state) {
      case "empty": return a.empty;
      case "error": return a.readFailed;
      case "unavailable": return a.unavailable;
      case "stale": return a.stale;
      case "ok": return a.ok;
      default: return null;
    }
  };

  // Parsed once, so the render guards stay narrowable by TypeScript.
  const horizonHours = num(forecast?.horizon_hours);
  const spread = num(forecast?.band_spread_ratio);
  const latencyMs = num(forecast?.latency_ms);
  const quality = forecast?.ohlc_quality ?? null;

  return (
    <BriefSection title={a.title} note={block.note ?? null}>
      <div data-brief-block="active-forecast" data-active-forecast-state={block.state}>
        {block.state === "empty" || block.state === "error" ? (
          <div className="space-y-1" data-active-forecast-nothing={block.state}>
            <p className="text-sm text-muted-foreground">{stateLine()}</p>
            {block.reason ? (
              <p className="break-words text-xs text-muted-foreground">{a.reason(block.reason)}</p>
            ) : null}
          </div>
        ) : (
          <div className="space-y-1.5">
            <p className="text-sm text-muted-foreground">{stateLine()}</p>
            <dl className="space-y-1.5">
              <ScalarRow
                label={a.symbolLabel}
                value={forecast?.symbol ?? block.symbol ?? null}
                unknownLabel={t.brief.states.unavailable}
              />
              <div data-active-forecast-policy>
                <ScalarRow
                  label={a.policyLabel}
                  value={
                    block.policy
                      ? a.policy[block.policy] ?? block.policy
                      : null
                  }
                  unknownLabel={t.brief.states.unavailable}
                />
              </div>
            </dl>
            {block.defer_reason ? (
              <p className="break-words text-xs text-muted-foreground">{a.deferReason(block.defer_reason)}</p>
            ) : null}
            {forecast && block.state !== "unavailable" && forecast.status === "ok" ? (
              <BriefDetails summary={a.detailSummary}>
                <dl className="space-y-1.5">
                  <ScalarRow
                    label={a.signalBasisLabel}
                    value={forecast.signal_basis === "close_only" ? a.signalBasisCloseOnly : null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.directionLabel}
                    value={directionText(forecast)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.horizonTitle}
                    value={horizonHours === null
                      ? null
                      : a.horizonLabel(String(horizonHours), barsOf(forecast) ?? "—")}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.pathsTitle}
                    value={forecast.paths === null || forecast.paths === undefined
                      ? null
                      : a.pathsLabel(String(forecast.paths), String(num(forecast.per_path_samples) ?? "—"))}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.cutoffLabel}
                    value={forecast.as_of ? formatDateTime(forecast.as_of) : null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.dataOpenLabel}
                    value={iso(forecast.data_open_time)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.generatedLabel}
                    value={forecast.created_at ? formatDateTime(forecast.created_at) : null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.recordedLabel}
                    value={block.recorded_at ? formatDateTime(block.recorded_at) : null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={forecast.band ? a.bandTitle(block.quote_currency ?? null) : null}
                    value={
                      forecast.band
                        ? (["low", "p50", "high"] as const)
                            .map((key) => {
                              const value = num(forecast.band?.[key]);
                              return value === null
                                ? null
                                : `${key} ${formatMoney(value, block.quote_currency ?? null)}`;
                            })
                            .filter((part): part is string => part !== null)
                            .join(" · ") || null
                        : null
                    }
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.spreadTitle}
                    value={spread === null
                      ? null
                      : formatNumber(spread, { maximumFractionDigits: 4 })}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.latencyTitle}
                    value={latencyMs === null
                      ? null
                      : `${formatNumber(latencyMs, { maximumFractionDigits: 0 })} ms`}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.ohlcQualityTitle}
                    value={ohlcStatusText(quality, a)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.ohlcMethodTitle}
                    value={correctionMethodText(quality, a)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.correctedBarsTitle}
                    value={quality?.corrected_bar_count == null ? null : String(quality.corrected_bar_count)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.correctedPathsTitle}
                    value={quality?.corrected_path_count == null ? null : String(quality.corrected_path_count)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.changedFieldsTitle}
                    value={changedFieldsText(quality, a)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.absoluteDeltaTitle}
                    value={absoluteDeltaText(quality, block.quote_currency ?? null, a)}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.modelTitle}
                    value={forecast.model_revision ?? null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                </dl>
                <p className="text-xs text-muted-foreground">{a.signalBasisNote}</p>
                {quality?.reason ? (
                  <p className="break-words text-xs text-muted-foreground">
                    {a.ohlcReason(quality.reason)}
                  </p>
                ) : null}
              </BriefDetails>
            ) : (
              <div className="space-y-1" data-active-forecast-failed>
                {block.reason || forecast?.reason ? (
                  <p className="break-words text-xs text-muted-foreground">
                    {a.reason(block.reason ?? forecast?.reason ?? "")}
                  </p>
                ) : null}
                <p className="text-xs text-muted-foreground">{a.noNumbers}</p>
              </div>
            )}
          </div>
        )}
      </div>
    </BriefSection>
  );
}
