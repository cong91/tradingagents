"use client";

import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, LineData, Time } from "lightweight-charts";
import { intlLocale, useLocale, useT } from "@/lib/i18n";
import { useResolvedTheme } from "@/lib/use-theme";
import { formatDateWith, formatSignedNumber, pnlTextClass } from "@/lib/format";
import type { AlphaPoint } from "./summary";

const FALLBACK_PROFIT = "#047857"; // emerald-700 — đồng bộ --profit bản sáng (globals.css:71)
const FALLBACK_LOSS = "#b91c1c"; // red-700 — đồng bộ --loss bản sáng (globals.css:72)
const FALLBACK_TEXT = "#737373";

// Token theme biên dịch kèm khối @supports lab(), nên getComputedStyle trả về
// lab(...) chứ không phải oklch(...). lightweight-charts chỉ đọc được cú pháp màu
// cũ (hex/rgb/hsl) và ném "Failed to parse color" với lab(). Canvas 2D là bộ quy
// đổi màu của chính trình duyệt: tô 1 pixel rồi đọc lại cho ra sRGB — đúng bằng
// màu chart sẽ vẽ, và tự đúng theo theme sáng/tối.
let probeCtx: CanvasRenderingContext2D | null | undefined;

function toSrgbColor(value: string): string | null {
  if (!CSS.supports("color", value)) return null;
  if (probeCtx === undefined) {
    probeCtx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  }
  if (!probeCtx) return null;

  probeCtx.clearRect(0, 0, 1, 1);
  probeCtx.fillStyle = value;
  probeCtx.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = probeCtx.getImageData(0, 0, 1, 1).data;
  if (a === 255) {
    const hex = (channel: number) => channel.toString(16).padStart(2, "0");
    return `#${hex(r)}${hex(g)}${hex(b)}`;
  }
  return `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
}

function resolveToken(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  const raw = styles.getPropertyValue(name).trim();
  return raw === "" ? fallback : toSrgbColor(raw) ?? fallback;
}

// Dữ liệu vào là chuỗi "YYYY-MM-DD" nên lightweight-charts trả time trong các
// formatter dạng BusinessDay ({year, month, day}); hai nhánh còn lại phòng khi
// caller đổi sang timestamp/chuỗi khác. Parse thủ công để tránh lệch múi giờ.
function timeToDate(time: Time): Date | null {
  if (typeof time === "object" && time !== null) {
    return new Date(time.year, time.month - 1, time.day);
  }
  if (typeof time === "number") return new Date(time * 1000);
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(time);
  return parts ? new Date(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3])) : null;
}

type Hover = {
  left: number;
  top: number;
  flip: boolean;
  dateLabel: string;
  value: number;
};

/**
 * Biểu đồ alpha tích luỹ theo ngày (lightweight-charts v5: chart.addSeries
 * với định nghĩa LineSeries). Trục giá và tooltip mang đơn vị %; formatter đọc
 * locale hiện hành lúc gọi nên không cần dựng lại chart khi đổi ngôn ngữ.
 * Component chỉ mount khi CÓ điểm dữ liệu — empty-state do section cha render.
 * Import động trong effect để không đụng SSR.
 */
export function AlphaChart({ points }: { points: AlphaPoint[] }) {
  const t = useT();
  const locale = useLocale();
  const theme = useResolvedTheme();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  // Bảng giá trị mới nhất cho phép effect tạo chart (bất đồng bộ) set đúng
  // dữ liệu dù points đã đổi trong lúc import đang chạy.
  const pointsRef = useRef(points);
  useEffect(() => {
    pointsRef.current = points;
  }, [points]);
  const last = points.length > 0 ? points[points.length - 1].value : null;

  // Tạo chart đúng một lần trong vòng đời của component.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const el = containerRef.current;
      if (!el) return;
      const { createChart, LineSeries, CrosshairMode } = await import("lightweight-charts");
      if (cancelled || !containerRef.current) return;

      const styles = getComputedStyle(document.documentElement);
      const profit = resolveToken(styles, "--profit", FALLBACK_PROFIT);
      const loss = resolveToken(styles, "--loss", FALLBACK_LOSS);
      const text = resolveToken(styles, "--muted-foreground", FALLBACK_TEXT);
      const lastValue =
        pointsRef.current.length > 0 ? pointsRef.current[pointsRef.current.length - 1].value : 0;

      const chart = createChart(el, {
        autoSize: true,
        localization: {
          locale: intlLocale(),
          timeFormatter: (time: Time) => {
            const date = timeToDate(time);
            return date
              ? formatDateWith(date, { day: "numeric", month: "short", year: "numeric" })
              : String(time);
          },
        },
        layout: {
          background: { color: "transparent" },
          textColor: text,
          attributionLogo: false,
        },
        grid: { vertLines: { visible: false }, horzLines: { visible: true } },
        rightPriceScale: { borderVisible: false },
        timeScale: {
          borderVisible: false,
          tickMarkFormatter: (time: Time) => {
            const date = timeToDate(time);
            return date ? formatDateWith(date, { day: "numeric", month: "short" }) : null;
          },
        },
        crosshair: { mode: CrosshairMode.Normal },
      });
      const series = chart.addSeries(LineSeries, {
        color: lastValue < 0 ? loss : profit,
        lineWidth: 2,
        priceFormat: {
          type: "custom",
          formatter: (price: number) => `${formatSignedNumber(price, 1)}%`,
          minMove: 0.1,
        },
      });
      series.setData(pointsRef.current.map((p) => ({ time: p.date, value: p.value })));
      chart.timeScale().fitContent();

      // Tooltip HTML: chỉ track điểm đang rê — vị trí/nội dung vào React state.
      chart.subscribeCrosshairMove((param) => {
        const bar =
          param.point && param.time !== undefined
            ? (param.seriesData.get(series) as LineData | undefined)
            : undefined;
        const date = param.time !== undefined ? timeToDate(param.time) : null;
        if (!bar || !param.point || !date) {
          setHover(null);
          return;
        }
        const flip = param.point.x > el.clientWidth * 0.6;
        setHover({
          left: flip ? param.point.x - 12 : param.point.x + 12,
          top: Math.max(4, param.point.y - 56),
          flip,
          dateLabel: formatDateWith(date, { day: "numeric", month: "short", year: "numeric" }),
          value: bar.value,
        });
      });

      chartRef.current = chart;
      seriesRef.current = series;
    })();

    return () => {
      cancelled = true;
      chartRef.current?.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Cập nhật dữ liệu khi points đổi (chart chưa sẵn sàng thì bỏ qua — effect
  // phía trên đã set pointsRef hiện hành lúc tạo).
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series || points.length === 0) return;

    const styles = getComputedStyle(document.documentElement);
    const profit = resolveToken(styles, "--profit", FALLBACK_PROFIT);
    const loss = resolveToken(styles, "--loss", FALLBACK_LOSS);
    const last = points[points.length - 1].value;
    series.applyOptions({ color: last < 0 ? loss : profit });
    series.setData(points.map((p) => ({ time: p.date, value: p.value })));
    chart.timeScale().fitContent();
  }, [points]);

  // Đổi theme/locale: đọc lại token màu và locale cho chart đã tạo (canvas
  // không tự theo CSS variables; applyOptions buộc vẽ lại, khi đó các formatter
  // tick/crosshair đọc locale mới).
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return;
    const styles = getComputedStyle(document.documentElement);
    const last = pointsRef.current.length > 0 ? pointsRef.current[pointsRef.current.length - 1].value : 0;
    chart.applyOptions({
      localization: { locale: intlLocale(locale) },
      layout: { textColor: resolveToken(styles, "--muted-foreground", FALLBACK_TEXT) },
    });
    series.applyOptions({
      color: last < 0
        ? resolveToken(styles, "--loss", FALLBACK_LOSS)
        : resolveToken(styles, "--profit", FALLBACK_PROFIT),
    });
  }, [theme, locale]);

  return (
    <div className="relative">
      <div
        ref={containerRef}
        role="img"
        aria-label={t.overview.chartAria}
        className="h-64 w-full"
      />
      {last !== null ? (
        <div className="pointer-events-none absolute left-3 top-2 z-10 flex items-center gap-2 rounded-md border bg-background/80 px-2 py-1 text-xs text-muted-foreground">
          <span
            aria-hidden="true"
            className="inline-block h-0.5 w-4 rounded"
            style={{ background: last < 0 ? "var(--loss)" : "var(--profit)" }}
          />
          <span>{t.overview.chart.legend}</span>
          <span className={`font-semibold tabular-nums ${pnlTextClass(last)}`}>
            {formatSignedNumber(last, 1)}%
          </span>
        </div>
      ) : null}
      {hover ? (
        <div
          className={`pointer-events-none absolute z-10 rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow-md ${hover.flip ? "-translate-x-full" : ""}`}
          style={{ left: hover.left, top: hover.top }}
        >
          <p className="text-muted-foreground">
            {t.overview.chart.tooltipDate}: {hover.dateLabel}
          </p>
          <p>
            {t.overview.chart.tooltipValue}:{" "}
            <span className={`font-semibold tabular-nums ${pnlTextClass(hover.value)}`}>
              {formatSignedNumber(hover.value, 1)}%
            </span>
          </p>
        </div>
      ) : null}
      <p className="mt-1 text-xs text-muted-foreground">{t.overview.chart.tooltipHint}</p>
    </div>
  );
}
