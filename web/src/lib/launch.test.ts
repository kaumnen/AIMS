import { expect, test } from "vitest";

import type { ProfileCheck } from "@/api/launch";

import { checkProblem, checkState, confirmWhy } from "./launch";

const check = (status: string, verdicts: ("ok" | "warn" | "fail")[]): ProfileCheck => ({
  run_id: "c",
  started_at: "2026-10-31T09:00:00+00:00",
  status,
  checks: verdicts.map((verdict, i) => ({ name: `t${i}`, verdict, detail: verdict })),
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
