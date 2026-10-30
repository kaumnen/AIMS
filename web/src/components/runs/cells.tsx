import type { RunSummary } from "@/api/runs";
import { duration, fullTime, started, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

const labels: Record<string, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  killed: "Killed",
  cancelled: "Cancelled",
  queued: "Queued",
  offered: "Starting",
  claimed: "Starting",
};

export function statusLabel(run: RunSummary): string {
  if (run.status === "running" && run.stale) return "Runner offline";
  return labels[run.status] ?? run.status;
}

export function Status({ run }: { run: RunSummary }) {
  const live = run.status === "running" && !run.stale;
  const waiting = run.state === "queued" || run.state === "offered" || run.state === "claimed";
  const ended = run.status === "completed" || run.status === "cancelled";
  return (
    <span
      className={cn("flex items-center gap-1.5", ended && "text-muted-foreground")}
      title={run.stale ? "The runner stopped reporting" : (run.reason ?? undefined)}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 shrink-0 rounded-full",
          live && "bg-pen-1",
          (waiting || run.stale) && "border-muted-foreground border",
        )}
      />
      {statusLabel(run)}
    </span>
  );
}

// Five ticks filled up to `value`. From 3, likely misbehaviour by the monitor's rubric, the
// ticks are red, so the red grows with how bad it is.
const RED_FROM = 3;

// `quiet` drops the red, for a flag a review set aside.
export function Meter({
  value,
  label,
  quiet = false,
}: {
  value: number;
  label: string;
  quiet?: boolean;
}) {
  return (
    <span role="img" aria-label={label} title={label} className="flex items-center gap-px">
      {[1, 2, 3, 4, 5].map((i) => (
        <span
          key={i}
          className={cn(
            "h-2.5 w-[3px] rounded-[1px]",
            i > value
              ? "bg-grid"
              : quiet
                ? "bg-muted-foreground"
                : i >= RED_FROM
                  ? "bg-flag"
                  : "bg-foreground",
          )}
        />
      ))}
    </span>
  );
}

export function Flags({ run }: { run: RunSummary }) {
  if (run.flags === 0) return null;
  const open = run.max_importance !== null;
  return (
    <span className="flex items-center justify-end gap-1.5">
      <span className={cn(!open && "text-muted-foreground")}>{run.flags}</span>
      {open && (
        <Meter
          value={run.max_importance ?? 0}
          label={`Highest open importance ${run.max_importance} of 5`}
        />
      )}
    </span>
  );
}

export function Verdict({ run }: { run: RunSummary }) {
  const s = run.verdict_severity;
  if (s === null) return null;
  return (
    <span className="flex items-center justify-end gap-1.5">
      <span className={cn(s === 0 && "text-muted-foreground")}>{s}</span>
      <Meter value={s} label={`Judged severity ${s} of 5`} />
    </span>
  );
}

export function Started({ run }: { run: RunSummary }) {
  if (run.started_at === null) return null;
  return <span title={fullTime(run.started_at)}>{started(run.started_at)}</span>;
}

export function Duration({ run, now }: { run: RunSummary; now: number }) {
  if (run.started_at === null) return null;
  const from = Date.parse(run.started_at);
  const to = run.ended_at === null ? now : Date.parse(run.ended_at);
  if (run.ended_at === null && run.status !== "running") return null;
  return <>{duration(to - from)}</>;
}

export function Cost({ run }: { run: RunSummary }) {
  return run.cost_usd === null ? null : <>{usd(run.cost_usd)}</>;
}

export function Reward({ run }: { run: RunSummary }) {
  return run.reward === null ? null : <>{run.reward.toFixed(2)}</>;
}

export function Steps({ run }: { run: RunSummary }) {
  return run.kind === "scenario" && run.started_at !== null ? <>{run.steps}</> : null;
}
