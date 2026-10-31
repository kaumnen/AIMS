import { createMemoryHistory } from "@tanstack/react-router";
import { beforeAll } from "vitest";

import { makeRouter } from "@/router";

// Route chunks load lazily and transform on first use. On a cold CI worker that took over 5 s,
// inside the first test's findBy, so load them all, and any lazy part the file opens, before
// any test starts its clock.
export function preloadRoutes(...parts: (() => Promise<unknown>)[]): void {
  beforeAll(async () => {
    const router = makeRouter(createMemoryHistory());
    await Promise.all([
      ...Object.values(router.routesById).map((r) => router.loadRouteChunk(r)),
      ...parts.map((load) => load()),
    ]);
  }, 60_000);
}
