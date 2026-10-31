import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { runnerInfo } from "@/test/event-source";

import type { Event } from "./events";
import { COALESCE_MS, type FromHub, Hub, type Open, PROTOCOL, type SourceEvents } from "./hub";
import type { StreamMessage } from "./stream";

interface Source {
  url: string;
  on: SourceEvents;
  closed: boolean;
}

let sources: Source[] = [];
const open: Open = (url, on) => {
  const source: Source = { url, on, closed: false };
  sources.push(source);
  return { close: () => (source.closed = true) };
};

class Tab {
  got: FromHub[] = [];
  send(m: FromHub): void {
    this.got.push(m);
  }
  // the stream messages each subscription got, then forgotten
  take(id = 1): StreamMessage[] {
    const out = this.got.flatMap((m) => (m.t === "msg" && m.id === id ? [m.message] : []));
    this.got = this.got.filter((m) => !(m.t === "msg" && m.id === id));
    return out;
  }
  last(): FromHub | undefined {
    return this.got.at(-1);
  }
}

const ev = (seq: number): Event =>
  ({ seq, type: "agent.step", ts: "", agent_id: "a", data: {} }) as unknown as Event;
const events = (run_id: string, ...seqs: number[]): StreamMessage => ({
  kind: "events",
  run_id,
  events: seqs.map(ev),
});
const delta = (run_id: string, text: string) => ({
  run_id,
  agent_id: "a",
  call_id: "c1",
  attempt: 1,
  kind: "text" as const,
  text,
  index: null,
  ms: 0,
});
const seqs = (ms: StreamMessage[]) =>
  ms.flatMap((m) => (m.kind === "events" ? m.events.map((e) => e.seq) : []));

function push(source: Source | undefined, m: StreamMessage, id = ""): void {
  source?.on.message(JSON.stringify(m), id);
}

function join(hub: Hub, tab: Tab): void {
  hub.receive(tab, { t: "hello", v: PROTOCOL });
}

const settle = () => vi.advanceTimersByTime(COALESCE_MS);

beforeEach(() => {
  sources = [];
  vi.useFakeTimers();
});

afterEach(() => vi.useRealTimers());

test("tabs share one stream and each subscription gets only what it asked for", () => {
  const hub = new Hub(open);
  const list = new Tab();
  const page = new Tab();
  join(hub, list);
  join(hub, page);
  hub.receive(list, { t: "sub", id: 1, wanted: { runs: true, run: [], deltas: [] } });
  hub.receive(page, {
    t: "sub",
    id: 1,
    wanted: { runs: false, run: [{ id: "r1", after: -1 }], deltas: ["r1"] },
  });
  settle();
  expect(sources.map((s) => s.url)).toEqual(["/api/stream?runs=true&run=r1&after=-1&deltas=r1"]);
  const source = sources[0];
  push(source, events("r1", 0, 1, 2));
  push(source, { kind: "runner", runner: runnerInfo() });
  push(source, { kind: "delta", deltas: [delta("r1", "a"), delta("r2", "b")] });
  push(source, { kind: "deltas_dropped", run_id: null, count: 3 });
  expect(list.take().map((m) => m.kind)).toEqual(["runner"]);
  const got = page.take();
  expect(seqs(got)).toEqual([0, 1, 2]);
  expect(got.flatMap((m) => (m.kind === "delta" ? m.deltas.map((d) => d.text) : []))).toEqual([
    "a",
  ]);
  expect(got.map((m) => m.kind)).toEqual(["events", "delta", "deltas_dropped"]);
});

test("changes in a burst reconnect once, resuming the run list from its version", () => {
  const hub = new Hub(open);
  const tab = new Tab();
  join(hub, tab);
  hub.receive(tab, { t: "sub", id: 1, wanted: { runs: true, run: [], deltas: [] } });
  settle();
  push(sources[0], { kind: "runner", runner: runnerInfo() }, "@7");
  hub.receive(tab, { t: "sub", id: 2, wanted: { runs: false, run: [], deltas: ["r1"] } });
  hub.receive(tab, {
    t: "sub",
    id: 3,
    wanted: { runs: false, run: [{ id: "r1", after: 4 }], deltas: [] },
  });
  settle();
  expect(sources.map((s) => [s.url, s.closed])).toEqual([
    ["/api/stream?runs=true", true],
    ["/api/stream?runs=true&run=r1&after=4&deltas=r1&last_event_id=%407", false],
  ]);
});

const run = (after: number) => ({ runs: false, run: [{ id: "r1", after }], deltas: ["r1"] });

test("a tab that joins behind the stream gets the gap, the others only what is new", () => {
  const hub = new Hub(open);
  const first = new Tab();
  const late = new Tab();
  join(hub, first);
  join(hub, late);
  hub.receive(first, { t: "sub", id: 1, wanted: run(-1) });
  settle();
  push(sources[0], events("r1", 0, 1, 2, 3, 4, 5));
  expect(seqs(first.take())).toEqual([0, 1, 2, 3, 4, 5]);
  hub.receive(late, { t: "sub", id: 1, wanted: run(2) });
  settle();
  expect(sources[1]?.url).toBe("/api/stream?run=r1&after=2&deltas=r1");
  // the new stream replays the answer in flight before anything else
  push(sources[1], { kind: "delta", deltas: [delta("r1", "half")] });
  push(sources[1], events("r1", 3, 4, 5, 6, 7));
  const a = first.take();
  const b = late.take();
  expect(seqs(a)).toEqual([6, 7]);
  expect(seqs(b)).toEqual([3, 4, 5, 6, 7]);
  expect(a[0]?.kind).toBe("delta");
  expect(b[0]?.kind).toBe("delta");
});

test("end reaches each subscriber once and the run leaves the stream when they go", () => {
  const hub = new Hub(open);
  const tab = new Tab();
  join(hub, tab);
  hub.receive(tab, {
    t: "sub",
    id: 1,
    wanted: { runs: false, run: [{ id: "r1", after: -1 }], deltas: [] },
  });
  settle();
  push(sources[0], { kind: "end", run_id: "r1" });
  push(sources[0], { kind: "end", run_id: "r1" });
  expect(tab.take().map((m) => m.kind)).toEqual(["end"]);
  hub.receive(tab, { t: "unsub", id: 1 });
  settle();
  expect(sources).toHaveLength(1);
  expect(sources[0]?.closed).toBe(true);
});

test("a list subscriber joining an open stream gets the runners it holds, no reconnect", () => {
  const hub = new Hub(open);
  const first = new Tab();
  const second = new Tab();
  join(hub, first);
  hub.receive(first, { t: "sub", id: 1, wanted: { runs: true, run: [], deltas: [] } });
  settle();
  push(sources[0], { kind: "runner", runner: runnerInfo() });
  join(hub, second);
  hub.receive(second, { t: "sub", id: 4, wanted: { runs: true, run: [], deltas: [] } });
  settle();
  expect(sources).toHaveLength(1);
  expect(second.take(4).map((m) => (m.kind === "runner" ? m.runner.name : m.kind))).toEqual([
    "local",
  ]);
});

test("tabs that stop answering are dropped and told to say hello again", () => {
  vi.setSystemTime(0);
  const hub = new Hub(open);
  const alive = new Tab();
  const crashed = new Tab();
  join(hub, alive);
  join(hub, crashed);
  expect(alive.last()).toEqual({ t: "state", state: "connecting", tabs: 2 });
  vi.setSystemTime(10_000);
  hub.receive(alive, { t: "pong" });
  vi.setSystemTime(16_000);
  hub.tick();
  expect(alive.got.slice(-2)).toEqual([
    { t: "ping" },
    { t: "state", state: "connecting", tabs: 1 },
  ]);
  hub.receive(crashed, { t: "unsub", id: 1 });
  expect(crashed.last()).toEqual({ t: "gone" });
  join(hub, crashed);
  expect(crashed.got.find((m) => m.t === "ack" && !m.known)).toBeDefined();
});

test("a tab on another protocol is answered but never joins", () => {
  const hub = new Hub(open);
  const old = new Tab();
  hub.receive(old, { t: "hello", v: PROTOCOL + 1 });
  expect(old.got).toEqual([{ t: "ack", v: PROTOCOL, known: false, state: "connecting", tabs: 0 }]);
});

test("state reaches every tab; a refusal closes them all until someone subscribes", () => {
  const hub = new Hub(open);
  const tab = new Tab();
  join(hub, tab);
  hub.receive(tab, { t: "sub", id: 1, wanted: { runs: true, run: [], deltas: [] } });
  settle();
  sources[0]?.on.open();
  sources[0]?.on.error(false);
  sources[0]?.on.open();
  sources[0]?.on.error(true);
  expect(
    tab.got.flatMap((m) => (m.t === "state" ? [m.state] : m.t === "closed" ? [m.t] : [])),
  ).toEqual(["connecting", "open", "retrying", "open", "connecting", "closed"]);
  hub.receive(tab, { t: "sub", id: 2, wanted: { runs: true, run: [], deltas: [] } });
  settle();
  expect(sources).toHaveLength(2);
});

test("reconnect opens a new stream though nothing changed, and the old one goes quiet", () => {
  const hub = new Hub(open);
  const tab = new Tab();
  join(hub, tab);
  hub.receive(tab, {
    t: "sub",
    id: 1,
    wanted: { runs: false, run: [{ id: "r1", after: -1 }], deltas: [] },
  });
  settle();
  hub.receive(tab, { t: "reconnect" });
  settle();
  expect(sources.map((s) => s.closed)).toEqual([true, false]);
  push(sources[0], events("r1", 0));
  expect(tab.take()).toEqual([]);
});
