import { type InfiniteData, QueryClient, QueryObserver } from "@tanstack/react-query";
import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";
import { run } from "@/test/runs";

import type { Schemas } from "./client";
import type { Event } from "./events";
import {
  flagsQuery,
  rawEventsQuery,
  type RawPage,
  stepEventsQuery,
  type StepInfo,
  stepsQuery,
  watchPolls,
} from "./run";
import { applyRuns } from "./runs";
import { mergeTail, RunTail, tailStart } from "./tail";

type StepPage = Schemas["StepPage"];
type WireDelta = Schemas["WireDelta"];

const RUN = "r1";
const fetch = vi.fn<(request: Request) => Promise<Response>>();
let frames: (() => void)[];

const step = (seq: number, n: number, agent = "a"): StepInfo =>
  ({
    seq,
    n,
    agent_id: agent,
    seq_from: seq - 1,
    seq_to: seq + 1,
    tools: [],
  }) as unknown as StepInfo;

const event = (seq: number, type: string, data: Record<string, unknown> = {}): Event =>
  ({ seq, type, ts: "2026-10-31T10:00:00+00:00", agent_id: "a", data }) as unknown as Event;

const delta = (over: Partial<WireDelta>): WireDelta => ({
  run_id: RUN,
  agent_id: "a",
  call_id: "c1",
  attempt: 1,
  kind: "text",
  text: "",
  index: null,
  ms: 0,
  ...over,
});

beforeEach(() => {
  FakeEventSource.all = [];
  frames = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function open(qc: QueryClient, after = 41) {
  const tail = new RunTail(RUN, qc, (fn) => frames.push(fn));
  tail.start(after);
  const source = FakeEventSource.all.at(-1);
  if (!source) throw new Error("no stream");
  return { tail, source };
}

const page = (items: Event[], newer: number | null = null, older: number | null = null) => ({
  pages: [{ items, older, newer } as RawPage],
  pageParams: [null],
});

const flush = () => {
  for (const fn of frames.splice(0)) fn();
};

test("the stream asks for the run past what the page holds, with its model output", () => {
  const { source, tail } = open(new QueryClient());
  expect(source.url).toBe(`/api/stream?run=${RUN}&after=41&deltas=${RUN}`);
  tail.stop();
});

test("model output builds up per call, a retry starts over and the logged answer ends it", () => {
  const { source, tail } = open(new QueryClient());
  source.send({
    kind: "delta",
    deltas: [
      delta({ kind: "start" }),
      delta({ kind: "reasoning", text: "Think" }),
      delta({ text: "Hel" }),
      delta({ text: "lo" }),
      delta({ kind: "tool_call", text: "bash", index: 0 }),
      delta({ kind: "tool_args", text: '{"cmd":', index: 0 }),
      delta({ kind: "tool_args", text: '"ls"}', index: 0 }),
      delta({ agent_id: "b", call_id: "c9", text: "other" }),
    ],
  });
  expect(tail.inflight).toEqual([]);
  flush();
  expect(tail.inflight.map((d) => [d.agent, d.text])).toEqual([
    ["a", "Hello"],
    ["b", "other"],
  ]);
  expect(tail.inflight[0]).toMatchObject({
    reasoning: "Think",
    tools: [{ name: "bash", args: '{"cmd":"ls"}' }],
  });

  source.send({
    kind: "delta",
    deltas: [delta({ kind: "start", attempt: 2 }), delta({ attempt: 2, text: "Again" })],
  });
  source.send({ kind: "delta", deltas: [delta({ attempt: 1, text: " late" })] });
  source.send({ kind: "deltas_dropped", count: 3, run_id: RUN });
  flush();
  expect(tail.inflight[0]).toMatchObject({ attempt: 2, text: "Again", partial: true });

  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(42, "model.response", { call_id: "c1", retry: true })],
  });
  flush();
  expect(tail.inflight.map((d) => d.call)).toEqual(["c1", "c9"]);
  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(43, "model.response", { call_id: "c1" })],
  });
  flush();
  expect(tail.inflight.map((d) => d.call)).toEqual(["c9"]);
  // the end of a logged call that comes after its answer starts no new draft
  source.send({ kind: "delta", deltas: [delta({ kind: "end", text: "completed" })] });
  flush();
  expect(tail.inflight.map((d) => d.call)).toEqual(["c9"]);
  tail.stop();
});

test("raw tabs at the end of the log take new events of their types; a search does not", () => {
  const qc = new QueryClient();
  const all = rawEventsQuery(RUN, {});
  const execs = rawEventsQuery(RUN, { types: ["exec.started"] });
  const searched = rawEventsQuery(RUN, { q: "pytest" });
  const paged = rawEventsQuery(RUN, { types: ["tool.call"] });
  qc.setQueryData(all.queryKey, page([event(40, "tool.call"), event(41, "exec.started")]));
  qc.setQueryData(execs.queryKey, page([event(41, "exec.started")]));
  qc.setQueryData(searched.queryKey, page([]));
  qc.setQueryData(paged.queryKey, page([event(40, "tool.call")], 40));
  // read from the end back: earlier pages to load, but nothing after the last event
  const last = rawEventsQuery(RUN, {}, "end");
  qc.setQueryData(
    last.queryKey,
    page([event(40, "tool.call"), event(41, "exec.started")], null, 40),
  );
  const { source, tail } = open(qc);
  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(41, "exec.started"), event(42, "file.access"), event(43, "exec.started")],
  });
  flush();
  const seqs = (key: readonly unknown[]) =>
    qc.getQueryData<InfiniteData<RawPage>>(key)?.pages.flatMap((p) => p.items.map((e) => e.seq));
  expect(seqs(all.queryKey)).toEqual([40, 41, 42, 43]);
  expect(seqs(execs.queryKey)).toEqual([41, 43]);
  expect(seqs(searched.queryKey)).toEqual([]);
  expect(seqs(paged.queryKey)).toEqual([40]);
  expect(seqs(last.queryKey)).toEqual([40, 41, 42, 43]);
  tail.stop();
});

test("step events read the rows that can still change once a frame and merge them", async () => {
  const qc = new QueryClient();
  const steps = stepsQuery(RUN);
  qc.setQueryData<InfiniteData<StepPage, number | null>>(steps.queryKey, {
    pages: [{ items: [step(10, 1), step(20, 1, "b"), step(30, 2)], next: null }],
    pageParams: [null],
  });
  fetch.mockResolvedValue(
    Response.json({ items: [step(20, 1, "b"), step(30, 2), step(50, 3)], next: null }),
  );
  const { source, tail } = open(qc);
  source.send({ kind: "events", run_id: RUN, events: [event(49, "exec.finished")] });
  source.send({ kind: "events", run_id: RUN, events: [event(50, "agent.step")] });
  flush();
  await waitFor(() =>
    expect(
      qc
        .getQueryData<InfiniteData<StepPage>>(steps.queryKey)
        ?.pages.flatMap((p) => p.items.map((s) => s.seq)),
    ).toEqual([10, 20, 30, 50]),
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  const url = new URL(fetch.mock.calls[0]?.[0].url ?? "");
  expect(url.pathname).toBe(`/api/runs/${RUN}/steps`);
  expect(url.searchParams.get("after")).toBe("10");
  tail.stop();
});

test("the run's end closes the stream and clears what was in flight", () => {
  const qc = new QueryClient();
  const { source, tail } = open(qc);
  source.send({ kind: "delta", deltas: [delta({ text: "x" })] });
  flush();
  expect(tail.inflight).toHaveLength(1);
  source.send({ kind: "end", run_id: RUN });
  expect(source.readyState).toBe(FakeEventSource.CLOSED);
  expect(tail.inflight).toEqual([]);
});

test("the run's end reads again what a view polled of it, and its sealed summary then nothing", async () => {
  fetch.mockImplementation(async () => Response.json({ items: [], next: null }));
  const qc = new QueryClient();
  const unwatch = watchPolls(qc);
  // a poll too slow to come round within the test
  const view = new QueryObserver(qc, { ...flagsQuery(RUN), refetchInterval: 60_000 });
  const off = view.subscribe(() => undefined);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  const { source, tail } = open(qc);
  source.send({ kind: "end", run_id: RUN });
  expect(qc.getQueryState(flagsQuery(RUN).queryKey)?.fetchStatus).toBe("fetching");
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  applyRuns(qc, [run(RUN, { status: "completed", state: "done" })]);
  expect(qc.getQueryState(flagsQuery(RUN).queryKey)?.isInvalidated).toBe(false);
  expect(fetch).toHaveBeenCalledTimes(2);
  off();
  unwatch();
  tail.stop();
});

test("the tail starts before the earliest row some agent may still change", () => {
  expect(tailStart([])).toBeNull();
  expect(tailStart([step(10, 1)])).toBeNull();
  expect(tailStart([step(10, 1), step(20, 2)])).toBe(10);
  expect(tailStart([step(10, 1), step(20, 1, "b"), step(30, 2), step(40, 3)])).toBe(10);
  const data = {
    pages: [
      { items: [step(10, 1), step(20, 2)], next: 20 },
      { items: [step(30, 3)], next: null },
    ],
    pageParams: [null, 20],
  } as InfiniteData<StepPage, number | null>;
  const merged = mergeTail(data, 20, { items: [step(30, 3), step(40, 4)], next: null });
  expect(merged.pages.map((p) => p.items.map((s) => s.seq))).toEqual([
    [10, 20],
    [30, 40],
  ]);
});

test("the default scheduler calls the browser's frame callback as a plain function", () => {
  const asked: unknown[] = [];
  vi.stubGlobal("requestAnimationFrame", function (this: unknown, fn: () => void) {
    // a browser refuses a call bound to anything but the window
    if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
    asked.push(fn);
    return 1;
  });
  const tail = new RunTail(RUN, new QueryClient());
  tail.start(0);
  FakeEventSource.all.at(-1)?.send({ kind: "delta", deltas: [delta({ text: "x" })] });
  expect(asked).toHaveLength(1);
  tail.stop();
});

test("what a step shows after its last tool event reads again the steps that run to the end", () => {
  const qc = new QueryClient();
  const last = stepEventsQuery(RUN, step(30, 2), 20, true);
  const cut = stepEventsQuery(RUN, step(10, 1), -1, false);
  qc.setQueryData(last.queryKey, []);
  qc.setQueryData(cut.queryKey, []);
  const { source, tail } = open(qc);
  source.send({ kind: "events", run_id: RUN, events: [event(60, "agent.finished")] });
  flush();
  expect(qc.getQueryState(last.queryKey)?.isInvalidated).toBe(true);
  expect(qc.getQueryState(cut.queryKey)?.isInvalidated).toBe(false);
  tail.stop();
});

test("a step event while the steps are read reads on once that read is in", async () => {
  const qc = new QueryClient();
  const steps = stepsQuery(RUN);
  const seqs = () =>
    qc
      .getQueryData<InfiniteData<StepPage>>(steps.queryKey)
      ?.pages.flatMap((p) => p.items.map((s) => s.seq));
  fetch.mockResolvedValueOnce(Response.json({ items: [step(10, 1), step(30, 2)], next: null }));
  await qc.prefetchInfiniteQuery(steps);
  const held = Promise.withResolvers<Response>();
  fetch.mockImplementationOnce(() => held.promise);
  void qc.refetchQueries({ queryKey: steps.queryKey });
  await waitFor(() => expect(qc.getQueryState(steps.queryKey)?.fetchStatus).toBe("fetching"));
  fetch.mockResolvedValue(Response.json({ items: [step(30, 2), step(50, 3)], next: null }));
  const { source, tail } = open(qc);
  source.send({ kind: "events", run_id: RUN, events: [event(50, "agent.step")] });
  flush();
  expect(fetch).toHaveBeenCalledTimes(2);
  held.resolve(Response.json({ items: [step(10, 1), step(30, 2)], next: null }));
  await waitFor(() => expect(seqs()).toEqual([10, 30, 50]));
  expect(fetch).toHaveBeenCalledTimes(3);
  tail.stop();
});
