import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunDetail } from "@/api/run";
import type { RunSummary } from "@/api/runs";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
const PARENT = "01a19300-0000-7000-8000-000000000001";
const REF = "01a19300-0000-7000-8000-000000000002";
const EXPLOIT = "01a19300-0000-7000-8000-000000000003";
let parent: RunSummary;
let ended: Record<string, unknown>[];
let posted: string[];

stubHeight();

const event = (seq: number, type: string, data: Record<string, unknown>) => ({
  agent_id: null,
  data,
  hash: "",
  prev_hash: "",
  run_id: PARENT,
  seq,
  ts: "2026-10-31T10:00:00Z",
  type,
  v: 1,
});

beforeEach(() => {
  parent = run(PARENT, { kind: "selftest", status: "running", state: "running" });
  ended = [
    {
      case: "reference",
      name: "reference",
      run_id: REF,
      ok: true,
      problems: [],
      notes: ["agent: reward 0"],
    },
  ];
  posted = [];
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    const q = url.searchParams;
    if (url.pathname === "/api/session")
      return Response.json({ name: "op", role: "operator", auth: true });
    if (url.pathname === `/api/runs/${PARENT}`) {
      const detail: Partial<RunDetail> = {
        run: parent,
        started: event(0, "run.started", {
          kind: "selftest",
          config: { cases: ["reference", "exploit", "untouched workdir"] },
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
    if (url.pathname === `/api/runs/${PARENT}/events` && q.get("types") === "selftest.case") {
      return Response.json({
        items: ended.map((d, i) => event(i + 1, "selftest.case", d)),
        next: null,
      });
    }
    if (url.pathname === "/api/runs" && q.get("kind") === "selftest.case") {
      return Response.json({
        items: [
          run(EXPLOIT, { kind: "selftest.case", origin: PARENT, case: "exploit" }),
          // a case run made before runs kept their case
          run(REF, { kind: "selftest.case", origin: PARENT, status: "completed", state: "done" }),
        ],
        next: null,
      });
    }
    if (url.pathname === "/api/runs" && q.get("kind") === "selftest") {
      return Response.json({ items: [parent], next: null });
    }
    if (url.pathname === "/api/scenarios/project/cache-keys/selftest") {
      posted.push(url.pathname);
      return Response.json({ run_ids: [PARENT], group: null }, { status: 202 });
    }
    if (url.pathname === "/api/scenarios/project/cache-keys") {
      return Response.json({
        source: "project",
        name: "cache-keys",
        writable: true,
        etag: "e1",
        errors: [],
        truth: true,
        title: "cache-keys",
        oversight: "observe",
        instruction: "Fix it.",
        environment: null,
        limits: {},
        agents: [],
        channels: [],
        variants: [],
        plants: [],
        selftest: { reference: true, exploit: true, claims: [], impossible: [] },
      });
    }
    if (url.pathname.endsWith("/events")) return Response.json({ items: [], next: null });
    if (url.pathname === "/api/scenarios") return Response.json([]);
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

test("a self-test run lists its cases: ended, running and waiting", async () => {
  renderAt(`/runs/${PARENT}`);
  const cases = await screen.findByRole("region", { name: "Cases" });
  expect(await within(cases).findByText("agent: reward 0")).toBeTruthy();
  const rows = within(cases).getAllByRole("row").slice(1);
  expect(rows.map((r) => r.textContent)).toEqual([
    expect.stringContaining("referencepassed01a19300-0000"),
    expect.stringContaining("exploitrunning01a19300-0000"),
    "untouched workdirwaiting",
  ]);
  expect(
    within(rows[1] as HTMLElement)
      .getByRole("link")
      .getAttribute("href"),
  ).toBe(`/runs/${EXPLOIT}`);
  expect(screen.getByRole("tab", { name: "Cases" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: /Self-test/ })).toBeTruthy();
});

test("a failed case says why", async () => {
  parent = { ...parent, status: "failed", state: "done", reason: "1 of 3 cases failed: exploit" };
  ended = [
    ...ended,
    {
      case: "exploit",
      name: "exploit",
      run_id: EXPLOIT,
      ok: false,
      problems: ["agent did not take plant notes"],
      notes: [],
    },
  ];
  renderAt(`/runs/${PARENT}`);
  const cases = await screen.findByRole("region", { name: "Cases" });
  expect(await within(cases).findByText("agent did not take plant notes")).toBeTruthy();
  expect(within(cases).getByText("not run")).toBeTruthy();
  expect(within(cases).getByText(/1 of 3 cases failed: exploit/)).toBeTruthy();
});

test("self-tests list their cases nested", async () => {
  renderAt("/runs?kind=selftest");
  fireEvent.click(await screen.findByRole("button", { name: "Cases" }));
  expect(await screen.findByRole("link", { name: "Case exploit" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Case" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Self-test of cache-keys" })).toBeTruthy();
});

test("the scenario page starts a self-test and follows it", async () => {
  const router = renderAt("/scenarios/project/cache-keys");
  fireEvent.click(await screen.findByRole("button", { name: "Self-test" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe(`/runs/${PARENT}`));
  expect(posted).toHaveLength(1);
});
