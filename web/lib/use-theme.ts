import { useSyncExternalStore } from "react";

import {
  DEFAULT_THEME,
  getResolvedTheme,
  getTheme,
  subscribeTheme,
  type ResolvedTheme,
  type Theme,
} from "./theme";

export function useTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, getTheme, () => DEFAULT_THEME);
}

/** Effective light/dark — for canvas charts that read CSS tokens once. */
export function useResolvedTheme(): ResolvedTheme {
  return useSyncExternalStore(subscribeTheme, getResolvedTheme, () => "light");
}
