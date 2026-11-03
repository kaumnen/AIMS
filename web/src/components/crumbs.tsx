import { Link, useMatches } from "@tanstack/react-router";
import { useEffect, useSyncExternalStore } from "react";

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    crumb?: string | ((params: Record<string, string>) => string);
  }
}

// A list remembers its filters so the crumb back to it lands where you left.
const searches = new Map<string, Record<string, unknown>>();

export function rememberSearch(pathname: string, search: Record<string, unknown>): void {
  searches.set(pathname, search);
}

// A page may name its tab more fully than its crumbs, such as a run by its scenario and status.
let named: string | null = null;
const listeners = new Set<() => void>();
const subscribe = (fn: () => void) => {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
};
const nameTab = (title: string | null) => {
  named = title;
  for (const fn of listeners) fn();
};

export function useTabTitle(title: string): void {
  useEffect(() => {
    nameTab(title);
    return () => nameTab(null);
  }, [title]);
}

// Where you are, as a trail back up; the tab title follows the last step.
export function Crumbs() {
  const crumbs = useMatches({
    select: (matches) =>
      matches.flatMap((m) => {
        const crumb = m.staticData.crumb;
        if (!crumb) return [];
        const label = typeof crumb === "string" ? crumb : crumb(m.params as Record<string, string>);
        return [{ to: m.pathname.replace(/\/$/, "") || "/", label }];
      }),
  });
  const title = crumbs
    .map((c) => c.label)
    .toReversed()
    .join(" · ");
  const own = useSyncExternalStore(subscribe, () => named);
  useEffect(() => {
    const shown = own ?? title;
    document.title = shown ? `${shown} · AIMS` : "AIMS";
  }, [own, title]);
  if (crumbs.length < 2) return null;
  return (
    <nav
      aria-label="Breadcrumb"
      className="text-muted-foreground flex items-center gap-1.5 px-4 pt-3"
    >
      {crumbs.map((c, i) =>
        i === crumbs.length - 1 ? (
          <span key={c.to} aria-current="page" className="text-foreground">
            {c.label}
          </span>
        ) : (
          <span key={c.to} className="flex items-center gap-1.5">
            <Link to={c.to} search={searches.get(c.to) ?? {}} className="hover:text-foreground">
              {c.label}
            </Link>
            <span aria-hidden>/</span>
          </span>
        ),
      )}
    </nav>
  );
}
