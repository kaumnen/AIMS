import { expect, test } from "vitest";

import type { ProfileCheck } from "@/api/launch";

import { checkProblem, checkState } from "./launch";

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
  expect(checkProblem(check("failed", ["warn", "fail"]))).toBe("t1: fail");
  expect(checkProblem(check("completed", ["ok"]))).toBeNull();
});
