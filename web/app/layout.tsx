import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ModeBar } from "@/components/mode-bar";
import { PreferencesSync } from "@/components/preferences-sync";
import { Sidebar } from "@/components/sidebar";
import { DEFAULT_LOCALE, LOCALE_INIT_SCRIPT } from "@/lib/i18n/locale";
import { messages } from "@/lib/i18n/messages";
import { THEME_INIT_SCRIPT } from "@/lib/theme";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

// Metadata is static (the server cannot read the stored locale): default
// locale here, client components update document.title per locale.
const copy = messages[DEFAULT_LOCALE].common;

export const metadata: Metadata = {
  title: {
    default: copy.appTitle,
    template: "%s — TradingAgents",
  },
  description: copy.appDescription,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // suppressHydrationWarning: the inline script may set lang/.dark on <html>
    // before React hydrates (stored preferences live in localStorage).
    <html
      lang={DEFAULT_LOCALE}
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script
          dangerouslySetInnerHTML={{ __html: `${LOCALE_INIT_SCRIPT};${THEME_INIT_SCRIPT}` }}
        />
      </head>
      <body className="flex min-h-full flex-col">
        <PreferencesSync />
        <ModeBar />
        <div className="flex flex-1">
          <Sidebar />
          <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
        </div>
      </body>
    </html>
  );
}
