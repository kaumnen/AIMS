import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunReport } from "@/api/report";
import type { FlagInfo } from "@/api/run";
import { fullReport, renderAt, REVIEW, RUN, serve, type Served, served } from "@/test/report";
import { preloadRoutes } from "@/test/routes";

preloadRoutes();

let state: Served;
let fetch: ReturnType<typeof serve>;
// Flags the fake server answers, per run. The shared fixture answers one list for every run.
let flagsOf: Record<string, FlagInfo[]>;

beforeEach(() => {
  state = served();
  fetch = serve(state);
  flagsOf = {};
  const base = fetch.getMockImplementation()!;
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    const flags = /^\/api\/runs\/([^/]+)\/flags$/.exec(path);
    if (flags) return Response.json({ items: flagsOf[flags[1] ?? ""] ?? [], next: null });
    if (path.endsWith("/sandboxes")) return Response.json([]);
    return base(request);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const requests = (path: string) =>
  fetch.mock.calls.map(([r]) => new URL(r.url)).filter((u) => u.pathname === path);

const live = (over: Partial<RunReport> = {}): RunReport => {
  const r = fullReport(over);
  return {
    ...r,
    header: { ...r.header, pins: { ...r.header.pins, source: "live" } },
    sources: r.sources.map((s) => ({ ...s, chosen: !s.review })),
  };
};

const flag = (seq: number, run: string, evidence: number[]): FlagInfo => ({
  agent_id: "agent",
  category: "scorer_tampering",
  count: 1,
  evidence,
  final: false,
  first_seq: null,
  importance: 4,
  justification: "",
  key: null,
  lane: null,
  last_seq: null,
  latest: seq,
  layer: "judge",
  phase: null,
  quote: null,
  quote_verified: null,
  reflags: 0,
  run_id: run,
  seq,
  source: "judge.1",
  status: "open",
  status_reason: null,
  status_seq: null,
  status_source: null,
  subcategory: null,
  suspicion: null,
  tainted: false,
  ts: "2026-10-30T10:06:31Z",
});

let router: ReturnType<typeof renderAt>;

// Opens the run's Report tab and finds one of its sections.
async function section(name: string) {
  router = renderAt(`/runs/${RUN}?tab=report`);
  return within(await screen.findByRole("region", { name }));
}

// The body rows of a table, cell by cell.
const cells = (table: HTMLElement) =>
  [...table.querySelectorAll("tbody tr")].map((tr) =>
    [...tr.querySelectorAll("td")].map((td) => td.textContent),
  );

test("the timeline lists every moment, its event and what the judge made of it", async () => {
  const timeline = await section("Timeline");
  expect(
    timeline.getByText(/^Starts and ends, flags at importance 3 or more/).textContent,
  ).toContain("plants reached and the events the judge cited.");
  const table = timeline.getByRole("table", { name: "Timeline" });
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent),
  ).toEqual(["Seq", "Step", "Event", "Categories", "Importance"]);
  const rows = cells(table);
  expect(rows).toHaveLength(9);
  expect(rows[0]).toEqual(["seq 0", "", "Run started: cache-keys", "", ""]);
  expect(rows[2]).toEqual(["seq 5", "1", "Flag: sweep", "Loop or confusion", "2"]);
  expect(rows[5]).toEqual([
    "seq 15",
    "2",
    "Monitor degraded: budget (the monitor spent $3.7619 of its $5, $1 of it kept for the judge)",
    "",
    "",
  ]);
});

test("a seq in the timeline opens the Steps tab there, under the report's source", async () => {
  const timeline = await section("Timeline");
  fireEvent.click(timeline.getByRole("button", { name: "seq 12" }));
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 12, source: REVIEW }),
  );
  expect(router.state.location.search).not.toHaveProperty("tab");
});

test("a run of more than one agent names the agent of each moment", async () => {
  const started = state.detail.started!;
  state.detail = {
    ...state.detail,
    started: { ...started, data: { agents: ["agent", "helper"] } },
  };
  const timeline = await section("Timeline");
  const table = timeline.getByRole("table", { name: "Timeline" });
  expect(within(table).getAllByRole("columnheader")[2]?.textContent).toBe("Agent");
  expect(cells(table)[1]?.slice(0, 3)).toEqual(["seq 1", "", "agent"]);
});

test("the timeline shows the scrubber with the flags and sweeps of the review", async () => {
  flagsOf[REVIEW] = [flag(61, REVIEW, [10])];
  const timeline = await section("Timeline");
  const scrubber = await timeline.findByRole("slider", { name: "Timeline" });
  await waitFor(() => expect(scrubber.querySelector('[data-flag="61"]')).not.toBeNull());
  expect(requests(`/api/runs/${REVIEW}/flags`).length).toBeGreaterThan(0);
  const sweeps = requests(`/api/runs/${REVIEW}/events`);
  expect(sweeps.some((u) => u.searchParams.getAll("types").includes("monitor.sweep"))).toBe(true);
  // no step is chosen in a report
  expect(scrubber.getAttribute("aria-valuetext")).toBe("No step chosen");
  fireEvent.pointerDown(scrubber.querySelector('[data-flag="61"]')!, { button: 0, pointerId: 1 });
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ flag: 61, source: REVIEW }),
  );
});

test("a report from the live monitor shows the run's own flags", async () => {
  state.report = live();
  flagsOf[RUN] = [flag(30, RUN, [10])];
  flagsOf[REVIEW] = [flag(61, REVIEW, [10])];
  const timeline = await section("Timeline");
  const scrubber = await timeline.findByRole("slider", { name: "Timeline" });
  await waitFor(() => expect(scrubber.querySelector('[data-flag="30"]')).not.toBeNull());
  expect(scrubber.querySelector('[data-flag="61"]')).toBeNull();
  fireEvent.pointerDown(scrubber.querySelector('[data-flag="30"]')!, { button: 0, pointerId: 1 });
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ flag: 30, source: "live" }),
  );
});

test("a click on the first step of the scrubber opens the Steps tab there", async () => {
  const timeline = await section("Timeline");
  const scrubber = await timeline.findByRole("slider", { name: "Timeline" });
  // 30 px of 800 is 27 s into the run, in step 1 (event 4)
  fireEvent.pointerDown(scrubber.querySelector("svg")!, {
    clientX: 30,
    clientY: 30,
    button: 0,
    pointerId: 1,
  });
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 4, source: REVIEW }),
  );
});

test("an arrow key on the scrubber opens the Steps tab at the first step", async () => {
  const timeline = await section("Timeline");
  const scrubber = await timeline.findByRole("slider", { name: "Timeline" });
  fireEvent.keyDown(scrubber, { key: "ArrowRight" });
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 4, source: REVIEW }),
  );
});

test("no steps, no scrubber", async () => {
  state.steps = [];
  const timeline = await section("Timeline");
  expect(timeline.getByRole("table", { name: "Timeline" })).toBeTruthy();
  expect(timeline.queryByRole("slider")).toBeNull();
});

test("run facts show the agents, the counts, the costs and the kernel events", async () => {
  const facts = await section("Run facts");
  expect(cells(facts.getByRole("table", { name: "Agents" }))).toEqual([
    [
      "agent",
      "glimmer-organism2",
      "accounts/x/models/glimmer",
      "3",
      "submitted",
      "unknown",
      "11m 54s",
    ],
  ]);
  expect(
    facts.getByText(
      "4,784 events, 3 steps, 2 flags. Cost: agents $0. Live monitor $4.42, judge included. " +
        "This review $0.73, judge included.",
    ),
  ).toBeTruthy();
  const calls = facts.getByText("Model calls by caller").closest("details")!;
  expect(cells(within(calls).getByRole("table", { hidden: true }))).toEqual([
    ["agent", "", "75", "unknown", "1,910,159", "32,915"],
    ["sweep", "sweep_window", "16", "$1.18", "302,968", "10,887"],
  ]);
  const kernel = facts.getByText("Kernel events").closest("details")!;
  expect(cells(within(kernel).getByRole("table", { hidden: true }))).toEqual([
    ["file.access", "1,490"],
    ["proc.exec", "307"],
  ]);
  expect(facts.queryByText("No kernel events.")).toBeNull();
});

test("a cost with unpriced calls is a floor; no kernel events say so", async () => {
  const r = live();
  state.report = {
    ...r,
    facts: {
      ...r.facts,
      agents: r.facts.agents.map((a) => ({ ...a, cost_usd: 0.5 })),
      agent_cost_usd: null,
      kernel: {},
      costs: [],
    },
  };
  const facts = await section("Run facts");
  expect(cells(facts.getByRole("table", { name: "Agents" }))[0]?.[5]).toBe("at least $0.50");
  expect(
    facts.getByText(
      "4,784 events, 3 steps, 2 flags. Cost: agents unknown. Live monitor $4.42, judge included.",
    ),
  ).toBeTruthy();
  expect(facts.getByText("No kernel events.")).toBeTruthy();
  expect(facts.queryByText("Kernel events")).toBeNull();
  expect(facts.queryByText("Model calls by caller")).toBeNull();
});

test("other sources leave out the chosen one and a row builds the report from its source", async () => {
  const sources = await section("Other monitor sources");
  const table = sources.getByRole("table", { name: "Other monitor sources" });
  const rows = cells(table);
  expect(rows).toHaveLength(1);
  expect(rows[0]?.slice(1, 6)).toEqual(["standard", "completed", "4 of 5", "no", "$4.42"]);
  expect(rows[0]?.[0]).toContain("Live monitor");
  fireEvent.click(within(table).getByRole("button", { name: /^Build from this source/ }));
  await waitFor(() => expect(router.state.location.search).toMatchObject({ source: "live" }));
  await waitFor(() =>
    expect(requests(`/api/runs/${RUN}/report`).at(-1)?.searchParams.get("source")).toBe("live"),
  );
});

test("a review among other sources links to its run", async () => {
  state.report = live();
  const sources = await section("Other monitor sources");
  const table = sources.getByRole("table", { name: "Other monitor sources" });
  expect(within(table).getByRole("link", { name: REVIEW.slice(0, 13) })).toBeTruthy();
  expect(cells(table)[0]?.[0]).toMatch(/^Review /);
  fireEvent.click(within(table).getByRole("button", { name: /^Build from this source/ }));
  await waitFor(() => expect(router.state.location.search).toMatchObject({ source: REVIEW }));
});

test("no other source says none", async () => {
  state.report = fullReport({ sources: fullReport().sources.filter((s) => s.chosen) });
  const sources = await section("Other monitor sources");
  expect(sources.getByText("None.")).toBeTruthy();
  expect(sources.queryByRole("table")).toBeNull();
});

test("the appendix holds answers, reports and disagreements as written", async () => {
  state.report = fullReport({
    appendix: {
      answers: [{ agent: "agent", seq: 19, step: 3, text: "<img src=x onerror=alert(1)>" }],
      reports: [{ agent: "agent", seq: 12, step: null, text: "I changed\n  the keys." }],
      disagreements: ["Rollouts differed on drift.", "**not bold**"],
    },
  });
  const appendix = await section("Appendix");
  const fold = appendix
    .getByText("Final answers, reports to the operator and judge disagreements")
    .closest("details")!;
  expect(fold.open).toBe(false);
  const fold$ = within(fold);
  expect(
    fold$.getAllByRole("heading", { level: 3, hidden: true }).map((h) => h.textContent),
  ).toEqual(["Final answer of agent", "Report of agent", "Judge disagreements"]);
  expect(fold$.getByText("<img src=x onerror=alert(1)>").tagName).toBe("BLOCKQUOTE");
  expect(fold.querySelector("img")).toBeNull();
  expect(fold$.getByText("I changed\n  the keys.", { normalizer: (s) => s }).tagName).toBe(
    "BLOCKQUOTE",
  );
  expect(fold$.getByText("**not bold**").tagName).toBe("BLOCKQUOTE");
  expect(fold.querySelector("strong")).toBeNull();
  const where = [...fold.querySelectorAll("h3 + p")].map((p) => p.textContent);
  expect(where).toEqual(["seq 19, step 3.", "seq 12."]);
  fireEvent.click(fold$.getByRole("button", { name: "seq 19", hidden: true }));
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 19, source: REVIEW }),
  );
});

test("an empty appendix says none", async () => {
  state.report = fullReport({ appendix: { answers: [], reports: [], disagreements: [] } });
  const appendix = await section("Appendix");
  expect(appendix.getByText("None.")).toBeTruthy();
  expect(appendix.queryByText(/^Final answers/)).toBeNull();
});
