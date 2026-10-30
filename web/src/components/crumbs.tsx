import { Link, useMatches } from "@tanstack/react-router";
import { useEffect } from "react";

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    crumb?: string;
  }
}

// Where you are, as a trail back up; the tab title follows the last step.
export function Crumbs() {
  const crumbs = useMatches({
    select: (matches) =>
      matches.flatMap((m) =>
        m.staticData.crumb ? [{ to: m.pathname, label: m.staticData.crumb }] : [],
      ),
  });
  const title = crumbs
    .map((c) => c.label)
    .toReversed()
    .join(" · ");
  useEffect(() => {
    document.title = title ? `${title} · AIMS` : "AIMS";
  }, [title]);
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
            <Link to={c.to} className="hover:text-foreground">
              {c.label}
            </Link>
            <span aria-hidden>/</span>
          </span>
        ),
      )}
    </nav>
  );
}
