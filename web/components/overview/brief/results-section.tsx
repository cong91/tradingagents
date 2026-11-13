"use client";

import {
  formatDateTime,
  formatPercent,
  formatSignedCurrency,
  pnlTone,
} from "@/lib/format";
import { useT } from "@/lib/i18n";

import { BriefSection, ScalarRow } from "./brief-section";
import {
  asCount,
  asNumber,
  inScope,
  type BriefLedger,
  type BriefPayload,
  type Scope,
} from "./contract";

function num(value: unknown): number | null {
  return asNumber(value);
}

function toneOf(value: number | null): "profit" | "loss" | null {
  if (value === null) return null;
  const tone = pnlTone(value);
  return tone === "neutral" ? null : tone;
}

export function ClosureEvidenceBlock({ ledger }: { ledger: BriefLedger }) {
  const r = useT().brief.results;
  const positions = ledger.positions;
  const evidence = positions?.closure_evidence ?? null;
  const evidenceStatus = evidence?.status ?? "unavailable";
  const statusNote =
    evidenceStatus === "complete"
      ? r.closureEvidenceComplete
      : evidenceStatus === "partial"
        ? r.closureEvidencePartial
        : r.closureEvidenceUnavailable;
  const recordedPositionsLabel =
    evidenceStatus === "complete" ? r.recordedPositions : r.recordedPositionsUnverified;
  const recordedClosed = positions
    ? asCount(positions.recorded_closed ?? positions.closed)
    : null;
  const verifiedClosed = asCount(evidence?.verified_closed);
  const coherentVerifiedCount =
    evidenceStatus !== "unavailable" &&
    verifiedClosed !== null &&
    verifiedClosed > 0 &&
    (recordedClosed === null || verifiedClosed <= recordedClosed) &&
    (positions === null || positions === undefined || verifiedClosed <= positions.total);
  const hasClosureProof = evidenceStatus === "complete" || coherentVerifiedCount;
  const firstOpened = positions?.first_opened
    ? formatDateTime(positions.first_opened)
    : null;
  const lastVerifiedClosed =
    hasClosureProof && positions?.last_closed
      ? formatDateTime(positions.last_closed)
      : null;
  const lastRecordedClosed =
    positions?.last_recorded_closed &&
    positions.last_recorded_closed !== positions.last_closed
      ? formatDateTime(positions.last_recorded_closed)
      : !lastVerifiedClosed && positions?.last_closed
        ? formatDateTime(positions.last_closed)
        : null;
  return (
    <div
      className="space-y-1.5"
      data-brief-block="closure-evidence"
      data-closure-evidence-status={evidenceStatus}
    >
      <p className="text-xs font-medium">{r.closureEvidenceTitle}</p>
      <p className="text-xs text-muted-foreground">{statusNote}</p>
      <dl className="space-y-1.5">
        <ScalarRow
          label={recordedPositionsLabel}
          value={positions ? String(positions.total) : null}
          unknownLabel={r.notEnoughData}
        />
        {recordedClosed !== null ? (
          <ScalarRow
            label={
              evidenceStatus === "complete"
                ? r.recordedClosureTimestampsComplete
                : r.recordedClosureTimestamps
            }
            value={String(recordedClosed)}
            unknownLabel={r.notEnoughData}
          />
        ) : null}
        {verifiedClosed !== null ? (
          <ScalarRow
            label={r.verifiedClosures}
            value={String(verifiedClosed)}
            unknownLabel={r.notEnoughData}
          />
        ) : null}
        {evidence?.unverified_positions != null ? (
          <ScalarRow
            label={r.positionsWithoutClosureProof}
            value={String(evidence.unverified_positions)}
            unknownLabel={r.notEnoughData}
          />
        ) : null}
        {evidence?.unverified_closed_timestamps != null ? (
          <ScalarRow
            label={r.timestampsWithoutClosureProof}
            value={String(evidence.unverified_closed_timestamps)}
            unknownLabel={r.notEnoughData}
          />
        ) : null}
      </dl>
      {firstOpened || lastVerifiedClosed || lastRecordedClosed ? (
        <p className="text-xs text-muted-foreground">
          {firstOpened ? `${r.firstRecordedOpen}: ${firstOpened}` : ""}
          {firstOpened && (lastVerifiedClosed || lastRecordedClosed) ? " · " : ""}
          {lastVerifiedClosed ? `${r.lastVerifiedClosure}: ${lastVerifiedClosed}` : ""}
          {lastVerifiedClosed && lastRecordedClosed ? " · " : ""}
          {lastRecordedClosed
            ? `${r.lastRecordedClosure}: ${lastRecordedClosed}`
            : ""}
        </p>
      ) : null}
      {evidenceStatus !== "complete" ? (
        <p className="text-xs text-muted-foreground">{r.closureCountCaveat}</p>
      ) : null}
      {evidence?.reason ? (
        <p className="break-words text-xs text-muted-foreground">{evidence.reason}</p>
      ) : null}
    </div>
  );
}

function ledgerMoney(amount: string | null | undefined, currency: string | null): string {
  if (amount == null) return "—";
  return currency ? `${amount} ${currency}` : amount;
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
  const totals = ledger.totals;
  const netRows = totals?.net_by_currency ?? [];

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
                // JSON object keys serialize a missing currency as "null".
                const key = row.currency ?? "null";
                const realized = totals?.realized_by_currency?.[key];
                const fee = totals?.fee_by_currency?.[key];
                const funding = totals?.funding_by_currency?.[key];
                const negative = row.amount?.startsWith("-") ?? false;
                const positive = row.amount != null && !negative && /[1-9]/.test(row.amount);
                return (
                  <tr key={row.currency === null ? "unknown-currency" : `currency:${row.currency}`} data-net-currency={key}>
                    <td className="py-1.5 pr-3 font-mono">{row.currency ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {ledgerMoney(realized, row.currency)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {ledgerMoney(fee, row.currency)}
                      {!row.fee_known ? <span className="block text-xs">{r.feeUnknown}</span> : null}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {funding ? (
                        <>
                          <span className="block">{r.fundingReceived}: {ledgerMoney(funding.received, row.currency)}</span>
                          <span className="block">{r.fundingPaid}: {ledgerMoney(funding.paid, row.currency)}</span>
                        </>
                      ) : "—"}
                      {!row.funding_known ? <span className="block text-xs">{r.fundingUnknown}</span> : null}
                    </td>
                    <td
                      className={`py-1.5 text-right font-medium tabular-nums ${
                        positive ? "text-profit" : negative ? "text-loss" : ""
                      }`}
                    >
                      {row.amount === null ? r.notEnoughData : `${positive ? "+" : ""}${ledgerMoney(row.amount, row.currency)}`}
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
          {r.fundingUnknown}: {ledger.funding_unknown_currencies.map((currency) => currency ?? r.currencyUnknown).join(", ")}
        </p>
      ) : null}
      <p className="text-xs text-muted-foreground">{r.ledgerNote}</p>
      <CoverageBlock data={data} />
    </div>
  );
}

function FundingObservations({ data }: { data: BriefPayload }) {
  const r = useT().brief.results;
  const observations = data.results.ledger.funding_observations;
  if (!observations?.events.length) return null;
  return (
    <div className="space-y-1.5" data-brief-block="funding-observations">
      <p className="text-xs font-medium">{r.fundingObservations}</p>
      <p className="text-xs text-muted-foreground">{r.fundingObservationsNote}</p>
      <ul className="space-y-1 text-xs">
        {observations.events.map((event) => (
          <li key={`${event.account_id}:${event.symbol}:${event.venue_income_id}`} className="break-words">
            {formatDateTime(event.funding_at)} · {event.account_id} · {event.venue_key} · {event.account_type} · {event.symbol} · {ledgerMoney(event.amount, event.currency)}
          </li>
        ))}
      </ul>
      {observations.truncated ? <p className="text-xs text-muted-foreground">{r.fundingObservationsTruncated}</p> : null}
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

export function ResultsSection({ data, scope }: { data: BriefPayload; scope: Scope }) {
  const t = useT();
  const r = t.brief.results;
  const estimated = data.results.estimated;
  const showSpot = inScope(scope, "spot");
  const showFutures = inScope(scope, "futures");

  return (
    <BriefSection title={r.title}>
      <div className="space-y-3">
        <VerifiedBlock data={data} />
        <ClosureEvidenceBlock ledger={data.results.ledger} />
        <FundingObservations data={data} />
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
