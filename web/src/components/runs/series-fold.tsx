import { ChevronRight } from "lucide-react";

import type { SeriesInfo } from "@/api/series";
import { statusLabel } from "@/components/runs/cells";
import { Input } from "@/components/ui/input";

export const MAX_RUNS = 100;

export interface SeriesDraft {
  name: string;
  runs: number;
  restart: boolean;
}

export const NO_SERIES: SeriesDraft = { name: "", runs: 1, restart: false };

// The run of the series a new start waits for, as the server names it.
export function busyRun(found: SeriesInfo | undefined) {
  if (!found?.busy) return null;
  return found.runs.find((r) => r.run_id === found.busy) ?? null;
}

// Where the runs go, as `aims run --series` prints it.
export function seriesText(found: SeriesInfo | undefined, restart: boolean): string {
  if (found === undefined) return "Checking the series";
  const busy = busyRun(found);
  if (found.busy) {
    const which = busy?.series_index != null ? `#${busy.series_index}` : `run ${found.busy}`;
    const state = busy ? statusLabel(busy).toLowerCase() : "not ended";
    return `Busy: ${which} is ${state}. Start after it ends.`;
  }
  if (restart) return "Starts again at #0 from the scenario's own folders.";
  if (found.runs.length === 0) return "New series; starts at #0.";
  const after = found.runs.find((r) => r.run_id === found.previous)?.series_index;
  if (found.previous === null || after == null) {
    return `No run of it completed; #${found.next_index} starts from the scenario's own folders.`;
  }
  return `Goes on after #${after}; the next run is #${found.next_index}.`;
}

export function SeriesSection({
  draft,
  found,
  unknown = false,
  onChange,
}: {
  draft: SeriesDraft;
  found: SeriesInfo | undefined;
  unknown?: boolean;
  onChange: (draft: SeriesDraft) => void;
}) {
  const name = draft.name.trim();
  const summary = name ? `${name} · ${draft.runs} ${draft.runs === 1 ? "run" : "runs"}` : "None";
  return (
    <details className="group sm:col-span-2" open={name ? true : undefined}>
      <summary className="flex cursor-default list-none items-center gap-2">
        <ChevronRight
          aria-hidden
          className="text-muted-foreground size-3.5 shrink-0 group-open:rotate-90"
        />
        <span className="text-muted-foreground">Series</span>
        <span className="truncate">{summary}</span>
      </summary>
      <div className="mt-2 flex flex-col gap-2">
        <p className="text-muted-foreground">
          Runs one after another; each starts from the series folders and canaries the one before it
          left, once that one completed.
        </p>
        <div className="grid grid-cols-[minmax(0,1fr)_6rem] gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-muted-foreground">Name</span>
            <Input
              value={draft.name}
              maxLength={100}
              placeholder="No series"
              className="h-7 md:text-sm"
              onChange={(e) => onChange({ ...draft, name: e.currentTarget.value })}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-muted-foreground">Runs</span>
            <Input
              type="number"
              min={1}
              max={MAX_RUNS}
              value={draft.runs}
              disabled={!name}
              className="h-7 md:text-sm"
              onChange={(e) => {
                const n = Math.round(Number(e.currentTarget.value));
                const runs = Number.isFinite(n) ? Math.min(MAX_RUNS, Math.max(1, n)) : 1;
                onChange({ ...draft, runs });
              }}
            />
          </label>
        </div>
        {name && (
          <p aria-live="polite" className={found?.busy ? "font-medium" : undefined}>
            {unknown
              ? "The series did not load; the server checks it when the runs start."
              : seriesText(found, draft.restart)}
          </p>
        )}
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={draft.restart}
            disabled={!name}
            onChange={(e) => onChange({ ...draft, restart: e.target.checked })}
            className="accent-foreground"
          />
          Start the series again at #0
        </label>
      </div>
    </details>
  );
}
