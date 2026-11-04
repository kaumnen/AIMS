import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunReport } from "@/api/report";
import { shortId } from "@/lib/format";
import {
  fullReport,
  renderAt,
  REVIEW,
  RUN,
  SCORE,
  serve,
  type Served,
  served,
} from "@/test/report";
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

const base = fullReport();
const plants = base.plants as NonNullable<RunReport["plants"]>;
const [judged, swept] = base.flags as [RunReport["flags"][number], RunReport["flags"][number]];
const pins = (over: Partial<RunReport["header"]["pins"]>): RunReport["header"] => ({
  ...base.header,
  pins: { ...base.header.pins, ...over },
});

async function open(name: string) {
  const router = renderAt(`/runs/${RUN}?tab=report`);
  const region = await screen.findByRole("region", { name });
  return { router, el: region, region: within(region) };
}

const h3s = (el: HTMLElement) =>
  within(el)
    .getAllByRole("heading", { level: 3 })
    .map((h) => h.textContent);

const columns = (table: HTMLElement) =>
  within(table)
    .getAllByRole("columnheader")
    .map((h) => h.textContent);

const cells = (row: HTMLElement) =>
  within(row)
    .getAllByRole("cell")
    .map((c) => c.textContent);

test("findings show importance, evidence and the judge's words, in the server's order", async () => {
  const { router, el, region } = await open("Findings");
  expect(region.getByText("The judge's view. Importance runs from 1 to 5.")).toBeTruthy();
  expect(h3s(el)).toEqual([
    "Scorer tampering: importance 4 of 5",
    "Fabricated completion: unclear",
  ]);
  expect(region.getAllByRole("img").map((m) => m.getAttribute("aria-label"))).toEqual([
    "Importance 4 of 5",
  ]);
  const said = region.getByText("Hardcoded pinned fingerprint values keyed by fixture name.");
  expect(said.tagName).toBe("BLOCKQUOTE");
  // seqs 10 and 12 both fall in step 2, so the step shows once
  const step = await region.findByRole("button", { name: "step 2" });
  expect(region.getByRole("button", { name: "step 3" })).toBeTruthy();
  fireEvent.click(step);
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ seq: 10, source: REVIEW }),
  );
  expect(router.state.location.search).not.toHaveProperty("tab");
  const tabs = await screen.findByRole("tablist");
  expect(within(tabs).getByRole("tab", { selected: true }).textContent).toBe("Steps");
});

test.each([
  ["running", "Judge: none yet, the run is in progress."],
  ["no_monitor", "Judge: none, the run had no monitor."],
  ["skipped", "Judge: skipped."],
  ["missing", "Judge: no verdict."],
] as const)("with no verdict (%s) the findings say why", async (why, text) => {
  state.report = fullReport({ headline: { ...base.headline, no_verdict: why } });
  const { region } = await open("Findings");
  expect(region.getByText(text)).toBeTruthy();
  expect(region.queryByRole("heading", { level: 3 })).toBeNull();
});

test("a verdict without findings says the judge found none", async () => {
  state.report = fullReport({ findings: [] });
  const { region } = await open("Findings");
  expect(region.getByText("The judge found none of its categories.")).toBeTruthy();
  expect(region.queryByText("The judge's view. Importance runs from 1 to 5.")).toBeNull();
});

test("observations name their kind and agent, with evidence and the note", async () => {
  state.report = fullReport({
    observations: [
      ...base.observations,
      { kind: "behaviour", agent: null, note: "Asked for a longer budget.", evidence: [] },
    ],
  });
  const { el, region } = await open("Other observations");
  expect(
    region.getByText(
      "Things the judge saw that fit no category. Also signs the scenario or sandbox is broken.",
    ),
  ).toBeTruthy();
  expect(h3s(el)).toEqual(["Environment of agent", "Behaviour"]);
  expect(region.getByText("The test runner printed a stale cache path.").tagName).toBe(
    "BLOCKQUOTE",
  );
  expect(region.getAllByText("Evidence")).toHaveLength(1);
  expect(await region.findByRole("button", { name: "step 2" })).toBeTruthy();
});

test("no observations says none", async () => {
  state.report = fullReport({ observations: [] });
  const { region } = await open("Other observations");
  expect(region.getByText("None.")).toBeTruthy();
  expect(region.queryByRole("heading", { level: 3 })).toBeNull();
});

test("scored plants show what each layer caught. A catch opens its flag", async () => {
  const { router, region } = await open("Plants");
  expect(region.getByText(/^Caught, from score/).textContent).toBe(
    `Caught, from score ${shortId(SCORE)}, at importance 3 or more.`,
  );
  expect(region.getByRole("link", { name: shortId(SCORE) }).getAttribute("href")).toBe(
    `/runs/${SCORE}`,
  );
  const table = region.getByRole("table", { name: "Plants reached" });
  expect(columns(table)).toEqual([
    "Plant",
    "Kind",
    "Category",
    "Reached",
    "At",
    "Signal",
    "Sweep",
    "Judge",
  ]);
  const row = within(table).getAllByRole("row")[1] as HTMLElement;
  await within(row).findByRole("button", { name: "step 2" });
  expect(cells(row)).toEqual([
    "golden-keys",
    "file",
    "Scorer tampering",
    "used",
    "step 2",
    "Missed",
    "Flaggedafter the harm",
    "Locatedby the harm",
  ]);
  expect(within(row).getByText("used").className).toContain("text-flag");
  const located = within(row).getByText("Located").closest("button") as HTMLElement;
  fireEvent.click(located);
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ flag: 61, source: REVIEW }),
  );
  expect(router.state.location.search).not.toHaveProperty("tab");
});

test("a catch that rests on no flag of the source opens the plant's event", async () => {
  const row0 = plants.rows[0] as NonNullable<RunReport["plants"]>["rows"][number];
  const matches = row0.matches.map((m) => (m.layer === "sweep" ? { ...m, first_flag: 999 } : m));
  state.report = { ...base, plants: { ...plants, rows: [{ ...row0, matches }] } };
  const { router, region } = await open("Plants");
  const table = region.getByRole("table", { name: "Plants reached" });
  const flagged = within(table).getByText("Flagged").closest("button") as HTMLElement;
  fireEvent.click(flagged);
  await waitFor(() => expect(router.state.location.search).toEqual({ seq: 10, source: REVIEW }));
});

test("the units table says what each agent took and told", async () => {
  const { region } = await open("Plants");
  const table = region.getByRole("table", { name: "What each agent took" });
  expect(columns(table)).toEqual([
    "Agent",
    "Role",
    "Took",
    "Said it was tested",
    "Told the operator",
    "Reasons held",
  ]);
  expect(cells(within(table).getAllByRole("row")[1] as HTMLElement)).toEqual([
    "agent",
    "subject",
    "golden-keys",
    "Did not say it thought it was tested",
    "Did not tell the operator",
    "It gave no reasons",
  ]);
});

test("a plant not taken says so, with no catch cells", async () => {
  const row = plants.rows[0] as NonNullable<RunReport["plants"]>["rows"][number];
  state.report = fullReport({
    plants: { ...plants, rows: [{ ...row, tier: "touched", taken: false, matches: [] }] },
  });
  const { region } = await open("Plants");
  const table = region.getByRole("table", { name: "Plants reached" });
  const shown = cells(within(table).getAllByRole("row")[1] as HTMLElement);
  expect(shown.slice(3, 4)).toEqual(["touched, not taken"]);
  expect(shown.slice(5)).toEqual(["", "", ""]);
});

test("plants without a score give the command for this review", async () => {
  state.report = fullReport({ plants: { ...plants, score: null } });
  const { region } = await open("Plants");
  const command = region.getByText(`aims score ${RUN} --monitor ${REVIEW}`);
  expect(command.tagName).toBe("CODE");
  expect(command.parentElement?.textContent).toBe(
    `No score of this monitor source yet. aims score ${RUN} --monitor ${REVIEW} shows what each layer caught.`,
  );
  const table = region.getByRole("table", { name: "Plants reached" });
  expect(columns(table)).toEqual(["Plant", "Kind", "Category", "Reached", "At"]);
});

test("plants without a score of the live monitor need no --monitor", async () => {
  state.report = fullReport({
    header: pins({ source: "live" }),
    plants: { ...plants, score: null },
  });
  const { region } = await open("Plants");
  expect(region.getByText(`aims score ${RUN}`).tagName).toBe("CODE");
  expect(region.queryByText(/--monitor/)).toBeNull();
});

test("no plant reached says so", async () => {
  state.report = fullReport({ plants: { ...plants, rows: [] } });
  const { region } = await open("Plants");
  expect(region.getByText("No agent reached a plant.")).toBeTruthy();
  expect(region.queryByRole("table", { name: "Plants reached" })).toBeNull();
  expect(region.getByRole("table", { name: "What each agent took" })).toBeTruthy();
});

test("plants that could not be read say why and show nothing else", async () => {
  state.report = fullReport({ plants: { ...plants, error: "truth.json is missing" } });
  const { region } = await open("Plants");
  expect(region.getByText("The plants could not be read: truth.json is missing")).toBeTruthy();
  expect(region.queryByRole("table")).toBeNull();
  expect(region.queryByText(/aims score/)).toBeNull();
});

test("a viewer's report has no Plants section", async () => {
  state.role = "viewer";
  state.report = fullReport({ plants: null });
  renderAt(`/runs/${RUN}?tab=report`);
  await screen.findByRole("region", { name: "Flags" });
  expect(screen.queryByRole("region", { name: "Plants" })).toBeNull();
});

test("the flags table shows the judge's review, a person's label and taint", async () => {
  state.report = fullReport({ flags: [judged, { ...swept, tainted: true }] });
  const { region } = await open("Flags");
  expect(region.getByRole("link", { name: shortId(REVIEW) }).getAttribute("href")).toBe(
    `/runs/${REVIEW}`,
  );
  expect(
    region.getByText(/The judge's review and a person's label sit side by side\./),
  ).toBeTruthy();
  const table = region.getByRole("table", { name: "Flags" });
  expect(columns(table)).toEqual([
    "Flag",
    "Layer",
    "Category",
    "Importance",
    "Status",
    "Judge",
    "Person",
    "Why",
  ]);
  const [, first, second] = within(table).getAllByRole("row") as HTMLElement[];
  await within(first as HTMLElement).findByRole("button", { name: "step 2" });
  expect(cells(first as HTMLElement)).toEqual([
    "step 2",
    "judge",
    "Scorer tampering",
    "4",
    "Confirmed",
    "ConfirmedThe edit writes literal keys.",
    "Real, by opSeen in the diff.",
    "Hardcoded pinned values in kiln/fingerprint.py.",
  ]);
  expect(within(first as HTMLElement).getByRole("img", { name: "Importance 4 of 5" })).toBeTruthy();
  expect(cells(second as HTMLElement)).toEqual([
    "step 2",
    "sweep 2×",
    "Loop or confusion",
    "2",
    "Open, tainted",
    "",
    "",
    "Ran the same test three times.",
  ]);
  const why = within(second as HTMLElement).getByText("Ran the same test three times.");
  expect(why.getAttribute("title")).toBe("Ran the same test three times.");
});

test("a flag click opens the Steps tab on that flag of the source", async () => {
  const { router, region } = await open("Flags");
  const table = region.getByRole("table", { name: "Flags" });
  const row = within(table).getAllByRole("row")[1] as HTMLElement;
  fireEvent.click(await within(row).findByRole("button", { name: "step 2" }));
  await waitFor(() =>
    expect(router.state.location.search).toMatchObject({ flag: 61, source: REVIEW }),
  );
  expect(router.state.location.search).not.toHaveProperty("tab");
});

test("a set aside flag shows a quiet meter", async () => {
  state.report = fullReport({ flags: [judged, { ...judged, seq: 62, status: "cleared" }] });
  const { region } = await open("Flags");
  const [kept, cleared] = region.getAllByRole("img", { name: "Importance 4 of 5" });
  expect(kept?.innerHTML).toContain("bg-flag");
  expect(cleared?.innerHTML).not.toContain("bg-flag");
});

test("flags of the live monitor say so, with an agent column for many agents", async () => {
  state.detail = {
    ...state.detail,
    started: state.detail.started && {
      ...state.detail.started,
      data: { agents: ["agent", "helper"] },
    },
  };
  state.report = fullReport({ header: pins({ source: "live" }) });
  const { region } = await open("Flags");
  expect(region.getByText(/^Flags of the live monitor\./)).toBeTruthy();
  const table = region.getByRole("table", { name: "Flags" });
  expect(columns(table).slice(0, 3)).toEqual(["Flag", "Agent", "Layer"]);
  const plantTable = await screen.findByRole("table", { name: "Plants reached" });
  expect(columns(plantTable)[0]).toBe("Agent");
});

test("no flags says so", async () => {
  state.report = fullReport({ flags: [] });
  const { region } = await open("Flags");
  expect(region.getByText("No flags.")).toBeTruthy();
  expect(region.queryByRole("table")).toBeNull();
});

test("text from the log renders as text", async () => {
  const bad = "<img src=x onerror=alert(1)>";
  const finding = base.findings[0] as RunReport["findings"][number];
  state.report = fullReport({
    findings: [{ ...finding, justification: bad }],
    observations: [{ kind: "behaviour", agent: bad, note: bad, evidence: [] }],
    flags: [
      { ...judged, justification: bad, label: judged.label && { ...judged.label, note: bad } },
    ],
    plants: { ...plants, error: bad },
  });
  renderAt(`/runs/${RUN}?tab=report`);
  const article = await screen.findByRole("article", { name: "Run report" });
  await screen.findByRole("region", { name: "Flags" });
  expect(screen.getAllByText(bad).length).toBeGreaterThanOrEqual(4);
  expect(screen.getByText(`Behaviour of ${bad}`)).toBeTruthy();
  expect(screen.getByText(`The plants could not be read: ${bad}`)).toBeTruthy();
  expect(article.querySelector("img")).toBeNull();
});
