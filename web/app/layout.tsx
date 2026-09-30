import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ModeBar } from "@/components/mode-bar";
import { Sidebar } from "@/components/sidebar";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "TradingAgents — Bảng điều khiển",
    template: "%s — TradingAgents",
  },
  description:
    "Bảng điều khiển nghiên cứu và phê duyệt giao dịch TradingAgents. Công cụ nghiên cứu — không phải lời khuyên tài chính.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="vi"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <ModeBar />
        <div className="flex flex-1">
          <Sidebar />
          <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
        </div>
      </body>
    </html>
  );
}
