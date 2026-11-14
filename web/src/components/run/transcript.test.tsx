// The step view's labels for an exec whose output ran past its limit. `truncated` is one flag
// for both streams.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Event } from "@/api/events";
import { activityQuery, stepEventsQuery, type StepInfo } from "@/api/run";

import { Transcript } from "./transcript";

const RUN = "r1";

const ev = (seq: number, type: string, data: Record<string, unknown>): Event =>
  ({ seq, type, ts: "2026-11-14T10:00:00Z", agent_id: "a", run_id: RUN, data }) as unknown as Event;

const step = {
  agent_id: "a",
  call_id: "m1",
  seq: 2,
  n: 1,
  seq_from: 0,
  seq_to: 6,
  status: "completed",
  ts_from: "2026-11-14T10:00:00Z",
  ts_to: "2026-11-14T10:00:01Z",
  tools: [
    { seq: 3, call_id: "c1", name: "bash", execs: ["x1"], exit_code: 0, end: "exited", done: true },
  ],
} as unknown as StepInfo;

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ items: [], next: null })),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function show(finished: Record<string, unknown>) {
  const qc = new QueryClient();
  const events = [
    ev(1, "model.response", { call_id: "m1", text: "", finish_reason: "tool_calls" }),
    ev(2, "agent.step", { n: 1, call_id: "m1", status: "completed" }),
    ev(3, "tool.call", { call_id: "c1", name: "bash", arguments: '{"command": "make"}' }),
    ev(4, "exec.started", { exec_id: "x1", call_id: "c1", command: "make" }),
    ev(5, "exec.finished", { exec_id: "x1", exit_code: 0, end: "exited", procs: 3, ...finished }),
    ev(6, "tool.result", { call_id: "c1", output: "ok" }),
  ];
  qc.setQueryData(stepEventsQuery(RUN, step, 0, true).queryKey, events);
  qc.setQueryData(activityQuery(RUN, ["x1"]).queryKey, new Map());
  render(
    <QueryClientProvider client={qc}>
      <Transcript runId={RUN} step={step} after={0} last multi={false} />
    </QueryClientProvider>,
  );
}

const labels = () =>
  screen
    .getAllByText(/^(Stdout|Stderr)|left out$/)
    .map((e) => e.textContent)
    .join(" | ");

test("only the stream that ran past its limit is said to be cut short", async () => {
  show({
    stdout: "Building 3 targets\n",
    stderr: "warning: x\n".repeat(10),
    truncated: true,
    stderr_tail: "error: link failed\n",
    stderr_omitted: 2 * 1024 * 1024,
  });
  await screen.findByText(/^Stdout/);
  expect(labels()).toBe("Stdout | Stderr, cut short | 2.0 MB left out");
});

test("an end that goes on where the head stops shows no gap", async () => {
  show({
    stdout: "collected 8 items\n",
    stderr: "",
    truncated: true,
    stdout_tail: "8 passed in 9.10s\n",
    stdout_omitted: 0,
  });
  await screen.findByText(/^Stdout/);
  expect(labels()).toBe("Stdout, cut short");
  expect(screen.getByText(/8 passed in 9\.10s/)).toBeTruthy();
});

test("an exec logged with no ends kept does not say which stream was cut", async () => {
  show({ stdout: "a\n", stderr: "b\n", truncated: true });
  await screen.findByText(/^Stdout/);
  expect(labels()).toBe("Stdout, cut short | Stderr, cut short");
});

test("output within the limit has no note", async () => {
  show({ stdout: "a\n", stderr: "b\n", truncated: false });
  await screen.findByText(/^Stdout/);
  expect(labels()).toBe("Stdout | Stderr");
});
