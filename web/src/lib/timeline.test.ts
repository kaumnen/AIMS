import { expect, test } from "vitest";

import type { Event } from "@/api/events";
import type { SampleWindow, StepInfo } from "@/api/run";

import { clocks, columns, offset, stepAt, stepOf, ticks, usage } from "./timeline";

const START = Date.parse("2026-10-30T10:00:00Z");

const started = (sandbox: string, clock?: { boot_us: number; tai_us: number }): Event =>
  ({
    agent_id: "agent",
    data: { sandbox_id: sandbox, config: clock ? { kernel_clock: clock } : {} },
    hash: "",
    prev_hash: "",
    run_id: "r",
    seq: 1,
    ts: "2026-10-30T10:00:00Z",
    type: "sandbox.started",
    v: 1,
  }) as Event;

const win = (sandbox: string, from: number, over: Partial<SampleWindow> = {}): SampleWindow => ({
  agent_id: "agent",
  count: 50,
  cpu_full_usec: null,
  cpu_some_usec: null,
  cpu_system_usec: null,
  cpu_throttled: null,
  cpu_throttled_usec: null,
  cpu_usec: 0,
  cpu_user_usec: null,
  hz: 50,
  io_full_usec: null,
  io_rbytes: 0,
  io_rios: null,
  io_some_usec: null,
  io_wbytes: 0,
  io_wios: null,
  memory_bytes: 1000,
  memory_full_usec: null,
  memory_some_usec: null,
  pids: 2,
  run_id: "r",
  sandbox_id: sandbox,
  seq: from,
  ts_from: from,
  ts_to: from + 999_999,
  ...over,
});

test("the tracer's clock pair places windows when it agrees with the sandbox start", () => {
  const boot = 5_000_000_000;
  const tai = START * 1000 - 300_000;
  const offsets = clocks(
    [started("s", { boot_us: boot, tai_us: tai })],
    [win("s", boot + 500_000)],
  );
  expect(offsets.get("s")).toBe(tai - boot);
});

test("a pair 37 s off, or none, falls back to the sandbox start", () => {
  const boot = 5_000_000_000;
  const first = boot + 500_000;
  const leap = clocks(
    [started("s", { boot_us: boot, tai_us: START * 1000 + 37_000_000 })],
    [win("s", first)],
  );
  expect(leap.get("s")).toBe(START * 1000 - first);
  expect(clocks([started("s")], [win("s", first)]).get("s")).toBe(START * 1000 - first);
  expect(clocks([started("t")], [win("s", first)]).has("t")).toBe(false);
});

test("usage is the change from the window before, unreadable values are gaps", () => {
  const lanes = usage(
    [
      win("s", 0, { cpu_usec: 1_000_000, io_rbytes: 100 }),
      win("s", 1_000_000, { cpu_usec: 1_500_000, io_rbytes: 300, io_wbytes: 50 }),
      win("s", 2_000_000, { cpu_usec: -1, memory_bytes: -1, pids: null }),
      win("x", 0, { agent_id: null }),
    ],
    new Map([
      ["s", START * 1000],
      ["x", START * 1000],
    ]),
  );
  expect([...lanes.keys()]).toEqual(["agent"]);
  const [a, b, c] = lanes.get("agent") ?? [];
  expect(a?.cpu).toBeNull();
  expect(a?.from).toBe(START);
  expect(b?.cpu).toBeCloseTo(0.5);
  expect(b?.io).toBe(250);
  expect(b?.mem).toBe(1000);
  expect(c?.cpu).toBeNull();
  expect(c?.mem).toBeNull();
  expect(c?.pids).toBeNull();
});

test("each pixel column keeps the highest value of the windows touching it", () => {
  const cols = columns(
    [
      { from: 0, to: 999, cpu: 0.2, io: 10, mem: 5, pids: 1 },
      { from: 1000, to: 1999, cpu: 0.9, io: null, mem: 3, pids: 1 },
      { from: 6000, to: 7999, cpu: 0.4, io: null, mem: null, pids: null },
    ],
    { t0: 0, t1: 10_000 },
    10,
  );
  expect(cols.map((c) => c.cpu)).toEqual([0.2, 0.9, null, null, null, null, 0.4, 0.4, null, null]);
  expect(cols[0]?.io).toBe(10);
  expect(
    columns([{ from: 0, to: 9999, cpu: 1, io: 1, mem: 1, pids: 1 }], { t0: 0, t1: 10_000 }, 2)[0]
      ?.cpu,
  ).toBe(1);
});

test("axis ticks fall on round offsets that fit the width", () => {
  expect(ticks({ t0: 0, t1: 430_000 }, 800)).toEqual([
    0, 60_000, 120_000, 180_000, 240_000, 300_000, 360_000, 420_000,
  ]);
  expect(ticks({ t0: 0, t1: 430_000 }, 300)).toEqual([0, 120_000, 240_000, 360_000]);
  expect(offset(0)).toBe("0:00");
  expect(offset(95_400)).toBe("1:35");
  expect(offset(3_725_000)).toBe("1:02:05");
});

const step = (seq: number, from: number, seqFrom: number, seqTo: number): StepInfo => ({
  agent_id: "agent",
  call_id: `m${seq}`,
  n: seq,
  seq,
  seq_from: seqFrom,
  seq_to: seqTo,
  source: "live",
  status: "completed",
  ts_from: new Date(START + from).toISOString(),
  ts_to: new Date(START + from + 500).toISOString(),
  tools: [],
  cost_usd: null,
});

test("a time picks the step under way, a seq the step holding it", () => {
  const steps = [step(4, 0, 2, 8), step(11, 10_000, 9, 12), step(20, 30_000, 18, 25)];
  expect(stepAt(steps, START - 5)?.seq).toBe(4);
  expect(stepAt(steps, START + 15_000)?.seq).toBe(11);
  expect(stepAt(steps, START + 90_000)?.seq).toBe(20);
  expect(stepOf(steps, 10)?.seq).toBe(11);
  expect(stepOf(steps, 15)?.seq).toBe(11);
  expect(stepOf(steps, 1)?.seq).toBe(4);
});
