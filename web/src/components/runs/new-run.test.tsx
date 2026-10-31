import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Estimate } from "@/api/launch";
import type { ScenarioInfo } from "@/api/scenarios";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { pick } from "@/test/pick";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: string;
let posted: Record<string, unknown>[];
let checks: string[][];

const NEW = "01a19000-0000-7000-8000-000000000001";

stubHeight();

const scenarios: ScenarioInfo[] = [
  {
    source: "project",
    name: "cache-keys",
    title: "cache-keys",
    writable: true,
    error: null,
    oversight: "human",
    agents: 1,
    profiles: ["spark"],
    variants: ["observed", "unobserved"],
  },
  {
    source: "project",
    name: "watched",
    title: "watched",
    writable: true,
    error: null,
    oversight: "observe",
    agents: 2,
    profiles: ["spark"],
    variants: [],
  },
  {
    source: "project",
    name: "broken",
    title: null,
    writable: true,
    error: "bad",
    oversight: null,
    agents: null,
    profiles: [],
    variants: [],
  },
];

function estimate(samples: number): Estimate {
  const ceiling = 9 * samples;
  return {
    scenario: "cache-keys",
    samples,
    lines: ["Cost estimate for cache-keys.", `Total: cannot exceed $${ceiling.toFixed(2)}.`],
    warnings: [],
    confirm: ceiling > 20,
    confirm_above_usd: 20,
    credits: [],
    parts: [
      {
        variant: null,
        agents: {} as Estimate["parts"][number]["agents"],
        monitor: null,
        monitor_preset: "standard",
        steps: {},
      },
    ],
    rates: [],
    total: { typical_usd: null, high_usd: null, ceiling_usd: ceiling },
    unpriced: [],
  };
}

beforeEach(() => {
  FakeEventSource.all = [];
  role = "operator";
  posted = [];
  checks = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/api/session") return Response.json({ name: "op", role, auth: true });
    if (path === "/api/scenarios") return Response.json(scenarios);
    if (path === "/api/models") {
      return Response.json({
        roles: { agent: "spark" },
        profiles: [
          {
            name: "spark",
            backend: "responses",
            model: "muse-spark",
            weights: "w",
            reasoning: "summary",
            roles: ["agent"],
            envs: ["MODEL_API_KEY"],
            runners: ["local"],
            check: {
              run_id: "c1",
              started_at: "2026-10-31T09:00:00+00:00",
              status: "completed",
              checks: [{ name: "tools", verdict: "warn", detail: "usage lacks cached" }],
            },
          },
          {
            name: "wonder",
            backend: "responses",
            model: "muse-wonder",
            weights: "w",
            reasoning: "summary",
            roles: [],
            envs: ["OTHER_KEY"],
            runners: [],
            check: null,
          },
        ],
      });
    }
    if (path.endsWith("/estimate")) {
      return Response.json(estimate(Number(url.searchParams.get("samples") ?? 1)));
    }
    if (path === "/api/models/check" && request.method === "POST") {
      checks.push(((await request.json()) as { profiles: string[] }).profiles);
      return Response.json({ run_ids: ["c2"], group: null }, { status: 202 });
    }
    if (path === "/api/runs" && request.method === "POST") {
      const body = (await request.json()) as Record<string, unknown>;
      posted.push(body);
      const ids = Array.from({ length: Number(body["samples"]) }, (_, i) => `${NEW}${i || ""}`);
      return Response.json({ run_ids: ids, group: ids.length > 1 ? "g" : null }, { status: 202 });
    }
    if (path === "/api/runs") return Response.json({ items: [], next: null });
    if (path === `/api/runs/${NEW}`) {
      return Response.json({ run: run(NEW), monitor_runs: [], score_runs: [] });
    }
    return Response.json({ items: [], next: null });
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function renderAt(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const models = () => fetch.mock.calls.filter(([r]) => new URL(r.url).pathname === "/api/models");

async function openForm() {
  fireEvent.click(await screen.findByRole("button", { name: "New run" }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByText("Total: cannot exceed $9.00.");
  return within(dialog);
}

test("viewers get no new run button", async () => {
  role = "viewer";
  renderAt("/runs");
  await screen.findByRole("button", { name: "Agent runs" });
  expect(screen.queryByRole("button", { name: "New run" })).toBeNull();
});

test("the form shows the scenario, its oversight, profile checks and the estimate", async () => {
  renderAt("/runs");
  const form = await openForm();
  expect(form.getByRole("combobox", { name: "Scenario" }).textContent).toContain("cache-keys");
  expect(form.queryByText(/broken/)).toBeNull();
  expect(form.getByText(/Human oversight: when the monitor pauses an agent/)).toBeTruthy();
  expect(form.queryByRole("alert")).toBeNull();
  expect(form.getByText("spark: works with warnings")).toBeTruthy();
  expect(form.getByText("tools: usage lacks cached")).toBeTruthy();
  expect(form.getByRole("combobox", { name: "Monitor" }).textContent).toContain(
    "From config (standard)",
  );

  await pick(form, "Scenario", /watched/);
  expect((await form.findByRole("alert")).textContent).toContain("Observe oversight");
  expect(form.queryByRole("combobox", { name: "Variant" })).toBeNull();

  await pick(form, "Monitor", "off");
  expect(form.getByRole("alert").textContent).toContain("No monitor");

  await pick(form, "Profile", /wonder/);
  expect(form.getByText("wonder: never checked")).toBeTruthy();
  expect(form.getByText("No connected runner has OTHER_KEY.")).toBeTruthy();
});

test("a model check runs on demand and reads the result once it ends", async () => {
  renderAt("/runs");
  const form = await openForm();
  const before = models().length;
  fireEvent.click(form.getByRole("button", { name: "Check" }));
  await waitFor(() => expect(checks).toEqual([["spark"]]));
  expect(await form.findByText("spark: checking")).toBeTruthy();
  const source = FakeEventSource.all[0];
  if (!source) throw new Error("no stream");
  act(() => source.send({ kind: "run", run: run("c2", { status: "queued", state: "claimed" }) }));
  act(() => source.send({ kind: "run", run: run("c2", { status: "running", state: "running" }) }));
  expect(form.getByText("spark: checking")).toBeTruthy();
  expect(models().length).toBe(before);
  act(() => source.send({ kind: "run", run: run("c2", { status: "completed", state: "done" }) }));
  await waitFor(() => expect(models().length).toBe(before + 1));
  expect(await form.findByText("spark: works with warnings")).toBeTruthy();
});

test("a run starts with the chosen options and opens", async () => {
  const router = renderAt("/runs");
  const source = await waitFor(() => {
    const s = FakeEventSource.all[0];
    if (!s) throw new Error("no stream");
    return s;
  });
  act(() => source.send({ kind: "runner", runner: runnerInfo({ kinds: ["scenario"] }) }));
  const form = await openForm();
  await pick(form, "Variant", "unobserved");
  await pick(form, "Runner", /local/);
  fireEvent.click(form.getByRole("checkbox", { name: /Score each run/ }));
  fireEvent.click(form.getByRole("button", { name: "Start run" }));
  await waitFor(() => expect(router.state.location.pathname).toBe(`/runs/${NEW}`));
  expect(posted).toEqual([
    {
      scenario: { source: "project", name: "cache-keys" },
      samples: 1,
      audit: "kernel",
      keep: false,
      score: true,
      confirm: false,
      variant: "unobserved",
      runner: "local",
    },
  ]);
});

test("runs above the confirm limit wait for a yes", async () => {
  const router = renderAt("/runs");
  const form = await openForm();
  fireEvent.change(form.getByRole("spinbutton", { name: "Samples" }), { target: { value: "3" } });
  await form.findByText("Total: cannot exceed $27.00.");
  const start = form.getByRole("button", { name: "Start 3 runs" });
  expect(start.hasAttribute("disabled")).toBe(true);
  fireEvent.click(
    form.getByRole("checkbox", { name: /The caps allow \$27.00, above the \$20.00 limit/ }),
  );
  expect(start.hasAttribute("disabled")).toBe(false);
  fireEvent.click(start);
  await waitFor(() => expect(router.state.location.search).toEqual({ scenario: "cache-keys" }));
  expect(posted[0]).toMatchObject({ samples: 3, confirm: true });
});
