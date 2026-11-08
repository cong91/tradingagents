"use client";

// Section "Nhật ký audit" — GET /api/audit (hợp đồng §6, server/audit.py).
// Chỉ-đọc: lọc theo ticker/hành động/giai đoạn/khoảng ngày, phân trang,
// banner halt trong ngày. Audit là dữ liệu phán xét → stale sau 10s.
//
// Gộp nhóm hẹp theo order_id (client-side, trên items[] đã fetch): các dòng
// JSONL chia sẻ cùng order_id không rỗng (live execute + bracket + dòng
// follow-up) thành 1 hàng chính + bước con; dòng KHÔNG order_id (mọi dòng
// plan, dry execute, rejected_*, time_stop_*) giữ hàng đơn. Dòng bất thường
// (fill/bracket chưa xác nhận) mở sẵn kèm badge "Cần kiểm tra".

import { useEffect, useMemo, useState } from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from "lucide-react";
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
import { ReasonText } from "@/components/ui/reason-text";
import { cn } from "cn";
import { formatReason, formatSymbol } from "@/lib/format";
import { useT, type Messages } from "@/lib/i18n";
import { DataMeta, EmptyState, ErrorState, SectionSkeleton } from "./section-states";
import { formatDateTime, formatPriceEst, formatQty } from "./display";
import { useSectionData } from "./use-section-data";
import type { AuditEvent, AuditPayload } from "./types";

const PAGE_SIZE = 25;
const STALE_SECONDS = 10;
const FIELD_CLASS =
  "h-9 w-full rounded-lg border border-input bg-background px-3 text-sm";

// Dòng bất thường theo hợp đồng (bracket/fill chưa xác nhận, state vị thế
// lỗi…) — cluster chứa action này luôn mở sẵn để lý do đọc được ngay.
const ANOMALY_ACTIONS = new Set([
  "bracket_verification_required",
  "bracket_failed",
  "entry_uncertain",
  "entry_partial_or_unknown",
  "position_state_failed",
  "scale_out_refused",
]);

// Danh sách hành động/giai đoạn (giá trị enum backend + nhãn theo locale):
// lib/i18n/messages/audit.ts (log.actions / log.phases).
function actionLabel(t: Messages, action: string): string {
  return t.audit.log.actions.find((option) => option.value === action)?.label ?? action;
}

function actionVariant(
  action: string
): "default" | "secondary" | "destructive" | "outline" {
  switch (action) {
    case "buy":
    case "approval_granted":
      return "default";
    case "approval_denied":
      return "destructive";
    case "sell":
    case "awaiting_approval":
    case "bracket_placed":
      return "secondary";
    case "rejected_by_risk":
    case "rejected_at_execute":
    case "execute_denied":
    case "derivatives_denied":
    case "bracket_failed":
    case "bracket_verification_required":
    case "entry_uncertain":
    case "entry_partial_or_unknown":
    case "position_state_failed":
    case "scale_out_refused":
      return "destructive";
    default:
      return "outline";
  }
}

type ClusterGroup = {
  kind: "cluster";
  key: string;
  main: AuditEvent;
  steps: AuditEvent[];
  anomalous: boolean;
};

type AuditGroup =
  | { kind: "control"; key: string; event: AuditEvent }
  | { kind: "single"; key: string; event: AuditEvent; anomalous: boolean }
  | ClusterGroup;

/** Gộp hẹp theo order_id không rỗng; thứ tự dòng giữ nguyên theo lần đầu xuất hiện. */
function clusterItems(items: AuditEvent[]): AuditGroup[] {
  const groups: AuditGroup[] = [];
  const byOrder = new Map<string, ClusterGroup>();
  for (const event of items) {
    const key = String(event._line_no ?? event.timestamp ?? "");
    if (event.event) {
      groups.push({ kind: "control", key: `ctrl-${key}`, event });
      continue;
    }
    const orderId = event.order_id ?? "";
    if (!orderId) {
      groups.push({
        kind: "single",
        key: `ev-${key}`,
        event,
        anomalous: ANOMALY_ACTIONS.has(event.action ?? ""),
      });
      continue;
    }
    let cluster = byOrder.get(orderId);
    if (!cluster) {
      cluster = { kind: "cluster", key: `grp-${orderId}`, main: event, steps: [], anomalous: false };
      byOrder.set(orderId, cluster);
      groups.push(cluster);
    } else {
      cluster.steps.push(event);
    }
    if (ANOMALY_ACTIONS.has(event.action ?? "")) cluster.anomalous = true;
  }
  return groups;
}

function ActionCell({ event }: { event: AuditEvent }) {
  const t = useT();
  return (
    <div className="flex flex-col items-start gap-1">
      <Badge variant={actionVariant(event.action ?? "")}>
        {event.action ? actionLabel(t, event.action) : t.audit.log.noAction}
      </Badge>
      {event.phase && (
        <span className="text-[11px] text-muted-foreground">
          {t.audit.log.phases.find((option) => option.value === event.phase)?.label ??
            event.phase}
        </span>
      )}
      {event.order_id && (
        <span className="font-mono text-[11px] text-muted-foreground">
          #{event.order_id}
        </span>
      )}
    </div>
  );
}

function ModeBadges({ event }: { event: AuditEvent }) {
  const t = useT();
  if (!event.mode) return null;
  return (
    <div className="flex flex-col items-start gap-0.5">
      <Badge variant={event.mode === "live" ? "destructive" : "secondary"}>
        {event.mode === "live" ? t.audit.log.live : t.audit.log.dry}
      </Badge>
      {event.confirmed != null && (
        <span className="text-[11px] text-muted-foreground">
          {event.confirmed ? t.audit.log.confirmed : t.audit.log.unconfirmed}
        </span>
      )}
    </div>
  );
}

/** Ô ký hiệu hiển thị dạng gạch qua formatSymbol; nguyên văn giữ trong title. */
function TickerCell({ ticker }: { ticker: string | null | undefined }) {
  return (
    <td className="px-3 py-2 font-medium" title={ticker ?? undefined}>
      {ticker ? formatSymbol(ticker) : "—"}
    </td>
  );
}

/** Khối lượng + giá ước tính gộp một cột (bảng không cần kéo ngang ở 1440px). */
function QtyPriceCell({ event }: { event: AuditEvent }) {
  const qty = event.qty != null ? formatQty(event.qty, event.ticker) : null;
  const price = event.price_est != null ? formatPriceEst(event.price_est) : null;
  if (!qty && !price) {
    return <td className="px-3 py-2 text-right text-muted-foreground">—</td>;
  }
  return (
    <td className="px-3 py-2 text-right tabular-nums">
      {[qty, price].filter(Boolean).join(" · ")}
    </td>
  );
}

function ReasonCell({ event, empty }: { event: AuditEvent; empty: string }) {
  const t = useT();
  const card = t.approvals.card;
  return (
    <td
      className="px-3 py-2 text-xs text-muted-foreground"
      title={formatReason(event.reason) || undefined}
    >
      <ReasonText
        text={event.reason}
        expandLabel={card.reasonExpand}
        collapseLabel={card.reasonCollapse}
        unreadableLabel={card.reasonUnreadable}
        emptyLabel={empty}
      />
    </td>
  );
}

function stepLabel(t: Messages, event: AuditEvent): string {
  const g = t.audit.log.group;
  switch (event.action) {
    case "buy":
    case "sell":
      return g.stepExecute;
    case "bracket_placed":
      return g.stepBracket;
    case "bracket_failed":
      return g.stepBracketFailed;
    case "bracket_verification_required":
      return g.stepBracketVerify;
    case "entry_uncertain":
      return g.stepEntryUncertain;
    case "entry_partial_or_unknown":
      return g.stepEntryPartial;
    case "position_state_failed":
      return g.stepPositionState;
    case "scale_out_refused":
      return g.stepScaleOut;
    default:
      return event.action ? actionLabel(t, event.action) : t.audit.log.noAction;
  }
}

function ControlLineRow({ event }: { event: AuditEvent }) {
  const isHalt = event.event === "halt";
  const t = useT();
  return (
    <tr className="border-b bg-amber-50/70 dark:bg-amber-950/40">
      <td colSpan={5} className="px-3 py-2">
        <div className="flex flex-wrap items-baseline gap-x-2 text-xs">
          <span className="font-semibold">
            {isHalt ? t.audit.log.haltRow : t.audit.log.haltResetRow}
          </span>
          {event.reason && (
            <span className="text-muted-foreground">{event.reason}</span>
          )}
          <span className="text-muted-foreground">
            · {formatDateTime(event.timestamp ?? null)}
          </span>
        </div>
      </td>
    </tr>
  );
}

function SingleRow({ group }: { group: Extract<AuditGroup, { kind: "single" }> }) {
  const t = useT();
  const { event } = group;
  return (
    <tr
      className={cn(
        "border-b align-top hover:bg-muted/40",
        group.anomalous && "bg-amber-50/70 dark:bg-amber-950/40"
      )}
    >
      <td className="px-3 py-2 text-xs whitespace-nowrap">
        {formatDateTime(event.timestamp ?? null)}
      </td>
      <TickerCell ticker={event.ticker} />
      <td className="px-3 py-2">
        <div className="flex flex-col items-start gap-1">
          <ActionCell event={event} />
          {group.anomalous && (
            <Badge variant="destructive">{t.audit.log.group.attentionBadge}</Badge>
          )}
          <ModeBadges event={event} />
        </div>
      </td>
      <QtyPriceCell event={event} />
      <ReasonCell event={event} empty="—" />
    </tr>
  );
}

function ClusterRow({ group }: { group: ClusterGroup }) {
  const t = useT();
  const c = t.audit.log;
  const [open, setOpen] = useState(group.anomalous);
  // Refetch định kỳ có thể mang cluster bất thường mới về sau lần mount đầu:
  // key hàng không đổi nên React tái dùng instance — mở theo anomalous hiện tại.
  // Điều chỉnh state trong lúc render (react.dev/reference/react/useState#storing-
  // information-from-previous-renders): theo dõi anomalous trước đó, chỉ mở lại
  // ở cạnh false→true, không tự đóng và giữ lựa chọn bấm của người dùng.
  const [wasAnomalous, setWasAnomalous] = useState(group.anomalous);
  if (group.anomalous !== wasAnomalous) {
    setWasAnomalous(group.anomalous);
    if (group.anomalous) setOpen(true);
  }
  const { main, steps } = group;
  const Chevron = open ? ChevronDownIcon : ChevronRightIcon;
  const mainAction = main.action ?? steps.find((step) => step.action)?.action ?? "";
  const firstReason = [main, ...steps].map((step) => step.reason).find(Boolean) ?? null;
  return (
    <>
      <tr
        className={cn(
          "border-b align-top hover:bg-muted/40",
          group.anomalous && "bg-amber-50/70 dark:bg-amber-950/40"
        )}
      >
        <td className="px-3 py-2 text-xs whitespace-nowrap">
          {formatDateTime(main.timestamp ?? null)}
        </td>
        <TickerCell ticker={main.ticker} />
        <td className="px-3 py-2">
          <div className="flex flex-col items-start gap-1">
            <button
              type="button"
              onClick={() => setOpen((current) => !current)}
              aria-expanded={open}
              className="-mx-1 flex items-center gap-1 rounded-md px-1 hover:bg-sidebar-accent/60"
            >
              <Chevron className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              <Badge variant={actionVariant(mainAction)}>
                {mainAction ? actionLabel(t, mainAction) : c.noAction}
              </Badge>
            </button>
            {group.anomalous && (
              <Badge variant="destructive">{c.group.attentionBadge}</Badge>
            )}
            <ModeBadges event={main} />
          </div>
        </td>
        <QtyPriceCell event={main} />
        <ReasonCell event={{ ...main, reason: firstReason }} empty="—" />
      </tr>
      {open && (
        <tr className="border-b bg-muted/30">
          <td colSpan={5} className="px-3 pb-2">
            <ul className="space-y-1.5 text-xs">
              {steps.map((step) => (
                <li key={String(step._line_no ?? step.timestamp)} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="whitespace-nowrap text-muted-foreground">
                    {formatDateTime(step.timestamp ?? null)}
                  </span>
                  <Badge variant={actionVariant(step.action ?? "")}>
                    {stepLabel(t, step)}
                  </Badge>
                  {step.reason && (
                    <ReasonText
                      text={step.reason}
                      expandLabel={t.approvals.card.reasonExpand}
                      collapseLabel={t.approvals.card.reasonCollapse}
                      unreadableLabel={t.approvals.card.reasonUnreadable}
                      className="text-muted-foreground"
                    />
                  )}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

/** Bảng audit thuần (props items) — phần fetch/lọc nằm ở AuditLogCard. */
export function AuditLogTable({ items }: { items: AuditEvent[] }) {
  const t = useT();
  const c = t.audit.log;
  const groups = useMemo(() => clusterItems(items), [items]);
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">{c.group.groupCaption}</p>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <caption className="sr-only">{c.caption}</caption>
          <thead>
            <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
              <th scope="col" className="px-3 py-2 font-medium">{c.colTime}</th>
              <th scope="col" className="px-3 py-2 font-medium">{c.colTicker}</th>
              <th scope="col" className="px-3 py-2 font-medium">{c.colAction}</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">{c.colQtyPrice}</th>
              <th scope="col" className="px-3 py-2 font-medium">{c.colReason}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) =>
              group.kind === "control" ? (
                <ControlLineRow key={group.key} event={group.event} />
              ) : group.kind === "single" ? (
                <SingleRow key={group.key} group={group} />
              ) : (
                <ClusterRow key={group.key} group={group} />
              )
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function AuditLogCard() {
  const t = useT();
  const c = t.audit.log;
  const [tickerInput, setTickerInput] = useState("");
  const [ticker, setTicker] = useState("");
  const [action, setAction] = useState("");
  const [phase, setPhase] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(1);

  // Gõ ticker → debounce 300ms trước khi bắn request; filter mới → về trang 1.
  useEffect(() => {
    const id = setTimeout(() => {
      setTicker(tickerInput.trim().toUpperCase());
      setPage(1);
    }, 300);
    return () => clearTimeout(id);
  }, [tickerInput]);

  const url = useMemo(() => {
    const query = new URLSearchParams();
    if (ticker) query.set("ticker", ticker);
    if (action) query.set("action", action);
    if (phase) query.set("phase", phase);
    if (fromDate) query.set("from", fromDate);
    if (toDate) query.set("to", toDate);
    query.set("page", String(page));
    query.set("page_size", String(PAGE_SIZE));
    return `/api/audit?${query.toString()}`;
  }, [ticker, action, phase, fromDate, toDate, page]);

  const { state, refresh, refreshing } = useSectionData<AuditPayload>(url);
  const data = state.status === "ready" ? state.data : null;

  const totalPages = data
    ? Math.max(1, Math.ceil(data.total_matching / PAGE_SIZE))
    : 1;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{c.title}</CardTitle>
        <CardDescription>{c.description}</CardDescription>
        <CardAction>
          <DataMeta
            generatedAt={data?.generated_at ?? null}
            staleAfterSeconds={STALE_SECONDS}
            onRefresh={refresh}
            refreshing={refreshing}
          />
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {data?.halted_today && (
          <div
            role="alert"
            className="flex flex-col gap-1 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3"
          >
            <p className="text-sm font-semibold text-destructive">
              {c.haltedTitle}
            </p>
            {data.halt_reason && (
              <p className="text-sm text-muted-foreground">{data.halt_reason}</p>
            )}
            <p className="text-xs text-muted-foreground">
              {c.lastHalt(formatDateTime(data.last_halt))}
              {data.last_halt_reset
                ? c.lastReset(formatDateTime(data.last_halt_reset))
                : c.notReset}
              {c.haltNote}
            </p>
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-ticker" className="text-xs text-muted-foreground">
              {c.ticker}
            </label>
            <input
              id="audit-ticker"
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value)}
              placeholder={c.tickerPlaceholder}
              className={cn(FIELD_CLASS, "mt-1")}
            />
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-action" className="text-xs text-muted-foreground">
              {c.action}
            </label>
            <select
              id="audit-action"
              value={action}
              onChange={(e) => {
                setAction(e.target.value);
                setPage(1);
              }}
              className={cn(FIELD_CLASS, "mt-1")}
            >
              <option value="">{c.all}</option>
              {c.actions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-phase" className="text-xs text-muted-foreground">
              {c.phase}
            </label>
            <select
              id="audit-phase"
              value={phase}
              onChange={(e) => {
                setPhase(e.target.value);
                setPage(1);
              }}
              className={cn(FIELD_CLASS, "mt-1")}
            >
              <option value="">{c.all}</option>
              {c.phases.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-from" className="text-xs text-muted-foreground">
              {c.from}
            </label>
            <input
              id="audit-from"
              type="date"
              value={fromDate}
              onChange={(e) => {
                setFromDate(e.target.value);
                setPage(1);
              }}
              className={cn(FIELD_CLASS, "mt-1")}
            />
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-to" className="text-xs text-muted-foreground">
              {c.to}
            </label>
            <input
              id="audit-to"
              type="date"
              value={toDate}
              onChange={(e) => {
                setToDate(e.target.value);
                setPage(1);
              }}
              className={cn(FIELD_CLASS, "mt-1")}
            />
          </div>
        </div>

        {state.status === "loading" && <SectionSkeleton rows={4} />}
        {state.status === "error" && (
          <ErrorState message={state.error.message} onRetry={refresh} />
        )}
        {state.status === "ready" && state.data.total_lines === 0 && (
          <EmptyState
            title={c.emptyTitle}
            hint={c.emptyHint}
            actionHref="/cycles"
            actionLabel={c.emptyAction}
          />
        )}
        {state.status === "ready" &&
          state.data.total_lines > 0 &&
          state.data.items.length === 0 && (
            <EmptyState
              title={c.noMatchTitle}
              hint={c.noMatchHint(state.data.total_lines)}
            />
          )}
        {state.status === "ready" && state.data.items.length > 0 && (
          <AuditLogTable items={state.data.items} />
        )}

        {state.status === "ready" && state.data.total_lines > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              {c.matching(state.data.total_matching, state.data.total_lines)}
              {state.data.skipped_lines ? c.skipped(state.data.skipped_lines) : ""}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                className="min-touch"
                disabled={page <= 1}
                onClick={() => setPage((current) => current - 1)}
              >
                <ChevronLeftIcon aria-hidden="true" />
                {c.prev}
              </Button>
              <span className="text-xs tabular-nums text-muted-foreground">
                {c.page(state.data.page, totalPages)}
              </span>
              <Button
                variant="outline"
                size="sm"
                className="min-touch"
                disabled={page >= totalPages}
                onClick={() => setPage((current) => current + 1)}
              >
                {c.next}
                <ChevronRightIcon aria-hidden="true" />
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
