import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ReportWarning, RunReport } from "@/api/report";
import { fullTime } from "@/lib/format";
import { renderAt, REVIEW, RUN, SCORE, serve, type Served, served } from "@/test/report";
import { preloadRoutes } from "@/test/routes";

preloadRoutes();

let state: Served;

beforeEach(() => {
  state = served();
  serve(state);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Pins = RunReport["header"]["pins"];

function withPins(over: Partial<Pins>) {
  const header = state.report.header;
  state.report = { ...state.report, header: { ...header, pins: { ...header.pins, ...over } } };
}

function warning(over: Partial<ReportWarning> & Pick<ReportWarning, "kind">): ReportWarning {
  return {
    run_id: RUN,
    seq: null,
    count: 1,
    agent: null,
    detail: null,
    part: null,
    // oxlint-disable-next-line unicorn/no-thenable
    then: null,
    lost: null,
    seqs: [],
    ...over,
  };
}

// A monitor that degraded at `seq`. `after` is what it did then.
function degraded(
  seq: number,
  part: string,
  after: string | null,
  over: Partial<ReportWarning> = {},
): ReportWarning {
  // oxlint-disable-next-line unicorn/no-thenable
  return { ...warning({ kind: "monitor_degraded", seq, part, ...over }), then: after };
}

// One section of the report as the tab shows it, once the report has loaded.
async function section(id: string): Promise<HTMLElement> {
  const article = await screen.findByRole("article", { name: "Run report" });
  return waitFor(() => {
    const el = article.querySelector<HTMLElement>(`[data-section="${id}"]`);
    if (!el) throw new Error(`no section ${id}`);
    return el;
  });
}

const open = () => renderAt(`/runs/${RUN}?tab=report`);

// The value of each pin of the header, by its label.
function pins(el: HTMLElement): Record<string, string> {
  const out: Record<string, string> = {};
  for (const dt of el.querySelectorAll("dt")) {
    out[dt.textContent ?? ""] = dt.nextElementSibling?.textContent ?? "";
  }
  return out;
}

// Each warning's line and what the log says.
function read(el: HTMLElement): string[][] {
  const list = within(el).getByRole("list", { name: "Warnings" });
  return within(list)
    .getAllByRole("listitem")
    .map((li) => [...li.querySelectorAll("p")].map((p) => p.textContent ?? ""));
}

test("the header pins every input of the report", async () => {
  open();
  const el = await section("header");
  expect(within(el).getByRole("heading", { level: 2 }).textContent).toBe("Run report: cache-keys");
  const p = state.report.header.pins;
  expect(pins(el)).toEqual({
    Run: RUN,
    Scenario: "cache-keys, variant unobserved",
    Status: "completed",
    Started: fullTime(state.report.header.started_at ?? ""),
    Duration: "12m 04s",
    "Hash chain": `verified, 4,784 events, head ${p.head.slice(0, 12)}`,
    Monitor: `review 01a18d3b-a156, head ${p.source_head?.slice(0, 12)}, identity ${p.monitor?.slice(0, 12)}`,
    Score: `01a18e00-0000, head ${p.score_head?.slice(0, 12)}`,
    Labels: `1, latest ${fullTime(p.labels_at ?? "")}. Labels sit beside the log. They are not in the hash chain.`,
    Access: "operator. Plants and scores included.",
    Links: "http://127.0.0.1:8765",
    Built: `AIMS 0.2.0rc1 at ${fullTime(p.built_at)}`,
  });
  expect(within(el).getByTitle(p.head).textContent).toBe(p.head.slice(0, 12));
  const review = within(el).getByRole("link", { name: "01a18d3b-a156" });
  expect(review.getAttribute("href")).toBe(`/runs/${REVIEW}`);
  const score = within(el).getByRole("link", { name: "01a18e00-0000" });
  expect(score.getAttribute("href")).toBe(`/runs/${SCORE}`);
});

test("the header names the live monitor and a chain that did not verify", async () => {
  withPins({
    source: "live",
    source_head: null,
    verified: false,
    verify_error: "hash mismatch at seq 9",
    labels: 0,
    labels_at: null,
    base_url: null,
    role: "local",
  });
  state.report = {
    ...state.report,
    header: { ...state.report.header, status: "failed", reason: "agent crashed" },
    facts: { ...state.report.facts, duration_ms: null },
  };
  open();
  const el = await section("header");
  const p = state.report.header.pins;
  expect(pins(el)).toMatchObject({
    Status: "failed (agent crashed)",
    Duration: "unknown",
    "Hash chain": `not verified (hash mismatch at seq 9), head ${p.head.slice(0, 12)}`,
    Monitor: `live monitor, identity ${p.monitor?.slice(0, 12)}`,
    Labels: "none. Labels sit beside the log. They are not in the hash chain.",
    Access: "local runs directory. Labels are only the ones written here.",
    Links: "none. Evidence shows as seq N.",
  });
  expect(within(el).getByText("not verified").className).toContain("text-flag");
});

test("a viewer's header leaves the score out", async () => {
  state.role = "viewer";
  withPins({ role: "viewer" });
  open();
  const el = await section("header");
  expect(pins(el)).toMatchObject({
    Score: "left out",
    Access: "viewer. Plants, ground truth and scores left out.",
  });
  expect(within(el).queryByRole("link", { name: "01a18e00-0000" })).toBeNull();
});

test("a run with no monitor and no score says none for both", async () => {
  withPins({ source: "live", source_head: null, monitor: null, score: null, score_head: null });
  open();
  expect(pins(await section("header"))).toMatchObject({ Monitor: "none", Score: "none" });
});

const headline = async () => (await section("headline")).textContent;

test("the headline puts the reward and its tests beside the judge", async () => {
  open();
  expect(await headline()).toBe(
    "Reward 1 (8 of 8 tests) · Judge: importance 4 of 5 · Claim matched: no",
  );
  const el = await section("headline");
  expect(el.querySelectorAll("[role=img]")).toHaveLength(1);
});

test.each([
  ["running", "none yet, the run is in progress"],
  ["no_monitor", "none, the run had no monitor"],
  ["skipped", "skipped"],
  ["missing", "no verdict"],
] as const)("the headline says why there is no verdict: %s", async (why, words) => {
  state.report = {
    ...state.report,
    headline: { ...state.report.headline, no_verdict: why, importance: null, claim_matched: null },
  };
  open();
  expect(await headline()).toBe(`Reward 1 (8 of 8 tests) · Judge: ${words}`);
  expect((await section("headline")).querySelector("[role=img]")).toBeNull();
});

test.each([
  [
    { importance: 0, claim_matched: "yes" },
    "Reward 1 (8 of 8 tests) · Judge: nothing found · Claim matched: yes",
  ],
  [
    { importance: null, passed: null, total: null, reward: 0.5 },
    "Reward 0.5 · Judge: nothing found · Claim matched: no",
  ],
  [
    { importance: null, reward: null, reward_error: "pytest crashed" },
    "Reward: none (verifier error) · Judge: nothing found · Claim matched: no",
  ],
  [
    { importance: null, scored: false, reward: null },
    "Reward: none · Judge: nothing found · Claim matched: no",
  ],
  [
    { importance: null, passed: null, total: null, reward: 0.00001 },
    "Reward 1e-05 · Judge: nothing found · Claim matched: no",
  ],
  [
    { importance: null, passed: null, total: null, reward: 1234567 },
    "Reward 1.23457e+06 · Judge: nothing found · Claim matched: no",
  ],
] as const)("the headline reads %j", async (over, line) => {
  state.report = { ...state.report, headline: { ...state.report.headline, ...over } };
  open();
  expect(await headline()).toBe(line);
});

test("the fixture's warnings name the live monitor of a report built from a review", async () => {
  open();
  // an event of the run reads seq N until its step loads
  const el = await section("warnings");
  await waitFor(() =>
    expect(read(el)).toEqual([
      [
        "Warning The live monitor ran out of budget at step 3. From there it ran signals only.",
        "The log says: the monitor spent $3.7619 of its $5, $1 of it kept for the judge",
      ],
      ["Note The cost of agent is incomplete. Some calls had no price."],
    ]),
  );
});

test("each kind of warning says what the Markdown says", async () => {
  withPins({ source: "live", verified: false });
  state.report = {
    ...state.report,
    warnings: [
      warning({ kind: "in_progress" }),
      warning({ kind: "verify_failed", detail: "hash mismatch at seq 9" }),
      degraded(7, "sweep", "continue"),
      degraded(8, "judge", null),
      degraded(9, "lane", "pause"),
      warning({ kind: "rollout_failed", seq: 20, part: "judge.2 (blind)", detail: "a <b>503</b>" }),
      warning({ kind: "audit_lost", seq: 2, count: 2, lost: 1234 }),
      warning({ kind: "audit_lost", seq: 3, lost: null }),
      warning({ kind: "log_truncated", seq: 20, detail: "torn line" }),
      warning({ kind: "replay_divergence", seq: 6, count: 3 }),
      warning({ kind: "tainted", seq: 20, count: 3, seqs: [16, 18, 2] }),
    ],
  };
  open();
  // an event of the run reads seq N until its step loads
  const el = await section("warnings");
  await waitFor(() =>
    expect(read(el)).toEqual([
      ["Note In progress. This report shows the log so far."],
      [
        "Caution The hash chain did not verify. Treat every fact below as unproven.",
        "The log says: hash mismatch at seq 9",
      ],
      ["Warning The monitor's sweeps stopped at step 2. It went on without that part."],
      ["Warning The monitor's judge stopped at step 2."],
      ["Warning The monitor's lane stopped at step 2. It paused the agents."],
      [
        "Warning The monitor's rollout judge.2 (blind) failed. The verdict at step 3 is from the other rollouts.",
        "The log says: a <b>503</b>",
      ],
      ["Warning The kernel audit missed 1,234 events, first at step 1."],
      ["Warning The kernel audit missed events, first at step 1."],
      [
        "Warning The log had a torn last line at step 3. The writer cut it and kept the bytes.",
        "The log says: torn line",
      ],
      ["Warning The replay differed from its recording 3 times, first at step 2."],
      [
        "Warning Flags raised while the verifier ran: seq 2, seq 16, seq 18. The reward stands as given.",
      ],
    ]),
  );
});

test("only a chain that did not verify is a red callout", async () => {
  state.report = {
    ...state.report,
    warnings: [
      warning({ kind: "verify_failed" }),
      warning({ kind: "in_progress" }),
      warning({ kind: "log_truncated", seq: 20 }),
    ],
  };
  open();
  const el = await section("warnings");
  const items = await within(el).findAllByRole("listitem");
  expect(items.map((li) => li.dataset["level"])).toEqual(["caution", "note", "warning"]);
  const red = items.filter(
    (li) => li.className.includes("border-flag") || li.querySelector(".text-flag") !== null,
  );
  expect(red).toEqual([items[0]]);
});

test("a review's warnings name the review and link to its events", async () => {
  state.report = {
    ...state.report,
    warnings: [
      degraded(33, "judge", "skip", { run_id: REVIEW }),
      degraded(34, "budget", "signals_only", { run_id: REVIEW }),
      warning({ kind: "tainted", run_id: REVIEW, count: 2, seqs: [61, 62] }),
    ],
  };
  open();
  const el = await section("warnings");
  await waitFor(() =>
    expect(read(el)).toEqual([
      ["Warning Review 01a18d3b-a156's judge stopped at seq 33. It skipped that part."],
      ["Warning Review 01a18d3b-a156 ran out of budget at seq 34. From there it ran signals only."],
      [
        "Warning Review 01a18d3b-a156 flagged events of the verifier: seq 61, seq 62. The reward stands as given.",
      ],
    ]),
  );
  const at = within(el).getByRole("link", { name: "seq 33" });
  expect(at.getAttribute("href")).toBe(`/runs/${REVIEW}?tab=raw&seq=33`);
  expect(within(el).getAllByRole("link", { name: "01a18d3b-a156" })).toHaveLength(3);
});

test("a tainted flag opens the Steps tab on that flag of the monitor that raised it", async () => {
  state.report = {
    ...state.report,
    warnings: [
      warning({ kind: "tainted", seq: 20, count: 1, seqs: [2] }),
      warning({ kind: "tainted", run_id: REVIEW, count: 1, seqs: [62] }),
    ],
  };
  const router = open();
  const el = await section("warnings");
  fireEvent.click(await within(el).findByRole("button", { name: "seq 2" }));
  await waitFor(() => expect(router.state.location.search).toEqual({ flag: 2, source: "live" }));
  // the tab change replaces the entry, so go back to the report by URL
  await router.navigate({ to: "/runs/$runId", params: { runId: RUN }, search: { tab: "report" } });
  const again = await section("warnings");
  fireEvent.click(await within(again).findByRole("button", { name: "seq 62" }));
  await waitFor(() => expect(router.state.location.search).toEqual({ flag: 62, source: REVIEW }));
});

test("a long list of flags shows twelve links and counts the rest", async () => {
  const seqs = Array.from({ length: 15 }, (_, i) => 100 + i);
  state.report = {
    ...state.report,
    warnings: [warning({ kind: "tainted", run_id: REVIEW, count: 15, seqs })],
  };
  open();
  const el = await section("warnings");
  await within(el).findByRole("button", { name: "seq 100" });
  expect(within(el).getAllByRole("button", { name: /^seq / })).toHaveLength(12);
  expect(el.textContent).toContain("seq 111 (3 more). The reward stands as given.");
});

test("a warning's step opens the Steps tab at its event", async () => {
  const router = open();
  const el = await section("warnings");
  fireEvent.click(await within(el).findByRole("button", { name: "step 3" }));
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 15, source: REVIEW }),
  );
  expect(router.state.location.search).not.toHaveProperty("tab");
  const tabs = await screen.findByRole("tablist");
  await waitFor(() =>
    expect(within(tabs).getByRole("tab", { selected: true }).textContent).toBe("Steps"),
  );
});

test("no warnings leave no trace", async () => {
  state.report = { ...state.report, warnings: [] };
  open();
  await section("header");
  const el = await section("warnings");
  expect(el.childElementCount).toBe(0);
});

test("the judge summary is quoted as written", async () => {
  open();
  const el = await section("summary");
  expect(within(el).getByRole("heading", { level: 2 }).textContent).toBe("Judge summary");
  expect(el.textContent).toContain("The judge's view, in its own words.");
  expect(el.querySelector("blockquote")?.textContent).toBe(state.report.summary);
});

test("a blank summary says there is none", async () => {
  state.report = { ...state.report, summary: "  \n " };
  open();
  const el = await section("summary");
  await within(el).findByText("No judge summary.");
  expect(el.querySelector("blockquote")).toBeNull();
  expect(el.textContent).not.toContain("The judge's view");
});

test("text from the log renders as text", async () => {
  const html = "<img src=x onerror=alert(1)>";
  state.report = {
    ...state.report,
    summary: html,
    header: { ...state.report.header, scenario: `<b>${html}</b>`, reason: html },
    warnings: [warning({ kind: "log_truncated", seq: 20, detail: html })],
  };
  open();
  const header = await section("header");
  const warned = await section("warnings");
  const summary = await section("summary");
  expect(within(header).getByRole("heading", { level: 2 }).textContent).toBe(
    `Run report: <b>${html}</b>`,
  );
  expect(pins(header)).toMatchObject({
    Scenario: `<b>${html}</b>, variant unobserved`,
    Status: `completed (${html})`,
  });
  expect(warned.textContent).toContain(`The log says: ${html}`);
  expect(summary.querySelector("blockquote")?.textContent).toBe(html);
  for (const el of [header, warned, summary]) expect(el.querySelector("img, b")).toBeNull();
});
