import { useQuery } from "@tanstack/react-query";

import { modelChecksQuery, type RunDetail } from "@/api/run";
import { ended } from "@/api/runs";
import { cancelledText } from "@/components/runs/cells";
import { cn } from "@/lib/utils";

const LIVE_MS = 2000;

const VERDICT = { ok: "ok", warn: "warning", fail: "failed" } as const;

interface Checked {
  profile: string;
  name: string;
  verdict: keyof typeof VERDICT;
  detail: string;
}

// The verdicts of a model check: each test of each profile, and what it saw.
export function ModelChecks({ runId, detail }: { runId: string; detail: RunDetail }) {
  const running = detail.run.status === "running" || !ended(detail.run);
  const logged = useQuery({
    ...modelChecksQuery(runId),
    refetchInterval: running ? LIVE_MS : false,
  });
  const config = detail.started?.data["config"] as { models?: unknown } | undefined;
  const models = config?.models;
  const planned = models && typeof models === "object" ? Object.keys(models) : [];
  const results = (logged.data ?? []).map((e) => e.data as unknown as Checked);
  const names = planned.length > 0 ? planned : [...new Set(results.map((r) => r.profile))];
  const done = names.filter((n) => results.some((r) => r.profile === n));
  // as Settings judges a profile: failed on any fail, with warnings on any warn
  const verdicts = done
    .map((n) => results.filter((r) => r.profile === n).map((r) => r.verdict))
    .filter((v) => !v.includes("fail"));
  const passed = verdicts.length;
  const warned = verdicts.filter((v) => v.includes("warn")).length;
  let waiting = false;
  return (
    <section aria-label="Checks" className="flex max-w-5xl flex-col gap-2 pt-2">
      <p>
        {detail.run.state === "cancelled"
          ? cancelledText(detail.run)
          : done.length === 0 && running
            ? "Waiting for the first profile."
            : `${passed} of ${names.length} ${names.length === 1 ? "profile" : "profiles"} passed${warned > 0 ? `, ${warned} with warnings` : ""}.`}
        {detail.run.state === "done" && detail.run.status !== "completed" && detail.run.reason && (
          <span className="text-muted-foreground"> {detail.run.reason}</span>
        )}
      </p>
      {/* a check cancelled before it started knows no profiles */}
      {names.length > 0 && (
        <table className="w-full">
          <thead className="text-muted-foreground text-left">
            <tr className="border-b">
              <th className="py-1 pr-4 font-normal">Profile</th>
              <th className="py-1 pr-4 font-normal">Test</th>
              <th className="py-1 pr-4 font-normal">Verdict</th>
              <th className="py-1 font-normal">What it saw</th>
            </tr>
          </thead>
          <tbody>
            {names.flatMap((name) => {
              const mine = results.filter((r) => r.profile === name);
              if (mine.length === 0) {
                const state = !running ? "not run" : waiting ? "waiting" : "running";
                waiting ||= running;
                return [
                  <tr key={name} className="border-b align-top last:border-0">
                    <td className="py-1.5 pr-4">{name}</td>
                    <td className="py-1.5 pr-4" />
                    <td
                      className={cn(
                        "py-1.5 pr-4",
                        state === "running" ? "text-pen-1" : "text-muted-foreground",
                      )}
                    >
                      {state}
                    </td>
                    <td className="py-1.5" />
                  </tr>,
                ];
              }
              return mine.map((r, i) => (
                <tr
                  key={`${name}/${r.name}`}
                  className={cn("align-top", i === mine.length - 1 && "border-b last:border-0")}
                >
                  <td className="py-1.5 pr-4">{i === 0 ? name : ""}</td>
                  <td className="py-1.5 pr-4">{r.name}</td>
                  <td
                    className={cn(
                      "py-1.5 pr-4",
                      r.verdict === "fail" && "font-medium",
                      r.verdict === "ok" && "text-muted-foreground",
                    )}
                  >
                    {VERDICT[r.verdict]}
                  </td>
                  <td className="py-1.5 wrap-anywhere">{r.detail}</td>
                </tr>
              ));
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
