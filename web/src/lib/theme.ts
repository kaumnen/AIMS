import { useSyncExternalStore } from "react";

export const themes = ["system", "light", "dark"] as const;
export type Theme = (typeof themes)[number];

const KEY = "aims.theme";
const listeners = new Set<() => void>();
let current: Theme = "system";

export function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

// CSS follows prefers-color-scheme until data-theme names a choice.
export function applyTheme(theme: Theme): void {
  current = theme;
  const root = document.documentElement;
  if (theme === "system") delete root.dataset["theme"];
  else root.dataset["theme"] = theme;
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    // private windows may refuse storage; the choice still holds for this page
  }
  applyTheme(theme);
  for (const fn of listeners) fn();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, () => current);
}
