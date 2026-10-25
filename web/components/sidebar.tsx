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
import { useT } from "@/lib/i18n";

const NAV_ITEMS = [
  { key: "overview", href: "/", icon: LayoutDashboardIcon },
  { key: "pipeline", href: "/pipeline", icon: WorkflowIcon },
  { key: "scanner", href: "/scanner", icon: ScanSearchIcon },
  { key: "approvals", href: "/approvals", icon: ClipboardCheckIcon },
  { key: "settings", href: "/settings", icon: SettingsIcon },
  { key: "audit", href: "/audit", icon: HistoryIcon },
] as const;

export function Sidebar() {
  const pathname = usePathname();
  const t = useT();

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r bg-sidebar">
      <nav aria-label={t.common.nav.label} className="flex-1 p-3">
        <ul className="space-y-1">
          {NAV_ITEMS.map(({ key, href, icon: Icon }) => {
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
                  <span>{t.common.nav[key]}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <p className="border-t p-3 text-[11px] leading-snug text-muted-foreground">
        {t.common.disclaimer}
      </p>
    </aside>
  );
}
