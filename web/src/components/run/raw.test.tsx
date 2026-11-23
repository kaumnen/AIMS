import { type InfiniteData, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Event } from "@/api/events";
import { rawEventsQuery, type RawPage } from "@/api/run";
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
