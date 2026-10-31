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
    hint: "Order đã gửi lên sàn và được xác nhận thực thi (executed)",
  },
  {
    key: "awaiting_approval",
    label: "Chờ duyệt",
    hint: "Cổng duyệt chưa mở — plan chưa được gửi lên sàn",
  },
  {
    key: "risk_rejected",
    label: "RiskGuard từ chối",
    hint: "Guard rủi ro chặn plan trước khi sinh lệnh",
  },
  {
    key: "not_executed",
    label: "Không có lệnh",
    hint: "Plan không sinh lệnh (sizing, budget, side rỗng…)",
  },
  {
    key: "execution_failed",
    label: "Thực thi lỗi",
    hint: "Gửi lệnh hoặc gọi sàn gặp lỗi — chưa xác nhận khớp",
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
    case "planning":
      return "Lập kế hoạch";
    case "execution":
      return "Thực thi lệnh";
    case "entry":
      return "Vào lệnh";
    case "protection":
      return "Bảo vệ vị thế";
    default:
      return phase;
  }
}

/** Nhãn cho từng terminal execution outcome — entered chỉ khi payload xác nhận
 *  order đã gửi (execution_status=executed); mọi outcome khác ghi rõ chưa vào lệnh. */
function executionStatusLabel(payload: FuturesCyclePayload): string {
  switch (payload.execution_status) {
    case "awaiting_approval":
      return `Chờ duyệt — chưa vào lệnh (${payload.execution_reason ?? "cổng duyệt chưa mở"})`;
    case "risk_rejected":
      return `RiskGuard từ chối plan — chưa vào lệnh (${payload.execution_reason ?? payload.plan_reason ?? "vượt trần rủi ro"})`;
    case "not_executed":
      return `Không có lệnh (${payload.plan_reason ?? payload.execution_reason ?? "plan không sinh lệnh"})`;
    case "failed":
      return `Thực thi lỗi — chưa vào lệnh (${payload.failure_reason ?? payload.error ?? "không rõ"})`;
    default:
      return "";
  }
}

function cycleOutcome(cycle: FuturesCycle): string {
  const payload: FuturesCyclePayload = cycle.payload ?? {};
  if (cycle.status === "completed" && cycle.phase === "entry") {
    // entered chỉ khi execution_status=executed và payload không phủ định
    // (executed=false) — khớp điều kiện DB trong job_store.futures_cycle_summary.
    if (payload.execution_status === "executed" && payload.executed !== false) {
      return "Triển khai được — đã vào lệnh";
    }
    // Chu kỳ cũ ghi completed/entry trước khi có execution_status: map rõ ràng
    // theo outcome payload đã ghi, không suy diễn thành "đã vào lệnh".
    return executionStatusLabel(payload) || "Hoàn tất nhưng không rõ thực thi (chu kỳ cũ)";
  }
  if (payload.execution_status) {
    const outcome = executionStatusLabel(payload);
    if (outcome) return outcome;
  }
  // Lỗi ở lớp ngoài (context/analysis): row failed với execution_status
  // not_started nhưng payload vẫn ghi failure_reason — hiển thị nó thay vì
  // rơi xuống reason chung.
  if (cycle.status === "failed" && payload.failure_reason) {
    return `Thực thi lỗi — chưa vào lệnh (${payload.failure_reason})`;
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

/** Kết quả gần nhất của một mã, suy ra từ status/phase/execution_status của
 *  chu kỳ mới nhất — không dùng signal_reason làm bằng chứng đã vào lệnh.
 *  entered chỉ khi payload xác nhận thực thi VÀ không bị executed=false phủ
 *  nhận; chu kỳ cũ thiếu trường executed ('') vẫn giữ nghĩa executed. */
function latestOutcome(row: FuturesCycleSymbolRollup): string {
  if (row.last_status === "completed" && row.last_phase === "entry"
      && row.last_execution_status === "executed"
      && row.last_executed !== "false") {
    return "Triển khai được — đã vào lệnh";
  }
  switch (row.last_execution_status) {
    case "awaiting_approval":
      return "Chờ duyệt — chưa vào lệnh";
    case "risk_rejected":
      return `RiskGuard từ chối plan — chưa vào lệnh${row.last_execution_reason ? ` (${row.last_execution_reason})` : ""}`;
    case "not_executed":
      return `Không có lệnh${row.last_execution_reason ? ` (${row.last_execution_reason})` : ""}`;
    case "failed":
      return `Thực thi lỗi — chưa vào lệnh${row.last_execution_reason ? ` (${row.last_execution_reason})` : ""}`;
  }
  if (row.last_status === "failed") {
    return `Thực thi lỗi — chưa vào lệnh${row.last_execution_reason ? ` (${row.last_execution_reason})` : ""}`;
  }
  if (row.last_phase === "market") return marketRejectionLabel(row.last_market_rejection_reason);
  if (row.last_phase === "confirmation") return confirmationLabel(row.last_confirmation_reason);
  if (row.last_phase === "proposal") return proposalLabel(row.last_signal_reason);
  if (row.last_signal_reason === "tradeable_signal") return "Đã phân tích — xem chi tiết từng chu kỳ";
  if (row.last_signal_reason) return proposalLabel(row.last_signal_reason);
  return "Chưa có dữ liệu";
}

function breakdownParts(row: FuturesCycleSymbolRollup): string[] {
  const parts: string[] = [];
  if (row.entered) parts.push(`${row.entered} vào lệnh`);
  if (row.awaiting_approval) parts.push(`${row.awaiting_approval} chờ duyệt`);
  if (row.risk_rejected) parts.push(`${row.risk_rejected} risk từ chối`);
  if (row.not_executed) parts.push(`${row.not_executed} không có lệnh`);
  if (row.execution_failed) parts.push(`${row.execution_failed} thực thi lỗi`);
  if (row.proposal_rejected) parts.push(`${row.proposal_rejected} agent từ chối`);
  if (row.confirmation_rejected) parts.push(`${row.confirmation_rejected} nến chưa xác nhận`);
  if (row.market_rejected) parts.push(`${row.market_rejected} loại từ bước quét`);
  if (row.in_flight) parts.push(`${row.in_flight} đang chạy`);
  if (row.interrupted) parts.push(`${row.interrupted} gián đoạn`);
  if (row.other) parts.push(`${row.other} khác`);
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
  const extraParts = [
    summary.in_flight ? `${summary.in_flight} đang chạy` : "",
    summary.awaiting_approval ? `${summary.awaiting_approval} chờ duyệt` : "",
    summary.risk_rejected ? `${summary.risk_rejected} risk từ chối` : "",
    summary.not_executed ? `${summary.not_executed} không có lệnh` : "",
    summary.execution_failed ? `${summary.execution_failed} thực thi lỗi` : "",
    summary.interrupted ? `${summary.interrupted} gián đoạn` : "",
    summary.other ? `${summary.other} khác` : "",
  ].filter(Boolean);

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-8">
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
        {extraParts.length > 0 ? ` · ${extraParts.join(" · ")}` : ""}
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
