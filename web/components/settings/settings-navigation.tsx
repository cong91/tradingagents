"use client";

import {
  ActivityIcon,
  BrainCircuitIcon,
  CableIcon,
  Settings2Icon,
  ShieldCheckIcon,
  type LucideIcon,
} from "lucide-react";
import { useT } from "@/lib/i18n";
import { SETTINGS_AREAS, type SettingsArea } from "./settings-layout";

const AREA_ICONS: Record<SettingsArea, LucideIcon> = {
  automation: ActivityIcon,
  ai: BrainCircuitIcon,
  risk: ShieldCheckIcon,
  connections: CableIcon,
  system: Settings2Icon,
};

export function SettingsNavigation({
  selected,
  onSelect,
}: {
  selected: SettingsArea;
  onSelect: (area: SettingsArea) => void;
}) {
  const t = useT();
  return (
    <nav aria-label={t.settings.screen.navigation} className="grid grid-cols-2 gap-1 border-b pb-2 min-[420px]:grid-cols-3 md:flex md:flex-col md:border-b-0 md:border-r md:pb-0 md:pr-3">
      {SETTINGS_AREAS.map((area) => {
        const Icon = AREA_ICONS[area];
        const active = selected === area;
        return (
          <a
            key={area}
            href={`#${area}`}
            aria-current={active ? "page" : undefined}
            onClick={(event) => {
              if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              event.preventDefault();
              if (window.location.hash !== `#${area}`) history.pushState(null, "", `#${area}`);
              onSelect(area);
            }}
            className={`inline-flex min-h-11 min-w-0 items-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}
          >
            <Icon className="size-4 shrink-0" aria-hidden="true" />
            {t.settings.screen.sections[area]}
          </a>
        );
      })}
    </nav>
  );
}
