import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { preloadRoutes } from "@/test/routes";

const RUN = "01a18a50-bee5-7263-ad19-24385640cee1";
const fetch = vi.fn<(request: Request) => Promise<Response>>();
let code: string;

preloadRoutes();
beforeEach(() => {
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session") return Response.json({ name: "v", role: "viewer", auth: true });
    if (path.startsWith(`/api/runs/${RUN}`)) {
      return Response.json(
        { type: "about:blank", title: "Forbidden", status: 403, detail: "refused", code },
        { status: 403, headers: { "content-type": "application/problem+json" } },
      );
    }
    return Response.json({ items: [], next: null });
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

test.each([
  ["truth", "it shows what a scenario hides."],
  ["kind", "it is of a kind this version does not know."],
])("a run refused to a viewer for its %s says why", async (refused, why) => {
  code = refused;
  const router = makeRouter(createMemoryHistory({ initialEntries: [`/runs/${RUN}`] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  const text = await screen.findByText(/^Only operators can open this run/);
  expect(text.textContent).toBe(`Only operators can open this run: ${why}Back to runs`);
});
