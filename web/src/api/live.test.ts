import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, runnerInfo } from "@/test/event-source";

import { Live } from "./live";

beforeEach(() => {
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => vi.unstubAllGlobals());

const runner = runnerInfo();

test("one connection hands each message to the listeners of its kind", () => {
  const live = new Live({ runs: true });
  const runners: string[] = [];
  const off = live.on("runner", (m) => runners.push(m.runner.name));
  const ends: string[] = [];
  live.on("end", (m) => ends.push(m.run_id));
  live.start();
  const [source] = FakeEventSource.all;
  expect(source?.url).toBe("/api/stream?runs=true");
  source?.send({ kind: "runner", runner });
  source?.send({ kind: "end", run_id: "r1" });
  off();
  source?.send({ kind: "runner", runner });
  expect(runners).toEqual(["local"]);
  expect(ends).toEqual(["r1"]);
});

test("state goes connecting, open, retrying and a refusal reports closed", () => {
  const onClosed = vi.fn<() => void>();
  const live = new Live({ runs: true }, onClosed);
  const seen: string[] = [];
  live.watch(() => seen.push(live.state));
  live.start();
  const source = FakeEventSource.all[0];
  source?.open();
  source?.fail(false);
  expect(onClosed).not.toHaveBeenCalled();
  source?.open();
  source?.fail(true);
  expect(onClosed).toHaveBeenCalledOnce();
  live.stop();
  expect(seen).toEqual(["open", "retrying", "open", "retrying", "connecting"]);
  expect(source?.readyState).toBe(FakeEventSource.CLOSED);
});

test("starting again replaces the connection", () => {
  const live = new Live({ runs: true });
  live.start();
  live.start();
  expect(FakeEventSource.all.map((s) => s.readyState)).toEqual([
    FakeEventSource.CLOSED,
    FakeEventSource.CONNECTING,
  ]);
});
