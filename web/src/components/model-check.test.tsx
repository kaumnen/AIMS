// A model check whose end the stream never brings: the backup poll ends it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";
import { run } from "@/test/runs";

import { LiveProvider } from "./live";
import { useModelCheck } from "./model-check";

const RUN = "01a19000-0000-7000-8000-00000000c0de";
const fetch = vi.fn<(request: Request) => Promise<Response>>();
const reads = { models: 0, run: 0 };

beforeEach(() => {
  reads.models = 0;
  reads.run = 0;
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/models/check") {
      return Response.json({ run_ids: [RUN], group: null }, { status: 202 });
    }
    if (path === "/api/models") {
      reads.models++;
      // an older check of the profile; the new one never started, so nothing names it
      const check = { run_id: "older", started_at: "2026-11-01T00:00:00+00:00", checks: [] };
      return Response.json({
        profiles: [{ name: "spark", check: { ...check, status: "completed" } }],
        roles: {},
      });
    }
    if (path === `/api/runs/${RUN}`) {
      reads.run++;
      // cancelled in the queue
      const summary = run(RUN, { kind: "models.check", status: "cancelled", state: "cancelled" });
      return Response.json({ run: summary, monitor_runs: [], score_runs: [], budget: [] });
    }
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  fetch.mockReset();
});

function Checks() {
  const { checking, check } = useModelCheck();
  return (
    <>
      <button type="button" onClick={() => void check(["spark"])}>
        Check
      </button>
      <p>{checking ? `checking ${checking.join(",")}` : "idle"}</p>
    </>
  );
}

test("a check cancelled in the queue ends and stops the poll though the stream missed it", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LiveProvider onClosed={async () => true}>
        <Checks />
      </LiveProvider>
    </QueryClientProvider>,
  );
  act(() => FakeEventSource.all[0]?.open());
  fireEvent.click(screen.getByRole("button", { name: "Check" }));
  await act(() => vi.advanceTimersByTimeAsync(100));
  // no `run` message for the check comes on the stream
  await act(() => vi.advanceTimersByTimeAsync(10_000));
  expect(screen.getByRole("paragraph").textContent).toBe("idle");
  const after = { ...reads };
  await act(() => vi.advanceTimersByTimeAsync(5 * 60_000));
  expect(reads).toEqual(after);
});
