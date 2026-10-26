import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { makeRouter } from "./router";

afterEach(cleanup);

async function renderAt(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

test("root redirects to runs", async () => {
  const router = await renderAt("/");
  expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();
  expect(router.state.location.pathname).toBe("/runs");
});

test("scenarios route renders", async () => {
  await renderAt("/scenarios");
  expect(await screen.findByRole("heading", { name: "Scenarios" })).toBeTruthy();
});
