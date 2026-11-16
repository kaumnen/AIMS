import { expect, test } from "vitest";

import type { Estimate, ProfileCheck } from "@/api/launch";

import { checkProblem, checkState, confirmWhy, profileUses, recordedModels } from "./launch";

const check = (status: string, verdicts: ("ok" | "warn" | "fail")[]): ProfileCheck => ({
  run_id: "c",
  started_at: "2026-10-31T09:00:00+00:00",
  status,
  checks: verdicts.map((verdict, i) => ({
    name: `t${i}`,
    verdict,
    detail: verdict,
    reasoning: null,
  })),
});

test("the worst smoke test decides a check, and old checks fall back on the run status", () => {
  expect(checkState(null)).toBe("none");
  expect(checkState(check("running", []))).toBe("running");
  expect(checkState(check("completed", ["ok", "ok"]))).toBe("ok");
  expect(checkState(check("completed", ["ok", "warn"]))).toBe("warn");
  expect(checkState(check("failed", ["warn", "fail"]))).toBe("fail");
  expect(checkState(check("completed", []))).toBe("ok");
  expect(checkState(check("failed", []))).toBe("fail");
  // a check cancelled or killed before it reached the profile did not fail it
  expect(checkState(check("cancelled", []))).toBe("cut");
  expect(checkState(check("killed", []))).toBe("cut");
  expect(checkState(check("cancelled", ["ok", "fail"]))).toBe("fail");
  expect(checkState(check("killed", ["ok"]))).toBe("ok");
  expect(checkProblem(check("failed", ["warn", "fail"]))).toBe("t1: fail");
  expect(checkProblem(check("completed", ["ok"]))).toBeNull();
});

const ask = (high: number | null, ceiling: number | null, confirm = true) =>
  confirmWhy({
    confirm,
    confirm_above_usd: 20,
    total: { typical_usd: high, high_usd: high, ceiling_usd: ceiling },
  });

test("the reason to confirm says when no cap bounds the cost, whatever the high figure", () => {
  expect(ask(5, 9, false)).toBeNull();
  expect(ask(25, 30)).toBe("The high estimate $25.00 is above the $20.00 limit.");
  expect(ask(null, 30)).toBe("The caps allow $30.00, above the $20.00 limit.");
  // a priced agent with no cost or step cap leaves no ceiling
  expect(ask(5, null)).toBe("No cap bounds the cost. The high estimate is $5.00.");
  expect(ask(null, null)).toBe("No cap bounds the cost and no past runs give an estimate.");
});

const part = (
  agent_profiles: Record<string, string>,
  monitor_profiles: Record<string, string>,
  recorded_models: Record<string, string> = {},
): Estimate["parts"][number] =>
  ({ agent_profiles, monitor_profiles, recorded_models }) as Estimate["parts"][number];

test("each profile a run calls is named once with what it does, agents first", () => {
  const uses = profileUses(
    {
      parts: [
        part({ a: "big" }, { "sweep:main": "small", judge: "big", channels: "small" }),
        part({ a: "big", b: "other" }, { "sweep:main": "small", extract: "tiny" }),
      ],
    },
    ["fallback"],
  );
  expect([...uses]).toEqual([
    ["big", ["agent a", "judge"]],
    ["other", ["agent b"]],
    ["small", ["sweep main", "channel sweep"]],
    ["tiny", ["extract"]],
  ]);
  expect([...profileUses({ parts: [part({ agent: "big" }, {})] }, [])]).toEqual([
    ["big", ["agent"]],
  ]);
  // without an estimate, the agents' profiles; with one that calls no model, none
  expect([...profileUses(undefined, ["big"])]).toEqual([["big", []]]);
  expect(profileUses({ parts: [part({}, {})] }, ["big"]).size).toBe(0);
});

test("the models a fork's agents call as recorded are named by profile, each once", () => {
  const ran = recordedModels({
    parts: [
      part({ a: "big", b: "big", c: "other" }, {}, { a: "old", b: "old", c: "older" }),
      part({ a: "big" }, {}, { a: "oldest" }),
    ],
  });
  expect([...ran]).toEqual([
    ["big", ["old", "oldest"]],
    ["other", ["older"]],
  ]);
  expect(recordedModels(undefined).size).toBe(0);
  expect(recordedModels({ parts: [part({ a: "big" }, {})] }).size).toBe(0);
});
