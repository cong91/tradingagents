"use client";

import { useLayoutEffect } from "react";

import { getLocale, readStoredLocale, setLocale } from "@/lib/i18n/locale";
import { getTheme, readStoredTheme, setTheme, watchSystemTheme } from "@/lib/theme";

/**
 * Restores the stored language/theme right after hydration (before paint).
 * Runs once in the root layout; also re-applies <html lang>/.dark after React's
 * dev Strict Mode remount resets attributes set by the inline <head> script.
 */
export function PreferencesSync() {
  useLayoutEffect(() => {
    setLocale(readStoredLocale() ?? getLocale(), { persist: false });
    setTheme(readStoredTheme() ?? getTheme(), { persist: false });
    return watchSystemTheme();
  }, []);
  return null;
}
