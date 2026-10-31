import { expect, test } from "vitest";

import { capsText, overrideKeys, reflow } from "./scenario";

test("caps read as one line", () => {
  expect(capsText({ steps: 80, cost_usd: 4, wall_s: 1800, warn_at: 0.8 })).toBe(
    "80 steps, $4, wall 30m, warns at 80%",
  );
  expect(capsText({ monitor_cost_usd: 0.5 })).toBe("monitor $0.5");
  expect(capsText({})).toBe("");
});

test("typed line breaks run on, paragraphs and lists stay", () => {
  expect(reflow("Fix the code\nso it passes.\n\nThen:\n- run it\n- submit\n")).toBe(
    "Fix the code so it passes.\n\nThen:\n- run it\n- submit",
  );
});

test("overrides are listed by key", () => {
  expect(overrideKeys({ system: "x", tools: { bash: { description: "y" } } })).toEqual([
    "system",
    "tools.bash.description",
  ]);
  expect(overrideKeys(null)).toEqual([]);
});
