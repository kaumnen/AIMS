import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { handoff } from "./session";

const fetch = vi.fn<(request: Request) => Promise<Response>>();

beforeEach(() => {
  fetch.mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetch.mockReset();
});

test("trades the code for a session and clears the fragment", async () => {
  history.replaceState(null, "", "/runs?q=1#code=abc");
  await handoff();
  expect(location.pathname + location.search + location.hash).toBe("/runs?q=1");
  expect(fetch).toHaveBeenCalledOnce();
  const request = fetch.mock.lastCall?.[0];
  expect(request?.method).toBe("POST");
  expect(request && new URL(request.url).pathname).toBe("/api/session");
  expect(await request?.json()).toEqual({ code: "abc" });
});

test.each([
  ["rejected", () => fetch.mockRejectedValue(new TypeError("offline"))],
  [
    "refused",
    () => fetch.mockResolvedValue(Response.json({ title: "x", status: 401 }, { status: 401 })),
  ],
])("clears the fragment when the trade is %s", async (_, fail) => {
  fail();
  history.replaceState(null, "", "/#code=abc");
  await handoff();
  expect(location.hash).toBe("");
  expect(fetch).toHaveBeenCalledOnce();
});

test("sends nothing without a code", async () => {
  history.replaceState(null, "", "/runs#other");
  await handoff();
  expect(fetch).not.toHaveBeenCalled();
  expect(location.hash).toBe("#other");
});
