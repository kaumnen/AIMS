import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import type { Event } from "@/api/events";
import { type CallCost, costsQuery, type RunDetail, sweepsQuery } from "@/api/run";
import { duration, shortId, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

import { TextOf } from "./text-of";

const LIVE_MS = 3000;

const schemaLabel: Record<string, string> = {
  sweep_window: "Window scores",
  sweep_extract: "Extracts",
  sweep_evaluate: "Rescores",
  sweep_proposal: "Proposal scores",
  channel_evaluate: "Channel reviews",
};
const roleLabel: Record<string, string> = {
  sweep: "Sweeps",
  judge: "Judge",
  review: "Resample reviews",
  score: "Scoring",
};

function costLabel(c: CallCost): string {
  if (c.role === null) return "Agents";
  if (c.role === "sweep" && c.schema_name !== null) {
    return schemaLabel[c.schema_name] ?? c.schema_name;
  }
  return roleLabel[c.role] ?? c.role;
}

const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const str = (v: unknown) => (typeof v === "string" && v !== "" ? v : undefined);

function CostLine({ costs }: { costs: readonly CallCost[] }) {
  if (costs.length === 0) return null;
  return (
    <ul aria-label="Cost by caller" className="flex flex-wrap gap-x-4 gap-y-0.5">
      {costs.map((c) => (
        <li
          key={`${c.role}/${c.schema_name}`}
          title={`${c.input_tokens} input and ${c.output_tokens} output tokens`}
        >
          <span className="text-muted-foreground">{costLabel(c)}</span>{" "}
          <span className="tabular-nums">{c.cost_usd === null ? "unpriced" : usd(c.cost_usd)}</span>{" "}
          <span className="text-muted-foreground tabular-nums">
            {c.calls} {c.calls === 1 ? "call" : "calls"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function SweepRow({
  sweep,
  runId,
  origin,
  multi,
}: {
  sweep: Event;
  runId: string;
  origin: string | null;
  multi: boolean;
}) {
  const d = sweep.data;
  const from = num(d["step_from"]) ?? 0;
  const to = num(d["step_to"]) ?? 0;
  const seqTo = num(d["seq_to"]);
  const hits = num(d["hits"]) ?? 0;
  const suspicion = num(d["suspicion"]);
  const history = num(d["history_suspicion"]);
  const window = num(d["window_suspicion"]);
  const tokens = num(d["tokens"]);
  const error = str(d["error"]);
  const range = sweep.agent_id === null ? "Channels" : `steps ${from} to ${to}`;
  const notes = [
    d["escalated"] === true ? "rescored" : null,
    (num(d["collapsed"]) ?? 0) > 0 ? `${num(d["collapsed"])} steps collapsed` : null,
    (num(d["dropped"]) ?? 0) > 0 ? `${num(d["dropped"])} hits cited unseen events` : null,
    (num(d["unverified"]) ?? 0) > 0 ? `${num(d["unverified"])} quotes not found` : null,
  ].filter(Boolean);
  const cell = "py-1.5 pr-4 align-top";
  return (
    <tbody data-sweep={sweep.seq} className="border-b last:border-0">
      <tr>
        {multi && <td className={cell}>{sweep.agent_id ?? ""}</td>}
        <td className={cn(cell, "whitespace-nowrap tabular-nums")}>
          {origin !== null && seqTo !== undefined ? (
            <Link
              to="/runs/$runId"
              params={{ runId: origin }}
              search={{ source: runId, seq: seqTo }}
              title="Open this range on the run it reviewed"
              className="underline underline-offset-2"
            >
              {range}
            </Link>
          ) : (
            range
          )}
        </td>
        <td className={cell}>{str(d["lane"]) ?? ""}</td>
        <td className={cell}>{str(d["trigger"]) ?? ""}</td>
        <td
          className={cn(cell, "tabular-nums")}
          title={
            history !== undefined && window !== undefined
              ? `Mean of ${history} over the run so far and ${window} over this range`
              : undefined
          }
        >
          {suspicion ?? ""}
        </td>
        <td className={cn(cell, "tabular-nums", hits > 0 ? "text-flag font-medium" : "")}>
          {hits}
        </td>
        <td className={cn(cell, "text-muted-foreground tabular-nums")}>
          {tokens === undefined ? "" : tokens.toLocaleString("en-US")}
        </td>
        <td className={cn(cell, "text-muted-foreground tabular-nums")}>
          {duration(num(d["duration_ms"]) ?? 0)}
        </td>
        <td className="py-1.5 text-right align-top tabular-nums">{usd(num(d["cost_usd"]) ?? 0)}</td>
      </tr>
      <tr>
        <td colSpan={multi ? 9 : 8} className="pb-2">
          {error ? (
            <p className="text-flag">Failed: {error}</p>
          ) : (
            <TextOf
              runId={runId}
              value={d["summary"]}
              mono={false}
              className="border-0 px-0 py-0"
            />
          )}
          {notes.length > 0 && <p className="text-muted-foreground">{notes.join(" · ")}</p>}
        </td>
      </tr>
    </tbody>
  );
}

// A review run's own record: what its calls cost and each sweep it made over the run it
// reviewed, oldest first. Its flags and verdict show on that run.
export default function ReviewSweeps({
  runId,
  detail,
  onRaw,
}: {
  runId: string;
  detail: RunDetail;
  onRaw: () => void;
}) {
  const run = detail.run;
  const running = run.status === "running";
  const every = running ? LIVE_MS : false;
  const sweeps = useQuery({ ...sweepsQuery(runId), refetchInterval: every });
  const costs = useQuery({ ...costsQuery(runId), refetchInterval: every });
  const origin = run.kind === "monitor" ? run.origin : null;
  const items = sweeps.data ?? [];
  const multi = new Set(items.flatMap((e) => (e.agent_id === null ? [] : [e.agent_id]))).size > 1;
  const signals = run.preset === "signals" || run.monitor_mode === "signals_only";
  return (
    <section
      aria-label="Sweeps"
      className="flex min-h-0 max-w-6xl flex-col gap-2 overflow-auto pt-2"
    >
      <p className="text-muted-foreground">
        {origin ? (
          <>
            Reviews{" "}
            <Link
              to="/runs/$runId"
              params={{ runId: origin }}
              search={{ source: runId }}
              className="text-foreground underline underline-offset-2"
            >
              run {shortId(origin)}
            </Link>
            ; its flags and verdict show there. Every event is under{" "}
          </>
        ) : (
          "Every event is under "
        )}
        <button
          type="button"
          onClick={onRaw}
          className="text-foreground underline underline-offset-2"
        >
          Raw events
        </button>
        .
      </p>
      <CostLine costs={costs.data ?? []} />
      {sweeps.isPending ? (
        <p className="text-muted-foreground">Loading sweeps</p>
      ) : sweeps.isError ? (
        <p className="text-muted-foreground">Sweeps did not load.</p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground">
          {running || run.state !== "done"
            ? "No sweeps yet."
            : signals
              ? "No sweeps: this review ran signals only."
              : "This review made no sweeps."}
        </p>
      ) : (
        <table className="w-full">
          <thead className="text-muted-foreground text-left">
            <tr className="border-b">
              {multi && <th className="py-1 pr-4 font-normal">Agent</th>}
              <th className="py-1 pr-4 font-normal">Range</th>
              <th className="py-1 pr-4 font-normal">Lane</th>
              <th className="py-1 pr-4 font-normal">Trigger</th>
              <th className="py-1 pr-4 font-normal">Suspicion</th>
              <th className="py-1 pr-4 font-normal">Flags</th>
              <th className="py-1 pr-4 font-normal">Tokens</th>
              <th className="py-1 pr-4 font-normal">Time</th>
              <th className="py-1 text-right font-normal">Cost</th>
            </tr>
          </thead>
          {items.map((e) => (
            <SweepRow key={e.seq} sweep={e} runId={runId} origin={origin} multi={multi} />
          ))}
        </table>
      )}
    </section>
  );
}
