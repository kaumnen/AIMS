import { type InfiniteData, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, onTestFinished, test, vi } from "vitest";

import type { Event } from "@/api/events";
import { rawEventsQuery, type RawPage } from "@/api/run";
import { RunTail } from "@/api/tail";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";

import { RawEvents } from "./raw";

const RUN = "r1";
const ROW = 24;
const fetch = vi.fn<(request: Request) => Promise<Response>>();
let log: Event[];
let reads: string[];
stubHeight(600);

const event = (seq: number, type = "file.access", data: Record<string, unknown> = {}): Event =>
  ({ seq, type, ts: "2026-10-31T10:00:00+00:00", agent_id: "a", data }) as unknown as Event;
const range = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, i) => event(from + i));

// Answers raw reads from `log` as the server does, after 5 ms: a page of `limit`, read back
// from `before` when only it is given.
beforeEach(() => {
  reads = [];
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const q = new URL(request.url).searchParams;
    reads.push(q.has("before") ? `before=${q.get("before")}` : `after=${q.get("after") ?? "-"}`);
    const items = log.filter(
      (e) => e.seq > Number(q.get("after") ?? -1) && e.seq < Number(q.get("before") ?? Infinity),
    );
    const limit = Number(q.get("limit"));
    const back = q.has("before") && !q.has("after");
    const got = back ? items.slice(-limit) : items.slice(0, limit);
    const edge = back ? got[0] : got.at(-1);
    await new Promise((r) => setTimeout(r, 5));
    return Response.json({ items: got, next: got.length === limit ? String(edge?.seq) : null });
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

const wait = (ms: number) => act(() => new Promise((r) => setTimeout(r, ms)));

// 400 ms of reads landing, with the scroll event a browser fires for each scrollTop the list sets
async function rest(box: HTMLElement) {
  for (let i = 0; i < 20; i++) {
    // oxlint-disable-next-line no-await-in-loop
    await wait(20);
    fireEvent.scroll(box);
  }
}

function show(qc: QueryClient, search = {}) {
  render(
    <QueryClientProvider client={qc}>
      <RawEvents runId={RUN} search={search} onChange={() => undefined} />
    </QueryClientProvider>,
  );
  return screen.findByRole("listbox", { name: "Events" });
}

test("a list whose reads swap its ends reads back only once the reader moves up", async () => {
  log = range(0, 348);
  const qc = new QueryClient();
  // 49 pages of one event each, 100 to 148, then a page of 200 that ends the log: a read back
  // lets that page go and moves the reader near the end, which reads it on again
  const one = range(100, 148).map((e) => ({ items: [e], older: e.seq, newer: e.seq }));
  qc.setQueryData<InfiniteData<RawPage>>(rawEventsQuery(RUN, {}).queryKey, {
    pages: [...one, { items: range(149, 348), older: 149, newer: null }],
    pageParams: range(100, 149).map((e) => ({ after: e.seq - 1 })),
  });
  const list = await show(qc);
  const box = list.parentElement as HTMLElement;
  await rest(box);
  expect(reads).toEqual([]);
  // the reader moves down, then up to the top
  box.scrollTop = 60 * ROW;
  fireEvent.scroll(box);
  await within(list).findByRole("option", { name: /^180\b/ });
  box.scrollTop = 0;
  fireEvent.scroll(box);
  await waitFor(() => expect(reads).toEqual(["before=100", "after=148"]));
  await rest(box);
  expect(reads).toEqual(["before=100", "after=148"]);
});

// A tail on the run, with its frames run by hand, and a tool call that matches no search.
function live(qc: QueryClient, after: number) {
  const frames: (() => void)[] = [];
  const tail = new RunTail(RUN, qc, (fn) => frames.push(fn));
  tail.start(after);
  const source = FakeEventSource.all.at(-1);
  if (!source) throw new Error("no stream");
  return {
    tail,
    batch: (seq: number) => {
      act(() => source.send({ kind: "events", run_id: RUN, events: [event(seq, "tool.call")] }));
      act(() => {
        for (const fn of frames.splice(0)) fn();
      });
    },
  };
}

const pagesOf = (qc: QueryClient, search: { q: string }) =>
  qc.getQueryData<InfiniteData<RawPage>>(rawEventsQuery(RUN, search).queryKey)?.pages ?? [];

test("a search on a live run keeps its matches however many batches match nothing", async () => {
  log = [3, 5, 8].map((seq) => event(seq, "tool.call", { q: "pytest" }));
  const qc = new QueryClient();
  const { tail, batch } = live(qc, 10);
  const search = { q: "pytest" };
  await show(qc, search);
  expect(await screen.findByText("3 events")).toBeTruthy();
  let blank = 0;
  for (let seq = 11; seq <= 70; seq++) {
    batch(seq);
    // oxlint-disable-next-line no-await-in-loop
    await wait(20);
    if (screen.queryByText("No events match.")) blank++;
  }
  expect(blank).toBe(0);
  expect(await screen.findByText("3 events")).toBeTruthy();
  await waitFor(() => expect(pagesOf(qc, search)).toHaveLength(1));
  tail.stop();
});

test("a read on at the page cap that finds no match lets one far page go at most", async () => {
  log = Array.from({ length: 10_000 }, (_, i) => event(i * 2, "tool.call", { q: "pytest" }));
  const qc = new QueryClient();
  const search = { q: "pytest" };
  qc.setQueryData<InfiniteData<RawPage>>(rawEventsQuery(RUN, search).queryKey, {
    pages: Array.from({ length: 50 }, (_, p) => ({
      items: log.slice(p * 200, p * 200 + 200),
      older: p === 0 ? null : p * 400,
      newer: p === 49 ? null : p * 400 + 398,
    })),
    pageParams: Array.from({ length: 50 }, (_, p) => (p === 0 ? null : { after: p * 400 - 2 })),
  });
  const { tail, batch } = live(qc, 19_998);
  const list = await show(qc, search);
  // the reader is at the end
  const box = list.parentElement as HTMLElement;
  box.scrollTop = 10_000 * ROW;
  fireEvent.scroll(box);
  const rows = () => pagesOf(qc, search).reduce((n, p) => n + p.items.length, 0);
  const key = rawEventsQuery(RUN, search).queryKey;
  // read `n` landed and the list is at its end again
  const landed = (n: number) =>
    waitFor(() => {
      expect(reads).toHaveLength(n);
      expect(qc.getQueryState(key)?.fetchStatus).toBe("idle");
      expect(pagesOf(qc, search).at(-1)?.newer).toBeNull();
    });
  batch(20_001);
  await waitFor(() => expect(reads).toHaveLength(1));
  // more comes while that read is under way: the list reads on as soon as it lands
  batch(20_003);
  await landed(2);
  expect(rows()).toBe(9800);
  batch(20_005);
  await landed(3);
  await rest(box);
  expect(reads).toEqual(["after=19998", "after=19998", "after=19998"]);
  expect(rows()).toBe(9800);
  tail.stop();
});

test("a read on at the page cap moves the reader up and reads on no further", async () => {
  log = range(0, 11_999);
  const qc = new QueryClient();
  qc.setQueryData<InfiniteData<RawPage>>(rawEventsQuery(RUN, {}).queryKey, {
    pages: Array.from({ length: 50 }, (_, p) => ({
      items: log.slice(p * 200, p * 200 + 200),
      older: p === 0 ? null : p * 200,
      newer: p * 200 + 199,
    })),
    pageParams: Array.from({ length: 50 }, (_, p) => (p === 0 ? null : { after: p * 200 - 1 })),
  });
  const list = await show(qc);
  const box = list.parentElement as HTMLElement;
  // event 9930 at the top, 70 rows from the end
  box.scrollTop = 9930 * ROW;
  fireEvent.scroll(box);
  // the first page went above and the reader's rows moved up with it, 270 rows from the end;
  // a browser tells the list so with a scroll event on the next frame
  await waitFor(() => expect(box.scrollTop).toBe(9730 * ROW));
  expect(reads).toEqual(["after=9999"]);
  await rest(box);
  expect(reads).toEqual(["after=9999"]);
});

// A list from the first event whose read on let the pages before `from` go.
function heldFrom(qc: QueryClient, from: number, to: number) {
  const pages: RawPage[] = [];
  for (let at = from; at <= to; at += 200) {
    const end = Math.min(at + 199, to);
    pages.push({ items: range(at, end), older: at, newer: end === log.at(-1)?.seq ? null : end });
  }
  qc.setQueryData<InfiniteData<RawPage>>(rawEventsQuery(RUN, {}).queryKey, {
    pages,
    pageParams: pages.map((p) => ({ after: (p.older ?? 0) - 1 })),
  });
}

test.each([
  ["First", () => fireEvent.click(screen.getByRole("button", { name: "First" }))],
  ["Home", (list: HTMLElement) => fireEvent.keyDown(list, { key: "Home" })],
])("%s on a list that let its first pages go reads from the first event", async (_, press) => {
  log = range(0, 2399);
  const qc = new QueryClient();
  heldFrom(qc, 2000, 2399);
  const list = await show(qc);
  const box = list.parentElement as HTMLElement;
  await rest(box);
  expect(reads).toEqual([]);
  press(list);
  // the rows shown stay up until the first page is in
  expect(within(list).getByRole("option", { name: /^2000\b/ })).toBeTruthy();
  await within(list).findByRole("option", { name: /^0\b/ });
  expect(box.scrollTop).toBe(0);
  await rest(box);
  expect(reads).toEqual(["after=-"]);
});

// Scrolls as far as happy-dom can: it lays nothing out, so a list's scroll height is the set
// height of its first child.
function scrolls() {
  const to = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(function (
    this: HTMLElement,
    at?: ScrollToOptions | number,
  ) {
    this.scrollTop = typeof at === "object" ? (at.top ?? 0) : 0;
    this.dispatchEvent(new Event("scroll"));
  });
  const height = vi
    .spyOn(HTMLElement.prototype, "scrollHeight", "get")
    .mockImplementation(function (this: HTMLElement) {
      return Number.parseFloat((this.firstElementChild as HTMLElement | null)?.style.height ?? "");
    });
  onTestFinished(() => {
    to.mockRestore();
    height.mockRestore();
  });
}

test("First at the end of a list reads from the first event if the start let it go", async () => {
  scrolls();
  log = range(0, 2399);
  const qc = new QueryClient();
  heldFrom(qc, 2000, 2199);
  const list = await show(qc);
  fireEvent.click(screen.getByRole("button", { name: "Last" }));
  await within(list).findByRole("option", { name: /^2399\b/ });
  fireEvent.click(screen.getByRole("button", { name: "First" }));
  await within(list).findByRole("option", { name: /^0\b/ });
  expect(reads).toEqual([`before=${Number.MAX_SAFE_INTEGER}`, "after=-"]);
});

test.each([
  ["a wheel up", (list: HTMLElement) => fireEvent.wheel(list.parentElement!, { deltaY: -100 })],
  ["an ArrowUp", (list: HTMLElement) => fireEvent.keyDown(list, { key: "ArrowUp" })],
])("%s at the top of a list that let its first pages go reads them back", async (_, move) => {
  log = range(0, 2399);
  const qc = new QueryClient();
  heldFrom(qc, 2000, 2399);
  const list = await show(qc, { seq: 2000 });
  const box = list.parentElement as HTMLElement;
  await rest(box);
  expect(reads).toEqual([]);
  move(list);
  await waitFor(() => expect(reads).toEqual(["before=2000"]));
  // the rows read in above leave the view where it was
  await waitFor(() => expect(box.scrollTop).toBe(200 * ROW));
  await rest(box);
  expect(reads).toEqual(["before=2000"]);
});
