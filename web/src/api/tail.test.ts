import {
  type InfiniteData,
  InfiniteQueryObserver,
  QueryClient,
  QueryObserver,
} from "@tanstack/react-query";
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

// A step whose tool call still runs, unless `done`.
const step = (seq: number, n: number, agent = "a", done = false): StepInfo =>
  ({
    seq,
    n,
    agent_id: agent,
    seq_from: seq - 1,
    seq_to: seq + 1,
    tools: [{ seq, call_id: `t${seq}`, name: "bash", done }],
  }) as unknown as StepInfo;

const event = (seq: number, type: string, data: Record<string, unknown> = {}, agent = "a"): Event =>
  ({ seq, type, ts: "2026-10-31T10:00:00+00:00", agent_id: agent, data }) as unknown as Event;

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

const range = (from: number, to: number, type = "file.access") =>
  Array.from({ length: to - from + 1 }, (_, i) => event(from + i, type));

const rawSeqs = (qc: QueryClient, key: readonly unknown[]) =>
  qc.getQueryData<InfiniteData<RawPage>>(key)?.pages.flatMap((p) => p.items.map((e) => e.seq));

interface Asked {
  url: URL;
  answer: (items: Event[], next?: number | null) => void;
  fail: () => void;
}

// Page reads the test answers by hand.
function heldReads(): Asked[] {
  const asked: Asked[] = [];
  fetch.mockImplementation((request) => {
    const held = Promise.withResolvers<Response>();
    asked.push({
      url: new URL(request.url),
      answer: (items, next = null) =>
        held.resolve(Response.json({ items, next: next === null ? null : String(next) })),
      fail: () =>
        held.resolve(
          Response.json(
            { type: "about:blank", title: "Server error", status: 500 },
            { status: 500, headers: { "content-type": "application/problem+json" } },
          ),
        ),
    });
    return held.promise;
  });
  return asked;
}

const asking = (asked: Asked[], param: string, value: string | null = null) => {
  const found = asked.find((a) =>
    value === null ? a.url.searchParams.has(param) : a.url.searchParams.get(param) === value,
  );
  if (!found) throw new Error(`no read with ${param}`);
  return found;
};

test("events that come while a raw list's first page is read join it once the page lands", async () => {
  const asked = heldReads();
  const qc = new QueryClient();
  const all = rawEventsQuery(RUN, {});
  const execs = rawEventsQuery(RUN, { types: ["exec.started"] });
  const { source, tail } = open(qc, 4);
  const reads = [qc.fetchInfiniteQuery(all), qc.fetchInfiniteQuery(execs)];
  await waitFor(() => expect(asked).toHaveLength(2));
  // the server read the pages up to 4; 5 and 6 are streamed before they land, 4 again too
  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(4, "file.access"), event(5, "exec.started"), event(6, "file.access")],
  });
  flush();
  asking(asked, "types").answer([event(1, "exec.started")]);
  const plain = asked.find((a) => !a.url.searchParams.has("types"));
  plain?.answer(range(0, 4));
  await Promise.all(reads);
  await waitFor(() => expect(rawSeqs(qc, all.queryKey)).toEqual([0, 1, 2, 3, 4, 5, 6]));
  expect(rawSeqs(qc, execs.queryKey)).toEqual([1, 5]);
  source.send({ kind: "events", run_id: RUN, events: [event(7, "exec.started")] });
  flush();
  expect(rawSeqs(qc, all.queryKey)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  expect(rawSeqs(qc, execs.queryKey)).toEqual([1, 5, 7]);
  tail.stop();
});

test("events that come while the next or an earlier page is read join after it", async () => {
  const asked = heldReads();
  const qc = new QueryClient();
  const start = rawEventsQuery(RUN, {});
  const end = rawEventsQuery(RUN, {}, "end");
  qc.setQueryData(start.queryKey, { ...page(range(0, 4), 4), pageParams: [null] });
  qc.setQueryData(end.queryKey, {
    ...page(range(3, 4), null, 3),
    pageParams: [{ before: Number.MAX_SAFE_INTEGER }],
  });
  const { source, tail } = open(qc, 4);
  const reads = [
    new InfiniteQueryObserver(qc, start).fetchNextPage(),
    new InfiniteQueryObserver(qc, end).fetchPreviousPage(),
  ];
  await waitFor(() => expect(asked).toHaveLength(2));
  source.send({ kind: "events", run_id: RUN, events: range(4, 6) });
  flush();
  // the next page found nothing past 4 when it was read
  asking(asked, "after", "4").answer([]);
  asking(asked, "before", "3").answer(range(0, 2));
  await Promise.all(reads);
  await waitFor(() => expect(rawSeqs(qc, start.queryKey)).toEqual([0, 1, 2, 3, 4, 5, 6]));
  expect(rawSeqs(qc, end.queryKey)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  tail.stop();
});

test("a raw list whose read fails takes the held events only if it still reaches the end", async () => {
  const asked = heldReads();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const first = rawEventsQuery(RUN, {});
  const again = rawEventsQuery(RUN, {}, "end");
  qc.setQueryData(again.queryKey, {
    ...page(range(0, 4)),
    pageParams: [{ before: Number.MAX_SAFE_INTEGER }],
  });
  const { source, tail } = open(qc, 4);
  const reads = [
    qc.fetchInfiniteQuery(first).catch(() => undefined),
    new InfiniteQueryObserver(qc, again).refetch(),
  ];
  await waitFor(() => expect(asked).toHaveLength(2));
  source.send({ kind: "events", run_id: RUN, events: range(5, 6) });
  flush();
  for (const a of asked) a.fail();
  await Promise.all(reads);
  await waitFor(() => expect(rawSeqs(qc, again.queryKey)).toEqual([0, 1, 2, 3, 4, 5, 6]));
  expect(qc.getQueryData(first.queryKey)).toBeUndefined();
  tail.stop();
});

test("past a server page of held events a raw list reads on by paging", async () => {
  const asked = heldReads();
  const qc = new QueryClient();
  const all = rawEventsQuery(RUN, {});
  const { source, tail } = open(qc, 4);
  const read = qc.fetchInfiniteQuery(all);
  await waitFor(() => expect(asked).toHaveLength(1));
  source.send({ kind: "events", run_id: RUN, events: range(5, 1005) });
  flush();
  asked[0]?.answer(range(0, 4));
  await read;
  const newer = () => qc.getQueryData<InfiniteData<RawPage>>(all.queryKey)?.pages.at(-1)?.newer;
  await waitFor(() => expect(newer()).toBe(4));
  source.send({ kind: "events", run_id: RUN, events: [event(1006, "file.access")] });
  flush();
  expect(rawSeqs(qc, all.queryKey)).toEqual([0, 1, 2, 3, 4]);
  tail.stop();
});

test("a tail stopped while a raw page is read holds nothing and leaves the cache", async () => {
  const asked = heldReads();
  const qc = new QueryClient();
  const all = rawEventsQuery(RUN, {});
  const { source, tail } = open(qc, 4);
  const read = qc.fetchInfiniteQuery(all);
  await waitFor(() => expect(asked).toHaveLength(1));
  source.send({ kind: "events", run_id: RUN, events: range(5, 6) });
  flush();
  tail.stop();
  expect(qc.getQueryCache().hasListeners()).toBe(false);
  asked[0]?.answer(range(0, 4));
  await read;
  await new Promise((done) => setTimeout(done, 0));
  expect(rawSeqs(qc, all.queryKey)).toEqual([0, 1, 2, 3, 4]);
});

test("a raw list takes the stream a page at a time up to 10,000 events, then reads on", async () => {
  const qc = new QueryClient();
  const last = rawEventsQuery(RUN, {}, "end");
  // the list holds the last full page of the log
  qc.setQueryData(last.queryKey, {
    ...page(range(200, 399), null, 200),
    pageParams: [{ before: Number.MAX_SAFE_INTEGER }],
  });
  const { source, tail } = open(qc, 399);
  const data = () => qc.getQueryData<InfiniteData<RawPage>>(last.queryKey);
  const full = data()?.pages[0];
  source.send({ kind: "events", run_id: RUN, events: range(400, 449) });
  flush();
  // a full page is left as it is: the new events start the next one
  expect(data()?.pages[0]?.items).toBe(full?.items);
  expect(data()?.pages[0]?.newer).toBe(399);
  expect(data()?.pageParams).toEqual([{ before: Number.MAX_SAFE_INTEGER }, { after: 399 }]);
  source.send({ kind: "events", run_id: RUN, events: range(450, 699) });
  flush();
  expect(data()?.pages.map((p) => p.items.length)).toEqual([200, 200, 100]);
  expect(data()?.pageParams.at(-1)).toEqual({ after: 599 });

  source.send({ kind: "events", run_id: RUN, events: range(700, 10_199) });
  flush();
  expect(data()?.pages).toHaveLength(50);
  expect(data()?.pages.every((p) => p.items.length === 200)).toBe(true);
  expect(data()?.pageParams).toHaveLength(50);

  // past 10,000 events the list takes no more: its oldest rows may be the ones in view
  source.send({ kind: "events", run_id: RUN, events: range(10_200, 10_399) });
  flush();
  expect(rawSeqs(qc, last.queryKey)).toEqual(range(200, 10_199).map((e) => e.seq));
  expect(data()?.pages.at(-1)?.newer).toBe(10_199);
  // a reader at the end reads on, which lets the oldest page go; one at the start reads back,
  // which lets the newest go
  rawServer(() => range(0, 10_399));
  const view = new InfiniteQueryObserver(qc, last);
  await view.fetchNextPage();
  expect(rawSeqs(qc, last.queryKey)).toEqual(range(400, 10_399).map((e) => e.seq));
  await view.fetchPreviousPage();
  expect(rawSeqs(qc, last.queryKey)).toEqual(range(200, 10_199).map((e) => e.seq));
  expect(data()?.pages.at(-1)?.newer).toBe(10_199);
  tail.stop();
});

test("a raw page read on from an event can read the events before it", async () => {
  fetch.mockImplementation(async () => Response.json({ items: range(100, 299), next: "299" }));
  const qc = new QueryClient();
  const read = await qc.fetchInfiniteQuery({
    ...rawEventsQuery(RUN, {}),
    initialPageParam: { after: 99 },
  });
  expect(read.pages[0]).toMatchObject({ older: 100, newer: 299 });
});

// Answers raw reads from `log` as the server does: a page of `limit`, read back from `before`
// when only it is given.
function rawServer(log: () => Event[]) {
  fetch.mockImplementation(async (request) => {
    const q = new URL(request.url).searchParams;
    const items = log().filter(
      (e) => e.seq > Number(q.get("after") ?? -1) && e.seq < Number(q.get("before") ?? Infinity),
    );
    const limit = Number(q.get("limit"));
    const back = q.has("before") && !q.has("after");
    const got = back ? items.slice(-limit) : items.slice(0, limit);
    const edge = back ? got[0] : got.at(-1);
    return Response.json({ items: got, next: got.length === limit ? String(edge?.seq) : null });
  });
}

test("a raw list read back from the end reads each page it held again on a refetch", async () => {
  rawServer(() => range(0, 449));
  const qc = new QueryClient();
  const last = rawEventsQuery(RUN, {}, "end");
  await qc.fetchInfiniteQuery(last);
  const view = new InfiniteQueryObserver(qc, last);
  await view.fetchPreviousPage();
  expect(rawSeqs(qc, last.queryKey)).toEqual(range(50, 449).map((e) => e.seq));
  await view.refetch();
  expect(rawSeqs(qc, last.queryKey)).toEqual(range(50, 449).map((e) => e.seq));
});

const lastNewer = (qc: QueryClient, key: readonly unknown[]) =>
  qc.getQueryData<InfiniteData<RawPage>>(key)?.pages.at(-1)?.newer;

test("a raw list kept from an earlier visit reads on to what the run logged since", async () => {
  rawServer(() => range(0, 10));
  const qc = new QueryClient();
  const all = rawEventsQuery(RUN, {});
  // the last visit left the list at 4; the run logged up to 9 meanwhile
  qc.setQueryData(all.queryKey, page(range(0, 4)));
  const { source, tail } = open(qc, 9);
  source.send({ kind: "events", run_id: RUN, events: [event(10, "file.access")] });
  flush();
  expect(rawSeqs(qc, all.queryKey)).toEqual([0, 1, 2, 3, 4]);
  await new InfiniteQueryObserver(qc, all).fetchNextPage();
  expect(rawSeqs(qc, all.queryKey)).toEqual(range(0, 10).map((e) => e.seq));
  source.send({ kind: "events", run_id: RUN, events: [event(11, "file.access")] });
  flush();
  expect(rawSeqs(qc, all.queryKey)).toEqual(range(0, 11).map((e) => e.seq));
  // with no tail, the run goes on past the list: it reads on once shown again
  tail.stop();
  expect(lastNewer(qc, all.queryKey)).toBe(11);
});

test("a raw list kept from an earlier visit whose page is read reads on once it is in", async () => {
  const asked = heldReads();
  const qc = new QueryClient();
  const last = rawEventsQuery(RUN, {}, "end");
  qc.setQueryData(last.queryKey, {
    ...page(range(3, 4), null, 3),
    pageParams: [{ before: Number.MAX_SAFE_INTEGER }],
  });
  const read = new InfiniteQueryObserver(qc, last).fetchPreviousPage();
  await waitFor(() => expect(asked).toHaveLength(1));
  const { tail } = open(qc, 9);
  asking(asked, "before", "3").answer(range(0, 2));
  await read;
  await waitFor(() => expect(lastNewer(qc, last.queryKey)).toBe(4));
  expect(rawSeqs(qc, last.queryKey)).toEqual([0, 1, 2, 3, 4]);
  tail.stop();
});

test("a search at the end of the log reads on by paging once events of its types come", async () => {
  const log = [event(40, "tool.call", { q: "pytest" }), event(41, "exec.started")];
  fetch.mockImplementation(async (request) => {
    const after = Number(new URL(request.url).searchParams.get("after") ?? -1);
    const items = log.filter((e) => e.seq > after && "q" in (e.data as object));
    return Response.json({ items, next: null });
  });
  const qc = new QueryClient();
  const searched = rawEventsQuery(RUN, { q: "pytest" });
  const other = rawEventsQuery(RUN, { q: "pytest", types: ["exec.started"] });
  const none = rawEventsQuery(RUN, { q: "nothing" });
  const { source, tail } = open(qc);
  const observer = new InfiniteQueryObserver(qc, searched);
  await observer.refetch();
  qc.setQueryData(other.queryKey, page([]));
  qc.setQueryData(none.queryKey, page([]));
  expect(lastNewer(qc, searched.queryKey)).toBeNull();
  log.push(event(42, "tool.call", { q: "pytest" }), event(43, "file.access"));
  source.send({ kind: "events", run_id: RUN, events: log.slice(2) });
  flush();
  // only the server can tell which events match: the list has a next page from its last match
  expect(rawSeqs(qc, searched.queryKey)).toEqual([40]);
  expect(lastNewer(qc, searched.queryKey)).toBe(40);
  // no event of its types came
  expect(lastNewer(qc, other.queryKey)).toBeNull();
  // one with no match yet reads from the first event
  expect(lastNewer(qc, none.queryKey)).toBe(-1);
  await observer.fetchNextPage();
  expect(rawSeqs(qc, searched.queryKey)).toEqual([40, 42]);
  expect(lastNewer(qc, searched.queryKey)).toBeNull();
  tail.stop();
});

test("a steps list kept from an earlier visit reads what the run logged since at the start", async () => {
  const qc = new QueryClient();
  qc.setQueryData<InfiniteData<StepPage, number | null>>(stepsQuery(RUN).queryKey, {
    pages: [{ items: [step(10, 1, "a", true), step(20, 2, "a", true)], next: null }],
    pageParams: [null],
  });
  // the run went on and is now held before the tool call of step 4: no step event comes
  const asked = stepReads(() => [
    step(10, 1, "a", true),
    step(20, 2, "a", true),
    step(30, 3, "a", true),
    step(40, 4),
  ]);
  const { tail } = open(qc, 45);
  await waitFor(() => expect(stepSeqs(qc)).toEqual([10, 20, 30, 40]));
  expect(asked.steps).toHaveLength(1);
  expect(asked.steps[0]?.searchParams.get("after")).toBe("20");
  tail.stop();
});

// Answers step reads from `rows` and reads of agent ends with `ends`; the URLs asked, by kind.
function stepReads(rows: () => StepInfo[], ends: Event[] = []) {
  const asked = { steps: [] as URL[], ends: [] as URL[] };
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname.endsWith("/steps")) {
      asked.steps.push(url);
      const after = Number(url.searchParams.get("after") ?? -1);
      return Response.json({ items: rows().filter((r) => r.seq > after), next: null });
    }
    asked.ends.push(url);
    return Response.json({ items: ends, next: null });
  });
  return asked;
}

const stepSeqs = (qc: QueryClient) =>
  qc
    .getQueryData<InfiniteData<StepPage>>(stepsQuery(RUN).queryKey)
    ?.pages.flatMap((p) => p.items.map((s) => s.seq));

test("step events read the rows that can still change once a frame and merge them", async () => {
  const qc = new QueryClient();
  const steps = stepsQuery(RUN);
  const asked = stepReads(() => [step(20, 1, "b"), step(30, 2), step(50, 3)]);
  const { source, tail } = open(qc);
  qc.setQueryData<InfiniteData<StepPage, number | null>>(steps.queryKey, {
    pages: [{ items: [step(10, 1), step(20, 1, "b"), step(30, 2)], next: null }],
    pageParams: [null],
  });
  source.send({ kind: "events", run_id: RUN, events: [event(49, "exec.finished")] });
  source.send({ kind: "events", run_id: RUN, events: [event(50, "agent.step")] });
  flush();
  await waitFor(() => expect(stepSeqs(qc)).toEqual([10, 20, 30, 50]));
  expect(asked.steps).toHaveLength(1);
  expect(asked.steps[0]?.pathname).toBe(`/api/runs/${RUN}/steps`);
  expect(asked.steps[0]?.searchParams.get("after")).toBe("10");
  tail.stop();
});

// Step rows of agents a and b, read after the tail opened: b was killed while it held before its
// tool call, which never runs; a goes on.
function killedB(qc: QueryClient) {
  qc.setQueryData<InfiniteData<StepPage, number | null>>(stepsQuery(RUN).queryKey, {
    pages: [{ items: [step(10, 1, "b"), step(20, 1, "a", true), step(30, 2)], next: null }],
    pageParams: [null],
  });
  // each landed read writes the steps once
  return (n: number) =>
    waitFor(() => expect(qc.getQueryState(stepsQuery(RUN).queryKey)?.dataUpdateCount).toBe(n + 1));
}

test("an agent that ended with a tool call under way leaves the tail once its rows are read", async () => {
  const qc = new QueryClient();
  let rows = [step(10, 1, "b"), step(20, 1, "a", true), step(30, 2)];
  const asked = stepReads(() => rows);
  const { source, tail } = open(qc);
  const landed = killedB(qc);
  source.send({
    kind: "events",
    run_id: RUN,
    events: [event(40, "agent.finished", {}, "b"), event(41, "exec.started")],
  });
  flush();
  await landed(1);
  // that read began after b's end, so b's last row is final once it is in
  expect(asked.steps[0]?.searchParams.has("after")).toBe(false);
  rows = [step(30, 2, "a", true), step(50, 3)];
  source.send({ kind: "events", run_id: RUN, events: [event(50, "agent.step")] });
  flush();
  await landed(2);
  expect(asked.steps[1]?.searchParams.get("after")).toBe("20");
  expect(stepSeqs(qc)).toEqual([10, 20, 30, 50]);
  tail.stop();
});

test("an agent that ended before the stream opened is read once, then leaves the tail", async () => {
  const qc = new QueryClient();
  const asked = stepReads(
    () => [step(10, 1, "b"), step(20, 1, "a", true), step(30, 2)],
    [event(12, "agent.finished", {}, "b")],
  );
  const { source, tail } = open(qc);
  const landed = killedB(qc);
  for (const seq of [41, 42, 43]) {
    source.send({ kind: "events", run_id: RUN, events: [event(seq, "exec.started")] });
    flush();
    // oxlint-disable-next-line no-await-in-loop
    await landed(seq - 40);
  }
  // the first step read began before b's end was known; the next one settles b
  expect(asked.steps.map((u) => u.searchParams.get("after"))).toEqual([null, null, "20"]);
  expect(asked.ends).toHaveLength(1);
  expect(asked.ends[0]?.searchParams.getAll("types")).toEqual(["agent.finished"]);
  expect(stepSeqs(qc)).toEqual([10, 20, 30]);
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

test("a last row with every tool result in, or one of an agent whose end was read, is final", () => {
  // b took one step and submitted; a went on for 700 steps and runs a tool now
  const rows = [
    step(2, 1, "b", true),
    ...Array.from({ length: 700 }, (_, i) => step(4 + i * 2, i + 1, "a", i < 699)),
  ];
  expect(tailStart(rows)).toBe(rows.at(-2)?.seq);
  // between a's steps nothing can change: the tail reads on past the last row
  const idle = rows.map((r) => ({ ...r, tools: [] }));
  expect(tailStart(idle)).toBe(rows.at(-1)?.seq);
  // b ended while its tool call was held, so that call has no result
  const held = [step(2, 1, "b"), ...rows.slice(1)];
  expect(tailStart(held)).toBeNull();
  expect(tailStart(held, new Set(["b"]))).toBe(rows.at(-2)?.seq);
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

test("a step event while the first or the next steps page is read reads on once it is in", async () => {
  for (const first of [true, false]) {
    const qc = new QueryClient();
    const steps = stepsQuery(RUN);
    const held = Promise.withResolvers<Response>();
    fetch.mockReset();
    fetch.mockImplementationOnce(() => held.promise);
    let read: Promise<unknown>;
    if (first) read = qc.prefetchInfiniteQuery(steps);
    else {
      qc.setQueryData<InfiniteData<StepPage, number | null>>(steps.queryKey, {
        pages: [{ items: [step(10, 1, "a", true)], next: 10 }],
        pageParams: [null],
      });
      read = new InfiniteQueryObserver(qc, steps).fetchNextPage();
    }
    // oxlint-disable-next-line no-await-in-loop
    await waitFor(() => expect(qc.getQueryState(steps.queryKey)?.fetchStatus).toBe("fetching"));
    const asked = stepReads(() => [step(10, 1, "a", true), step(30, 2), step(50, 3)]);
    const { source, tail } = open(qc);
    source.send({ kind: "events", run_id: RUN, events: [event(50, "agent.step")] });
    flush();
    expect(asked.steps).toHaveLength(0);
    // the page was read before step 50 was logged
    const items = first ? [step(10, 1, "a", true), step(30, 2)] : [step(30, 2)];
    held.resolve(Response.json({ items, next: null }));
    // oxlint-disable-next-line no-await-in-loop
    await read;
    // oxlint-disable-next-line no-await-in-loop
    await waitFor(() => expect(stepSeqs(qc)).toEqual([10, 30, 50]));
    expect(asked.steps.map((u) => u.searchParams.get("after"))).toEqual(["10"]);
    tail.stop();
  }
});
