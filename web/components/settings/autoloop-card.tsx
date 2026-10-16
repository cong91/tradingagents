"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  LoaderCircleIcon,
  OctagonXIcon,
  PlayIcon,
  TimerIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { cn } from "cn";
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
import { autoloopApi, toAutoloopError } from "./autoloop-api";
import {
  AUTOLOOP_DEFAULT_INTERVAL,
  AUTOLOOP_GATE_ROWS,
  AUTOLOOP_MAX_INTERVAL,
  AUTOLOOP_MIN_INTERVAL,
  type AutoloopLastCycle,
  type AutoloopPayload,
} from "./autoloop-types";
import { formatDateTimeVi } from "./format-time";

// Poll chậm khi rảnh, nhanh khi một chu kỳ đang chạy để spinner "Chạy thử"
// tự tắt đúng lúc; cùng nhịp stale 30s mà màn Cấu hình dùng cho /api/settings.
const POLL_IDLE_MS = 30_000;
const POLL_RUNNING_MS = 5_000;

type Feedback = { role: "status" | "alert"; text: string };

const RUN_STATUS_TEXT: Record<string, string> = {
  started: "Đã gửi yêu cầu — chu kỳ đang bắt đầu.",
  already_running: "Một chu kỳ đang chạy — bỏ qua yêu cầu mới.",
  skipped_busy: "Bỏ qua — run/daily job khác đang bận.",
};

/** Khoảng cách phút hợp lệ: số nguyên trong [15, 1440] — cùng ràng buộc server. */
function parseInterval(raw: string): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const value = Number(raw.trim());
  if (!Number.isInteger(value)) return null;
  return AUTOLOOP_MIN_INTERVAL <= value && value <= AUTOLOOP_MAX_INTERVAL ? value : null;
}

/** Tóm tắt một dòng của chu kỳ gần nhất (thời gian + trạng thái + ứng viên). */
function lastCycleSummary(cycle: AutoloopLastCycle): string {
  const when = formatDateTimeVi(cycle.finished_at ?? cycle.started_at);
  if (cycle.status === "skipped_busy") {
    return `${when} · Bỏ qua — ${cycle.reason ?? "worker khác đang bận"}`;
  }
  if (cycle.status === "failed") {
    return `${when} · Thất bại — ${cycle.error ?? "lỗi không xác định"}`;
  }
  if (cycle.status === "running" || cycle.status === "timeout") {
    const telemetry = cycle.telemetry;
    const ticker = telemetry?.active_ticker ?? telemetry?.current_ticker;
    const stage = telemetry?.active_stage ?? telemetry?.last_stage;
    const progress = [
      ticker ? `Mã: ${ticker}` : null,
      telemetry?.active_node ? `Agent: ${telemetry.active_node}` : null,
      stage ? `${telemetry?.active_stage ? "Bước" : "Bước gần nhất"}: ${stage}` : null,
    ].filter(Boolean).join(" · ");
    const status = cycle.status === "running"
      ? "Đang chạy"
      : `Hết thời gian — ${cycle.error ?? "vượt quá thời gian chờ"}`;
    return `${when} · ${status}${progress ? ` · ${progress}` : ""}`;
  }
  const executed = cycle.results.filter((result) => result.executed).length;
  const candidates =
    cycle.candidates.length > 0 ? `Ứng viên: ${cycle.candidates.join(", ")}` : "Không có ứng viên";
  return `${when} · Hoàn tất · ${candidates} · ${executed}/${cycle.results.length} lệnh đã thực thi`;
}

function GateBadge({ open }: { open: boolean }) {
  return open ? (
    <Badge className="border-emerald-600 bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400">
      Mở
    </Badge>
  ) : (
    <Badge variant="destructive">Đóng</Badge>
  );
}

export function AutoloopCard() {
  const [phase, setPhase] = useState<"loading" | "error" | "ready">("loading");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [payload, setPayload] = useState<AutoloopPayload | null>(null);
  // Bản nháp khoảng cách phút: poll không ghi đè khi user đang sửa (§0.2 stale rule).
  const [intervalDraft, setIntervalDraft] = useState(String(AUTOLOOP_DEFAULT_INTERVAL));
  const [intervalError, setIntervalError] = useState<string | null>(null);
  const [gateWarning, setGateWarning] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [savePending, setSavePending] = useState(false);
  const [runPending, setRunPending] = useState(false);
  const [haltPending, setHaltPending] = useState(false);
  // Halt state KHÔNG đọc từ GET /api/autoloop — chỉ phản hồi kết quả POST ngay
  // tại chỗ; trạng thái halt toàn cục xem ở banner health của màn.
  const [halted, setHalted] = useState<boolean | null>(null);

  const hasDataRef = useRef(false);
  const draftDirtyRef = useRef(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const applyPayload = useCallback((data: AutoloopPayload) => {
    setPayload(data);
    if (!draftDirtyRef.current) setIntervalDraft(String(data.interval_minutes));
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await autoloopApi.get();
      applyPayload(data);
      hasDataRef.current = true;
      setPhase("ready");
    } catch (err) {
      setErrorMessage(toAutoloopError(err).message);
      // Đã có dữ liệu: giữ nguyên UI thay vì sập cả card xuống trạng thái lỗi.
      if (!hasDataRef.current) setPhase("error");
    }
  }, [applyPayload]);

  // Nạp lần đầu trong callback (không setState đồng bộ trong effect body),
  // rồi poll theo trạng thái running_cycle.
  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  const runningCycle = payload?.running_cycle ?? false;
  useEffect(() => {
    if (phase !== "ready") return;
    const pollId = setInterval(() => void load(), runningCycle ? POLL_RUNNING_MS : POLL_IDLE_MS);
    return () => clearInterval(pollId);
  }, [load, phase, runningCycle]);

  const save = useCallback(
    async (enabled: boolean) => {
      const interval = parseInterval(intervalDraft);
      if (interval === null) {
        draftDirtyRef.current = true;
        setIntervalError(
          `Khoảng cách phải là số nguyên từ ${AUTOLOOP_MIN_INTERVAL} đến ${AUTOLOOP_MAX_INTERVAL} phút.`,
        );
        return;
      }
      setIntervalError(null);
      setGateWarning(null);
      setSavePending(true);
      try {
        const data = await autoloopApi.set(enabled, interval);
        draftDirtyRef.current = false;
        applyPayload(data);
        setFeedback({
          role: "status",
          text: enabled
            ? `Đã bật vòng tự động — chu kỳ chạy mỗi ${data.interval_minutes} phút.`
            : "Đã tắt vòng tự động.",
        });
      } catch (err) {
        const error = toAutoloopError(err);
        if (error.code === "gate_closed") {
          setGateWarning(
            "Cổng an toàn chưa mở — vòng tự động từ chối bật (thiết kế fail-closed).",
          );
        } else {
          setFeedback({ role: "alert", text: error.message });
        }
      } finally {
        setSavePending(false);
      }
    },
    [applyPayload, intervalDraft],
  );

  const runNow = useCallback(async () => {
    setRunPending(true);
    setFeedback(null);
    try {
      const res = await autoloopApi.runNow();
      setFeedback({ role: "status", text: RUN_STATUS_TEXT[res.status] ?? `Phản hồi máy chủ: ${res.status}.` });
      // GET lại sau 1s để running_cycle kịp phản ánh worker vừa được spawn.
      setTimeout(() => {
        if (aliveRef.current) void load();
      }, 1_000);
    } catch (err) {
      setFeedback({ role: "alert", text: toAutoloopError(err).message });
    } finally {
      setRunPending(false);
    }
  }, [load]);

  const doHalt = useCallback(async () => {
    setHaltPending(true);
    try {
      const res = await autoloopApi.halt();
      setHalted(res.halted === true);
      setFeedback({
        role: "status",
        text: "Đã ghi halt vào audit log — RiskGuard chặn mọi lệnh mới còn lại hôm nay, ở cả tiến trình khác.",
      });
    } catch (err) {
      setFeedback({ role: "alert", text: toAutoloopError(err).message });
    } finally {
      setHaltPending(false);
    }
  }, []);

  const doResetHalt = useCallback(async () => {
    setHaltPending(true);
    try {
      const res = await autoloopApi.resetHalt();
      setHalted(res.halted);
      setFeedback({ role: "status", text: "Đã gỡ halt hôm nay — RiskGuard nhận lệnh mới bình thường." });
    } catch (err) {
      setFeedback({ role: "alert", text: toAutoloopError(err).message });
    } finally {
      setHaltPending(false);
    }
  }, []);

  if (phase === "loading") {
    return (
      <Card aria-busy="true" aria-live="polite">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TimerIcon className="size-4" aria-hidden="true" />
            Chế độ tự động
          </CardTitle>
          <CardDescription>Đang tải trạng thái vòng tự động từ GET /api/autoloop…</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-20 animate-pulse rounded-lg bg-muted" />
        </CardContent>
      </Card>
    );
  }

  if (phase === "error" || !payload) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TriangleAlertIcon className="size-4 shrink-0 text-destructive" aria-hidden="true" />
            Chế độ tự động
          </CardTitle>
          <CardDescription>{errorMessage ?? "Không tải được trạng thái vòng tự động."}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" className="min-touch" onClick={() => void load()}>
            Thử lại
          </Button>
        </CardContent>
      </Card>
    );
  }

  const { enabled, gates, last_cycle: lastCycle } = payload;
  const cycleBusy = runningCycle || runPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TimerIcon className="size-4" aria-hidden="true" />
          Chế độ tự động
        </CardTitle>
        <CardDescription>
          Vòng quét + giao dịch theo lịch chạy trên server: sàng universe, phân tích ứng viên rồi
          tự duyệt theo đúng cổng an toàn của engine. Bật chỉ được khi các cổng auto-confirm cùng mở.
        </CardDescription>
        <CardAction>
          {enabled ? <Badge variant="default">Đang bật</Badge> : <Badge variant="outline">Đang tắt</Badge>}
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* (1) Bảng 6 hàng cổng an toàn — chỉ đọc, cùng nguồn với chính sách của engine. */}
        <section aria-label="Cổng an toàn">
          <ul className="space-y-2">
            {AUTOLOOP_GATE_ROWS.map(({ key, label, hint }) => (
              <li
                key={key}
                className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
              >
                <span className="text-sm">
                  {label}
                  {hint ? <span className="block text-xs text-muted-foreground">{hint}</span> : null}
                </span>
                <GateBadge open={gates[key]} />
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">
            Bật futures cần đặt exec_derivatives=True trong .env theo hướng dẫn — vòng tự động sẽ mở
            long/short đòn bẩy cho ứng viên crypto khi cổng này mở (đòn bẩy tối đa 3×, trần exposure
            do RiskGuard kiểm).
          </p>
          <p
            className={cn(
              "mt-2 text-sm font-medium",
              gates.all_open ? "text-profit" : "text-amber-700 dark:text-amber-400",
            )}
          >
            {gates.all_open
              ? "Đủ điều kiện tự động"
              : "Chưa đủ điều kiện — vòng tự động không thể bật"}
          </p>
        </section>

        {/* (2) Công tắc + khoảng cách phút. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-lg border p-3">
          <label
            htmlFor="autoloop-enabled"
            className="inline-flex min-touch cursor-pointer items-center gap-2"
          >
            <input
              id="autoloop-enabled"
              type="checkbox"
              checked={enabled}
              disabled={savePending}
              onChange={(event) => void save(event.target.checked)}
              className="size-6 cursor-pointer accent-primary"
            />
            <span className="text-sm font-medium">Bật vòng tự động</span>
          </label>
          <span className="ml-auto flex flex-wrap items-center gap-2">
            <label htmlFor="autoloop-interval" className="text-sm text-muted-foreground">
              Khoảng cách (phút)
            </label>
            <input
              id="autoloop-interval"
              type="text"
              inputMode="numeric"
              value={intervalDraft}
              onChange={(event) => {
                draftDirtyRef.current = true;
                setIntervalDraft(event.target.value);
                setIntervalError(null);
              }}
              disabled={savePending}
              aria-invalid={intervalError ? true : undefined}
              aria-describedby={intervalError ? "autoloop-interval-error" : undefined}
              className="h-9 w-24 rounded-lg border border-input bg-background px-3 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-[invalid=true]:border-destructive"
            />
            <Button
              variant="outline"
              size="sm"
              className="min-touch"
              onClick={() => void save(enabled)}
              disabled={savePending}
            >
              Áp dụng
            </Button>
          </span>
        </div>
        {intervalError ? (
          <p id="autoloop-interval-error" role="alert" className="text-xs text-destructive">
            {intervalError}
          </p>
        ) : null}
        {gateWarning ? (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
          >
            <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
            {gateWarning}
          </div>
        ) : null}

        {/* (3) Chạy thử một chu kỳ. */}
        <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
          <Button className="min-touch" onClick={() => void runNow()} disabled={cycleBusy}>
            {cycleBusy ? (
              <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
            ) : (
              <PlayIcon aria-hidden="true" />
            )}
            {cycleBusy ? "Chu kỳ đang chạy…" : "Chạy thử một chu kỳ"}
          </Button>
          <span className="min-w-0 flex-1 text-sm text-muted-foreground">
            Chạy ngay một vòng không cần chờ lịch — sàng universe rồi phân tích top ứng viên.
          </span>
        </div>

        {/* (4) STOP khẩn cấp — không gate, luôn gửi được. */}
        <div className="rounded-lg border-2 border-destructive/60 bg-destructive/5 p-3">
          <p className="flex items-center gap-2 text-sm font-semibold text-destructive">
            <OctagonXIcon className="size-4" aria-hidden="true" />
            STOP khẩn cấp
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            Ghi halt vào audit log — RiskGuard chặn mọi lệnh mới còn lại hôm nay, ở cả tiến trình khác.
          </p>
          <div className="mt-3">
            {halted === true ? (
              <div className="flex flex-wrap items-center gap-3">
                <Badge variant="destructive">Halt đang hiệu lực (theo card này)</Badge>
                <Button
                  variant="outline"
                  className="min-touch"
                  onClick={() => void doResetHalt()}
                  disabled={haltPending}
                >
                  {haltPending ? (
                    <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
                  ) : null}
                  Gỡ halt hôm nay
                </Button>
              </div>
            ) : (
              <Button
                variant="destructive"
                className="min-touch"
                onClick={() => void doHalt()}
                disabled={haltPending}
              >
                {haltPending ? (
                  <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
                ) : (
                  <OctagonXIcon aria-hidden="true" />
                )}
                DỪNG TẤT CẢ GIAO DỊCH HÔM NAY
              </Button>
            )}
          </div>
        </div>

        {/* (5) Chu kỳ gần nhất — một dòng. */}
        {lastCycle ? (
          <p
            className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground"
            title={lastCycleSummary(lastCycle)}
          >
            <span className="font-medium text-foreground">Chu kỳ gần nhất: </span>
            {lastCycleSummary(lastCycle)}
          </p>
        ) : null}

        {feedback ? (
          <p
            role={feedback.role}
            className={cn("text-sm", feedback.role === "alert" ? "text-destructive" : "text-profit")}
          >
            {feedback.text}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
