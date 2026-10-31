import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ProfileInfo } from "@/api/launch";
import type { ProviderCredit, TokenInfo } from "@/api/settings";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { pick } from "@/test/pick";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: "operator" | "viewer";
let checked: ProfileInfo["check"];
let credits: ProviderCredit[];
let tokens: TokenInfo[];
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

const token = (over: Partial<TokenInfo>): TokenInfo => ({
  name: "me",
  role: "operator",
  created: 1_761_900_000,
  revoked: null,
  ui: false,
  ...over,
});

const problem = (status: number, detail: string) =>
  Response.json({ status, title: "Conflict", detail }, { status });

const calls = (method: string, path: string) =>
  fetch.mock.calls
    .map(([r]) => r)
    .filter((r) => r.method === method && new URL(r.url).pathname.startsWith(path));

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
  tokens = [
    token({ name: "old", role: "viewer", created: 1_761_800_000, revoked: 1_761_850_000 }),
    token({ name: "me" }),
    token({ name: "ui", ui: true }),
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
    if (url.pathname === "/api/tokens" && request.method === "GET") {
      if (role !== "operator") return problem(403, "operators only");
      return Response.json(tokens);
    }
    if (url.pathname === "/api/tokens" && request.method === "POST") {
      const body = (await request.clone().json()) as { name: string; role: TokenInfo["role"] };
      if (body.name === "race") return problem(409, "a token named 'race' exists");
      tokens = [...tokens, token({ name: body.name, role: body.role, created: 1_761_950_000 })];
      return Response.json({ ...body, token: "aims_secret" }, { status: 201 });
    }
    if (url.pathname.startsWith("/api/tokens/") && request.method === "DELETE") {
      const name = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      tokens = tokens.map((t) => (t.name === name ? { ...t, revoked: 1_761_960_000 } : t));
      return new Response(null, { status: 204 });
    }
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

const rowOf = (table: HTMLElement, name: string) =>
  within(table).getByText(name, { selector: "span" }).closest("tr") as HTMLElement;

test("tokens list the live ones first and mark yours and the ui token", async () => {
  renderAt("/settings/tokens");
  const table = await screen.findByRole("table");
  const names = within(table)
    .getAllByRole("row")
    .slice(1)
    .map((r) => r.querySelector("td span")?.textContent);
  expect(names).toEqual(["me", "ui", "old"]);
  expect(within(rowOf(table, "me")).getByText("you")).toBeTruthy();
  expect(within(rowOf(table, "ui")).getByText("aims ui")).toBeTruthy();
  const old = rowOf(table, "old");
  expect(within(old).getByText(/^Revoked /)).toBeTruthy();
  expect(within(old).queryByRole("button")).toBeNull();
  expect(screen.getByText("2 live · 1 revoked")).toBeTruthy();
});

test("a new token shows its value once with how to use it", async () => {
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
  renderAt("/settings/tokens");
  await screen.findByRole("table");
  fireEvent.click(screen.getByRole("button", { name: "New token" }));
  const dialog = within(await screen.findByRole("dialog"));
  const name = dialog.getByRole("textbox");
  const create = dialog.getByRole("button", { name: "Create" });

  fireEvent.change(name, { target: { value: "old" } });
  expect(dialog.getByText(/names stay taken/)).toBeTruthy();
  expect(create).toHaveProperty("disabled", true);
  fireEvent.change(name, { target: { value: "a b" } });
  expect(create).toHaveProperty("disabled", true);

  fireEvent.change(name, { target: { value: "race" } });
  fireEvent.click(create);
  expect(await dialog.findByText("a token named 'race' exists")).toBeTruthy();

  fireEvent.change(name, { target: { value: "box" } });
  await pick(dialog, "Role", "Runner");
  fireEvent.click(create);
  expect(await dialog.findByText("aims_secret")).toBeTruthy();
  expect(await calls("POST", "/api/tokens").at(-1)?.json()).toEqual({
    name: "box",
    role: "runner",
  });
  expect(dialog.getByText(/aims runner --server/)).toBeTruthy();
  fireEvent.click(dialog.getByRole("button", { name: "Copy" }));
  expect(await dialog.findByRole("button", { name: "Copied" })).toBeTruthy();
  expect(writeText).toHaveBeenCalledWith("aims_secret");

  const table = screen.getByRole("table", { hidden: true });
  await waitFor(() => expect(within(table).queryByText("box")).toBeTruthy());
  expect(dialog.getByText("aims_secret")).toBeTruthy();
  fireEvent.click(dialog.getByRole("button", { name: "Done" }));
  await waitFor(() => expect(screen.queryByText("aims_secret")).toBeNull());
});

test("revoking warns about your own token and the ui token", async () => {
  renderAt("/settings/tokens");
  const table = await screen.findByRole("table");

  fireEvent.click(within(table).getByRole("button", { name: "Revoke ui" }));
  let dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByText(/until aims ui starts again/)).toBeTruthy();
  expect(dialog.queryByText(/signs you out/)).toBeNull();
  fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(calls("DELETE", "/api/tokens/")).toHaveLength(0);

  fireEvent.click(within(table).getByRole("button", { name: "Revoke me" }));
  dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByText(/signs you out/)).toBeTruthy();
  fireEvent.click(dialog.getByRole("button", { name: "Revoke" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(new URL(calls("DELETE", "/api/tokens/")[0]?.url ?? "").pathname).toBe("/api/tokens/me");
  expect(await within(rowOf(table, "me")).findByText(/^Revoked /)).toBeTruthy();
});

test("viewers have no tokens page", async () => {
  role = "viewer";
  renderAt("/settings/tokens");
  expect(await screen.findByText("Only operators see and manage tokens.")).toBeTruthy();
  const nav = screen.getByRole("navigation", { name: "Settings" });
  expect(within(nav).queryByText("Tokens")).toBeNull();
  expect(within(nav).getByText("Spend")).toBeTruthy();
  expect(calls("GET", "/api/tokens")).toHaveLength(0);
});
