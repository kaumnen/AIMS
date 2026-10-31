import { type InfiniteData, QueryClient } from "@tanstack/react-query";
import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";

import type { Schemas } from "./client";
import type { Event } from "./events";
import { rawEventsQuery, type StepInfo, stepsQuery } from "./run";
import { mergeTail, RunTail, tailStart } from "./tail";

type StepPage = Schemas["StepPage"];
type EventPage = Schemas["EventPage"];
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

const page = (items: Event[], next: string | null = null) => ({
  pages: [{ items, next } as EventPage],
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
  qc.setQueryData(paged.queryKey, page([event(40, "tool.call")], "40"));
  const { source, tail } = open(qc);
  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(41, "exec.started"), event(42, "file.access"), event(43, "exec.started")],
  });
  flush();
  const seqs = (key: readonly unknown[]) =>
    qc.getQueryData<InfiniteData<EventPage>>(key)?.pages.flatMap((p) => p.items.map((e) => e.seq));
  expect(seqs(all.queryKey)).toEqual([40, 41, 42, 43]);
  expect(seqs(execs.queryKey)).toEqual([41, 43]);
  expect(seqs(searched.queryKey)).toEqual([]);
  expect(seqs(paged.queryKey)).toEqual([40]);
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
