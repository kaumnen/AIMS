import { expect, test } from "vitest";

import { run } from "@/test/runs";

import { statusLabel } from "./cells";

const held = (by: "monitor" | "operator") => ({
  agent: "a",
  by,
  seq: 1,
  since: "2026-10-31T00:00:00Z",
});

test("a running run with an open pause reads Held or Paused", () => {
  expect(statusLabel(run("r"))).toBe("Running");
  expect(statusLabel(run("r", { held: [held("operator")] }))).toBe("Paused");
  expect(statusLabel(run("r", { held: [held("operator"), held("monitor")] }))).toBe("Held");
  expect(statusLabel(run("r", { held: [held("monitor")], stale: true }))).toBe("Runner offline");
});
