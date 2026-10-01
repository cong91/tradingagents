"use client";

// Section "Nhật ký audit" — GET /api/audit (hợp đồng §6, server/audit.py).
// Chỉ-đọc: lọc theo ticker/hành động/giai đoạn/khoảng ngày, phân trang,
// banner halt trong ngày. Audit là dữ liệu phán xét → stale sau 10s.

import { useEffect, useMemo, useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
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
import { cn } from "cn";
import { DataMeta, EmptyState, ErrorState, SectionSkeleton } from "./section-states";
import { formatDateTime, formatPriceEst, formatQty } from "./display";
import { useSectionData } from "./use-section-data";
import type { AuditEvent, AuditPayload } from "./types";

const PAGE_SIZE = 25;
const STALE_SECONDS = 10;
const FIELD_CLASS =
  "h-9 w-full rounded-lg border border-input bg-background px-3 text-sm";

const ACTION_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "buy", label: "Mua (buy)" },
  { value: "sell", label: "Bán (sell)" },
  { value: "no_order", label: "Không đặt lệnh (no_order)" },
  { value: "rejected_by_risk", label: "RiskGuard từ chối (rejected_by_risk)" },
  { value: "rejected_at_execute", label: "Từ chối lúc thực thi (rejected_at_execute)" },
  { value: "execute_denied", label: "Thực thi bị chặn (execute_denied)" },
  { value: "awaiting_approval", label: "Chờ duyệt (awaiting_approval)" },
  { value: "approval_granted", label: "Đã duyệt (approval_granted)" },
  { value: "approval_denied", label: "Từ chối duyệt (approval_denied)" },
];

const PHASE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "plan", label: "Lập kế hoạch (plan)" },
  { value: "execute", label: "Thực thi (execute)" },
];

function actionLabel(action: string): string {
  return ACTION_OPTIONS.find((option) => option.value === action)?.label ?? action;
}

function actionVariant(
  action: string
): "default" | "secondary" | "destructive" | "outline" {
  switch (action) {
    case "buy":
    case "approval_granted":
      return "default";
    case "sell":
    case "awaiting_approval":
      return "secondary";
    case "rejected_by_risk":
    case "rejected_at_execute":
    case "execute_denied":
    case "approval_denied":
      return "destructive";
    default:
      return "outline";
  }
}

function ActionCell({ event }: { event: AuditEvent }) {
  return (
    <div className="flex flex-col gap-1">
      <Badge variant={actionVariant(event.action ?? "")}>
        {event.action ? actionLabel(event.action) : "Không có hành động"}
      </Badge>
      {event.phase && (
        <span className="text-[11px] text-muted-foreground">
          {PHASE_OPTIONS.find((option) => option.value === event.phase)?.label ??
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

function ModeCell({ event }: { event: AuditEvent }) {
  if (!event.mode) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="flex flex-col gap-0.5">
      <Badge variant={event.mode === "live" ? "destructive" : "secondary"}>
        {event.mode === "live" ? "LIVE — lệnh thật" : "Mô phỏng (dry)"}
      </Badge>
      {event.confirmed != null && (
        <span className="text-[11px] text-muted-foreground">
          {event.confirmed ? "Đã xác nhận" : "Chưa xác nhận"}
        </span>
      )}
    </div>
  );
}

function ControlLineRow({ event }: { event: AuditEvent }) {
  const isHalt = event.event === "halt";
  return (
    <tr className="border-b bg-amber-50/70 dark:bg-amber-950/40">
      <td colSpan={7} className="px-3 py-2">
        <div className="flex flex-wrap items-baseline gap-x-2 text-xs">
          <span className="font-semibold">
            {isHalt ? "HALT — dừng giao dịch trong ngày" : "Mở lại giao dịch (halt_reset)"}
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

function AuditRow({ event }: { event: AuditEvent }) {
  return (
    <tr className="border-b align-top hover:bg-muted/40">
      <td className="px-3 py-2 text-xs whitespace-nowrap">
        {formatDateTime(event.timestamp ?? null)}
      </td>
      <td className="px-3 py-2 font-medium">{event.ticker ?? "—"}</td>
      <td className="px-3 py-2">
        <ActionCell event={event} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {formatQty(event.qty ?? null, event.ticker)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {formatPriceEst(event.price_est ?? null)}
      </td>
      <td className="px-3 py-2">
        <ModeCell event={event} />
      </td>
      <td
        className="max-w-72 truncate px-3 py-2 text-xs text-muted-foreground"
        title={event.reason ?? undefined}
      >
        {event.reason ?? "—"}
      </td>
    </tr>
  );
}

export function AuditLogCard() {
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
        <CardTitle>Nhật ký audit</CardTitle>
        <CardDescription>
          {
            "GET /api/audit — nhật ký kiểm soát thực thi (JSONL, mới nhất trước): mọi quyết định RiskGuard, lệnh và hành động duyệt đều nằm ở đây."
          }
        </CardDescription>
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
              RiskGuard đã dừng giao dịch hôm nay
            </p>
            {data.halt_reason && (
              <p className="text-sm text-muted-foreground">{data.halt_reason}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Halt gần nhất: {formatDateTime(data.last_halt)}
              {data.last_halt_reset
                ? ` · Mở lại gần nhất: ${formatDateTime(data.last_halt_reset)}`
                : " · Chưa được mở lại"}
              . Việc mở lại halt chỉ thực hiện được trên máy chủ — cố ý không có
              nút reset trên UI.
            </p>
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-ticker" className="text-xs text-muted-foreground">
              Ticker
            </label>
            <input
              id="audit-ticker"
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value)}
              placeholder="VD: BTC-USD"
              className={cn(FIELD_CLASS, "mt-1")}
            />
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-action" className="text-xs text-muted-foreground">
              Hành động
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
              <option value="">Tất cả</option>
              {ACTION_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-phase" className="text-xs text-muted-foreground">
              Giai đoạn
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
              <option value="">Tất cả</option>
              {PHASE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="col-span-2 md:col-span-1">
            <label htmlFor="audit-from" className="text-xs text-muted-foreground">
              Từ ngày
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
              Đến ngày
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
            title="Nhật ký audit chưa có dòng nào"
            hint="File audit JSONL chưa được tạo — đây là dấu hiệu cài mới, không phải lỗi. Chạy pipeline hằng ngày hoặc phê duyệt một lệnh để tạo dòng audit đầu tiên."
            actionHref="/approvals"
            actionLabel="Đến hàng đợi duyệt lệnh (M4)"
          />
        )}
        {state.status === "ready" &&
          state.data.total_lines > 0 &&
          state.data.items.length === 0 && (
            <EmptyState
              title="Không có dòng nào khớp bộ lọc"
              hint={`Tổng cộng ${state.data.total_lines} dòng audit nhưng không dòng nào khớp điều kiện hiện tại. Thử nới lỏng bộ lọc hoặc xóa khoảng ngày.`}
            />
          )}
        {state.status === "ready" && state.data.items.length > 0 && (
          <>
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-sm">
                <caption className="sr-only">
                  Nhật ký audit thực thi — mới nhất trước
                </caption>
                <thead>
                  <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
                    <th scope="col" className="px-3 py-2 font-medium">Thời điểm</th>
                    <th scope="col" className="px-3 py-2 font-medium">Ticker</th>
                    <th scope="col" className="px-3 py-2 font-medium">Hành động</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Khối lượng</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Giá ước tính</th>
                    <th scope="col" className="px-3 py-2 font-medium">Chế độ</th>
                    <th scope="col" className="px-3 py-2 font-medium">Lý do</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.items.map((event) =>
                    event.event ? (
                      <ControlLineRow key={`ctrl-${event._line_no}`} event={event} />
                    ) : (
                      <AuditRow key={`ev-${event._line_no}`} event={event} />
                    )
                  )}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                {state.data.total_matching} dòng khớp / {state.data.total_lines} dòng
                tổng cộng
                {state.data.skipped_lines
                  ? ` · ${state.data.skipped_lines} dòng JSON hỏng đã bỏ qua`
                  : ""}
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
                  Trang trước
                </Button>
                <span className="text-xs tabular-nums text-muted-foreground">
                  Trang {state.data.page}/{totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="min-touch"
                  disabled={page >= totalPages}
                  onClick={() => setPage((current) => current + 1)}
                >
                  Trang sau
                  <ChevronRightIcon aria-hidden="true" />
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
