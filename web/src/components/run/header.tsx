import { Link } from "@tanstack/react-router";

import type { RunDetail } from "@/api/run";
import { Duration, Flags, Started, statusLabel, Verdict } from "@/components/runs/cells";
import { shortId, usd } from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

import { RunCommands } from "./commands";
import { RunControls } from "./controls";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{children}</dd>
    </div>
  );
}

// What run this is and how it went, at a glance.
export function RunHeader({ detail }: { detail: RunDetail }) {
  const run = detail.run;
  const now = useNow(run.status === "running");
  const title =
    run.kind === "monitor" ? "Review" : run.kind === "score" ? "Score" : (run.scenario ?? run.kind);
  return (
    <header className="flex flex-wrap items-end gap-x-8 gap-y-2 pb-3">
      <div className="min-w-0">
        <h1 className="flex items-baseline gap-2 text-lg font-medium">
          <span className="truncate">{title}</span>
          {run.variant && <span className="text-muted-foreground text-sm">{run.variant}</span>}
          {run.series_index !== null && (
            <span className="text-muted-foreground text-sm">#{run.series_index}</span>
          )}
        </h1>
        <p className="text-muted-foreground flex flex-wrap gap-x-2 font-mono text-xs">
          <span>{run.run_id}</span>
          {run.kind !== "scenario" && run.scenario && <span>of {run.scenario}</span>}
          {run.origin && (
            <Link
              to="/runs/$runId"
              params={{ runId: run.origin }}
              className="hover:text-foreground underline underline-offset-2"
            >
              run {shortId(run.origin)}
            </Link>
          )}
        </p>
      </div>
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        <Fact label="Status">
          <span className={cn(run.status === "running" && !run.stale && "text-pen-1")}>
            {statusLabel(run)}
          </span>
        </Fact>
        {run.started_at && (
          <Fact label="Started">
            <Started run={run} />
          </Fact>
        )}
        {run.started_at && (
          <Fact label="Duration">
            <Duration run={run} now={now} />
          </Fact>
        )}
        {run.kind === "scenario" && <Fact label="Steps">{run.steps}</Fact>}
        {run.cost_usd !== null && <Fact label="Cost">{usd(run.cost_usd)}</Fact>}
        {run.reward !== null && <Fact label="Reward">{run.reward.toFixed(2)}</Fact>}
        {run.flags > 0 && (
          <Fact label="Flags">
            <Flags run={run} />
          </Fact>
        )}
        {run.verdict_severity !== null && (
          <Fact label="Verdict">
            <Verdict run={run} />
          </Fact>
        )}
        {run.runner && <Fact label="Runner">{run.runner}</Fact>}
      </dl>
      <div className="ml-auto flex items-start gap-1.5">
        <RunControls detail={detail} />
        <RunCommands run={run} />
      </div>
    </header>
  );
}
