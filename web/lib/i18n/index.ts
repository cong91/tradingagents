// Lightweight in-house i18n: typed dictionaries (lib/i18n/messages) + a
// useSyncExternalStore hook over the module-level locale store, so plain
// modules (fetch error builders, lib/format.ts) read the same locale.

import { useSyncExternalStore } from "react";

import {
  getLocale,
  getServerLocale,
  INTL_LOCALE,
  subscribeLocale,
  type Locale,
} from "./locale";
import { getMessages, messages, type Messages } from "./messages";

export type { Locale } from "./locale";
export type { Messages } from "./messages";

export function useLocale(): Locale {
  return useSyncExternalStore(subscribeLocale, getLocale, getServerLocale);
}

/** Dictionary for the active locale; re-renders the caller on locale change. */
export function useT(): Messages {
  return messages[useLocale()];
}

export { getMessages };

/** Intl tag ("en-US" / "vi-VN") for the given or active locale. */
export function intlLocale(locale: Locale = getLocale()): string {
  return INTL_LOCALE[locale];
}
