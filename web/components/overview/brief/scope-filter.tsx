"use client";

// Scope filter (UX plan §7.2): one control that narrows every block on the
// page to Spot, Futures or both. It is UI-local — the API has no scope
// parameter — so the choice is stored per browser and applied through a prop,
// which keeps each section independently testable.
//
// Keyboard/AT: a labelled group of toggle buttons with aria-pressed, not a
// fake tablist. Targets are 44px (min-touch) like every other control here.
//
// The store follows lib/theme.ts + lib/use-theme.ts: a module-level value read
// through useSyncExternalStore with a server snapshot, so the first client
// render matches the server's ("all") and no effect has to write state back.

import { useSyncExternalStore } from "react";

import { cn } from "cn";
import { useT } from "@/lib/i18n";

import type { Scope } from "./contract";

const STORAGE_KEY = "tradingagents.brief.scope";
const SCOPES: Scope[] = ["all", "spot", "futures"];

export function isScope(value: unknown): value is Scope {
  return typeof value === "string" && (SCOPES as string[]).includes(value);
}

const listeners = new Set<() => void>();

function readStoredScope(): Scope {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return isScope(raw) ? raw : "all";
  } catch {
    return "all";
  }
}

let current: Scope | null = null;

function getScope(): Scope {
  // Read lazily on first client read: never during the server render.
  if (current === null) current = readStoredScope();
  return current;
}

function setScope(next: Scope): void {
  current = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Storage unavailable — keep the choice for this page session only.
  }
  listeners.forEach((listener) => listener());
}

function subscribeScope(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Scope state + persistence. The server snapshot is always "all". */
export function useScope(): [Scope, (next: Scope) => void] {
  const scope = useSyncExternalStore<Scope>(subscribeScope, getScope, () => "all");
  return [scope, setScope];
}

export function ScopeFilter({
  scope,
  onChange,
}: {
  scope: Scope;
  onChange: (next: Scope) => void;
}) {
  const t = useT();
  return (
    <div
      role="group"
      aria-label={t.brief.scope.label}
      className="flex flex-wrap items-center gap-1.5"
    >
      {SCOPES.map((value) => {
        const active = scope === value;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(value)}
            className={cn(
              "min-touch rounded-full border px-3 text-xs font-medium",
              active
                ? "border-transparent bg-primary text-primary-foreground"
                : "border-border text-muted-foreground hover:bg-muted"
            )}
          >
            {t.brief.scope[value]}
          </button>
        );
      })}
    </div>
  );
}
