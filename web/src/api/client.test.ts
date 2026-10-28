import { afterEach, expect, expectTypeOf, test, vi } from "vitest";

import { $api, client, type Problem, type Schemas } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("errors are typed as problems", async () => {
  const problem = { title: "Not signed in", status: 401 };
  const headers = { "content-type": "application/problem+json" };
  vi.stubGlobal("fetch", async () => Response.json(problem, { status: 401, headers }));
  const { data, error } = await client.GET("/api/runs", { params: { query: { limit: 10 } } });
  expectTypeOf(data).toEqualTypeOf<Schemas["RunPage"] | undefined>();
  expectTypeOf(error).branded.toEqualTypeOf<Problem | undefined>();
  expect(error?.title).toBe("Not signed in");
});

test("queryOptions keys by method, path and init", () => {
  const options = $api.queryOptions("get", "/api/runs/{run_id}", {
    params: { path: { run_id: "r" } },
  });
  expect(options.queryKey).toEqual([
    "get",
    "/api/runs/{run_id}",
    { params: { path: { run_id: "r" } } },
  ]);
  expectTypeOf(options.queryKey[1]).toEqualTypeOf<"/api/runs/{run_id}">();
});
