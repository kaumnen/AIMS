import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ProfileInfo } from "@/api/launch";
import type { ProviderCredit } from "@/api/settings";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: "operator" | "viewer";
let checked: ProfileInfo["check"];
let credits: ProviderCredit[];
const CHECK = "01a19200-0000-7000-8000-000000000001";

const profile = (over: Partial<ProfileInfo>): ProfileInfo => ({
  name: "spark",
  backend: "responses",
  model: "spark-1.3",
  weights: "closed",
  reasoning: "medium",
  roles: ["agent"],
  envs: ["META_API_KEY"],
  runners: ["local"],
  check: null,
  ...over,
});

const credit = (over: Partial<ProviderCredit>): ProviderCredit => ({
  provider: "meta",
  spent_usd: 0,
  credit_usd: null,
  left_usd: null,
  left_after_typical_usd: null,
  left_after_high_usd: null,
  ...over,
});

const posts = () =>
  fetch.mock.calls.map(([r]) => r).filter((r) => r.method === "POST" && r.url.includes("/check"));

beforeEach(() => {
  role = "operator";
  checked = null;
  credits = [
    credit({ provider: "fireworks", spent_usd: 1.5, credit_usd: null }),
    credit({ provider: "meta", spent_usd: 145, credit_usd: 150, left_usd: 5 }),
    credit({ provider: "modal", spent_usd: 21, credit_usd: 20, left_usd: -1 }),
  ];
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/session") return Response.json({ name: "me", role, auth: true });
    if (url.pathname === "/api/models")
      return Response.json({
        profiles: [
          profile({ check: checked }),
          profile({ name: "glimmer", roles: [], envs: ["MODAL_KEY"], runners: [] }),
        ],
        roles: { agent: "spark" },
      });
    if (url.pathname === "/api/models/check") {
      checked = {
        run_id: CHECK,
        started_at: "2026-10-31T10:00:00+00:00",
        status: "completed",
        checks: [{ name: "tools", verdict: "warn", detail: "slow" }],
      };
      return Response.json({ run_ids: [CHECK] }, { status: 202 });
    }
    if (url.pathname === "/api/runners")
      return Response.json([
        runnerInfo({ name: "box", local: false, connected: false, stale: true, seen_s: 7200 }),
        runnerInfo({ name: "local", connected: false, stale: true }),
      ]);
    if (url.pathname === "/api/spend") return Response.json({ providers: credits, lines: [] });
    return Response.json({});
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

test("settings open on the model profiles and a check follows its run", async () => {
  const router = renderAt("/settings");
  const table = await screen.findByRole("table");
  expect(router.state.location.pathname).toBe("/settings/models");
  const spark = within(table).getByText("spark").closest("tr") as HTMLElement;
  expect(within(spark).getByText("agent")).toBeTruthy();
  expect(within(spark).getByText("set on local")).toBeTruthy();
  expect(within(spark).getByText("never checked")).toBeTruthy();
  const glimmer = within(table).getByText("glimmer").closest("tr") as HTMLElement;
  expect(within(glimmer).getByText("no connected runner has them")).toBeTruthy();

  fireEvent.click(within(spark).getByRole("button", { name: "Check spark" }));
  expect(await within(spark).findByText("checking")).toBeTruthy();
  expect(await posts()[0]?.json()).toEqual({ profiles: ["spark"] });
  expect(screen.getByRole("button", { name: "Check all" })).toHaveProperty("disabled", true);

  act(() => {
    FakeEventSource.all[0]?.open();
    FakeEventSource.all[0]?.send({
      kind: "run",
      run: run(CHECK, { kind: "models.check", status: "completed", state: "done" }),
    });
  });
  expect(await within(spark).findByText("works with warnings")).toBeTruthy();
  expect(within(spark).getByText("tools: slow")).toBeTruthy();
  expect(within(spark).getByRole("link").getAttribute("href")).toBe(`/runs/${CHECK}`);
});

test("viewers see the profiles without checks", async () => {
  role = "viewer";
  renderAt("/settings/models");
  await screen.findByRole("table");
  expect(screen.queryByRole("button", { name: /Check/ })).toBeNull();
});

test("runners join the list with the live stream", async () => {
  renderAt("/settings/runners");
  const table = await screen.findByRole("table");
  const box = within(table).getByText("box").closest("tr") as HTMLElement;
  expect(within(box).getByText("Offline · seen 2h 00m ago")).toBeTruthy();
  expect(screen.getByText("0 of 2 online")).toBeTruthy();

  act(() => {
    FakeEventSource.all[0]?.open();
    FakeEventSource.all[0]?.send({
      kind: "runner",
      runner: runnerInfo({
        free: 1,
        running: ["r1"],
        tracer: { alive: false, detail: "bpftrace exited", restarts: 2 },
      }),
    });
  });
  const local = (await within(table).findByText("this machine")).closest("tr") as HTMLElement;
  expect(within(local).getByText("Online · 1 of 2 slots free · 1 running")).toBeTruthy();
  expect(within(local).getByText("off")).toBeTruthy();
  expect(within(local).getByText("2 restarts")).toBeTruthy();
  expect(within(local).getByText("bpftrace exited")).toBeTruthy();
  expect(screen.getByText("1 of 2 online")).toBeTruthy();
});

test("spend shows each provider against its credit and what runs out", async () => {
  renderAt("/settings/spend");
  const table = await screen.findByRole("table");
  expect(
    screen.getByText("$167.50 paid to providers in the runs this control plane holds"),
  ).toBeTruthy();
  const row = (name: string) => within(table).getByText(name).closest("tr") as HTMLElement;
  expect(within(row("fireworks")).getByText("not set")).toBeTruthy();
  expect(within(row("fireworks")).queryByRole("meter")).toBeNull();
  const meta = row("meta");
  expect(within(meta).getByText("$5.00").className).toContain("text-flag");
  expect(within(meta).getByRole("meter").getAttribute("aria-valuenow")).toBe("97");
  expect(within(row("modal")).getByText("over by $1.00")).toBeTruthy();
});

test("spend is asked again when a run ends", async () => {
  renderAt("/settings/spend");
  await screen.findByRole("table");
  credits = [credit({ provider: "meta", spent_usd: 146, credit_usd: 150, left_usd: 4 })];
  act(() => {
    FakeEventSource.all[0]?.open();
    FakeEventSource.all[0]?.send({ kind: "run", run: run("r2", { state: "done" }) });
  });
  expect(await screen.findByText("$4.00")).toBeTruthy();
});
