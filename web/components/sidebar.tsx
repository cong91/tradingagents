"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import {
  ClipboardCheckIcon,
  HistoryIcon,
  LayoutDashboardIcon,
  ListChecksIcon,
  MenuIcon,
  ScanSearchIcon,
  SettingsIcon,
  WorkflowIcon,
  XIcon,
} from "lucide-react";
import { cn } from "cn";

const NAV_ITEMS = [
  { label: "Tổng quan lợi nhuận", href: "/", icon: LayoutDashboardIcon },
  { label: "Agent Pipeline", href: "/pipeline", icon: WorkflowIcon },
  { label: "Tìm cặp giao dịch", href: "/scanner", icon: ScanSearchIcon },
  { label: "Chu kỳ quét", href: "/cycles", icon: ListChecksIcon },
  { label: "Duyệt lệnh", href: "/approvals", icon: ClipboardCheckIcon },
  { label: "Cấu hình", href: "/settings", icon: SettingsIcon },
  { label: "Lịch sử & Audit", href: "/audit", icon: HistoryIcon },
] as const;

export function Sidebar() {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <aside className="flex w-full flex-col border-b bg-sidebar md:w-56 md:shrink-0 md:border-b-0 md:border-r">
      <button
        type="button"
        aria-controls="primary-navigation"
        aria-expanded={mobileOpen}
        aria-label={mobileOpen ? "Đóng điều hướng chính" : "Mở điều hướng chính"}
        className="min-touch flex items-center gap-2 px-4 text-sm font-medium md:hidden"
        onClick={() => setMobileOpen((open) => !open)}
      >
        {mobileOpen ? (
          <XIcon className="size-4" aria-hidden="true" />
        ) : (
          <MenuIcon className="size-4" aria-hidden="true" />
        )}
        <span>Điều hướng</span>
      </button>
      <nav
        id="primary-navigation"
        aria-label="Điều hướng chính"
        className={cn("flex-1 p-3 md:block", mobileOpen ? "block" : "hidden")}
      >
        <ul className="space-y-1">
          {NAV_ITEMS.map(({ label, href, icon: Icon }) => {
            const active =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <li key={href}>
                <Link
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "min-touch flex items-center gap-2.5 rounded-lg px-3 text-sm",
                    active
                      ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                      : "text-sidebar-foreground hover:bg-sidebar-accent/60"
                  )}
                  onClick={() => setMobileOpen(false)}
                >
                  <Icon className="size-4 shrink-0" aria-hidden="true" />
                  <span>{label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
        <p className="mt-3 border-t pt-3 text-[11px] leading-snug text-muted-foreground md:hidden">
          Công cụ nghiên cứu — không phải lời khuyên tài chính.
        </p>
      </nav>
      <p className="hidden border-t p-3 text-[11px] leading-snug text-muted-foreground md:block">
        Công cụ nghiên cứu — không phải lời khuyên tài chính.
      </p>
    </aside>
  );
}
