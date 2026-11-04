import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { reportSections, type ReportWarning } from "@/api/report";
import { fullReport, renderAt, REVIEW, RUN, serve, type Served, served } from "@/test/report";
import { preloadRoutes } from "@/test/routes";

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
