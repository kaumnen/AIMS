import { expect, test } from "vitest";

import type { CommandInfo } from "@/api/commands";

import { answerOf } from "./commands";

const command = (over: Partial<CommandInfo>): CommandInfo => ({
  id: "c1",
  run_id: "01a18a50-bee5-7263-ad19-24385640cee1",
  runner: "local",
  action: "pause",
  agent: null,
  // oxlint-disable-next-line unicorn/no-thenable
  then: null,
  text: null,
  reason: null,
  by: null,
  created: 0,
  acked: 1,
  outcome: "done",
  state: "done",
  in_part: false,
  final: true,
  ...over,
});

test("a done command reads as done in part by its answer, not by its text", () => {
  expect(answerOf(command({ outcome: "Done." }))).toBe("Done");
  const note = "the sandbox of agent a did not freeze: gone";
  expect(answerOf(command({ outcome: note, in_part: true }))).toBe(note);
});

test("a refused or dropped command says why", () => {
  expect(answerOf(command({ state: "failed", outcome: "failed: no agent b" }))).toBe("no agent b");
  const dropped = command({ state: "dropped", outcome: "dropped: the run ended" });
  expect(answerOf(dropped)).toBe("Dropped: the run ended");
  expect(answerOf(command({ state: "waiting", acked: null, outcome: null }))).toBe("Waiting");
});
