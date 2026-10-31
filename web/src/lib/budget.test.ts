import { expect, test } from "vitest";

import {
  capText,
  changesText,
  draftText,
  inForce,
  readCell,
  readDraft,
  type RunCaps,
} from "./budget";

test("a cell keeps, lifts or sets its cap, and says when it does not read", () => {
  expect(readCell("steps", "")).toBeUndefined();
  expect(readCell("steps", " None ")).toBeNull();
  expect(readCell("cost_usd", "no cap")).toBeNull();
  expect(readCell("output_tokens", "100,000")).toBe(100_000);
  expect(readCell("steps", "1.5")).toBe("Write a whole number from 1, or none.");
  expect(readCell("steps", "0")).toBe("Write a whole number from 1, or none.");
  expect(readCell("cost_usd", "$2.5")).toBe(2.5);
  expect(readCell("cost_usd", "-1")).toBe("Write dollars above 0, or none.");
  expect(readCell("wall_s", "90")).toBe(90);
  expect(readCell("wall_s", "30m")).toBe(1800);
  expect(readCell("wall_s", "2H")).toBe(7200);
  expect(readCell("wall_s", "soon")).toBe("Write seconds, or 30m, 2h, or none.");
});

test("the draft becomes the override the API takes, rows left empty left out", () => {
  const { budget, problems } = readDraft({
    every: { steps: "200", cost_usd: "" },
    agents: { alice: { wall_s: "1h" }, bob: {} },
    run: { cost_usd: "none", steps: "x" },
  });
  expect(budget).toEqual({
    every: { steps: 200 },
    agents: { alice: { wall_s: 3600 } },
    run: { cost_usd: null },
  });
  expect(problems).toEqual({ "run.steps": "Write a whole number from 1, or none." });
  expect(readDraft({ every: {}, agents: {}, run: {} }).budget).toBeUndefined();
  expect(draftText(budget, true)).toBe("every agent steps 200, alice wall 1h, run cost no cap");
  expect(draftText({ every: { steps: 9 } }, false)).toBe("steps 9");
});

test("a run's changes read as the CLI prints them", () => {
  expect(
    changesText(
      [
        { scope: "agent", agent: "alice", key: "steps", value: 200, scenario: 80 },
        { scope: "run", agent: null, key: "cost_usd", value: null, scenario: 10 },
      ],
      true,
    ),
  ).toBe("alice steps 200 (scenario 80), run cost no cap (scenario $10)");
  expect(changesText([], false)).toBeNull();
  expect([capText("output_tokens", 100000), capText("wall_s", 90)]).toEqual(["100,000", "90s"]);
});

test("a placeholder shows the cap in force, varies when the agents differ", () => {
  const none = { steps: null, output_tokens: null, cost_usd: null, wall_s: null };
  const caps: RunCaps = {
    agents: {
      alice: { ...none, steps: 80, cost_usd: 4 },
      bob: { ...none, steps: 30, cost_usd: 4 },
    },
    run: { ...none, cost_usd: 10 },
  };
  expect(inForce(caps, "every", "steps")).toBe("varies");
  expect(inForce(caps, "every", "cost_usd")).toBe("$4");
  expect(inForce(caps, "agent:bob", "steps")).toBe("30");
  expect(inForce(caps, "run", "cost_usd")).toBe("$10");
  expect(inForce(caps, "run", "wall_s")).toBe("no cap");
  expect(inForce(undefined, "run", "steps")).toBe("");
});
