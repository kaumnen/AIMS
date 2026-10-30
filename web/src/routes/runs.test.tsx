import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunSummary } from "@/api/runs";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let lists: Record<string, RunSummary[]>;

const BEE5 = "01a18a50-bee5-7263-ad19-24385640cee1";
const REVIEW = "01a18d3b-a156-750a-b263-958e86bdd35e";

stubHeight();

beforeEach(() => {
  FakeEventSource.all = [];
  lists = {
    "kind=scenario": [
      run(BEE5, {
        status: "completed",
        state: "done",
        ended_at: "2026-10-30T10:12:04+00:00",
        steps: 72,
        cost_usd: 1.12123625,
        reward: 1,
        flags: 3,
        max_importance: 3,
        variant: "observed",
        reviews: 1,
      }),
      run("01a18a50-bed7-7299-bcd4-67af4b1be7f8", {
        scenario: "word-count",
        status: "failed",
        state: "done",
      }),
    ],
    [`kind=monitor&origin=${BEE5}`]: [
      run(REVIEW, { kind: "monitor", origin: BEE5, flags: 16, verdict_severity: 4 }),
    ],
  };
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/session")
      return Response.json({ name: "op", role: "operator", auth: true });
    if (url.pathname === "/api/scenarios") return Response.json([]);
    if (url.pathname === "/api/runs") {
      const q = new URLSearchParams(url.search);
      q.delete("limit");
      q.sort();
      return Response.json({ items: lists[q.toString()] ?? [], next: null });
    }
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

const runQueries = () =>
  fetch.mock.calls
    .map(([r]) => new URL(r.url))
    .filter((u) => u.pathname === "/api/runs")
    .map((u) => {
      const q = new URLSearchParams(u.search);
      q.sort();
      return q.toString();
    });

const rowOf = (text: string) => {
  const row = screen.getByRole("link", { name: text }).closest('[role="row"]');
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${text}`);
  return within(row);
};

test("agent runs list with their numbers", async () => {
  renderAt("/runs");
  const links = await screen.findAllByRole("link", { name: "cache-keys" });
  expect(links[0]?.getAttribute("href")).toBe(`/runs/${BEE5}`);
  const bee5 = rowOf("cache-keys");
  expect(bee5.getByText("Completed")).toBeTruthy();
  expect(bee5.getByText("observed")).toBeTruthy();
  expect(bee5.getByText("12m 04s")).toBeTruthy();
  expect(bee5.getByText("72")).toBeTruthy();
  expect(bee5.getByText("$1.12")).toBeTruthy();
  expect(bee5.getByText("1.00")).toBeTruthy();
  expect(bee5.getByRole("img", { name: "Highest open importance 3 of 5" })).toBeTruthy();
  expect(screen.getByText("Failed")).toBeTruthy();
  expect(screen.getByText("2 runs")).toBeTruthy();
  expect(runQueries()).toEqual(["kind=scenario&limit=100"]);
});

test("filters live in the URL and reach the server", async () => {
  const router = renderAt("/runs");
  await screen.findAllByRole("link", { name: "cache-keys" });
  fireEvent.click(screen.getByRole("button", { name: "Reviews" }));
  expect(router.state.location.search).toEqual({ kind: "monitor" });
  const series = screen.getByRole("textbox", { name: "Series" });
  fireEvent.change(series, { target: { value: "s1" } });
  fireEvent.keyDown(series, { key: "Enter" });
  expect(await screen.findByText("No runs match these filters.")).toBeTruthy();
  expect(router.state.location.search).toEqual({ kind: "monitor", series: "s1" });
  expect(runQueries()).toContain("kind=monitor&limit=100&series=s1");
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(router.state.location.search).toEqual({ kind: "monitor" });
});

test("a run's reviews open under it", async () => {
  renderAt("/runs");
  await screen.findAllByRole("link", { name: "cache-keys" });
  const toggle = screen.getByRole("button", { name: "1 review" });
  fireEvent.click(toggle);
  const review = await screen.findByRole("link", { name: "Review" });
  expect(review.getAttribute("href")).toBe(`/runs/${REVIEW}`);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(runQueries()).toContain(`kind=monitor&limit=1000&origin=${BEE5}`);
  fireEvent.click(toggle);
  expect(screen.queryByRole("link", { name: "Review" })).toBeNull();
});

test("the stream updates rows and adds new runs", async () => {
  renderAt("/runs");
  await screen.findAllByRole("link", { name: "cache-keys" });
  const source = FakeEventSource.all[0];
  act(() => {
    source?.open();
    source?.send({
      kind: "run",
      run: run("01a18a50-bed7-7299-bcd4-67af4b1be7f8", {
        scenario: "word-count",
        status: "killed",
        state: "done",
      }),
    });
    source?.send({ kind: "run", run: run("01a18e00-0000-7000-8000-000000000000") });
    source?.send({
      kind: "run",
      run: run("01a18e00-0000-7000-8000-000000000001", { kind: "score" }),
    });
  });
  expect(await screen.findByText("Killed")).toBeTruthy();
  expect(screen.getAllByRole("link", { name: "cache-keys" })).toHaveLength(2);
  expect(screen.getByText("3 runs")).toBeTruthy();
  expect(runQueries()).toHaveLength(1);
});
