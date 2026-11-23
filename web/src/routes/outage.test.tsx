// A run page through a time with no stream: the control plane out of reach, or a stream the
// browser gave up on. Its lists read on from where they got to, not every page again.
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Event } from "@/api/events";
import { REOPEN_MS } from "@/api/live";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { renderAt, reportDetail, RUN, serve, served } from "@/test/report";
import { preloadRoutes } from "@/test/routes";

preloadRoutes();
stubHeight();

// the run logged events 0 to 9, as its detail says
const LOG = Array.from(
  { length: 10 },
  (_, seq) =>
    ({
      seq,
      type: "file.access",
      ts: "2026-10-30T10:00:00Z",
      agent_id: "agent",
      data: {},
    }) as Event,
);

let away: boolean;
let fetch: ReturnType<typeof serve>;

beforeEach(() => {
  away = false;
  FakeEventSource.all = [];
});

// the run ended, or still runs
function serveRun(running: boolean) {
  const detail = running
    ? reportDetail({ status: "running", state: "running", ended_at: null })
    : reportDetail();
  fetch = serve({ ...served(), detail });
  const answer = fetch.getMockImplementation();
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (away && url.pathname === "/api/session") {
      return new Response(null, { status: 502, headers: { "Content-Length": "0" } });
    }
    if (url.pathname === `/api/runs/${RUN}/events` && url.searchParams.get("limit") === "200") {
      const after = Number(url.searchParams.get("after") ?? -1);
      return Response.json({ items: LOG.filter((e) => e.seq > after), next: null });
    }
    return answer!(request);
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// reads of a list's pages since call `from`, and the tail's reads of steps that can still change
const reads = (from: number) => {
  const urls = fetch.mock.calls.slice(from).map(([r]) => new URL(r.url));
  const of = (path: string, limit: string) =>
    urls.filter(
      (u) => u.pathname === `/api/runs/${RUN}/${path}` && u.searchParams.get("limit") === limit,
    ).length;
  return { steps: of("steps", "200"), raw: of("events", "200"), tail: of("steps", "500") };
};

// the page's stream, once the page shows
async function shown(tab: string, ready: RegExp) {
  renderAt(`/runs/${RUN}?tab=${tab}`);
  await screen.findByText(ready);
  const page = FakeEventSource.all.find((s) => s.url.includes("runs=true"));
  if (!page) throw new Error("no stream");
  act(() => page.open());
  return page;
}

test("a run page back after the control plane was out of reach reads on its steps", async () => {
  serveRun(true);
  const page = await shown("steps", /Step 3/);
  away = true;
  act(() => page.fail(true));
  const again = await screen.findByRole("button", { name: "Try again" });
  const from = fetch.mock.calls.length;
  away = false;
  fireEvent.click(again);
  await screen.findByText(/Step 3/);
  await waitFor(() => expect(reads(from).tail).toBe(1));
  expect(reads(from).steps).toBe(0);
});

test("an ended run's raw list reads no page again on a focus after the stream opened again", async () => {
  serveRun(false);
  const page = await shown("raw", /10 events/);
  act(() => page.fail(true));
  const reopened = await waitFor(
    () => {
      const s = FakeEventSource.all.at(-1);
      if (s === undefined || s === page) throw new Error("not yet");
      return s;
    },
    { timeout: REOPEN_MS + 2000 },
  );
  act(() => reopened.open());
  const from = fetch.mock.calls.length;
  act(() => void window.dispatchEvent(new Event("visibilitychange")));
  await new Promise((r) => setTimeout(r, 100));
  expect(reads(from).raw).toBe(0);
  expect(screen.getByText(/10 events/)).toBeTruthy();
}, 15_000);
