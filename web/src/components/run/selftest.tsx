import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { type RunDetail, selftestCasesQuery } from "@/api/run";
import { casesQuery } from "@/api/runs";
import { shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

const LIVE_MS = 2000;

interface Ended {
  case: string;
  name: string;
  run_id: string | null;
  ok: boolean;
  problems: string[];
  notes: string[];
}

// The cases of a self-test: what each proved, or why not, with the run it made.
export function SelfTestCases({ runId, detail }: { runId: string; detail: RunDetail }) {
  const running = detail.run.status === "running" || detail.run.state !== "done";
  const ended = useQuery({
    ...selftestCasesQuery(runId),
    refetchInterval: running ? LIVE_MS : false,
  });
  const runs = useQuery(casesQuery(runId));
  const config = detail.started?.data["config"] as { cases?: unknown } | undefined;
  const planned = Array.isArray(config?.cases) ? config.cases.map(String) : [];
  const results = (ended.data ?? []).map((e) => e.data as unknown as Ended);
  const recorded = new Set(results.map((r) => r.run_id));
  const live = (runs.data?.items ?? []).find((r) => !recorded.has(r.run_id) && r.state !== "done");
  const names = planned.length > 0 ? planned : results.map((r) => r.name);
  const passed = results.filter((r) => r.ok).length;
  const rows: { name: string; state: string; run: string | null; r: Ended | undefined }[] = [];
  let waiting = false;
  for (const name of names) {
    const r = results.find((x) => x.name === name);
    if (r) rows.push({ name, state: r.ok ? "passed" : "failed", run: r.run_id, r });
    else if (running && !waiting) {
      rows.push({ name, state: "running", run: live?.run_id ?? null, r });
      waiting = true;
    } else rows.push({ name, state: running ? "waiting" : "not run", run: null, r });
  }
  return (
    <section aria-label="Cases" className="flex max-w-5xl flex-col gap-2 pt-2">
      <p>
        {results.length === 0 && running
          ? "Waiting for the first case."
          : `${passed} of ${names.length} ${names.length === 1 ? "case" : "cases"} proved what they should.`}
        {detail.run.state === "done" && detail.run.status !== "completed" && detail.run.reason && (
          <span className="text-muted-foreground"> {detail.run.reason}</span>
        )}
      </p>
      <table className="w-full">
        <thead className="text-muted-foreground text-left">
          <tr className="border-b">
            <th className="py-1 pr-4 font-normal">Case</th>
            <th className="py-1 pr-4 font-normal">Result</th>
            <th className="py-1 pr-4 font-normal">Run</th>
            <th className="py-1 font-normal">What it saw</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ name, state, run, r }) => {
            return (
              <tr key={name} className="border-b align-top last:border-0">
                <td className="py-1.5 pr-4">{name}</td>
                <td
                  className={cn(
                    "py-1.5 pr-4",
                    state === "failed" && "font-medium",
                    state === "running" && "text-pen-1",
                    (state === "waiting" || state === "not run") && "text-muted-foreground",
                  )}
                >
                  {state}
                </td>
                <td className="py-1.5 pr-4 font-mono">
                  {run && (
                    <Link
                      to="/runs/$runId"
                      params={{ runId: run }}
                      className="underline-offset-2 hover:underline"
                    >
                      {shortId(run)}
                    </Link>
                  )}
                </td>
                <td className="py-1.5">
                  {r && (
                    <ul className="flex flex-col gap-0.5">
                      {r.problems.map((p) => (
                        <li key={p}>{p}</li>
                      ))}
                      {r.notes.map((n) => (
                        <li key={n} className="text-muted-foreground">
                          {n}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
