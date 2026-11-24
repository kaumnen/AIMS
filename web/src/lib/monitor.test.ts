import { expect, test } from "vitest";

import type { FlagInfo, StepInfo } from "@/api/run";

import { cited, flagStep, groupFlags } from "./monitor";

const step = (n: number, seqFrom: number, seqTo: number, agent = "agent"): StepInfo => ({
  agent_id: agent,
  call_id: `m${n}`,
  n,
  seq: seqTo,
  seq_from: seqFrom,
  seq_to: seqTo,
  source: "live",
  status: "completed",
  ts_from: "2026-11-23T22:00:00Z",
  ts_to: "2026-11-23T22:00:01Z",
  tools: [],
  cost_usd: null,
});

const flag = (seq: number, evidence: number[], agent: string | null = "agent"): FlagInfo =>
  ({ agent_id: agent, evidence, importance: 2, seq, status: "open" }) as FlagInfo;

// Steps 1, 2 and 49 of a live run. The task text came at seq 1519, the final answer at 7935.
const steps = [step(1, 1521, 1595), step(2, 1596, 1624), step(49, 7900, 7934)];

test("a flag that cites the task text goes where its agent acted", () => {
  // The judge cited the task and the agent's final claim. It once showed at step 1.
  const judged = flag(8374, [1519, 7935]);
  expect(cited(judged, steps)).toBe(7935);
  expect(flagStep(judged, steps)?.n).toBe(49);
  // Before the steps load, the first event cited stands.
  expect(cited(judged, [])).toBe(1519);
});

test("a flag that cites only what came before the first step stays at step 1", () => {
  const setup = flag(1530, [12, 1519]);
  expect(cited(setup, steps)).toBe(12);
  expect(flagStep(setup, steps)?.n).toBe(1);
});

test("a flag whose evidence lies in steps keeps its first event", () => {
  const swept = flag(1700, [1600, 7910]);
  expect(cited(swept, steps)).toBe(1600);
  expect(flagStep(swept, steps)?.n).toBe(2);
});

test("the first step is the flag agent's own", () => {
  const two = [step(1, 1521, 1595), step(1, 1600, 1610, "b"), step(2, 1611, 1624, "b")];
  // seq 1596 is after agent's first step and before b's.
  expect(cited(flag(1700, [1596, 1620], "b"), two)).toBe(1620);
  expect(cited(flag(1700, [1596, 1620], "agent"), two)).toBe(1596);
  expect(cited(flag(1700, [1519, 1596], null), two)).toBe(1596);
});

test("flags of one importance list in the order of where they sit", () => {
  const judged = flag(8374, [1519, 7935]);
  const swept = flag(1700, [1600]);
  const [[, open] = ["open", []]] = groupFlags([judged, swept], steps);
  expect(open.map((f) => f.seq)).toEqual([1700, 8374]);
});
