import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useRef } from "react";

import type { Event } from "@/api/events";
import {
  activityQuery,
  type ExecActivity,
  isBlob,
  stepEventsQuery,
  type StepInfo,
  type ToolInfo,
} from "@/api/run";
import { summary } from "@/lib/event-text";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

import { KernelFold } from "./kernel";
import { TextOf } from "./text-of";
import { parseArgs } from "./tool-text";

type Data = Record<string, unknown>;
const data = (e: Event | undefined): Data => (e?.data ?? {}) as Data;
const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const MAX_EXECS = 100;

const noteLabels: Record<string, string> = {
  "monitor.flag": "Flag",
  "monitor.action": "Monitor",
  "operator.action": "Operator",
  "sys.denied": "Denied",
  "plant.triggered": "Plant",
  "replay.divergence": "Replay diverged",
  "agent.compacted": "Compacted",
  "agent.held": "Held",
};

// Importance 3 to 5 is likely to clear misbehaviour, so it reads red, like the runs list.
const loud = (e: Event) => e.type === "monitor.flag" && (num(data(e)["importance"]) ?? 0) >= 3;

export type Tone = "red" | "ink";

// What a chosen flag cites: events by seq, and kernel records by the exec that holds them.
export interface Focus {
  seqs: ReadonlySet<number>;
  execs: ReadonlyMap<string, readonly number[]>;
  tone: Tone;
}

const NO_FOCUS: Focus = { seqs: new Set(), execs: new Map(), tone: "ink" };

export const litClass = (tone: Tone | undefined) =>
  tone === undefined
    ? ""
    : cn(
        "-mx-2 rounded-sm px-2 pb-2 ring-1",
        tone === "red" ? "bg-flag/5 ring-flag/40" : "bg-foreground/5 ring-foreground/25",
      );

function Section({
  label,
  aside,
  lit,
  children,
}: {
  label: ReactNode;
  aside?: ReactNode;
  lit?: Tone | undefined;
  children: ReactNode;
}) {
  return (
    <section
      data-evidence={lit ? "" : undefined}
      className={cn("flex flex-col gap-1.5 border-t pt-3", litClass(lit))}
    >
      <h3 className="text-muted-foreground flex items-baseline gap-2">
        <span className="text-foreground font-medium">{label}</span>
        {aside}
      </h3>
      {children}
    </section>
  );
}

// One agent step as it happened: what the agent was told, what the model said, each call it
// made, what ran and what came back.
export function Transcript({
  runId,
  step,
  after,
  last,
  multi,
  focus = NO_FOCUS,
  children,
}: {
  runId: string;
  step: StepInfo;
  after: number;
  last: boolean;
  multi: boolean;
  focus?: Focus;
  // what follows the step, such as the agent's next step while it is written
  children?: ReactNode;
}) {
  const q = useQuery(stepEventsQuery(runId, step, after, last));
  const tools = step.tools ?? [];
  const execs = tools.flatMap((t) => t.execs ?? []).slice(0, MAX_EXECS);
  const activity = useQuery(activityQuery(runId, execs));
  const article = useRef<HTMLElement>(null);
  const lit = (...seqs: (number | undefined)[]) =>
    seqs.some((n) => n !== undefined && focus.seqs.has(n)) ? focus.tone : undefined;
  const focusKey = [...focus.seqs].join();
  useEffect(() => {
    if (!q.isSuccess || focusKey === "") return;
    article.current?.querySelector("[data-evidence]")?.scrollIntoView?.({ block: "center" });
  }, [q.isSuccess, focusKey]);
  if (q.isPending) return <p className="text-muted-foreground">Loading step {step.n}</p>;
  if (q.isError) return <p className="text-muted-foreground">Step {step.n} did not load.</p>;
  const events = q.data;
  const inputs = events.filter((e) => e.type === "agent.input");
  const responses = events.filter(
    (e) => e.type === "model.response" && data(e)["call_id"] === step.call_id,
  );
  const response = responses.at(-1);
  const r = data(response);
  const usage = (r["usage"] ?? {}) as Data;
  const by = (type: string, key: string) =>
    new Map(events.filter((e) => e.type === type).map((e) => [String(data(e)[key]), e]));
  const calls = by("tool.call", "call_id");
  const results = by("tool.result", "call_id");
  const started = by("exec.started", "exec_id");
  const finished = by("exec.finished", "exec_id");
  const notes = events.filter((e) => e.type in noteLabels && e.seq > step.seq_from);
  const answer = events.find((e) => e.type === "agent.finished");
  const said = data(answer)["answer"];
  const submit = tools.find((t) => t.name === "submit");
  const raw = submit ? data(calls.get(submit.call_id))["arguments"] : undefined;
  const repeated =
    typeof said === "string" &&
    typeof raw === "string" &&
    (parseArgs(raw) as Data | null)?.["answer"] === said;
  const scored = events.find((e) => e.type === "task.scored");

  const facts = [
    step.status !== "completed" ? step.status : null,
    num(usage["input_tokens"]) !== undefined ? `${num(usage["input_tokens"])} in` : null,
    num(usage["output_tokens"]) !== undefined ? `${num(usage["output_tokens"])} out` : null,
    num(r["latency_ms"]) !== undefined
      ? `${((num(r["latency_ms"]) ?? 0) / 1000).toFixed(1)} s`
      : null,
    step.cost_usd !== null && step.cost_usd !== undefined ? usd(step.cost_usd) : null,
    responses.length > 1 ? `${responses.length} attempts` : null,
    step.source === "replay" ? "replayed" : null,
  ].filter(Boolean);

  return (
    <article ref={article} className="flex max-w-4xl flex-col gap-4 pb-8">
      <h2 className="flex flex-wrap items-baseline gap-x-3 text-base font-medium">
        Step {step.n}
        {multi && step.agent_id && <span className="text-muted-foreground">{step.agent_id}</span>}
        <span className="text-muted-foreground text-sm font-normal tabular-nums">
          {facts.join(" · ")}
        </span>
      </h2>
      {inputs.map((e) => (
        <Section
          key={e.seq}
          label={data(e)["kind"] === "instruction" ? "Task" : "Input"}
          lit={lit(e.seq)}
        >
          <TextOf runId={runId} value={data(e)["text"]} mono={false} />
        </Section>
      ))}
      <Section
        label="Model"
        aside={typeof r["finish_reason"] === "string" ? r["finish_reason"] : null}
        lit={lit(...responses.map((e) => e.seq))}
      >
        {r["reasoning"] ? (
          <details>
            <summary className="text-muted-foreground cursor-default">Reasoning</summary>
            <TextOf runId={runId} value={r["reasoning"]} mono={false} className="mt-1" />
          </details>
        ) : null}
        {r["text"] ? (
          <TextOf runId={runId} value={r["text"]} mono={false} />
        ) : (
          <p className="text-muted-foreground">
            {tools.length
              ? `No text, ${tools.length} ${tools.length === 1 ? "call" : "calls"}.`
              : "No text."}
          </p>
        )}
        {typeof r["error"] === "string" && <p className="text-flag">{r["error"]}</p>}
      </Section>
      {tools.map((t) => (
        <ToolCall
          key={t.call_id}
          runId={runId}
          tool={t}
          call={calls.get(t.call_id)}
          result={results.get(t.call_id)}
          started={started}
          finished={finished}
          activity={activity.data}
          focus={focus}
        />
      ))}
      {notes.map((e) => (
        <p
          key={e.seq}
          data-evidence={lit(e.seq) ? "" : undefined}
          className={cn(
            "border-l-2 pl-2",
            loud(e) ? "border-flag" : "border-grid text-muted-foreground",
            lit(e.seq) && "bg-foreground/5",
          )}
        >
          <span className="font-medium">{noteLabels[e.type]}</span> {summary(e)}
        </p>
      ))}
      {answer && (
        <Section label="Answer" aside={String(data(answer)["reason"] ?? "")} lit={lit(answer.seq)}>
          {repeated ? (
            <p className="text-muted-foreground">The submit call above.</p>
          ) : (
            <TextOf runId={runId} value={said} mono={false} />
          )}
        </Section>
      )}
      {scored && <Score event={scored} lit={lit(scored.seq)} />}
      {children}
    </article>
  );
}

function ToolCall({
  runId,
  tool,
  call,
  result,
  started,
  finished,
  activity,
  focus,
}: {
  runId: string;
  tool: ToolInfo;
  call: Event | undefined;
  result: Event | undefined;
  started: Map<string, Event>;
  finished: Map<string, Event>;
  activity: Map<string, ExecActivity> | undefined;
  focus: Focus;
}) {
  const raw = data(call)["arguments"];
  const ids = tool.execs ?? [];
  const seqs = [
    tool.seq,
    call?.seq,
    result?.seq,
    ...ids.flatMap((id) => [started.get(id)?.seq, finished.get(id)?.seq]),
  ];
  const cites =
    seqs.some((n) => n !== undefined && focus.seqs.has(n)) || ids.some((id) => focus.execs.has(id));
  const out = data(result);
  const marks = [
    tool.denied ? "denied" : null,
    tool.error ? "error" : null,
    tool.exit_code !== null && tool.exit_code !== undefined ? `exit ${tool.exit_code}` : null,
    tool.end && tool.end !== "exited" ? tool.end : null,
    tool.duration_ms !== null && tool.duration_ms !== undefined ? `${tool.duration_ms} ms` : null,
  ].filter(Boolean);
  const bad = Boolean(tool.denied) || Boolean(tool.error) || (tool.exit_code ?? 0) !== 0;
  return (
    <Section
      label={<span className="font-mono">{tool.name}</span>}
      aside={<span className={cn("tabular-nums", bad && "text-flag")}>{marks.join(" · ")}</span>}
      lit={cites ? focus.tone : undefined}
    >
      {isBlob(raw) ? <TextOf runId={runId} value={raw} /> : <Args raw={raw} />}
      {ids.map((id) => (
        <Exec
          key={id}
          runId={runId}
          execId={id}
          started={started.get(id)}
          finished={finished.get(id)}
          activity={activity?.get(id)}
          focus={focus.execs.get(id)}
          tone={focus.tone}
        />
      ))}
      {result ? (
        <>
          <p className="text-muted-foreground">
            Returned to the agent
            {out["truncated"] ? ", cut short" : ""}
            {out["replayed"] ? ", replayed" : ""}
          </p>
          <TextOf runId={runId} value={out["output"]} />
        </>
      ) : (
        <p className="text-muted-foreground">No result yet.</p>
      )}
    </Section>
  );
}

const long = (v: unknown) => typeof v === "string" && (v.length > 80 || v.includes("\n"));

// Short arguments sit on one line; long or multi-line ones get a block each.
function Args({ raw }: { raw: unknown }) {
  const parsed = typeof raw === "string" ? parseArgs(raw) : raw;
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return typeof raw === "string" && raw ? <pre className="font-mono text-xs">{raw}</pre> : null;
  }
  const entries = Object.entries(parsed as Data);
  const inline = entries.filter(([, v]) => !long(v));
  const blocks = entries.filter(([, v]) => long(v));
  return (
    <div className="flex flex-col gap-1.5">
      {inline.length > 0 && (
        <dl className="flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-xs">
          {inline.map(([k, v]) => (
            <div key={k} className="flex gap-1.5">
              <dt className="text-muted-foreground">{k}</dt>
              <dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {blocks.map(([k, v]) => (
        <div key={k}>
          <p className="text-muted-foreground font-mono text-xs">{k}</p>
          <pre className="max-h-96 overflow-auto rounded-sm border px-2 py-1.5 font-mono text-xs break-words whitespace-pre-wrap">
            {String(v)}
          </pre>
        </div>
      ))}
    </div>
  );
}

function Exec({
  runId,
  execId,
  started,
  finished,
  activity,
  focus,
  tone,
}: {
  runId: string;
  execId: string;
  started: Event | undefined;
  finished: Event | undefined;
  activity: ExecActivity | undefined;
  focus: readonly number[] | undefined;
  tone: Tone;
}) {
  const s = data(started);
  const f = data(finished);
  const stderr = f["stderr"];
  return (
    <div className="border-grid flex flex-col gap-1 border-l pl-3">
      <details>
        <summary className="text-muted-foreground cursor-default">
          Ran {num(f["procs"]) !== undefined ? `${num(f["procs"])} processes` : "a command"}
          {f["spilled"] ? `, ${num(f["spilled"])} records spilled` : ""}
          {f["dropped"] ? `, ${num(f["dropped"])} records dropped` : ""}
        </summary>
        <div className="mt-1 flex flex-col gap-1">
          <TextOf runId={runId} value={s["command"]} />
          {f["stdout"] ? (
            <>
              <p className="text-muted-foreground">Stdout{f["truncated"] ? ", cut short" : ""}</p>
              <TextOf runId={runId} value={f["stdout"]} />
            </>
          ) : null}
          {stderr ? (
            <>
              <p className="text-muted-foreground">Stderr</p>
              <TextOf runId={runId} value={stderr} />
            </>
          ) : null}
        </div>
      </details>
      <KernelFold
        runId={runId}
        execId={execId}
        activity={activity}
        focus={focus ?? []}
        tone={tone}
      />
    </div>
  );
}

function Score({ event, lit }: { event: Event; lit: Tone | undefined }) {
  const d = data(event);
  const metrics = (d["metrics"] ?? {}) as Data;
  const passed = num(metrics["passed"]);
  const total = num(metrics["total"]);
  const reward = num(d["reward"]);
  return (
    <Section label="Verifier" aside={typeof d["error"] === "string" ? d["error"] : null} lit={lit}>
      <p className="tabular-nums">
        {reward === undefined ? "No reward" : `Reward ${reward.toFixed(2)}`}
        {passed !== undefined && total !== undefined ? `, ${passed} of ${total} tests passed` : ""}
      </p>
    </Section>
  );
}
