"use client";

import { useEffect, useRef } from "react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
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

/**
 * Biểu đồ alpha tích luỹ theo ngày (lightweight-charts v5: chart.addSeries
 * với định nghĩa LineSeries). Component chỉ mount khi CÓ điểm dữ liệu —
 * empty-state do section cha render. Import động trong effect để không đụng SSR.
 */
export function AlphaChart({ points }: { points: AlphaPoint[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  // Bảng giá trị mới nhất cho phép effect tạo chart (bất đồng bộ) set đúng
  // dữ liệu dù points đã đổi trong lúc import đang chạy.
  const pointsRef = useRef(points);
  useEffect(() => {
    pointsRef.current = points;
  }, [points]);

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
      const last =
        pointsRef.current.length > 0 ? pointsRef.current[pointsRef.current.length - 1].value : 0;

      const chart = createChart(el, {
        autoSize: true,
        localization: { locale: "vi-VN" },
        layout: {
          background: { color: "transparent" },
          textColor: text,
          attributionLogo: false,
        },
        grid: { vertLines: { visible: false }, horzLines: { visible: true } },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderVisible: false },
        crosshair: { mode: CrosshairMode.Normal },
      });
      const series = chart.addSeries(LineSeries, {
        color: last < 0 ? loss : profit,
        lineWidth: 2,
        priceFormat: { type: "price", precision: 1, minMove: 0.1 },
      });
      series.setData(pointsRef.current.map((p) => ({ time: p.date, value: p.value })));
      chart.timeScale().fitContent();

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

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label="Biểu đồ alpha tích luỹ theo ngày, đơn vị phần trăm"
      className="h-64 w-full"
    />
  );
}
