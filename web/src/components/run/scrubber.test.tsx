import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { Event } from "@/api/events";
import type { StepInfo } from "@/api/run";

import { bandsOf, Strip } from "./scrubber";

afterEach(cleanup);

const T0 = Date.parse("2026-10-30T10:00:00Z");

const step = (n: number, agent: string, at: number): StepInfo => ({
  agent_id: agent,
  call_id: `m${n}`,
  n,
  seq: n * 10 + (agent === "b" ? 5 : 0),
  seq_from: n * 10 + (agent === "b" ? 3 : -2),
  seq_to: n * 10 + (agent === "b" ? 9 : 4),
  source: "live",
  status: "completed",
  ts_from: new Date(T0 + at * 1000).toISOString(),
  ts_to: new Date(T0 + at * 1000 + 2000).toISOString(),
  tools: [],
  cost_usd: null,
});

const sweep = (agent: string | null, from: number, to: number, error?: string): Event =>
  ({
    agent_id: agent,
    data: {
      name: "standard",
      lane: "a",
      step_from: from,
      step_to: to,
      ...(error ? { error } : {}),
    },
    hash: "",
    prev_hash: "",
    run_id: "r",
    seq: 1,
    ts: "2026-10-30T10:00:00Z",
    type: "monitor.sweep",
    v: 1,
  }) as Event;

test("sweeps become coverage bands per agent; the run-wide channel sweep has none", () => {
  expect(
    bandsOf([sweep("a", 1, 5), sweep(null, 0, 0), sweep("b", 0, 0), sweep("b", 2, 3, "timeout")]),
  ).toEqual([
    { agent: "a", from: 1, to: 5, error: false },
    { agent: "b", from: 2, to: 3, error: true },
  ]);
});

test("each agent gets a lane with its bands, and harm draws a line at its step", () => {
  const steps = [step(1, "a", 0), step(1, "b", 10), step(2, "a", 30), step(2, "b", 40)];
  render(
    <Strip
      span={{ t0: T0, t1: T0 + 60_000 }}
      lanes={[
        { agent: "a", windows: [] },
        { agent: "b", windows: [] },
      ]}
      steps={steps}
      selected={steps[0]!}
      onSelect={vi.fn<(seq: number) => void>()}
      flags={[]}
      bands={[
        { agent: "a", from: 1, to: 2, error: false },
        { agent: "b", from: 2, to: 2, error: true },
      ]}
      harm={27}
    />,
  );
  const timeline = screen.getByRole("slider", { name: "Timeline" });
  const lane = (agent: string) => timeline.querySelector(`[data-lane="${agent}"]`)!;
  expect(lane("a").querySelector('[data-band="1-2"]')).toBeTruthy();
  expect(lane("b").querySelector('[data-band="2-2"]')?.getAttribute("class")).toContain("muted");
  expect(lane("a").textContent).toBe("a");
  expect(lane("a").querySelectorAll("line").length).toBe(2);
  // Harm at seq 27 sits in step 2 of agent b, drawn at that step's end (42 s of 60).
  const harm = timeline.querySelector('[data-harm="27"] line');
  expect(Number(harm?.getAttribute("x1"))).toBeCloseTo((42 / 60) * 800);
});
