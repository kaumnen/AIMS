import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunDetail } from "@/api/run";
import type { RunSummary } from "@/api/runs";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { preloadRoutes } from "@/test/routes";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
const CHECK = "01a19400-0000-7000-8000-000000000001";
let check: RunSummary;
let checked: Record<string, unknown>[];

preloadRoutes();
stubHeight();

const event = (seq: number, type: string, data: Record<string, unknown>) => ({
  agent_id: null,
  data,
  hash: "",
  prev_hash: "",
  run_id: CHECK,
  seq,
  ts: "2026-11-01T10:00:00Z",
  type,
  v: 1,
});

const line = (profile: string, name: string, verdict: string, detail = "") => ({
  profile,
  name,
  verdict,
  detail,
});

beforeEach(() => {
  check = run(CHECK, { kind: "models.check", status: "running", state: "running" });
  checked = [
    line("spark", "plain", "ok", "said ok"),
    line("spark", "tools", "warn", "2 calls in one turn with parallel_tool_calls off"),
  ];
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/session")
      return Response.json({ name: "op", role: "operator", auth: true });
    if (url.pathname === `/api/runs/${CHECK}`) {
      const detail: Partial<RunDetail> = {
        run: check,
        started: event(0, "run.started", {
          kind: "models.check",
          config: { models: { spark: {}, glimmer: {}, ember: {} } },
        }) as RunDetail["started"],
        finished: null,
        verdict: null,
        monitor_runs: [],
        score_runs: [],
        budget: [],
        series_previous: null,
      };
      return Response.json(detail);
    }
    if (
      url.pathname === `/api/runs/${CHECK}/events` &&
      url.searchParams.get("types") === "model.checked"
    ) {
      return Response.json({
        items: checked.map((d, i) => event(i + 1, "model.checked", d)),
        next: null,
      });
    }
    if (url.pathname.endsWith("/events")) return Response.json({ items: [], next: null });
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function renderAt(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

test("a model check lists each test: checked, running and waiting", async () => {
  renderAt(`/runs/${CHECK}`);
  const checks = await screen.findByRole("region", { name: "Checks" });
  expect(await within(checks).findByText("said ok")).toBeTruthy();
  const rows = within(checks).getAllByRole("row").slice(1);
  expect(rows.map((r) => r.textContent)).toEqual([
    "sparkplainoksaid ok",
    "toolswarning2 calls in one turn with parallel_tool_calls off",
    "glimmerrunning",
    "emberwaiting",
  ]);
  expect(within(checks).getByText("1 of 3 profiles passed, 1 with warnings.")).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Checks" })).toBeTruthy();
});

test("a failed check shows what it saw and the profiles it never reached", async () => {
  check = { ...check, status: "failed", state: "done" };
  checked = [...checked, line("glimmer", "plain", "fail", "401 Unauthorized")];
  renderAt(`/runs/${CHECK}`);
  const checks = await screen.findByRole("region", { name: "Checks" });
  expect(await within(checks).findByText("401 Unauthorized")).toBeTruthy();
  expect(within(checks).getByText("failed")).toBeTruthy();
  expect(within(checks).getByText("not run")).toBeTruthy();
  expect(within(checks).getByText("1 of 3 profiles passed, 1 with warnings.")).toBeTruthy();
});
