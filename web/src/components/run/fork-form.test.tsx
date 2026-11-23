import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunDetail, StepInfo } from "@/api/run";
import { LiveProvider } from "@/components/live";
import { FakeEventSource } from "@/test/event-source";
import { run } from "@/test/runs";

import ForkForm from "./fork-form";

vi.mock("@tanstack/react-router", async (actual) => ({
  ...(await actual<typeof import("@tanstack/react-router")>()),
  useNavigate: () => () => Promise.resolve(),
}));

const fetch = vi.fn<(request: Request) => Promise<Response>>();
beforeEach(() => {
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async () =>
    Response.json({ profiles: [], roles: {}, lines: [], warnings: [], parts: [] }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

test("a step whose tool calls never returned names its agent and says nothing of the run", async () => {
  // agent a ended in its step 3 with its tool calls under way; b went on and the run passed
  const detail = {
    run: run("r1", { status: "passed", state: "finished", steps: 10 }),
    started: {
      seq: 0,
      type: "run.started",
      ts: "",
      agent_id: null,
      data: { agents: ["a", "b"], oversight: "observe" },
    },
    finished: { seq: 99, type: "run.finished", ts: "", agent_id: null, data: {} },
    verdict: null,
    monitor_runs: [],
    score_runs: [],
    budget: [],
    series_previous: null,
    replayable: { a: 2, b: 7 },
    listed: [],
  } as unknown as RunDetail;
  const step = { seq: 30, n: 3, agent_id: "a", seq_from: 29, seq_to: 31, tools: [] };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LiveProvider onClosed={async () => true}>
        <ForkForm detail={detail} step={step as unknown as StepInfo} multi onDone={() => {}} />
      </LiveProvider>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText(
      "The tool calls of a's step 3 never returned, so a fork cannot keep it.",
    ),
  ).toBeTruthy();
});
