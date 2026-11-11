import { Link } from "@tanstack/react-router";

import type { RunDetail } from "@/api/run";
import { ended, type RunSummary } from "@/api/runs";
import { useTabTitle } from "@/components/crumbs";
import { Duration, Flags, Started, statusLabel, Verdict } from "@/components/runs/cells";
import { changesText } from "@/lib/budget";
import { shortId, usd } from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

import { RunCommands } from "./commands";
import { agentsOf, RunControls } from "./controls";
import { DeleteRun } from "./delete";
import { useSource } from "./monitor";
import { SeriesPrevious, SeriesStrip } from "./series";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{children}</dd>
    </div>
  );
}

const record = (v: unknown): Record<string, unknown> | undefined =>
  typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : undefined;

// Where a fork cut its origin, as its start recorded it: after a step of each agent, or after
// an event of the origin for a run of several agents.
export function forkOf(detail: RunDetail): { origin: string; seq?: number; text: string } | null {
  const fork = record(record(detail.started?.data["config"])?.["fork"]);
  const origin = fork?.["origin"];
  if (!fork || typeof origin !== "string") return null;
  const cut = fork["cut"];
  if (typeof cut !== "number") return { origin, text: `after step ${String(fork["step"])}` };
  const agent = fork["agent"];
  const n = typeof agent === "string" ? record(fork["steps"])?.[agent] : undefined;
  const at = typeof n === "number" ? `, at step ${n} of ${String(agent)}` : "";
  return { origin, seq: cut, text: `after event ${cut}${at}` };
}

const titles: Partial<Record<string, string>> = {
  monitor: "Review",
  score: "Score",
  selftest: "Self-test",
  "selftest.case": "Self-test case",
  "models.check": "Model check",
};

// What a run is called: its scenario, or what kind of job it is.
export function runTitle(run: RunSummary): string {
  return run.kind === "selftest.case" && run.case
    ? `Self-test case ${run.case}`
    : (titles[run.kind] ?? run.scenario ?? run.kind);
}

// What run this is and how it went, at a glance. A scenario run's flags and verdict are those
// of the monitor the panel shows, its own or a review.
export function RunHeader({
  detail,
  source: asked,
}: {
  detail: RunDetail;
  source?: string | undefined;
}) {
  const run = detail.run;
  const now = useNow(run.status === "running");
  const { source } = useSource(run.run_id, detail, asked);
  const watch = run.kind === "scenario" ? source : { run, review: false };
  const fork = forkOf(detail);
  const title = runTitle(run);
  // An open run puts its status first, so a narrow tab still says when it is held.
  const tab = `${title} ${shortId(run.run_id)}`;
  useTabTitle(ended(run) ? tab : `${statusLabel(run)} · ${tab}`);
  const budget = changesText(detail.budget, agentsOf(detail).length > 1);
  return (
    <header className="flex flex-wrap items-end gap-x-8 gap-y-2 pb-3 print:hidden">
      <div className="min-w-0">
        <h1 className="flex items-baseline gap-2 text-lg font-medium">
          <span className="truncate">{title}</span>
          {run.variant && <span className="text-muted-foreground text-sm">{run.variant}</span>}
          {run.series !== null && run.series_index !== null && (
            <Link
              to="/runs"
              search={{ series: run.series }}
              title={`Runs of series ${run.series}`}
              className="text-muted-foreground hover:text-foreground truncate text-sm underline-offset-2 hover:underline"
            >
              {run.series} #{run.series_index}
            </Link>
          )}
        </h1>
        <p className="text-muted-foreground flex flex-wrap gap-x-2 font-mono text-xs">
          <span>{run.run_id}</span>
          {run.kind !== "scenario" && run.scenario && run.scenario !== run.kind && (
            <span>of {run.scenario}</span>
          )}
          {run.origin && (
            <Link
              to="/runs/$runId"
              params={{ runId: run.origin }}
              className="hover:text-foreground underline underline-offset-2"
            >
              run {shortId(run.origin)}
            </Link>
          )}
          {fork && (
            <span>
              fork of{" "}
              <Link
                to="/runs/$runId"
                params={{ runId: fork.origin }}
                search={fork.seq === undefined ? {} : { seq: fork.seq }}
                className="hover:text-foreground underline underline-offset-2"
              >
                run {shortId(fork.origin)}
              </Link>{" "}
              {fork.text}
            </span>
          )}
          {run.series !== null && detail.series_previous && (
            <SeriesPrevious name={run.series} previous={detail.series_previous} />
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
        {watch && watch.run.flags > 0 && (
          <Fact label={watch.review ? "Review flags" : "Flags"}>
            <Flags run={watch.run} />
          </Fact>
        )}
        {watch && watch.run.verdict_severity !== null && (
          <Fact label={watch.review ? "Review verdict" : "Verdict"}>
            <Verdict run={watch.run} />
          </Fact>
        )}
        {budget && <Fact label="Budget">{budget}</Fact>}
        {run.runner && <Fact label="Runner">{run.runner}</Fact>}
      </dl>
      <div className="ml-auto flex items-start gap-1.5">
        <RunControls detail={detail} />
        <RunCommands run={run} />
        <DeleteRun run={run} title={title} />
      </div>
      {run.series !== null && <SeriesStrip name={run.series} current={run.run_id} />}
    </header>
  );
}
