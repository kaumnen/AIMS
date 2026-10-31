import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import type { RunSummary } from "@/api/runs";
import { seriesQuery } from "@/api/series";
import { statusLabel } from "@/components/runs/cells";
import { shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

const MAX_CHIPS = 12;

function dot(run: RunSummary): string {
  if (run.status === "running") return run.stale ? "border-muted-foreground border" : "bg-pen-1";
  if (run.status === "completed") return "bg-foreground";
  if (run.started_at === null && run.state !== "cancelled" && run.state !== "done") {
    return "border-muted-foreground border";
  }
  return "bg-muted-foreground";
}

function More({ name, count }: { name: string; count: number }) {
  return (
    <Link
      to="/runs"
      search={{ series: name }}
      className="text-muted-foreground hover:text-foreground px-1"
    >
      +{count} more
    </Link>
  );
}

// The run this one starts from, by its index once the series is read: the runs of a series
// are queued in the same millisecond, so their short ids are alike.
export function SeriesPrevious({ name, previous }: { name: string; previous: string }) {
  const series = useQuery(seriesQuery(name));
  const index = series.data?.runs.find((r) => r.run_id === previous)?.series_index;
  return (
    <Link
      to="/runs/$runId"
      params={{ runId: previous }}
      title={previous}
      className="hover:text-foreground underline underline-offset-2"
    >
      after {index === undefined || index === null ? `run ${shortId(previous)}` : `#${index}`}
    </Link>
  );
}

// The runs of a series in order, around this one, each a link.
export function SeriesStrip({ name, current }: { name: string; current: string }) {
  const series = useQuery(seriesQuery(name));
  const runs = series.data?.runs ?? [];
  if (runs.length < 2) return null;
  const at = Math.max(
    0,
    runs.findIndex((r) => r.run_id === current),
  );
  const start = Math.min(Math.max(0, at - Math.floor(MAX_CHIPS / 2)), runs.length - MAX_CHIPS);
  const from = Math.max(0, start);
  const shown = runs.slice(from, from + MAX_CHIPS);
  const after = runs.length - from - shown.length;
  return (
    <nav aria-label={`Series ${name}`} className="flex w-full flex-wrap items-center gap-1">
      {from > 0 && <More name={name} count={from} />}
      {shown.map((r) => {
        const here = r.run_id === current;
        return (
          <Link
            key={r.run_id}
            to="/runs/$runId"
            params={{ runId: r.run_id }}
            aria-current={here ? "page" : undefined}
            title={`#${r.series_index} · ${statusLabel(r)}${r.reason ? ` · ${r.reason}` : ""}`}
            className={cn(
              "hover:bg-accent flex items-center gap-1.5 rounded-sm border px-1.5 py-0.5 tabular-nums",
              here ? "border-foreground" : "text-muted-foreground border-transparent",
            )}
          >
            <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", dot(r))} />#
            {r.series_index}
            <span className="sr-only">{statusLabel(r)}</span>
          </Link>
        );
      })}
      {after > 0 && <More name={name} count={after} />}
    </nav>
  );
}
