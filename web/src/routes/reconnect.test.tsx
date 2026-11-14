// The browser gives up on the stream for good when a reconnect is answered with an error, as by
// a proxy while the server restarts. What the page does then.
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { REOPEN_MS } from "@/components/live";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { preloadRoutes } from "@/test/routes";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
preloadRoutes();
stubHeight();

// what the next read of the session gets; null for the signed-in operator
let session: (() => Response) | null;

beforeEach(() => {
  session = null;
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session") {
      const answer = session;
      session = null;
      return answer?.() ?? Response.json({ name: "op", role: "operator", auth: true });
    }
    if (path === "/api/runs") return Response.json({ items: [run("r1")], next: null });
    if (path === "/api/scenarios") return Response.json([]);
    if (path === "/api/models") return Response.json({ profiles: [], roles: {} });
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

async function shown() {
  const router = makeRouter(createMemoryHistory({ initialEntries: ["/runs"] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Runs" });
  const source = FakeEventSource.all[0];
  if (!source) throw new Error("no stream");
  act(() => source.open());
  act(() => source.send({ kind: "runner", runner: runnerInfo() }, "@57"));
  await screen.findByText("tracer on");
  return source;
}

const usable = () => FakeEventSource.all.filter((s) => s.readyState !== FakeEventSource.CLOSED);

test("a stream the browser gave up on opens again while the session stands", async () => {
  const source = await shown();
  act(() => source.fail(true));
  expect(usable()).toHaveLength(0);
  await waitFor(() => expect(usable()).toHaveLength(1), { timeout: REOPEN_MS + 2000 });
  // from where the last one got to, so the runs that changed meanwhile come
  expect(new URL(usable()[0]?.url ?? "", location.href).searchParams.get("last_event_id")).toBe(
    "@57",
  );
  // still away: it waits longer, then tries again
  const second = usable()[0];
  act(() => second?.fail(true));
  await new Promise((r) => setTimeout(r, REOPEN_MS + 200));
  expect(usable()).toHaveLength(0);
  await waitFor(() => expect(usable()).toHaveLength(1), { timeout: REOPEN_MS + 2000 });
  act(() => usable()[0]?.open());
  expect(screen.queryByText("Reconnecting")).toBeNull();
  expect(screen.getByRole("heading", { name: "Runs" })).toBeTruthy();
}, 15_000);

test("a refused session shows the sign-in form and opens no stream", async () => {
  const source = await shown();
  session = () =>
    Response.json(
      { type: "about:blank", title: "Unauthorized", status: 401 },
      { status: 401, headers: { "content-type": "application/problem+json" } },
    );
  act(() => source.fail(true));
  await screen.findByLabelText("Token");
  await new Promise((r) => setTimeout(r, REOPEN_MS + 300));
  expect(usable()).toHaveLength(0);
});

test.each([
  ["no body", () => new Response(null, { status: 502, headers: { "Content-Length": "0" } })],
  ["a text body", () => new Response("Bad Gateway", { status: 502 })],
])("a session read answered 502 with %s says the control plane is away", async (_, gateway) => {
  const source = await shown();
  session = gateway;
  act(() => source.fail(true));
  await screen.findByText(/Cannot reach the control plane/);
  expect(screen.queryByText(/Something went wrong/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByRole("heading", { name: "Runs" });
  await waitFor(() => expect(usable()).toHaveLength(1));
});
