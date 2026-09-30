// Định dạng thời gian hiển thị cho màn pipeline: ISO-8601 UTC (hợp đồng §0)
// → giờ địa phương "HH:mm:ss". Input sai format thì trả nguyên văn.

const clockFormat = new Intl.DateTimeFormat("vi-VN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return clockFormat.format(date);
}
