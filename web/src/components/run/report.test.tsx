import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { reportSections, type ReportWarning } from "@/api/report";
import type { RunSummary } from "@/api/runs";
import { FakeEventSource } from "@/test/event-source";
import {
  fullReport,
  renderAt,
  reportDetail,
  REVIEW,
  RUN,
  serve,
  type Served,
  served,
} from "@/test/report";
import { preloadRoutes } from "@/test/routes";
import { run as summary } from "@/test/runs";

import { SECTIONS } from "./report";
import { visible } from "./report/parts";

preloadRoutes();

// a character by its code point, so the source holds no hidden ones
const ch = (n: number) => String.fromCodePoint(n);

let state: Served;
let fetch: ReturnType<typeof serve>;

beforeEach(() => {
  state = served();
  fetch = serve(state);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const reportRequests = () =>
  fetch.mock.calls
    .map(([r]) => new URL(r.url))
    .filter((u) => u.pathname === `/api/runs/${RUN}/report`)
    .map((u) => u.searchParams);

test("the UI has a part for every section the server writes", () => {
  expect(Object.keys(SECTIONS).toSorted()).toEqual([...reportSections].toSorted());
});

test("every section of a full report renders, in the server's order", async () => {
  renderAt(`/runs/${RUN}?tab=report`);
  const article = await screen.findByRole("article", { name: "Run report" });
  await waitFor(() => expect(article.querySelector("[data-section]")).not.toBeNull());
  const shown = [...article.querySelectorAll<HTMLElement>("[data-section]")];
  expect(shown.map((el) => el.dataset["section"])).toEqual([...reportSections]);
  const empty = shown.filter((el) => !el.textContent?.trim()).map((el) => el.dataset["section"]);
  expect(empty).toEqual([]);
});

test("Report sits after Steps, and Steps stays the default tab", async () => {
  renderAt(`/runs/${RUN}`);
  const tabs = await screen.findByRole("tablist");
  await waitFor(() => expect(within(tabs).getAllByRole("tab")).toHaveLength(4));
  expect(
    within(tabs)
      .getAllByRole("tab")
      .map((t) => t.textContent),
  ).toEqual(["Steps", "Report", "Score", "Raw events"]);
  expect(within(tabs).getByRole("tab", { selected: true }).textContent).toBe("Steps");
  expect(reportRequests()).toHaveLength(0);
});

test("a monitor run has no report, so ?tab=report opens its default tab", async () => {
  state.detail = { ...state.detail, run: { ...state.detail.run, kind: "monitor" } };
  renderAt(`/runs/${RUN}?tab=report`);
  const tabs = await screen.findByRole("tablist");
  expect(within(tabs).queryByRole("tab", { name: "Report" })).toBeNull();
  expect(within(tabs).getByRole("tab", { selected: true }).textContent).toBe("Sweeps");
  expect(reportRequests()).toHaveLength(0);
});

test("the report is built from the source in the URL", async () => {
  const router = renderAt(`/runs/${RUN}?tab=report&source=live`);
  await screen.findByRole("article", { name: "Run report" });
  expect(reportRequests().at(-1)?.get("source")).toBe("live");
  expect(router.state.location.search).toMatchObject({ tab: "report", source: "live" });
});

test("without a source in the URL the server picks it", async () => {
  renderAt(`/runs/${RUN}?tab=report`);
  await screen.findByRole("article", { name: "Run report" });
  expect(reportRequests().at(-1)?.has("source")).toBe(false);
});

test("a report the server cannot build says why", async () => {
  state.fail = { status: 404, detail: `no monitor run ${REVIEW}` };
  renderAt(`/runs/${RUN}?tab=report&source=${REVIEW}`);
  expect(await screen.findByText(`No report: no monitor run ${REVIEW}.`)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
});

test("Download Markdown saves the report of the source shown", async () => {
  const made: Blob[] = [];
  const create = vi.spyOn(URL, "createObjectURL").mockImplementation((b) => {
    made.push(b as Blob);
    return "blob:report";
  });
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  renderAt(`/runs/${RUN}?tab=report`);
  fireEvent.click(await screen.findByRole("button", { name: "Download Markdown" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  const md = reportRequests().find((q) => q.get("format") === "md");
  expect(md?.get("source")).toBe(REVIEW);
  expect(await made[0]?.text()).toBe(state.markdown);
  const a = click.mock.contexts[0] as HTMLAnchorElement;
  expect(a.download).toBe(`aims-report-${RUN}.md`);
  click.mockRestore();
  create.mockRestore();
  revoke.mockRestore();
});

test("a download that failed says so on its own source only", async () => {
  const base = fetch.getMockImplementation()!;
  fetch.mockImplementation(async (request) => {
    if (new URL(request.url).searchParams.get("format") === "md") throw new TypeError("down");
    return base(request);
  });
  const router = renderAt(`/runs/${RUN}?tab=report`);
  fireEvent.click(await screen.findByRole("button", { name: "Download Markdown" }));
  expect(await screen.findByText("The Markdown did not download.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Build from this source: the live monitor" }));
  await waitFor(() =>
    expect(router.state.location.search).toEqual({ tab: "report", source: "live" }),
  );
  await waitFor(() => expect(reportRequests().at(-1)?.get("source")).toBe("live"));
  expect(await screen.findByRole("button", { name: "Download Markdown" })).toBeTruthy();
  expect(screen.queryByText("The Markdown did not download.")).toBeNull();
});

test("hidden characters from the log show as visible escapes", () => {
  expect(visible(`a${ch(0x202e)}b${ch(0x200b)}c${ch(0x2066)}d`)).toBe("a\\u202Eb\\u200Bc\\u2066d");
  expect(visible("tab\tline\r\nend\rx")).toBe("tab\tline\nend\nx");
  expect(visible([0x07, 0xfeff, 0x2028, 0xe0041].map(ch).join(""))).toBe(
    "\\u0007\\uFEFF\\u2028\\U000E0041",
  );
  expect(visible(`plain ascii, ${ch(0xe9)}moji ok`)).toBe(`plain ascii, ${ch(0xe9)}moji ok`);
});

test("a long quote is cut until someone asks for the rest", async () => {
  state.report = fullReport({ summary: `${"a".repeat(2000)}${ch(0x202e)}${"b".repeat(1233)}` });
  renderAt(`/runs/${RUN}?tab=report`);
  const show = await screen.findByRole("button", { name: "Cut, 1,234 more characters. Show all" });
  const quote = screen.getByRole("region", { name: "Judge summary" }).querySelector("blockquote");
  expect(quote?.textContent).toBe("a".repeat(2000));
  fireEvent.click(show);
  expect(quote?.textContent).toBe(`${"a".repeat(2000)}\\u202E${"b".repeat(1233)}`);
  expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
});

test("an event before the first step goes by its seq", async () => {
  state.report = fullReport({
    warnings: [{ ...(state.report.warnings[0] as ReportWarning), seq: 0 }],
  });
  renderAt(`/runs/${RUN}?tab=report`);
  const warnings = await screen.findByRole("list", { name: "Warnings" });
  expect(within(warnings).getByRole("button", { name: "seq 0" })).toBeTruthy();
});

test("a cut never splits a character in two", async () => {
  state.report = fullReport({ summary: `${"a".repeat(1999)}\u{1F600}${"b".repeat(10)}` });
  renderAt(`/runs/${RUN}?tab=report`);
  await screen.findByRole("button", { name: "Cut, 10 more characters. Show all" });
  const quote = screen.getByRole("region", { name: "Judge summary" }).querySelector("blockquote");
  expect(quote?.textContent).toBe(`${"a".repeat(1999)}\u{1F600}`);
});

test("the report of a run cancelled in the queue is not built again", async () => {
  state.detail = reportDetail({
    status: "cancelled",
    state: "cancelled",
    started_at: null,
    ended_at: null,
    reason: "cancelled by op",
  });
  state.fail = { status: 404, detail: "run 1 has no events" };
  const router = renderAt(`/runs/${RUN}?tab=report`);
  expect(await screen.findByText("No report: run 1 has no events.")).toBeTruthy();
  const [query] = router.options.context.queryClient
    .getQueryCache()
    .findAll({ queryKey: ["runs", "report", RUN] });
  const every = query?.observers[0]?.options.refetchInterval;
  expect(typeof every === "function" ? every(query as never) : every).toBe(false);
});

// The review the report is built from, as the run list holds it.
const review = (over: Partial<RunSummary>) =>
  summary(REVIEW, { kind: "monitor", origin: RUN, started_at: "2026-10-30T11:30:00Z", ...over });

test("a report built from a review still going is built again as the review ends", async () => {
  const report = fullReport();
  const going = (status: string) => ({
    ...report,
    sources: report.sources.map((s) => (s.chosen ? { ...s, status } : s)),
  });
  state.report = going("running");
  const base = fetch.getMockImplementation();
  if (!base) throw new Error("no fetch");
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname !== "/api/runs" || url.searchParams.get("kind") !== "monitor") {
      return base(request);
    }
    return Response.json({ items: [review({})], next: null });
  });
  renderAt(`/runs/${RUN}?tab=report`);
  await screen.findByRole("region", { name: "Judge summary" });
  expect(reportRequests()).toHaveLength(1);
  state.report = going("completed");
  const stream = FakeEventSource.all.findLast((s) => s.url.includes("runs=true"));
  if (!stream) throw new Error("no list stream");
  act(() => stream.send({ kind: "run", run: review({ status: "completed", state: "done" }) }));
  await waitFor(() => expect(reportRequests()).toHaveLength(2));
});

test("a failed build again keeps the report on screen", async () => {
  renderAt(`/runs/${RUN}?tab=report`);
  await screen.findByRole("region", { name: "Judge summary" });
  state.fail = { status: 500, detail: "down" };
  fireEvent.click(screen.getByRole("button", { name: "Build again" }));
  expect(await screen.findByText("The last build failed.", {}, { timeout: 15_000 })).toBeTruthy();
  expect(screen.getByRole("region", { name: "Judge summary" })).toBeTruthy();
}, 20_000);

test("a source the server cannot build from offers the default one", async () => {
  state.fail = { status: 404, detail: `no monitor run ${REVIEW}` };
  const router = renderAt(`/runs/${RUN}?tab=report&source=${REVIEW}`);
  fireEvent.click(await screen.findByRole("button", { name: "Build from the default source" }));
  await waitFor(() => expect(router.state.location.search).toEqual({ tab: "report" }));
});

test("Print or save as PDF prints the page", async () => {
  const print = vi.fn<() => void>();
  vi.stubGlobal("print", print);
  renderAt(`/runs/${RUN}?tab=report`);
  fireEvent.click(await screen.findByRole("button", { name: "Print or save as PDF" }));
  expect(print).toHaveBeenCalledOnce();
});

test("print opens every fold and names the page, then puts both back", async () => {
  renderAt(`/runs/${RUN}?tab=report`);
  const article = await screen.findByRole("article", { name: "Run report" });
  await screen.findByRole("region", { name: "Judge summary" });
  const folds = [...article.querySelectorAll("details")];
  expect(folds.length).toBeGreaterThan(1);
  const [kept, ...rest] = folds as [HTMLDetailsElement, ...HTMLDetailsElement[]];
  kept.open = true;
  const before = document.title;
  window.dispatchEvent(new Event("beforeprint"));
  expect(folds.every((d) => d.open)).toBe(true);
  expect(document.title).toBe(`Run report: cache-keys, run ${RUN}`);
  window.dispatchEvent(new Event("afterprint"));
  expect(kept.open).toBe(true);
  expect(rest.some((d) => d.open)).toBe(false);
  expect(document.title).toBe(before);
});

// Every link to an event or a flag, once the steps are in.
async function eventLinks() {
  renderAt(`/runs/${RUN}?tab=report`);
  const article = await screen.findByRole("article", { name: "Run report" });
  await waitFor(() => expect(within(article).getAllByText(/^step \d+$/).length).toBeGreaterThan(0));
  return [...article.querySelectorAll<HTMLElement>('[title^="Event "], [title^="Flag at event "]')];
}

test("every link to an event prints as its seq with the URL it opens", async () => {
  const report = fullReport();
  const warning = report.warnings[0] as ReportWarning;
  // oxlint-disable-next-line unicorn/no-thenable
  const tainted = { ...warning, kind: "tainted", part: null, then: null, seq: null } as const;
  // a warning of the review at its own event, and tainted flags of the run and of the review
  state.report = {
    ...report,
    warnings: [
      ...report.warnings,
      { ...warning, run_id: REVIEW, seq: 33 },
      { ...tainted, seqs: [5, 9] },
      { ...tainted, run_id: REVIEW, seqs: [61] },
    ],
  };
  const links = await eventLinks();
  expect(links.some((l) => l.title.startsWith("Flag"))).toBe(true);
  expect(links.some((l) => l.title.includes(" of run "))).toBe(true);
  // each link that prints wrong, by its title and what is wrong
  const wrong = links.flatMap((link) => {
    const seq = /event (\d+)/i.exec(link.title)?.[1];
    // the run that holds the event: named in the title, else the reported run or its review
    const run = / of run (\S+)/.exec(link.title)?.[1];
    const holders = run
      ? [run]
      : link.title.endsWith("of the review")
        ? [REVIEW]
        : link.title.startsWith("Flag")
          ? [RUN, REVIEW]
          : [RUN];
    const urls = holders.map((r) => `http://127.0.0.1:8765/runs/${r}?seq=${seq}`);
    // print hides the link's own words, so they must sit in an element
    const bare = [...link.childNodes].some((n) => n.nodeType !== Node.ELEMENT_NODE);
    return [
      ...(link.dataset["printSeq"] === seq ? [] : [`${link.title}: seq`]),
      ...(urls.includes(link.dataset["printUrl"] ?? "") ? [] : [`${link.title}: url`]),
      ...(bare ? [`${link.title}: bare text`] : []),
    ];
  });
  expect(wrong).toEqual([]);
});

test("without a base URL a link prints as its seq alone, as the Markdown has it", async () => {
  const report = fullReport();
  state.report = {
    ...report,
    header: { ...report.header, pins: { ...report.header.pins, base_url: null } },
  };
  const links = await eventLinks();
  expect(links.length).toBeGreaterThan(0);
  expect(links.every((l) => l.dataset["printSeq"] !== undefined)).toBe(true);
  expect(links.filter((l) => l.dataset["printUrl"] !== undefined)).toEqual([]);
});
