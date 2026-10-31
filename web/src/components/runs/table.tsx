import { Link } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight } from "lucide-react";
import { Fragment, type ReactNode, useEffect, useRef } from "react";

import type { RunSummary } from "@/api/runs";
import { shortId } from "@/lib/format";
import { presetText } from "@/lib/monitor";
import { cn } from "@/lib/utils";

import { Cost, Duration, Flags, Reward, Started, Status, Steps, Verdict } from "./cells";

export type Row =
  { kind: "run"; run: RunSummary; child: boolean } | { kind: "note"; key: string; text: string };

export const ROW_HEIGHT = 28;

// Below md only status, scenario and flags stay.
const grid =
  "grid grid-cols-[2.25rem_6.5rem_minmax(0,1fr)_4.5rem] md:grid-cols-[2.25rem_6.5rem_minmax(12rem,1fr)_7.5rem_5rem_3.5rem_4.5rem_4rem_5.5rem_5.5rem_8rem] gap-x-3 px-2";
const wide = "max-md:hidden";
const num = "text-right tabular-nums";
const at = "absolute inset-x-0 top-0 h-7";

const columns: { label: string; className?: string; hidden?: boolean }[] = [
  { label: "Reviews", hidden: true },
  { label: "Status" },
  { label: "Scenario" },
  { label: "Started", className: wide },
  { label: "Duration", className: cn(wide, num) },
  { label: "Steps", className: cn(wide, num) },
  { label: "Cost", className: cn(wide, num) },
  { label: "Reward", className: cn(wide, num) },
  { label: "Flags", className: num },
  { label: "Verdict", className: cn(wide, num) },
  { label: "Runner", className: wide },
];

export function RunsTable({
  rows,
  now,
  expanded,
  onToggle,
  onNearEnd,
  footer,
}: {
  rows: readonly Row[];
  now: number;
  expanded: ReadonlySet<string>;
  onToggle: (runId: string) => void;
  onNearEnd: () => void;
  footer: ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  // The compiler leaves this component alone; rows get plain numbers, so they still memoize.
  // oxlint-disable-next-line react/incompatible-library
  const v = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    getItemKey: (i) => {
      const row = rows[i];
      return row === undefined ? i : row.kind === "run" ? row.run.run_id : row.key;
    },
  });
  const items = v.getVirtualItems();
  const last = items.at(-1)?.index ?? -1;
  useEffect(() => {
    if (last >= rows.length - 20) onNearEnd();
  }, [last, rows.length, onNearEnd]);

  return (
    <div ref={scroller} className="min-h-0 flex-1 overflow-auto rounded-sm border">
      <div role="table" aria-label="Runs" aria-rowcount={rows.length + 1}>
        <div role="rowgroup" className="bg-background sticky top-0 z-20 border-b">
          <div role="row" className={cn(grid, "text-muted-foreground h-8 items-center")}>
            {columns.map((c) => (
              <span key={c.label} role="columnheader" className={cn("truncate", c.className)}>
                {c.hidden ? <span className="sr-only">{c.label}</span> : c.label}
              </span>
            ))}
          </div>
        </div>
        <div role="rowgroup" className="relative" style={{ height: v.getTotalSize() }}>
          {items.map((item) => {
            const row = rows[item.index];
            if (row === undefined) return null;
            return row.kind === "run" ? (
              <RunRow
                key={item.key}
                run={row.run}
                child={row.child}
                index={item.index + 2}
                top={item.start}
                now={row.run.status === "running" ? now : 0}
                open={expanded.has(row.run.run_id)}
                onToggle={onToggle}
              />
            ) : (
              <div
                key={item.key}
                role="row"
                aria-rowindex={item.index + 2}
                className={cn(grid, at, "text-muted-foreground items-center")}
                style={{ transform: `translateY(${item.start}px)` }}
              >
                <span role="cell" className="col-span-full pl-[8.75rem]">
                  {row.text}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      {footer}
    </div>
  );
}

function RunRow({
  run,
  child,
  index,
  top,
  now,
  open,
  onToggle,
}: {
  run: RunSummary;
  child: boolean;
  index: number;
  top: number;
  now: number;
  open: boolean;
  onToggle: (runId: string) => void;
}) {
  const extra: [string, ReactNode][] = [];
  if (run.variant) extra.push(["variant", run.variant]);
  if (run.series !== null && run.series_index !== null) {
    extra.push([
      "series",
      <Link
        key="series"
        to="/runs"
        search={{ series: run.series }}
        title={`Runs of series ${run.series}`}
        className="hover:text-foreground relative z-10 underline-offset-2 hover:underline"
      >
        #{run.series_index}
      </Link>,
    ]);
  }
  const preset = run.kind === "monitor" ? presetText(run) : null;
  if (preset) extra.push(["preset", preset]);
  if (run.kind === "monitor" && run.origin && !child)
    extra.push(["origin", `review of ${shortId(run.origin)}`]);
  if (run.reason && run.started_at === null) extra.push(["reason", run.reason]);
  const name = child
    ? run.kind === "selftest.case"
      ? "Case"
      : "Review"
    : run.kind === "selftest"
      ? `Self-test of ${run.scenario ?? "a scenario"}`
      : (run.scenario ?? run.kind);
  const holds = run.kind === "selftest" ? "cases" : run.reviews === 1 ? "review" : "reviews";
  return (
    <div
      role="row"
      aria-rowindex={index}
      className={cn(
        grid,
        at,
        "hover:bg-accent has-[a:focus-visible]:bg-accent items-center",
        child && "bg-muted/50",
      )}
      style={{ transform: `translateY(${top}px)` }}
    >
      <span role="cell" className="relative z-10 flex">
        {(run.reviews > 0 || run.kind === "selftest") && !child && (
          <button
            type="button"
            onClick={() => onToggle(run.run_id)}
            aria-expanded={open}
            aria-label={run.kind === "selftest" ? "Cases" : `${run.reviews} ${holds}`}
            className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 flex items-center gap-0.5 rounded-sm tabular-nums outline-none focus-visible:ring-2"
          >
            <ChevronRight aria-hidden className={cn("size-3.5", open && "rotate-90")} />
            {run.kind !== "selftest" && run.reviews}
          </button>
        )}
      </span>
      <span role="cell">
        <Status run={run} />
      </span>
      <span role="cell" className={cn("flex min-w-0 items-baseline gap-2", child && "pl-4")}>
        <Link
          to="/runs/$runId"
          params={{ runId: run.run_id }}
          className="max-w-full shrink-0 truncate outline-none after:absolute after:inset-0"
        >
          {name}
        </Link>
        {extra.length > 0 && (
          <span className="text-muted-foreground truncate">
            {extra.map(([key, part], i) => (
              <Fragment key={key}>
                {i > 0 && " · "}
                {part}
              </Fragment>
            ))}
          </span>
        )}
      </span>
      <span role="cell" className={cn(wide, "text-muted-foreground tabular-nums")}>
        <Started run={run} />
      </span>
      <span role="cell" className={cn(wide, num)}>
        <Duration run={run} now={now} />
      </span>
      <span role="cell" className={cn(wide, num)}>
        <Steps run={run} />
      </span>
      <span role="cell" className={cn(wide, num)}>
        <Cost run={run} />
      </span>
      <span role="cell" className={cn(wide, num)}>
        <Reward run={run} />
      </span>
      <span role="cell" className={num}>
        <Flags run={run} />
      </span>
      <span role="cell" className={cn(wide, num)}>
        <Verdict run={run} />
      </span>
      <span role="cell" className={cn(wide, "text-muted-foreground truncate")}>
        {run.runner}
      </span>
    </div>
  );
}
