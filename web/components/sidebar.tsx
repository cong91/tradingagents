"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ClipboardCheckIcon,
  HistoryIcon,
  LayoutDashboardIcon,
  ScanSearchIcon,
  SettingsIcon,
  WorkflowIcon,
} from "lucide-react";
import { cn } from "cn";

const NAV_ITEMS = [
  { label: "Tổng quan lợi nhuận", href: "/", icon: LayoutDashboardIcon },
  { label: "Agent Pipeline", href: "/pipeline", icon: WorkflowIcon },
  { label: "Tìm cặp giao dịch", href: "/scanner", icon: ScanSearchIcon },
  { label: "Duyệt lệnh", href: "/approvals", icon: ClipboardCheckIcon },
  { label: "Cấu hình", href: "/settings", icon: SettingsIcon },
  { label: "Lịch sử & Audit", href: "/audit", icon: HistoryIcon },
] as const;

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r bg-sidebar">
      <nav aria-label="Điều hướng chính" className="flex-1 p-3">
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
                >
                  <Icon className="size-4 shrink-0" aria-hidden="true" />
                  <span>{label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <p className="border-t p-3 text-[11px] leading-snug text-muted-foreground">
        Công cụ nghiên cứu — không phải lời khuyên tài chính.
      </p>
    </aside>
  );
}
