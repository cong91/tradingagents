// Module-level locale store shared by React (useSyncExternalStore) and plain
// modules (format helpers, fetch error builders) that cannot call hooks.
//
// Hydration: the server always renders DEFAULT_LOCALE. `current` starts at
// DEFAULT_LOCALE on the client too and only changes after <LocaleSync> has
// mounted (layout effect), so the first client render matches the server HTML.

export const LOCALES = ["en", "vi"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en";
export const LOCALE_STORAGE_KEY = "tradingagents.locale";

/** BCP 47 tag used for Intl number/date formatting. */
export const INTL_LOCALE: Record<Locale, string> = {
  en: "en-US",
  vi: "vi-VN",
};

let current: Locale = DEFAULT_LOCALE;
const listeners = new Set<() => void>();

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

export function getLocale(): Locale {
  return current;
}

export function getServerLocale(): Locale {
  return DEFAULT_LOCALE;
}

export function subscribeLocale(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Stored choice, or null when storage is empty/unavailable (private mode, blocked). */
export function readStoredLocale(): Locale | null {
  try {
    const value = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return isLocale(value) ? value : null;
  } catch {
    return null;
  }
}

function applyDocumentLang(locale: Locale) {
  if (typeof document !== "undefined") document.documentElement.lang = locale;
}

/** Switch the active locale; `persist: false` is used when restoring from storage. */
export function setLocale(next: Locale, { persist = true }: { persist?: boolean } = {}) {
  applyDocumentLang(next);
  if (persist) {
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      // Storage unavailable — the choice lasts for this page session only.
    }
  }
  if (next === current) return;
  current = next;
  listeners.forEach((listener) => listener());
}

/**
 * Inline <head> script: sets <html lang> from storage before first paint so
 * assistive tech sees the right language even before React hydrates.
 */
export const LOCALE_INIT_SCRIPT = `(function(){try{var l=localStorage.getItem(${JSON.stringify(
  LOCALE_STORAGE_KEY
)});if(l===${LOCALES.map((l) => JSON.stringify(l)).join("||l===")})document.documentElement.lang=l}catch(e){}})()`;
