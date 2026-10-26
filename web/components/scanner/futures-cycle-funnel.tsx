"use client";

// Phễu kết quả mọi chu kỳ autoloop + bảng theo mã. Click một mã để mở rộng xem
// từng chu kỳ của mã đó và lý do tiếng Việt cho việc bị loại / vào lệnh
// (GET /api/autoloop/cycles?symbol=…).

import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { scannerApi } from "./scanner-api";
import { formatTimestamp } from "./scanner-format";
import type {
  FuturesCycle,
  FuturesCyclePayload,
  FuturesCycleSymbolRollup,
  FuturesCycleSummary,
} from "./scanner-types";

const FUNNEL_STEPS = [
  {
    key: "market_rejected",
    label: "Bị loại ở bước quét thị trường",
    hint: "Chưa có setup, hoặc mã không có hợp đồng futures USDT",
  },
  {
    key: "confirmation_rejected",
    label: "Bị loại ở bước xác nhận nến",
    hint: "Nến đóng 1h/4h chưa cùng hướng với tín hiệu",
  },
  {
    key: "proposal_rejected",
    label: "Bị loại ở bước đề xuất vào lệnh",
    hint: "Agent phân tích xong nhưng không đề xuất vào lệnh",
  },
  {
    key: "entered",
    label: "Triển khai được",
    hint: "Đủ điều kiện vào lệnh và đã thực thi",
  },
] as const;

function signalLabel(signal: string | null | undefined): string {
  switch (signal) {
    case "Buy":
      return "Mua";
    case "Sell":
      return "Bán";
    case "Underweight":
      return "Thiên vị bán";
    case "Overweight":
      return "Thiên vị mua";
    case "Hold":
      return "Giữ";
    default:
      return signal?.trim() || "Chưa có dữ liệu";
  }
}

function marketRejectionLabel(reason: string | null | undefined): string {
  if (!reason) return "Chưa có dữ liệu";
  if (/no active USDT-settled futures contract/i.test(reason)) {
    return "Không có hợp đồng futures USDT trên sàn (chỉ có spot) — không thể thực thi";
  }
  return reason;
}

function confirmationLabel(reason: string | null | undefined): string {
  switch (reason) {
    case "doji_candle":
      return "Nến doji — chưa có hướng rõ ràng";
    case "direction_mismatch":
      return "Nến đóng ngược hướng tín hiệu";
    case "confirmation_missing":
    case null:
    case undefined:
    case "":
      return "Nến đóng 1h/4h chưa cùng hướng tín hiệu";
    default:
      return reason;
  }
}

function proposalLabel(reason: string | null | undefined): string {
  if (!reason) return "Chưa có dữ liệu";
  if (reason.startsWith("agent_proposal_missing")) {
    return "Agent không đề xuất điều kiện vào lệnh (stop / đòn bẩy / ngân sách rủi ro)";
  }
  if (reason.startsWith("agent_proposal_rejected")) {
    const detail = reason.slice("agent_proposal_rejected:".length).trim();
    return `Đề xuất bị từ chối: ${detail || "vượt trần rủi ro"}`;
  }
  switch (reason) {
    case "agent_abstained":
      return "Agent phân tích xong nhưng không đề xuất vào lệnh — bằng chứng chưa đủ mạnh";
    case "position_management_only":
      return "Chỉ quản lý vị thế đang mở, không vào lệnh mới";
    case "structured_terms_unavailable":
      return "Agent không đưa ra đủ điều kiện vào lệnh";
    default:
      return reason;
  }
}

function phaseLabel(phase: string): string {
  switch (phase) {
    case "market":
      return "Quét thị trường";
    case "confirmation":
      return "Xác nhận nến";
    case "proposal":
      return "Đề xuất vào lệnh";
    case "entry":
      return "Vào lệnh";
    case "protection":
      return "Bảo vệ vị thế";
    default:
      return phase;
  }
}

function cycleOutcome(cycle: FuturesCycle): string {
  const payload: FuturesCyclePayload = cycle.payload ?? {};
  if (cycle.status === "completed" && cycle.phase === "entry") {
    return "Triển khai được — đã vào lệnh";
  }
  if (cycle.phase === "market") return marketRejectionLabel(payload.market_rejection_reason);
  if (cycle.phase === "confirmation") return confirmationLabel(payload.confirmation_reason);
  if (cycle.phase === "proposal") {
    if (payload.signal_reason === "tradeable_signal") return "Tín hiệu tốt nhưng chưa qua bước xác nhận";
    return proposalLabel(payload.signal_reason);
  }
  if (payload.plan_reason) return payload.plan_reason;
  return payload.signal_reason ?? payload.reason ?? "Chưa có dữ liệu";
}

/** Một chu kỳ chỉ ghi lý do của bước nó dừng lại, nên lý do mới nhất là
 *  trường last_* duy nhất khác null trên dòng rollup. */
function latestOutcome(row: FuturesCycleSymbolRollup): string {
  if (row.last_market_rejection_reason) return marketRejectionLabel(row.last_market_rejection_reason);
  if (row.last_confirmation_reason) return confirmationLabel(row.last_confirmation_reason);
  if (row.last_signal_reason === "tradeable_signal") return "Triển khai được — đã vào lệnh";
  if (row.last_signal_reason) return proposalLabel(row.last_signal_reason);
  return "Chưa có dữ liệu";
}

function breakdownParts(row: FuturesCycleSymbolRollup): string[] {
  const parts: string[] = [];
  if (row.entered) parts.push(`${row.entered} vào lệnh`);
  if (row.proposal_rejected) parts.push(`${row.proposal_rejected} agent từ chối`);
  if (row.confirmation_rejected) parts.push(`${row.confirmation_rejected} nến chưa xác nhận`);
  if (row.market_rejected) parts.push(`${row.market_rejected} loại từ bước quét`);
  if (row.in_flight) parts.push(`${row.in_flight} đang chạy`);
  if (row.interrupted) parts.push(`${row.interrupted} gián đoạn`);
  return parts;
}

/** Khối trích nguyên văn quyết định của Quản lý danh mục cho một chu kỳ:
 * Rating, luận điểm và Futures Execution Intent — lý do thật đằng sau
 *  việc từ chối / vào lệnh, đúng từng chữ agent đã viết. */
export function AgentDecisionNote({ note }: { note: string | null | undefined }) {
  if (!note?.trim()) return null;
  return (
    <div className="mt-1.5 rounded-md border-l-2 border-muted-foreground/40 bg-muted/40 px-3 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        Quản lý danh mục · nguyên văn
      </p>
      <p className="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed">
        {note}
      </p>
    </div>
  );
}

function SymbolRow({ row }: { row: FuturesCycleSymbolRollup }) {
  const [open, setOpen] = useState(false);
  const [cycles, setCycles] = useState<FuturesCycle[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && cycles === null && !loading) {
      setLoading(true);
      scannerApi.futuresCycles(100, row.symbol).then(
        (body) => {
          setCycles(body.items);
          setError(null);
        },
        (err: unknown) => {
          setError(err instanceof Error ? err.message : "Không tải được chu kỳ của mã này");
        },
      ).finally(() => setLoading(false));
    }
  };

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <>
      <tr className="align-top">
        <td className="py-2 pr-3">
          <button
            type="button"
            onClick={toggle}
            aria-expanded={open}
            className="flex min-touch items-center gap-1.5 rounded-md px-1.5 py-1 text-left font-mono text-sm hover:bg-sidebar-accent/60"
          >
            <Chevron className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            {row.symbol}
          </button>
        </td>
        <td className="py-2 pr-3 tabular-nums">{row.total}</td>
        <td className="py-2 pr-3 text-xs">
          {breakdownParts(row).length > 0 ? (
            <span className="text-muted-foreground">{breakdownParts(row).join(" · ")}</span>
          ) : (
            <span className="text-muted-foreground">Chưa có dữ liệu</span>
          )}
        </td>
        <td className="py-2 pr-3">{signalLabel(row.last_signal)}</td>
        <td className="py-2 pr-3 text-xs">{latestOutcome(row)}</td>
        <td className="py-2 whitespace-nowrap text-xs text-muted-foreground">
          {formatTimestamp(row.last_updated)}
        </td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={6} className="pb-3">
            {loading ? (
              <p className="flex items-center gap-2 px-2 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                Đang tải chu kỳ của {row.symbol}…
              </p>
            ) : null}
            {error ? <p role="alert" className="px-2 text-xs text-destructive">{error}</p> : null}
            {cycles !== null && cycles.length === 0 && !error ? (
              <p className="px-2 text-xs text-muted-foreground">Không có chu kỳ nào được ghi nhận.</p>
            ) : null}
            {cycles !== null && cycles.length > 0 ? (
              <ul className="space-y-1.5 px-2">
                {cycles.map((cycle) => (
                  <li key={cycle.cycle_id} className="rounded-md border p-2 text-xs">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="whitespace-nowrap text-muted-foreground">
                        {formatTimestamp(cycle.created_at)}
                      </span>
                      <Badge variant="outline">{phaseLabel(cycle.phase)}</Badge>
                      <span className="font-medium">{signalLabel(cycle.payload?.signal)}</span>
                      <span className="text-muted-foreground">— {cycleOutcome(cycle)}</span>
                    </div>
                    <AgentDecisionNote note={cycle.payload?.agent_decision_note} />
                  </li>
                ))}
              </ul>
            ) : null}
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function FuturesCycleFunnel({
  summary,
  symbols,
}: {
  summary: FuturesCycleSummary | null;
  symbols: FuturesCycleSymbolRollup[];
}) {
  if (!summary) return null;
  const extras = summary.in_flight + summary.interrupted + summary.other;

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {FUNNEL_STEPS.map((step) => (
          <div key={step.key} className="rounded-lg border p-3">
            <p className="text-lg font-semibold tabular-nums">{summary[step.key]}</p>
            <p className="text-xs font-medium">{step.label}</p>
            <p className="mt-1 text-[11px] leading-snug text-muted-foreground">{step.hint}</p>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Tổng {summary.total} chu kỳ đã ghi nhận
        {extras > 0
          ? ` · ${summary.in_flight} đang chạy · ${summary.interrupted} gián đoạn${
              summary.other ? ` · ${summary.other} khác` : ""
            }`
          : ""}
        . Click một mã để xem từng chu kỳ và lý do cụ thể.
      </p>
      {symbols.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Kết quả chu kỳ autoloop theo mã</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">Mã</th>
                <th scope="col" className="py-2 pr-3 font-medium">Chu kỳ</th>
                <th scope="col" className="py-2 pr-3 font-medium">Sơ lược</th>
                <th scope="col" className="py-2 pr-3 font-medium">Tín hiệu gần nhất</th>
                <th scope="col" className="py-2 pr-3 font-medium">Kết quả gần nhất</th>
                <th scope="col" className="py-2 font-medium">Cập nhật</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {symbols.map((row) => (
                <SymbolRow key={row.symbol} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
