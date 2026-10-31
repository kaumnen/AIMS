import { useInfiniteQuery, useQueries, useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import type { Event } from "@/api/events";
import {
  eventQuery,
  type FlagInfo,
  flagsQuery,
  type RunDetail,
  runQuery,
  type StepInfo,
  stepsQuery,
  stepTypes,
  sweepsQuery,
} from "@/api/run";
import { isProblem } from "@/api/session";
import type { InFlight } from "@/api/tail";
import { ForkHere } from "@/components/run/fork";
import { RunHeader } from "@/components/run/header";
import { HoldBanner } from "@/components/run/hold";
import { InFlightStep, useRunTail } from "@/components/run/live-tail";
import { defaultSource, LIVE, MonitorPanel, useSources } from "@/components/run/monitor";
import { RAW_SEARCH_ID, RawEvents, type RawSearch } from "@/components/run/raw";
import { ScoreTab, type StepsLink, useHarm } from "@/components/run/score";
import { Scrubber } from "@/components/run/scrubber";
import { SelfTestCases } from "@/components/run/selftest";
import { type StepFlags, StepList } from "@/components/run/step-list";
import { type Focus, Transcript } from "@/components/run/transcript";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { shortId } from "@/lib/format";
import {
  cited,
  filterFlags,
  type FlagFilters,
  type FlagLayer,
  flagStep,
  flagTone,
  layers,
  reaches,
} from "@/lib/monitor";
import { stepOf } from "@/lib/timeline";

interface RunSearch extends RawSearch {
  tab?: "raw" | "score";
  score?: string;
  step?: number;
  source?: string;
  flag?: number;
  layer?: FlagLayer;
  category?: string;
}

const LIVE_MS = 3000;
const MAX_EVIDENCE = 20;
const isLayer = (v: unknown): v is FlagLayer => layers.some((l) => l.value === v);

const int = (v: unknown) => {
  const n = Number(v);
  return v !== undefined && v !== "" && Number.isInteger(n) && n >= 0 ? n : undefined;
};

function parseSearch(raw: Record<string, unknown>): RunSearch {
  const out: RunSearch = {};
  if (raw["tab"] === "raw" || raw["tab"] === "score") out.tab = raw["tab"];
  if (typeof raw["score"] === "string" && raw["score"]) out.score = raw["score"];
  const step = int(raw["step"]);
  if (step !== undefined) out.step = step;
  const seq = int(raw["seq"]);
  if (seq !== undefined) out.seq = seq;
  if (typeof raw["q"] === "string" && raw["q"].trim()) out.q = raw["q"].trim();
  if (typeof raw["group"] === "string" && raw["group"]) out.group = raw["group"];
  if (typeof raw["source"] === "string" && raw["source"]) out.source = raw["source"];
  const flag = int(raw["flag"]);
  if (flag !== undefined) out.flag = flag;
  if (isLayer(raw["layer"])) out.layer = raw["layer"];
  if (typeof raw["category"] === "string" && raw["category"]) out.category = raw["category"];
  return out;
}

const without = <K extends keyof RunSearch>(prev: RunSearch, ...keys: K[]): RunSearch => {
  const next = { ...prev };
  for (const k of keys) delete next[k];
  return next;
};

export const Route = createFileRoute("/runs/$runId")({
  staticData: { crumb: (p) => shortId(p["runId"] ?? "") },
  validateSearch: parseSearch,
  loader: ({ context, params }) => {
    void context.queryClient.prefetchQuery(runQuery(params.runId));
  },
  component: Run,
});

// A key typed into a field is text, not a shortcut.
function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return (
    e.metaKey ||
    e.ctrlKey ||
    e.altKey ||
    (t !== null && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)))
  );
}

function Run() {
  const { runId } = Route.useParams();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const detail = useQuery(runQuery(runId));
  const change = (update: (prev: RunSearch) => RunSearch) =>
    void navigate({ search: update, replace: true });
  const run = detail.data?.run;
  const inflight = useRunTail(
    runId,
    run !== undefined && run.state !== "done",
    (run?.events ?? 0) - 1,
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || typing(e)) return;
      e.preventDefault();
      void navigate({ search: (prev) => ({ ...prev, tab: "raw" }), replace: true }).then(() =>
        document.getElementById(RAW_SEARCH_ID)?.focus(),
      );
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  if (detail.isPending) return <p className="text-muted-foreground">Loading run</p>;
  if (detail.isError) {
    const status = isProblem(detail.error) ? detail.error.status : 0;
    const missing = status === 404;
    if (status === 403) {
      return (
        <p className="text-muted-foreground flex items-center gap-1">
          Only operators can open this run: it shows what a scenario hides.
          <Button variant="link" size="sm" render={<Link to="/runs" />}>
            Back to runs
          </Button>
        </p>
      );
    }
    return (
      <p className="text-muted-foreground flex items-center gap-1">
        {missing ? `No run ${runId}.` : "The run did not load."}
        {missing ? (
          <Button variant="link" size="sm" render={<Link to="/runs" />}>
            Back to runs
          </Button>
        ) : (
          <Button variant="link" size="sm" onClick={() => void detail.refetch()}>
            Retry
          </Button>
        )}
      </p>
    );
  }
  const kind = detail.data.run.kind;
  // a case of a self-test is scripted: no model steps, its record is the raw log
  const stepped = kind !== "selftest.case";
  const tab = search.tab ?? (stepped ? "steps" : "raw");
  const scored = kind === "scenario";
  const toSteps = (to: StepsLink) =>
    change((prev) => ({ ...without(prev, "tab", "flag", "seq", "step"), ...to }));
  return (
    <div className="flex h-full flex-col">
      <RunHeader detail={detail.data} />
      <HoldBanner
        detail={detail.data}
        onFlag={(seq) =>
          change((prev) => ({ ...without(prev, "tab", "seq", "step"), flag: seq, source: LIVE }))
        }
      />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          change((prev) => {
            const { tab: _, ...rest } = prev;
            return v === "raw" || v === "score" ? { ...rest, tab: v } : rest;
          })
        }
        className="min-h-0 flex-1"
      >
        <TabsList variant="line" className="border-b">
          {stepped && (
            <TabsTrigger value="steps">{kind === "selftest" ? "Cases" : "Steps"}</TabsTrigger>
          )}
          {scored && <TabsTrigger value="score">Score</TabsTrigger>}
          <TabsTrigger value="raw">Raw events</TabsTrigger>
        </TabsList>
        <TabsContent value="steps" className="flex min-h-0 flex-col pt-1">
          {kind === "selftest" ? (
            <SelfTestCases runId={runId} detail={detail.data} />
          ) : (
            <Steps
              runId={runId}
              detail={detail.data}
              search={search}
              onChange={change}
              inflight={inflight}
            />
          )}
        </TabsContent>
        {scored && (
          <TabsContent value="score" className="flex min-h-0 flex-col pt-1">
            <Score
              runId={runId}
              detail={detail.data}
              search={search}
              onChange={change}
              toSteps={toSteps}
            />
          </TabsContent>
        )}
        <TabsContent value="raw" className="flex min-h-0 flex-col pt-1">
          <RawEvents runId={runId} search={search} onChange={change} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Score({
  runId,
  detail,
  search,
  onChange,
  toSteps,
}: {
  runId: string;
  detail: RunDetail;
  search: RunSearch;
  onChange: (update: (prev: RunSearch) => RunSearch) => void;
  toSteps: (to: StepsLink) => void;
}) {
  const { sources, ready } = useSources(runId, detail);
  const monitor = search.source ?? (ready ? defaultSource(sources) : LIVE);
  return (
    <ScoreTab
      runId={runId}
      detail={detail}
      scoreId={search.score}
      onScore={(id) => onChange((prev) => ({ ...prev, score: id }))}
      monitor={monitor}
      onSteps={toSteps}
    />
  );
}

// The step holding `seq`, among the steps of the agent it belongs to when that is known.
function stepFor(steps: readonly StepInfo[], seq: number, agent: string | null | undefined) {
  const own = agent ? steps.filter((s) => s.agent_id === agent) : [];
  return stepOf(own.length > 0 ? own : steps, seq);
}

function Steps({
  runId,
  detail,
  search,
  onChange,
  inflight,
}: {
  runId: string;
  detail: RunDetail;
  search: RunSearch;
  onChange: (update: (prev: RunSearch) => RunSearch) => void;
  inflight: readonly InFlight[];
}) {
  const q = useInfiniteQuery(stepsQuery(runId));
  const steps = q.data?.pages.flatMap((p) => p.items) ?? [];
  const more = q.hasNextPage;
  const harms = useHarm(runId);
  // An open run shows its latest step until someone picks one.
  const [follow] = useState(() => detail.run.state !== "done");

  // The monitor whose flags and sweeps the panel, the timeline and the list show.
  const { sources, ready } = useSources(runId, detail);
  const sourceId = search.source ?? (ready ? defaultSource(sources) : undefined);
  const source =
    sources.find((s) => s.id === sourceId) ?? (ready || sourceId === LIVE ? sources[0] : undefined);
  const watcher =
    source === undefined || source.run.started_at === null
      ? undefined
      : source.review
        ? source.run.run_id
        : runId;
  const every = source?.run.status === "running" ? LIVE_MS : false;
  const flagsQ = useQuery({
    ...flagsQuery(watcher ?? ""),
    enabled: watcher !== undefined,
    refetchInterval: every,
  });
  const sweepsQ = useQuery({
    ...sweepsQuery(watcher ?? ""),
    enabled: watcher !== undefined,
    refetchInterval: every,
  });
  const flags = flagsQ.data ?? [];
  const filters: FlagFilters = { layer: search.layer, category: search.category };
  const shown = filterFlags(flags, filters);
  const flag = flags.find((f) => f.seq === search.flag);

  // What the chosen flag cites, fetched to learn each event's agent and exec.
  const wanted = [
    ...new Set([
      ...(flag?.evidence ?? []).toSorted((a, b) => a - b).slice(0, MAX_EVIDENCE),
      ...(search.seq === undefined ? [] : [search.seq]),
    ]),
  ];
  const fetched = useQueries({ queries: wanted.map((seq) => eventQuery(runId, seq)) });
  const evidence = new Map<number, Event>();
  for (const r of fetched) if (r.data) evidence.set(r.data.seq, r.data);
  const execs = new Map<string, number[]>();
  for (const e of evidence.values()) {
    const id = (e.data as Record<string, unknown>)["exec_id"];
    if (typeof id !== "string" || stepTypes.includes(e.type)) continue;
    execs.set(id, [...(execs.get(id) ?? []), e.seq]);
  }
  const focus: Focus = {
    seqs: new Set(wanted),
    execs,
    tone: flag && flagTone(flag).red ? "red" : "ink",
  };

  // A step, an event or a flag past the loaded pages: page on until it is in.
  const target =
    search.step ??
    search.seq ??
    (flag ? cited(flag) : follow ? Number.POSITIVE_INFINITY : undefined);
  const loading = target !== undefined && !reaches(steps, target, more);
  const fetching = q.isFetchingNextPage;
  useEffect(() => {
    if (loading && !fetching) void q.fetchNextPage();
  }, [loading, fetching, q]);

  let current: StepInfo | undefined;
  if (search.step !== undefined) current = steps.find((s) => s.seq === search.step);
  else if (search.seq !== undefined && !loading)
    current = stepFor(steps, search.seq, evidence.get(search.seq)?.agent_id);
  else if (flag && !loading) current = flagStep(flag, steps);
  else if (follow && !loading) current = steps.at(-1);
  if (current === undefined && !loading) current = steps[0];
  const at = current === undefined ? -1 : steps.indexOf(current);

  const select = (seq: number) => onChange((prev) => ({ ...prev, step: seq }));
  const chosen = (prev: RunSearch, f: FlagInfo): RunSearch => {
    const step = reaches(steps, cited(f), more) ? flagStep(f, steps) : undefined;
    return { ...without(prev, "seq", "step"), flag: f.seq, ...(step ? { step: step.seq } : {}) };
  };
  const pickFlag = (f: FlagInfo) =>
    onChange((prev) => (prev.flag === f.seq ? without(prev, "flag") : chosen(prev, f)));
  const pickSeq = (seq: number) => {
    const step = reaches(steps, seq, more)
      ? stepFor(steps, seq, evidence.get(seq)?.agent_id)
      : undefined;
    onChange((prev) => ({ ...without(prev, "step"), seq, ...(step ? { step: step.seq } : {}) }));
  };
  const order = shown.toSorted((a, b) => cited(a) - cited(b) || a.seq - b.seq);
  // From the URL as it is now, so a quick second press moves on from the first.
  const walk = (back: boolean) =>
    onChange((prev) => {
      const i = order.findIndex((x) => x.seq === prev.flag);
      const here = steps.find((s) => s.seq === prev.step) ?? current;
      const from = here?.seq_from ?? -1;
      const next =
        i >= 0
          ? order[i + (back ? -1 : 1)]
          : back
            ? order.findLast((x) => cited(x) < from)
            : order.find((x) => cited(x) >= from);
      return next === undefined ? prev : chosen(prev, next);
    });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e)) return;
      if (e.key === "f" || e.key === "F") {
        if (order.length === 0) return;
        e.preventDefault();
        walk(e.key === "F");
        return;
      }
      const move =
        e.key === "j" || e.key === "ArrowDown" ? 1 : e.key === "k" || e.key === "ArrowUp" ? -1 : 0;
      const next = steps[at + move];
      if (move === 0 || next === undefined) return;
      e.preventDefault();
      select(next.seq);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (q.isPending) return <p className="text-muted-foreground">Loading steps</p>;
  if (q.isError) return <p className="text-muted-foreground">Steps did not load.</p>;
  if (steps.length === 0 && detail.run.state !== "done" && detail.run.kind === "scenario") {
    return <p className="text-muted-foreground">No steps yet.</p>;
  }
  if (steps.length === 0) {
    const origin = detail.run.kind === "monitor" ? detail.run.origin : null;
    return (
      <p className="text-muted-foreground flex flex-wrap items-center gap-1">
        {origin ? (
          <>
            This review has no agent steps. Its flags show on
            <Button
              variant="link"
              size="sm"
              className="px-0"
              render={
                <Link to="/runs/$runId" params={{ runId: origin }} search={{ source: runId }} />
              }
            >
              the run it reviewed
            </Button>
            ; its own record is under
          </>
        ) : (
          "This run has no agent steps. Its record is under"
        )}
        <Button
          variant="link"
          size="sm"
          className="px-0"
          onClick={() => onChange((prev) => ({ ...prev, tab: "raw" }))}
        >
          Raw events
        </Button>
      </p>
    );
  }
  const agents = detail.started?.data["agents"];
  const multi = Array.isArray(agents) && agents.length > 1;
  const prev =
    current === undefined
      ? undefined
      : steps.slice(0, at).findLast((s) => s.agent_id === current.agent_id);
  // Each agent's next step while its model writes it, under that agent's latest step.
  const latest = new Map<string, StepInfo>();
  for (const s of steps) latest.set(s.agent_id ?? "", s);
  // a call that already has its step row was logged before the stream opened
  const logged = new Set(steps.map((s) => s.call_id));
  const writing = more
    ? []
    : inflight
        .filter((d) => d.agent !== null && !logged.has(d.call))
        .map((d) => ({ draft: d, n: (latest.get(d.agent ?? "")?.n ?? 0) + 1 }));
  const drafting =
    current !== undefined && latest.get(current.agent_id ?? "")?.seq === current.seq
      ? writing.filter((w) => w.draft.agent === current.agent_id)
      : [];
  const harmed = new Set<number>();
  for (const h of harms) {
    const step = reaches(steps, h.seq, more) ? stepFor(steps, h.seq, h.agent) : undefined;
    if (step) harmed.add(step.seq);
  }
  const marks = new Map<number, StepFlags>();
  for (const f of shown) {
    if (!reaches(steps, cited(f), more)) continue;
    const step = flagStep(f, steps);
    if (!step) continue;
    const m = marks.get(step.seq) ?? { count: 0, red: false };
    marks.set(step.seq, { count: m.count + 1, red: m.red || flagTone(f).red });
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {steps[0] && (
        <Scrubber
          runId={runId}
          detail={detail}
          steps={steps}
          selected={current ?? steps[0]}
          onSelect={select}
          flags={shown}
          sweeps={sweepsQ.data ?? []}
          flag={search.flag}
          onFlag={pickFlag}
          {...(harms[0] ? { harm: harms[0].seq } : {})}
        />
      )}
      <div className="flex min-h-0 flex-1 flex-col gap-4 md:grid md:grid-cols-[20rem_minmax(0,1fr)] md:grid-rows-2 xl:grid-cols-[22rem_minmax(0,1fr)_22rem] xl:grid-rows-1">
        <StepList
          steps={steps}
          selected={current?.seq}
          multi={multi}
          flags={marks}
          harm={harmed}
          pending={writing.map((w) => ({ agent: w.draft.agent, n: w.n }))}
          onSelect={select}
          onNearEnd={() => {
            if (more && !fetching) void q.fetchNextPage();
          }}
          className="max-md:max-h-48 max-md:shrink-0 md:col-start-1 md:row-start-1"
        />
        {source && (
          <MonitorPanel
            sources={sources}
            source={source}
            onSource={(id) =>
              onChange((p) => ({ ...without(p, "flag", "seq", "layer", "category"), source: id }))
            }
            flags={flags}
            loading={flagsQ.isLoading}
            shown={shown}
            filters={filters}
            onFilters={(f) =>
              onChange((p) => ({
                ...without(p, "layer", "category"),
                ...(f.layer ? { layer: f.layer } : {}),
                ...(f.category ? { category: f.category } : {}),
              }))
            }
            selected={search.flag}
            onFlag={pickFlag}
            steps={steps}
            more={more}
            multi={multi}
            evidence={evidence}
            onSeq={pickSeq}
            className="max-md:max-h-72 max-md:shrink-0 md:col-start-1 md:row-start-2 xl:col-start-3 xl:row-start-1"
          />
        )}
        <div className="min-h-0 overflow-auto max-md:min-h-96 max-md:flex-1 md:col-start-2 md:row-span-2 md:row-start-1 xl:row-span-1">
          {current ? (
            <Transcript
              key={current.seq}
              runId={runId}
              step={current}
              after={prev ? prev.seq_to : -1}
              last={at === steps.length - 1 && !more}
              multi={multi}
              focus={focus}
              actions={<ForkHere detail={detail} step={current} />}
            >
              {drafting.map((w) => (
                <InFlightStep key={w.draft.call} draft={w.draft} n={w.n} />
              ))}
            </Transcript>
          ) : (
            <p className="text-muted-foreground">Loading step</p>
          )}
        </div>
      </div>
    </div>
  );
}
