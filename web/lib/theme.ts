// Theme preference store (light / dark / follow OS). Same hydration contract as
// lib/i18n/locale.ts: the server and the first client render use DEFAULT_THEME;
// <PreferencesSync> restores the stored choice in a layout effect, and the
// inline <head> script applies the `.dark` class before first paint.

export const THEMES = ["light", "dark", "system"] as const;
export type Theme = (typeof THEMES)[number];
export type ResolvedTheme = "light" | "dark";

export const DEFAULT_THEME: Theme = "light";
export const THEME_STORAGE_KEY = "tradingagents.theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

let current: Theme = DEFAULT_THEME;
let resolved: ResolvedTheme = "light";
const listeners = new Set<() => void>();

export function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia(DARK_QUERY).matches;
  } catch {
    return false;
  }
}

function resolve(theme: Theme): ResolvedTheme {
  if (theme === "system") return systemPrefersDark() ? "dark" : "light";
  return theme;
}

/** Re-apply the class on <html> (React's dev remount can clear it). */
export function applyThemeClass(theme: Theme = current) {
  if (typeof document === "undefined") return;
  const next = resolve(theme);
  const root = document.documentElement;
  root.classList.toggle("dark", next === "dark");
  root.style.colorScheme = next;
  if (next !== resolved) {
    resolved = next;
    listeners.forEach((listener) => listener());
  }
}

export function readStoredTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    return null;
  }
}

export function setTheme(next: Theme, { persist = true }: { persist?: boolean } = {}) {
  if (persist) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Storage unavailable — keep the choice for this page session only.
    }
  }
  const changed = next !== current;
  current = next;
  applyThemeClass(next);
  if (changed) listeners.forEach((listener) => listener());
}

/** Follow OS changes while the preference is "system"; returns a cleanup. */
export function watchSystemTheme(): () => void {
  try {
    const query = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      if (current === "system") applyThemeClass("system");
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  } catch {
    return () => {};
  }
}

export function getTheme(): Theme {
  return current;
}

export function getResolvedTheme(): ResolvedTheme {
  return resolved;
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY
)});var d=t==="dark"||(t==="system"&&window.matchMedia(${JSON.stringify(
  DARK_QUERY
)}).matches);if(d){document.documentElement.classList.add("dark");document.documentElement.style.colorScheme="dark"}}catch(e){}})()`;
