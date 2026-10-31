import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";

import { ACK_MS, Channel, type Member } from "./channel";
import type { Event } from "./events";
import { type FromHub, type FromTab, PING_MS, PROTOCOL, SILENT_MS, type Wanted } from "./hub";
import { Live } from "./live";

class FakePort extends EventTarget {
  sent: FromTab[] = [];
  closed = false;
  postMessage(m: FromTab): void {
    this.sent.push(m);
  }
  start(): void {}
  close(): void {
    this.closed = true;
  }
  reply(m: FromHub): void {
    if (!this.closed) this.dispatchEvent(new MessageEvent("message", { data: m }));
  }
}

class FakeWorker extends EventTarget {
  port = new FakePort();
  fail(): void {
    this.dispatchEvent(new Event("error"));
  }
}

const fakeWorker = () => new FakeWorker();

function member(wanted: Wanted) {
  return {
    wanted: vi.fn<() => Wanted>(() => wanted),
    deliver: vi.fn<Member["deliver"]>(),
    shared: vi.fn<Member["shared"]>(),
    closed: vi.fn<Member["closed"]>(),
    fallback: vi.fn<Member["fallback"]>(),
  };
}

const ack = (known = false): FromHub => ({
  t: "ack",
  v: PROTOCOL,
  known,
  state: "open",
  tabs: 2,
});
const list: Wanted = { runs: true, run: [], deltas: [] };

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("hello goes first, subscriptions go up and messages come back to theirs", () => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  const m = member(list);
  expect(channel.add(m)).toBe(1);
  worker.port.reply(ack());
  worker.port.reply({ t: "msg", id: 1, message: { kind: "end", run_id: "r1" } });
  worker.port.reply({ t: "ping" });
  channel.remove(1);
  expect(worker.port.sent).toEqual([
    { t: "hello", v: PROTOCOL },
    { t: "sub", id: 1, wanted: list },
    { t: "pong" },
    { t: "unsub", id: 1 },
  ]);
  expect(m.shared).toHaveBeenCalledWith("open", 2);
  expect(m.deliver).toHaveBeenCalledWith({ kind: "end", run_id: "r1" });
  vi.advanceTimersByTime(ACK_MS);
  expect(m.fallback).not.toHaveBeenCalled();
});

test.each([
  ["no answer", () => vi.advanceTimersByTime(ACK_MS)],
  ["an error", (w: FakeWorker) => w.fail()],
  ["no EventSource in the worker", (w: FakeWorker) => w.port.reply({ t: "unsupported" })],
  ["another protocol", (w: FakeWorker) => w.port.reply({ ...ack(), v: PROTOCOL + 1 } as FromHub)],
])("%s falls back for good", (_, fail) => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  const m = member(list);
  channel.add(m);
  fail(worker);
  expect(m.fallback).toHaveBeenCalledOnce();
  expect(worker.port.closed).toBe(true);
  expect(channel.failed).toBe(true);
  expect(channel.add(member(list))).toBeNull();
});

test("silence leads to a probe; an answer keeps the worker, none falls back", () => {
  vi.setSystemTime(0);
  const worker = fakeWorker();
  const channel = new Channel(worker);
  const m = member(list);
  channel.add(m);
  worker.port.reply(ack());
  const hellos = () => worker.port.sent.filter((s) => s.t === "hello").length;
  vi.advanceTimersByTime(SILENT_MS + PING_MS);
  expect(hellos()).toBe(2);
  worker.port.reply(ack(true));
  expect(worker.port.sent.filter((s) => s.t === "sub")).toHaveLength(1);
  vi.advanceTimersByTime(SILENT_MS + PING_MS);
  expect(hellos()).toBe(3);
  vi.advanceTimersByTime(ACK_MS);
  expect(m.fallback).toHaveBeenCalledOnce();
});

test("a worker that lost this tab gets its subscriptions again, as they stand", () => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  worker.port.reply(ack());
  const m = member({ runs: false, run: [{ id: "r1", after: 3 }], deltas: [] });
  channel.add(m);
  m.wanted.mockReturnValue({ runs: false, run: [{ id: "r1", after: 9 }], deltas: [] });
  worker.port.reply({ t: "gone" });
  worker.port.reply(ack(false));
  expect(worker.port.sent.slice(-2)).toEqual([
    { t: "hello", v: PROTOCOL },
    { t: "sub", id: 1, wanted: { runs: false, run: [{ id: "r1", after: 9 }], deltas: [] } },
  ]);
});

test("pagehide says bye, pageshow hello, sign-out asks for a new stream, closed reaches all", () => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  worker.port.reply(ack());
  const a = member(list);
  const b = member(list);
  channel.add(a);
  channel.add(b);
  channel.hide();
  channel.show();
  channel.reconnect();
  expect(worker.port.sent.slice(-3).map((s) => s.t)).toEqual(["bye", "hello", "reconnect"]);
  worker.port.reply({ t: "closed" });
  expect(a.closed).toHaveBeenCalledOnce();
  expect(b.closed).toHaveBeenCalledOnce();
});

const ev = (seq: number, type: string): Event =>
  ({ seq, type, ts: "", agent_id: "a", data: {} }) as unknown as Event;

test("a shared subscription filters types itself and falls back from what it holds", () => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  worker.port.reply(ack());
  const live = new Live(
    { run: [{ id: "r1", after: 3 }], types: ["agent.step"] },
    undefined,
    () => channel,
  );
  const got: number[] = [];
  live.on("events", (m) => got.push(...m.events.map((e) => e.seq)));
  live.start();
  expect(worker.port.sent.at(-1)).toEqual({
    t: "sub",
    id: 1,
    wanted: { runs: false, run: [{ id: "r1", after: 3 }], deltas: [] },
  });
  expect([live.state, live.sharing]).toEqual(["open", 2]);
  worker.port.reply({
    t: "msg",
    id: 1,
    message: { kind: "events", run_id: "r1", events: [ev(4, "agent.step"), ev(5, "exec.started")] },
  });
  expect(got).toEqual([4]);
  expect(FakeEventSource.all).toHaveLength(0);
  worker.fail();
  expect(FakeEventSource.all.map((s) => s.url)).toEqual([
    "/api/stream?run=r1&after=5&types=agent.step",
  ]);
  expect([live.state, live.sharing]).toEqual(["retrying", null]);
});

test("stopping a shared subscription unsubscribes it; a refusal reaches onClosed", () => {
  const worker = fakeWorker();
  const channel = new Channel(worker);
  worker.port.reply(ack());
  const onClosed = vi.fn<() => void>();
  const live = new Live({ runs: true }, onClosed, () => channel);
  live.start();
  worker.port.reply({ t: "closed" });
  expect(onClosed).toHaveBeenCalledOnce();
  live.stop();
  expect(worker.port.sent.at(-1)).toEqual({ t: "unsub", id: 1 });
  expect([live.state, live.sharing]).toEqual(["connecting", null]);
});
