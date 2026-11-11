"use client";

// The operating brief card (plan §5.1, UX plan §4). One read-only aggregate on
// top of the dashboard: what needs handling, the reconciled result, the open
// risk and what the bot is doing today.
//
// Truthfulness rules this component must keep visible (each is asserted by the
// offline browser test):
//   * a null figure renders "not enough data" — never 0;
//   * reconciled and estimated money live in separately labelled blocks;
//   * coverage rows keep their own window and are never summed;
//   * "no signal" and "pipeline error" are two different rows;
//   * the only action is refresh + links away: no control here changes state.

import { LoaderCircleIcon, TriangleAlertIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatSignedCurrency,
  pnlTone,
} from "@/lib/format";
import { useT } from "@/lib/i18n";
import type { Messages } from "@/lib/i18n";
import type { PanelState } from "@/components/overview/use-panel";

import { AttentionList } from "./attention-list";
import { BriefDetails, BriefSection, ScalarRow } from "./brief-section";
import { ScopeFilter } from "./scope-filter";
import {
  asCount,
  asNumber,
  inScope,
  type BriefPayload,
  type BriefShadow,
  type Scope,
} from "./contract";

function num(value: unknown): number | null {
  return asNumber(value);
}

function pctText(value: number | null): string {
  // Plain number WITHOUT the sign: the i18n string owns the "%" so it is
  // appended exactly once (passing formatPercent here produced "18.4%%").
  return value === null ? "" : formatNumber(value, { maximumFractionDigits: 1 });
}

function toneOf(value: number | null): "profit" | "loss" | null {
  if (value === null) return null;
  const tone = pnlTone(value);
  return tone === "neutral" ? null : tone;
}

// ---------------------------------------------------------------------------
// status


function StatusBlock({ data }: { data: BriefPayload }) {
  const t = useT();
  const s = t.brief.status;
  const status = data.status;
  const modeLabel = (value: boolean | null): string =>
    value === null ? s.unknown : value ? s.on : s.off;
  const venue =
    status.venue_profile === "demo"
      ? s.venueDemo
      : status.venue_profile === "production"
        ? s.venueProduction
        : null;
  const server =
    status.server_status === "ok"
      ? s.serverOk
      : status.server_status === "degraded"
        ? s.serverDegraded
        : null;

  return (
    <BriefSection title={s.title}>
      <dl className="space-y-1.5">
        <ScalarRow
          label={s.mode}
          value={s.execMode[status.exec_mode] ?? status.exec_mode}
          unknownLabel={t.brief.states.unavailable}
        />
        <div data-brief-row="auto_spot">
          <ScalarRow label={s.autoSpot} value={modeLabel(status.auto_spot)} unknownLabel={s.unknown} />
        </div>
        <div data-brief-row="auto_futures">
          <ScalarRow
            label={s.autoFutures}
            value={modeLabel(status.auto_futures)}
            unknownLabel={s.unknown}
          />
        </div>
        <div data-brief-row="venue">
          <ScalarRow label={s.venue} value={venue} unknownLabel={s.venueUnknown} />
        </div>
        <div data-brief-row="halted">
          <ScalarRow
            label={s.halted}
            // Yes/No, not the label again: "Order placement halted | Halted today"
            // repeated the same words and read as one broken sentence (B3).
            value={
              status.halted_today === null
                ? null
                : status.halted_today
                  ? s.haltedYes
                  : s.haltedNo
            }
            unknownLabel={s.unknown}
            tone={status.halted_today === true ? "loss" : null}
          />
        </div>
        <ScalarRow
          label={s.pendingApprovals}
          value={status.pending_approvals === null ? null : String(status.pending_approvals)}
          unknownLabel={s.unknown}
          tone={status.pending_approvals ? "loss" : null}
        />
        <ScalarRow label={s.serverStatus} value={server} unknownLabel={s.unknown} />
      </dl>
      {status.halted_today === true && status.halt_reason ? (
        <p className="text-xs text-muted-foreground">{s.haltedReason(status.halt_reason)}</p>
      ) : null}
    </BriefSection>
  );
}

// ---------------------------------------------------------------------------
// results


function CoverageBlock({ data }: { data: BriefPayload }) {
  const t = useT();
  const r = t.brief.results;
  const coverage = data.results.ledger.coverage ?? [];
  if (coverage.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium">{r.coverageTitle}</p>
      <ul className="space-y-1.5">
        {coverage.map((row, index) => {
          const status = r.coverageStatus[row.status] ?? row.status;
          const drift = num(row.realized_diff_ratio);
          return (
            <li
              key={`${row.account_id ?? "row"}-${index}`}
              data-coverage-status={row.status}
              className="rounded-lg border border-dashed px-3 py-2 text-xs"
            >
              <p className="font-medium">
                {status}
                {row.window_start && row.window_end
                  ? ` · ${r.coverageWindow(
                      formatDateTime(row.window_start),
                      formatDateTime(row.window_end)
                    )}`
                  : ""}
              </p>
              <p className="text-muted-foreground">
                {r.fillsFound(String(asCount(row.fills_found) ?? "—"))}
                {drift === null
                  ? ` · ${r.driftUnknown}`
                  : ` · ${r.driftRatio(formatPercent(drift * 100))}`}
              </p>
              {(row.warnings ?? []).map((warning) => (
                <p key={warning} className="break-words text-muted-foreground">
                  {warning}
                </p>
              ))}
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-muted-foreground">{r.coverageNote}</p>
    </div>
  );
}

function VerifiedBlock({ data }: { data: BriefPayload }) {
  const t = useT();
  const r = t.brief.results;
  const ledger = data.results.ledger;
  const netRows = ledger.totals?.net_by_currency ?? [];

  // Only the NET TABLE depends on rows existing. Coverage evidence and
  // manual/unlinked fills must render whatever the totals say: an operator
  // told "reconciliation failed" needs the row that says why, and a coverage
  // error can arrive with no net rows at all. Returning early here hid both
  // (blocking finding B1).
  return (
    <div className="space-y-2" data-brief-block="verified">
      <p className="text-xs font-medium">{r.verifiedTitle}</p>
      {ledger.status === "unavailable" || netRows.length === 0 ? (
        <div className="space-y-1">
          <p className="text-sm text-muted-foreground">{r.verifiedNone}</p>
          {ledger.reason ? (
            <p className="break-words text-xs text-muted-foreground">{ledger.reason}</p>
          ) : null}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{r.verifiedCaption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-1.5 pr-3 font-medium">{r.currency}</th>
                <th scope="col" className="py-1.5 pr-3 text-right font-medium">{r.realized}</th>
                <th scope="col" className="py-1.5 pr-3 text-right font-medium">{r.fees}</th>
                <th scope="col" className="py-1.5 pr-3 text-right font-medium">{r.funding}</th>
                <th scope="col" className="py-1.5 text-right font-medium">{r.net}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {netRows.map((row) => {
                const net = num(row.net);
                return (
                  <tr key={row.currency ?? "unknown-currency"} data-net-currency={row.currency ?? "null"}>
                    <td className="py-1.5 pr-3 font-mono">{row.currency ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {num(row.realized) === null ? "—" : formatMoney(num(row.realized), row.currency)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {/* Fees are folded into net only when the venue semantics allow it. */}
                      {row.fee_known === false
                        ? r.feeUnknown
                        : num(row.fees) === null
                          ? "—"
                        : formatMoney(num(row.fees), row.currency)}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">
                    {row.funding_known === false ? r.fundingUnknown : formatMoney(num(row.funding), row.currency)}
                  </td>
                  <td
                    className={`py-1.5 text-right font-medium tabular-nums ${
                      toneOf(net) === "profit" ? "text-profit" : toneOf(net) === "loss" ? "text-loss" : ""
                    }`}
                  >
                    {net === null ? r.notEnoughData : formatSignedCurrency(net, row.currency)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>
      )}
      {ledger.funding_unknown_currencies?.length ? (
        <p className="text-xs text-muted-foreground">
          {r.fundingUnknown}: {ledger.funding_unknown_currencies.join(", ")}
        </p>
      ) : null}
      <p className="text-xs text-muted-foreground">{r.ledgerNote}</p>
      <CoverageBlock data={data} />
    </div>
  );
}

function UnattributedBlock({ data }: { data: BriefPayload }) {
  const t = useT();
  const r = t.brief.results;
  const rows = data.results.unattributed_manual;
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1" data-brief-block="unattributed">
      <p className="text-xs font-medium">{r.unattributedTitle}</p>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        {rows.map((row, index) => {
          const window =
            row.window_start && row.window_end
              ? `${formatDateTime(row.window_start)} → ${formatDateTime(row.window_end)}`
              : "—";
          return (
            <li key={`${row.account_id ?? "row"}-${index}`}>
              {r.unattributedRow(window, String(asCount(row.count) ?? "—"))}
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-muted-foreground">{r.unattributedNote}</p>
    </div>
  );
}

function ResultsBlock({ data, scope }: { data: BriefPayload; scope: Scope }) {
  const t = useT();
  const r = t.brief.results;
  const estimated = data.results.estimated;
  const showSpot = inScope(scope, "spot");
  const showFutures = inScope(scope, "futures");

  return (
    <BriefSection title={r.title}>
      <div className="space-y-3">
        <VerifiedBlock data={data} />
        <UnattributedBlock data={data} />
        <div className="space-y-1.5" data-brief-block="estimated">
          <p className="text-xs font-medium">{r.estimatedTitle}</p>
          <dl className="space-y-1.5">
            {showSpot ? (
              <div data-brief-row="spot_estimated">
                <ScalarRow
                  label={r.spotAuditTotal}
                  value={
                    estimated.spot_audit_realized_total === null
                      ? null
                      : formatSignedCurrency(
                          estimated.spot_audit_realized_total,
                          estimated.spot_currency
                        )
                  }
                  unknownLabel={r.notEnoughData}
                  tone={toneOf(estimated.spot_audit_realized_total)}
                />
              </div>
            ) : null}
            {showSpot ? (
              <div data-brief-row="spot_today">
                <ScalarRow
                  label={r.spotAuditToday}
                  value={
                    estimated.spot_audit_realized_today === null
                      ? null
                      : formatSignedCurrency(
                          estimated.spot_audit_realized_today,
                          estimated.spot_currency
                        )
                  }
                  unknownLabel={r.notEnoughData}
                  tone={toneOf(estimated.spot_audit_realized_today)}
                />
              </div>
            ) : null}
            {showFutures ? (
              <div data-brief-row="futures_unrealized">
                <ScalarRow
                  label={r.futuresUnrealized}
                  value={
                    estimated.futures_unrealized === null
                      ? null
                      : formatSignedCurrency(
                          estimated.futures_unrealized,
                          estimated.futures_unrealized_currency
                        )
                  }
                  unknownLabel={r.notEnoughData}
                  tone={toneOf(estimated.futures_unrealized)}
                />
              </div>
            ) : null}
          </dl>
          <p className="text-xs text-muted-foreground">{r.estimatedNote}</p>
        </div>
      </div>
    </BriefSection>
  );
}

// ---------------------------------------------------------------------------
// risk


function RiskBlock({ data, scope }: { data: BriefPayload; scope: Scope }) {
  const t = useT();
  const r = t.brief.risk;
  const risk = data.risk;
  const showSpot = inScope(scope, "spot");
  const showFutures = inScope(scope, "futures");
  const exposure = risk.exposure.value_pct;
  const limits = risk.limits;

  const limitText = (key: string): string | null => {
    const value = num(limits[key]);
    return value === null ? null : r.limitValue(formatNumber(value, { maximumFractionDigits: 0 }));
  };

  return (
    <BriefSection title={r.title}>
      <dl className="space-y-1.5">
        {showSpot ? (
          <div data-brief-row="open_spot">
            <ScalarRow
              label={`${r.openPositions} · ${r.spot}`}
              value={risk.open_positions.spot === null ? null : String(risk.open_positions.spot)}
              unknownLabel={r.unknown}
            />
          </div>
        ) : null}
        {showFutures ? (
          <div data-brief-row="open_futures">
            <ScalarRow
              label={`${r.openPositions} · ${r.futures}`}
              value={
                risk.open_positions.futures === null
                  ? null
                  : String(risk.open_positions.futures)
              }
              unknownLabel={r.unknown}
            />
          </div>
        ) : null}
        {showFutures ? (
          <div data-brief-row="exposure">
            <ScalarRow
              label={r.exposure}
              value={exposure === null ? null : r.exposureOf(pctText(exposure))}
              unknownLabel={r.unknown}
              reason={risk.exposure.reason}
            />
          </div>
        ) : null}
        {showFutures ? (
          <div data-brief-row="heat">
            {/* Open heat is never claimed: risk-at-stop is not in the snapshot. */}
            <ScalarRow
              label={r.heat}
              value={null}
              unknownLabel={r.unknown}
              reason={risk.heat.reason}
            />
          </div>
        ) : null}
      </dl>

      {risk.positions_read ? (
        <p className="text-xs text-muted-foreground">
          {r.positionsRead}:{" "}
          {r.positionsReadStatus[risk.positions_read.status] ?? risk.positions_read.status}
          {risk.positions_read.observed_at
            ? ` · ${r.observedAt(formatDateTime(risk.positions_read.observed_at))}`
            : ""}
        </p>
      ) : null}

      <div className="space-y-1.5" data-brief-block="protection">
        <p className="text-xs font-medium">{r.protectionTitle}</p>
        <p className="text-xs text-muted-foreground">{r.protectionScope}</p>
        <dl className="space-y-1.5">
          <ScalarRow
            label={r.protectionConfiguredLabel}
            value={
              risk.protection.configured === null
                ? null
                : r.protectionCount(risk.protection.configured)
            }
            unknownLabel={r.unknown}
          />
          <ScalarRow
            label={r.protectionMissingLabel}
            value={
              risk.protection.missing === null
                ? null
                : r.protectionCount(risk.protection.missing)
            }
            unknownLabel={r.unknown}
            tone={risk.protection.missing ? "loss" : null}
          />
          <ScalarRow
            label={r.protectionUnknownLabel}
            value={
              risk.protection.unknown === null
                ? null
                : r.protectionCount(risk.protection.unknown)
            }
            unknownLabel={r.unknown}
          />
        </dl>
        <p className="text-xs text-muted-foreground">{risk.protection.reason}</p>
      </div>

      <BriefDetails summary={r.limitsTitle}>
        <dl className="space-y-1.5">
          <ScalarRow label={r.limitTotalExposure} value={limitText("total_exposure_pct")} unknownLabel={r.unknown} />
          <ScalarRow
            label={r.limitDerivativesSoft}
            value={limitText("derivatives_exposure_soft_pct")}
            unknownLabel={r.unknown}
          />
          <ScalarRow
            label={r.limitDerivativesHard}
            value={limitText("derivatives_exposure_hard_pct")}
            unknownLabel={r.unknown}
          />
          <ScalarRow label={r.limitOpenHeat} value={limitText("open_heat_pct")} unknownLabel={r.unknown} />
        </dl>
        <p className="text-xs text-muted-foreground">{String(limits.note ?? "")}</p>
      </BriefDetails>
    </BriefSection>
  );
}

// ---------------------------------------------------------------------------
// activity


/** The truthful line for each kind of "nothing recorded yet". */
function shadowStateCopy(t: Messages, shadow: BriefShadow): string {
  const a = t.brief.activity;
  switch (shadow.state) {
    case "disabled":
      return a.shadowDisabled;
    case "error":
      return a.shadowReadFailed;
    case "empty":
      return a.shadowEmpty;
    default:
      // An older payload without `state` falls back to the neutral line rather
      // than claiming the worker is off.
      return a.shadowUnavailable;
  }
}


function ActivityBlock({ data, scope }: { data: BriefPayload; scope: Scope }) {
  const t = useT();
  const a = t.brief.activity;
  const activity = data.activity;

  // Cycles are the derivatives path: in Spot scope this block says so instead
  // of pretending to cover Spot.
  if (!inScope(scope, "futures")) {
    return (
      <BriefSection title={a.title}>
        <p className="text-sm text-muted-foreground">{t.brief.scope.futuresOnly}</p>
      </BriefSection>
    );
  }

  const reasons = activity.no_entry_top_reasons ?? [];
  return (
    <BriefSection title={a.title} note={activity.note}>
      <p className="text-xs text-muted-foreground">{a.window}</p>
      <dl className="space-y-1.5">
        <div data-brief-row="cycles_today">
          <ScalarRow
            label={a.cycles}
            value={activity.cycles_today === null ? null : String(activity.cycles_today)}
            unknownLabel={t.brief.states.unavailable}
          />
        </div>
        <div data-brief-row="analysed_today">
          <ScalarRow
            label={a.analysed}
            value={activity.analysed_today === null ? null : String(activity.analysed_today)}
            unknownLabel={t.brief.states.unavailable}
          />
        </div>
        <div data-brief-row="entries_today">
          <ScalarRow
            label={a.entries}
            value={activity.entries_today === null ? null : String(activity.entries_today)}
            unknownLabel={t.brief.states.unavailable}
          />
        </div>
        <div data-brief-row="pipeline_errors">
          <ScalarRow
            label={a.pipelineErrors}
            value={
              activity.pipeline_error_cycles === null
                ? null
                : String(activity.pipeline_error_cycles)
            }
            unknownLabel={t.brief.states.unavailable}
            tone={activity.pipeline_error_cycles ? "loss" : null}
          />
        </div>
      </dl>
      <p className="text-xs text-muted-foreground">{a.pipelineErrorsNote}</p>

      <div className="space-y-1" data-brief-block="no_entry_reasons">
        <p className="text-xs font-medium">{a.noEntryTitle}</p>
        {reasons.length === 0 ? (
          <p className="text-sm text-muted-foreground">{a.noEntryNone}</p>
        ) : (
          <ul className="space-y-1">
            {reasons.map((row) => (
              <li
                key={`${row.code}-${row.reason ?? ""}`}
                data-reason-code={row.code}
                className="rounded-lg border border-dashed px-3 py-2 text-xs"
              >
                <p className="font-medium">
                  {a.reasonCodes[row.code] ?? row.code} · {a.reasonCount(String(row.count))}
                </p>
                {row.reason ? (
                  <p className="break-words text-muted-foreground">{row.reason}</p>
                ) : null}
                {row.sample_cycle_id ? (
                  <p className="font-mono text-muted-foreground">{row.sample_cycle_id}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-1" data-brief-block="shadow">
        <p className="text-xs font-medium">{a.shadowTitle}</p>
        {activity.shadow.status === "ok" ? (
          <ShadowDetail shadow={activity.shadow} />
        ) : (
          // One sentence per state. The server names which kind of "nothing"
          // this is, so an operator with the worker enabled is never told it is
          // disabled (which is what one shared line used to say).
          <div data-shadow-state={activity.shadow.state ?? "empty"}>
            <p className="text-sm text-muted-foreground">{shadowStateCopy(t, activity.shadow)}</p>
            {activity.shadow.reason ? (
              <p className="text-xs text-muted-foreground">{activity.shadow.reason}</p>
            ) : null}
          </div>
        )}
        {activity.shadow.kronos ? (
          <p className="text-xs text-muted-foreground" data-shadow-kronos={activity.shadow.kronos.available}>
            {activity.shadow.kronos.available ? a.shadowKronosReady : a.shadowKronosAbsent}
            {activity.shadow.kronos.reason ? (
              <span className="block">{activity.shadow.kronos.reason}</span>
            ) : null}
          </p>
        ) : null}
      </div>
    </BriefSection>
  );
}

/**
 * Shadow observations, compact: per-provider counts plus the newest rows.
 *
 * The ratio the model reports is a SAMPLE ratio over N paths, so it is printed
 * as "2/3" beside its denominator and the calibration note — never as a bare
 * percentage. A deterministic rule shows no ratio at all: it has no
 * distribution, and printing one would invent it.
 */
function ShadowDetail({ shadow }: { shadow: BriefShadow }) {
  const t = useT();
  const a = t.brief.activity;
  const providers = shadow.providers_detail ?? [];
  const observations = shadow.observations ?? [];
  return (
    <div className="space-y-1.5 text-xs" data-brief-block="shadow-detail">
      {providers.map((block) => (
        <div
          key={block.provider}
          data-shadow-provider={block.provider}
          className="rounded-lg border border-dashed px-2 py-1.5"
        >
          <p className="font-mono">{block.provider}</p>
          <p className="text-muted-foreground">
            {a.shadowAttempts(String(block.attempts.ok), String(block.attempts.rows))}
            {block.attempts.error + block.attempts.timeout + block.attempts.unavailable > 0
              ? ` · ${a.shadowFailed}: ${block.attempts.error + block.attempts.timeout + block.attempts.unavailable}`
              : ""}
          </p>
          {block.distribution_kind === "model_samples" && block.attempts.ok > 0 ? (
            <p className="font-mono text-muted-foreground">
              {a.shadowVerdicts}: {block.verdicts.up}/{block.attempts.ok} ·{" "}
              {block.verdicts.down}/{block.attempts.ok} · {block.verdicts.flat}/{block.attempts.ok}
            </p>
          ) : null}
          {block.distribution_kind === "deterministic_rule" ? (
            <p className="text-muted-foreground">{a.shadowRuleNote}</p>
          ) : null}
        </div>
      ))}
      {observations.length > 0 ? (
        <ul className="space-y-1">
          {observations.map((row, index) => (
            <li key={`${row.symbol}-${row.provider}-${index}`} className="text-muted-foreground">
              <span className="font-mono">{row.symbol}</span> · {row.timeframe} ·{" "}
              {row.horizon_hours}h ·{" "}
              {a.shadowLatest(row.symbol, row.direction ?? row.status)}
              {row.generation_kind === "retrospective" ? ` (${a.shadowRetrospective})` : ""}
            </li>
          ))}
        </ul>
      ) : null}
      <p className="text-muted-foreground">{a.shadowRatioNote}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// active kronos forecast


/**
 * The ACTIVE Kronos forecast of the most recent recorded cycle — its own
 * section, deliberately separate from the shadow block above (shadow rows are
 * recorded OUTSIDE the decision path and can never stand in for what a live
 * cycle actually saw). Compact by default; the numbers live behind a native
 * <details>. A failed block carries NO numbers, so nothing is printed that
 * could be read as a forecast, and no LLM-call saving or profit is claimed.
 */
function ActiveForecastBlock({ data, scope }: { data: BriefPayload; scope: Scope }) {
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
      case "off": return a.off;
      case "empty": return a.empty;
      case "error": return a.readFailed;
      case "unavailable": return a.unavailable;
      case "stale": return a.stale;
      case "ok": return a.ok;
      default: return null;
    }
  };

  const barsOf = (): string | null => {
    const hours = forecast?.horizon_hours;
    const timeframe = forecast?.timeframe ?? null;
    const step = timeframe ? /^(\d+)h$/.exec(timeframe) : null;
    if (hours == null || !step) return null;
    return String(Math.floor(hours / Number(step[1])));
  };

  const iso = (epochMs: unknown): string | null => {
    const value = num(epochMs);
    if (value === null) return null;
    return formatDateTime(new Date(value).toISOString());
  };

  const directionText = (): string | null => {
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
  };

  // Parsed once, so the render guards stay narrowable by TypeScript.
  const horizonHours = num(forecast?.horizon_hours);
  const spread = num(forecast?.band_spread_ratio);
  const latencyMs = num(forecast?.latency_ms);

  return (
    <BriefSection title={a.title} note={block.note ?? null}>
      <div data-brief-block="active-forecast" data-active-forecast-state={block.state}>
        {block.state === "off" || block.state === "empty" || block.state === "error" ? (
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
                    label={a.directionLabel}
                    value={directionText()}
                    unknownLabel={t.brief.states.unavailable}
                  />
                  <ScalarRow
                    label={a.horizonTitle}
                    value={horizonHours === null
                      ? null
                      : a.horizonLabel(String(horizonHours), barsOf() ?? "—")}
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
                    label={a.modelTitle}
                    value={forecast.model_revision ?? null}
                    unknownLabel={t.brief.states.unavailable}
                  />
                </dl>
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

// ---------------------------------------------------------------------------
// experiments


function ExperimentsBlock({ data }: { data: BriefPayload }) {
  const t = useT();
  const e = t.brief.experiments;
  const experiments = data.experiments;
  return (
    <BriefSection title={e.title}>
      {experiments.status === "unavailable" ? (
        <p className="text-sm text-muted-foreground">
          {e.empty}
          <span className="block text-xs">{experiments.reason}</span>
        </p>
      ) : (
        <div className="space-y-1 text-xs">
          {experiments.latest ? (
            <>
              <p className="font-mono">{e.latest(experiments.latest.run_id)}</p>
              {experiments.latest.hypothesis ? (
                <p className="text-muted-foreground">{e.hypothesis(experiments.latest.hypothesis)}</p>
              ) : null}
            </>
          ) : (
            <p className="text-muted-foreground">{e.empty}</p>
          )}
          {Object.keys(experiments.verdicts ?? {}).length > 0 ? (
            <p className="text-muted-foreground">
              {e.verdicts(
                Object.entries(experiments.verdicts)
                  .map(([verdict, count]) => `${verdict}: ${count}`)
                  .join(" · ")
              )}
            </p>
          ) : null}
          <p className="text-muted-foreground">{experiments.note}</p>
        </div>
      )}
    </BriefSection>
  );
}

// ---------------------------------------------------------------------------
// card


export function BriefCard({
  state,
  scope,
  onScopeChange,
  onRetry,
}: {
  state: PanelState<BriefPayload>;
  scope: Scope;
  onScopeChange: (next: Scope) => void;
  onRetry: () => void;
}) {
  const t = useT();

  return (
    <Card data-brief-state={state.status}>
      <CardHeader>
        <CardTitle as="h2">{t.brief.title}</CardTitle>
        <CardDescription>{t.brief.description}</CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            <Badge variant="outline">{t.brief.readOnlyBadge}</Badge>
            {/* No per-card refresh in the healthy state: the page header owns
                the one primary refresh and the brief auto-polls. A failing read
                gets its own retry below — that is the recovery affordance. */}
          </div>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        <ScopeFilter scope={scope} onChange={onScopeChange} />

        {state.status === "loading" ? (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
            {t.brief.states.loading}
          </p>
        ) : state.status === "error" ? (
          <div role="alert" className="space-y-2 rounded-lg border border-dashed px-4 py-4">
            <p className="text-sm font-medium text-destructive">
              {t.brief.states.cannotRead(state.code)}
            </p>
            <p className="text-sm text-muted-foreground">{state.message}</p>
            <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
              {t.common.retry}
            </Button>
          </div>
        ) : (
          <>
            {state.status === "stale" ? (
              <p role="alert" className="flex items-start gap-2 text-sm text-destructive">
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                {t.brief.states.staleNote(state.code, state.message)}
              </p>
            ) : null}

            <AttentionList items={state.data.needs_attention} />

            <div className="grid gap-4 lg:grid-cols-2">
              <StatusBlock data={state.data} />
              <ResultsBlock data={state.data} scope={scope} />
              <RiskBlock data={state.data} scope={scope} />
              <ActivityBlock data={state.data} scope={scope} />
              {/* The ACTIVE forecast is its own section — it never merges into
                  the shadow rows above, which are observation-only. */}
              <ActiveForecastBlock data={state.data} scope={scope} />
            </div>

            <ExperimentsBlock data={state.data} />

            <p className="text-xs text-muted-foreground">
              {t.brief.generatedAt(formatDateTime(state.data.generated_at))} ·{" "}
              {state.data.risk.positions_read?.observed_at
                ? `${t.brief.dataAtExchange(
                    formatDateTime(state.data.risk.positions_read.observed_at)
                  )} · `
                : ""}
              {t.brief.staleAfter(state.data.stale_after)} · {state.data.disclaimer}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
