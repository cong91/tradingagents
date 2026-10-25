"use client";

import { LanguagesIcon, MonitorIcon, MoonIcon, SunIcon, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useLocale, useT } from "@/lib/i18n";
import { setLocale, type Locale } from "@/lib/i18n/locale";
import { setTheme, type Theme } from "@/lib/theme";
import { useTheme } from "@/lib/use-theme";

// Language names are shown in their own language so they stay recognizable
// whichever locale is active.
const LANGUAGE_OPTIONS: readonly { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "vi", label: "Tiếng Việt" },
];

type Option<T extends string> = { value: T; label: string; icon?: LucideIcon };

/** Segmented single-choice control (radio semantics, ≥44px touch targets). */
function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly Option<T>[];
  onChange: (next: T) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t py-3 first:border-t-0 first:pt-0">
      <span className="text-sm font-medium">{label}</span>
      <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-1">
        {options.map(({ value: optionValue, label: optionLabel, icon: Icon }) => {
          const active = optionValue === value;
          return (
            <Button
              key={optionValue}
              role="radio"
              aria-checked={active}
              variant={active ? "default" : "outline"}
              size="sm"
              className="min-touch"
              onClick={() => onChange(optionValue)}
            >
              {Icon ? <Icon aria-hidden="true" /> : null}
              {optionLabel}
            </Button>
          );
        })}
      </div>
    </div>
  );
}

/** Per-browser display preferences: UI language and light/dark theme. */
export function InterfacePreferencesCard() {
  const t = useT();
  const locale = useLocale();
  const theme = useTheme();
  const c = t.common.preferences;

  const themeOptions: readonly Option<Theme>[] = [
    { value: "light", label: c.themeLight, icon: SunIcon },
    { value: "dark", label: c.themeDark, icon: MoonIcon },
    { value: "system", label: c.themeSystem, icon: MonitorIcon },
  ];

  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <LanguagesIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
      </CardHeader>
      <CardContent>
        <Segmented
          label={c.language}
          value={locale}
          options={LANGUAGE_OPTIONS}
          onChange={(next) => setLocale(next)}
        />
        <Segmented
          label={c.theme}
          value={theme}
          options={themeOptions}
          onChange={(next) => setTheme(next)}
        />
      </CardContent>
    </Card>
  );
}
