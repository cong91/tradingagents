"use client";

import { useEffect } from "react";

type NavigateEvent = Event & {
  navigationType: string;
  destination: { url: string; sameDocument: boolean };
};

type Navigation = EventTarget;

export function useUnsavedNavigation(unsaved: boolean, message: string) {
  useEffect(() => {
    if (!unsaved) return;

    const samePage = (url: URL) =>
      url.origin === window.location.origin &&
      url.pathname === window.location.pathname &&
      url.search === window.location.search;

    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const warnLink = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const element = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(element instanceof HTMLAnchorElement) || element.target === "_blank" || element.hasAttribute("download")) return;
      if (samePage(new URL(element.href, window.location.href))) return;
      if (!window.confirm(message)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    const warnTraversal = (event: Event) => {
      const navigation = event as NavigateEvent;
      if (navigation.navigationType !== "traverse" || !navigation.destination.sameDocument ||
          !event.cancelable || samePage(new URL(navigation.destination.url))) return;
      if (!window.confirm(message)) event.preventDefault();
    };

    // Older browsers lack this API; non-cancelable traversals cannot be stopped by popstate.
    const navigation = (window as Window & { navigation?: Navigation }).navigation;
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", warnLink, true);
    navigation?.addEventListener("navigate", warnTraversal);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", warnLink, true);
      navigation?.removeEventListener("navigate", warnTraversal);
    };
  }, [message, unsaved]);
}
