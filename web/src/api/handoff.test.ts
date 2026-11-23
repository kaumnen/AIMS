// A tab opened by `aims ui` trades its code for a session while another tab holds the stream
// all tabs share.
import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";
import { shareStreams } from "@/test/shared-worker";

import { type FromHub, PROTOCOL } from "./hub";
import { handoff } from "./session";

const fetch = vi.fn<(request: Request) => Promise<Response>>();

beforeEach(() => {
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetch.mockReset();
});

test("a new session opens the shared stream again, a refused code leaves it", async () => {
  const hub = shareStreams();
  // the other tab's list, on the old cookie
  const other = { send: vi.fn<(m: FromHub) => void>() };
  hub.receive(other, { t: "hello", v: PROTOCOL });
  hub.receive(other, { t: "sub", id: 1, wanted: { runs: true, run: [], deltas: [] } });
  const first = await waitFor(() => {
    const s = FakeEventSource.all[0];
    if (!s) throw new Error("no stream");
    return s;
  });
  first.open();

  fetch.mockResolvedValue(Response.json({ title: "x", status: 401 }, { status: 401 }));
  history.replaceState(null, "", "/#code=old");
  await handoff();
  await new Promise((r) => setTimeout(r, 100));
  expect(FakeEventSource.all).toEqual([first]);

  fetch.mockResolvedValue(new Response(null, { status: 204 }));
  history.replaceState(null, "", "/#code=abc");
  await handoff();
  await waitFor(() => expect(FakeEventSource.all).toHaveLength(2));
  expect(first.readyState).toBe(FakeEventSource.CLOSED);
});
