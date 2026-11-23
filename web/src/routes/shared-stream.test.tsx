// A run page on the stream that tabs share through a worker, as in desktop browsers.
import { act, cleanup, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { REOPEN_MS } from "@/api/live";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { renderAt, reportDetail, RUN, serve, served } from "@/test/report";
import { preloadRoutes } from "@/test/routes";
import { shareStreams } from "@/test/shared-worker";

preloadRoutes();
stubHeight();

beforeEach(() => {
  FakeEventSource.all = [];
  shareStreams();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const carries = (s: FakeEventSource) =>
  new URL(s.url, location.href).searchParams.getAll("run").includes(RUN);

test("a run page follows its run again once the shared stream opens after the browser gave up", async () => {
  const fetch = serve({
    ...served(),
    detail: reportDetail({ status: "running", state: "running", ended_at: null }),
  });
  renderAt(`/runs/${RUN}`);
  const first = await waitFor(() => {
    const s = FakeEventSource.all.find(carries);
    if (!s) throw new Error("no stream carries the run");
    return s;
  });
  act(() => first.open());
  act(() => first.fail(true));
  // the page's stream opens again, and the run's tail with it
  const again = await waitFor(
    () => {
      const s = FakeEventSource.all.at(-1);
      if (s === undefined || s === first || !carries(s)) throw new Error("not yet");
      return s;
    },
    { timeout: REOPEN_MS + 2000 },
  );
  act(() => again.open());
  const steps = () =>
    fetch.mock.calls.filter(([r]) => new URL(r.url).pathname === `/api/runs/${RUN}/steps`).length;
  const before = steps();
  // the run ends while the page shows it: its last steps are read
  act(() => again.send({ kind: "end", run_id: RUN }));
  await waitFor(() => expect(steps()).toBeGreaterThan(before));
}, 15_000);
