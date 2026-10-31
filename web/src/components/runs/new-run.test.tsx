import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Estimate } from "@/api/launch";
import type { ScenarioInfo } from "@/api/scenarios";
import type { SeriesInfo } from "@/api/series";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { pick } from "@/test/pick";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: string;
let posted: Record<string, unknown>[];
let checks: string[][];
let budgets: (string | null)[];
let samplesAsked: number[];
let seriesFound: Record<string, SeriesInfo>;
let seriesPost: Response | null;

const CAPS = { steps: 80, output_tokens: null, cost_usd: 4, wall_s: 1800 };
const NO_CAPS = { steps: null, output_tokens: null, cost_usd: null, wall_s: null };

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

function estimate(samples: number, budget: string | null = null): Estimate {
  const ceiling = 9 * samples;
  const every = budget ? (JSON.parse(budget) as { every?: { steps?: number } }).every : undefined;
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
        replayed: {},
        caps: { agents: { agent: { ...CAPS, ...every } }, run: NO_CAPS },
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
  budgets = [];
  samplesAsked = [];
  seriesFound = {};
  seriesPost = null;
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
      const budget = url.searchParams.get("budget");
      budgets.push(budget);
      samplesAsked.push(Number(url.searchParams.get("samples") ?? 1));
      return Response.json(estimate(Number(url.searchParams.get("samples") ?? 1), budget));
    }
    if (path === "/api/models/check" && request.method === "POST") {
      checks.push(((await request.json()) as { profiles: string[] }).profiles);
      return Response.json({ run_ids: ["c2"], group: null }, { status: 202 });
    }
    if (path.startsWith("/api/series/")) {
      const name = decodeURIComponent(path.slice("/api/series/".length));
      if (name === "broken") {
        return Response.json(
          {
            type: "about:blank",
            title: "Invalid request",
            status: 422,
            code: "invalid",
            errors: [],
          },
          { status: 422, headers: { "content-type": "application/problem+json" } },
        );
      }
      return Response.json(
        seriesFound[name] ?? { name, next_index: 0, previous: null, busy: null, runs: [] },
      );
    }
    if (path === "/api/runs" && request.method === "POST") {
      const body = (await request.json()) as Record<string, unknown>;
      posted.push(body);
      if (seriesPost) return seriesPost;
      const count = Number(body["series"] ? body["runs"] : body["samples"]);
      const ids = Array.from({ length: count }, (_, i) => `${NEW}${i || ""}`);
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

test("an oversight over the scenario's changes the warning and goes with the run", async () => {
  renderAt("/runs");
  const form = await openForm();
  expect(form.getByRole("combobox", { name: "Oversight" }).textContent).toContain(
    "From the scenario (human)",
  );
  await pick(form, "Oversight", "autonomous");
  expect(form.getByRole("alert").textContent).toContain("Autonomous oversight");
  fireEvent.click(form.getByRole("button", { name: "Start run" }));
  await waitFor(() => expect(posted).toHaveLength(1));
  expect(posted[0]).toMatchObject({ oversight: "autonomous" });
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

test("a budget over the scenario's caps is estimated and goes with the run", async () => {
  renderAt("/runs");
  const form = await openForm();
  const fold = form.getByText("Budget").closest("details");
  if (!fold) throw new Error("no budget section");
  expect(fold.open).toBe(false);
  expect(within(fold).getByText("Scenario caps")).toBeTruthy();
  fold.open = true;
  const steps = form.getByRole("textbox", { name: "Agent steps" });
  const wall = form.getByRole("textbox", { name: "Agent wall" });
  expect([steps.getAttribute("placeholder"), wall.getAttribute("placeholder")]).toEqual([
    "80",
    "30m",
  ]);
  expect(form.getByRole("textbox", { name: "Run cost" }).getAttribute("placeholder")).toBe(
    "no cap",
  );
  fireEvent.change(steps, { target: { value: "200" } });
  fireEvent.change(form.getByRole("textbox", { name: "Run cost" }), { target: { value: "none" } });
  await waitFor(() =>
    expect(budgets.at(-1)).toBe(JSON.stringify({ every: { steps: 200 }, run: { cost_usd: null } })),
  );
  expect(within(fold).getByText("steps 200, run cost no cap")).toBeTruthy();

  fireEvent.change(wall, { target: { value: "soon" } });
  expect(await form.findByText("Wall: Write seconds, or 30m, 2h, or none.")).toBeTruthy();
  const start = form.getByRole("button", { name: "Start run" });
  expect(start.hasAttribute("disabled")).toBe(true);
  fireEvent.change(wall, { target: { value: "2h" } });
  expect(start.hasAttribute("disabled")).toBe(false);
  fireEvent.click(start);
  await waitFor(() => expect(posted).toHaveLength(1));
  expect(posted[0]).toMatchObject({
    budget: { every: { steps: 200, wall_s: 7200 }, run: { cost_usd: null } },
  });
});

const LAST = "01a18000-0000-7000-8000-00000000000a";

async function openSeries(form: ReturnType<typeof within>) {
  const fold = form.getByText("Series").closest("details");
  if (!fold) throw new Error("no series section");
  expect(fold.open).toBe(false);
  expect(within(fold).getByText("None")).toBeTruthy();
  fold.open = true;
  return within(fold);
}

test("a series goes on where the server says, counts its runs and starts them", async () => {
  seriesFound["nightly"] = {
    name: "nightly",
    next_index: 5,
    previous: LAST,
    busy: null,
    runs: [run(LAST, { series: "nightly", series_index: 4 })],
  };
  const router = renderAt("/runs");
  const form = await openForm();
  const fold = await openSeries(form);
  expect(fold.getByRole("spinbutton", { name: "Runs" }).hasAttribute("disabled")).toBe(true);
  fireEvent.change(fold.getByRole("textbox", { name: "Name" }), { target: { value: "nightly" } });
  fireEvent.change(fold.getByRole("spinbutton", { name: "Runs" }), { target: { value: "3" } });
  expect(await fold.findByText("Goes on after #4; the next run is #5.")).toBeTruthy();
  expect(fold.getByText("nightly · 3 runs")).toBeTruthy();
  expect(form.getByRole("spinbutton", { name: "Samples" }).hasAttribute("disabled")).toBe(true);
  expect(form.getByRole("spinbutton", { name: "At once" }).hasAttribute("disabled")).toBe(true);
  await form.findByText("Total: cannot exceed $27.00.");
  expect(samplesAsked.at(-1)).toBe(3);
  fireEvent.click(form.getByRole("checkbox", { name: /above the \$20.00 limit/ }));
  fireEvent.click(form.getByRole("button", { name: "Start 3 runs" }));
  await waitFor(() => expect(router.state.location.search).toEqual({ series: "nightly" }));
  expect(posted[0]).toMatchObject({ samples: 1, series: "nightly", runs: 3, confirm: true });
  expect(posted[0]).not.toHaveProperty("restart");
});

test("a busy series holds the start and a restart says so", async () => {
  seriesFound["nightly"] = {
    name: "nightly",
    next_index: 2,
    previous: null,
    busy: NEW,
    runs: [run(NEW, { series: "nightly", series_index: 1, status: "waiting", state: "waiting" })],
  };
  renderAt("/runs");
  const form = await openForm();
  const fold = await openSeries(form);
  fireEvent.change(fold.getByRole("textbox", { name: "Name" }), { target: { value: "nightly" } });
  expect(await fold.findByText("Busy: #1 is waiting. Start after it ends.")).toBeTruthy();
  expect(form.getByRole("button", { name: "Start run" }).hasAttribute("disabled")).toBe(true);

  fireEvent.change(fold.getByRole("textbox", { name: "Name" }), { target: { value: "fresh" } });
  expect(await fold.findByText("New series; starts at #0.")).toBeTruthy();
  fireEvent.click(fold.getByRole("checkbox", { name: "Start the series again at #0" }));
  expect(fold.getByText("Starts again at #0 from the scenario's own folders.")).toBeTruthy();
  seriesPost = Response.json(
    {
      type: "about:blank",
      title: "Conflict",
      status: 409,
      detail: `run ${NEW} of series fresh has not ended`,
      code: "series_busy",
      errors: [],
    },
    { status: 409, headers: { "content-type": "application/problem+json" } },
  );
  const start = form.getByRole("button", { name: "Start run" });
  await waitFor(() => expect(start.hasAttribute("disabled")).toBe(false));
  fireEvent.click(start);
  expect(
    await form.findByText(new RegExp(`run ${NEW} of series fresh has not ended`)),
  ).toBeTruthy();
  expect(posted[0]).toMatchObject({ series: "fresh", runs: 1, restart: true });
});

test("a series that does not load leaves the check to the server", async () => {
  renderAt("/runs");
  const form = await openForm();
  const fold = await openSeries(form);
  fireEvent.change(fold.getByRole("textbox", { name: "Name" }), { target: { value: "broken" } });
  expect(
    await fold.findByText("The series did not load; the server checks it when the runs start."),
  ).toBeTruthy();
  expect(form.getByRole("button", { name: "Start run" }).hasAttribute("disabled")).toBe(false);
});
