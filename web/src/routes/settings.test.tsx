import { EditorView } from "@codemirror/view";
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { ProfileInfo } from "@/api/launch";
import type { PromptText } from "@/api/scenario-files";
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
// config layers as dotted key -> text, with each file's version
let layers: Record<"user" | "project", { values: Record<string, string>; etag: string }>;
const PATHS = { user: "/home/me/.config/aims/config.toml", project: "/work/aims.toml" };
const DEFAULTS: Record<string, [string, string[] | null]> = {
  "prompts.system": ["Work on the task.", null],
  "prompts.denied": ["Denied.", null],
  "prompts.budget_steps": ["You have used {used} of {limit} steps.", ["limit", "used"]],
  "prompts.tools.bash.timeout": ["Timed out after {timeout_s} s.", ["timeout_s"]],
  "prompts.monitor.judge_system": ["Judge the run.", []],
  "prompts.monitor.categories.drift": ["Off the task.", null],
};
const CHECK = "01a19200-0000-7000-8000-000000000001";
// the config files' texts; a project file can be missing
let texts: Record<"user" | "project", string>;
let projectExists: boolean;
let saves: { layer: string; text: string; ifMatch: string | null; ifNoneMatch: string | null }[];

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

function promptText(key: string): PromptText {
  const [fallback, placeholders] = DEFAULTS[key] ?? ["", null];
  const user = layers.user.values[key] ?? null;
  const project = layers.project.values[key] ?? null;
  return {
    text: project ?? user ?? fallback,
    source: project !== null ? "project" : user !== null ? "user" : "default",
    default: fallback,
    user,
    project,
    placeholders,
  };
}

const under = (prefix: string) =>
  Object.fromEntries(
    Object.keys(DEFAULTS)
      .filter((k) => k.startsWith(prefix) && !k.slice(prefix.length).includes("."))
      .map((k) => [k.slice(prefix.length), promptText(k)]),
  );

const promptsInfo = () => ({
  agent: under("prompts."),
  tools: { bash: under("prompts.tools.bash.") },
  monitor: under("prompts.monitor."),
  categories: under("prompts.monitor.categories."),
});

async function putLayer(layer: "user" | "project", request: Request) {
  const file = layers[layer];
  const exists = layer === "user" || projectExists;
  const ifMatch = request.headers.get("If-Match");
  const ifNoneMatch = request.headers.get("If-None-Match");
  const { text } = (await request.clone().json()) as { text: string };
  saves.push({ layer, text, ifMatch, ifNoneMatch });
  if (exists ? ifMatch?.replaceAll('"', "") !== file.etag : ifNoneMatch !== "*") {
    return problem(412, "the file changed since it was read");
  }
  if (text.includes("nonsense")) {
    const msg = `${PATHS[layer]}: unknown table(s) nonsense`;
    return Response.json(
      {
        status: 422,
        title: "Config does not load",
        detail: msg,
        code: "config",
        errors: [{ loc: [], msg, line: null }],
      },
      { status: 422 },
    );
  }
  texts[layer] = text;
  if (layer === "project") projectExists = true;
  file.etag = `${file.etag}+`;
  return Response.json({ layer, path: PATHS[layer], exists: true, text, etag: file.etag });
}

async function checkLayer(request: Request) {
  const { text } = (await request.clone().json()) as { text: string };
  const line = text.split("\n").findIndex((l) => l.endsWith("= "));
  return Response.json(
    line < 0
      ? { ok: true, errors: [], warnings: [] }
      : {
          ok: false,
          errors: [{ loc: [], msg: "Invalid value (column 9)", line: line + 1 }],
          warnings: [],
        },
  );
}

async function patchLayer(layer: "user" | "project", request: Request) {
  const file = layers[layer];
  if (request.headers.get("If-Match")?.replaceAll('"', "") !== file.etag) {
    return problem(412, "the file changed since it was read");
  }
  const body = (await request.clone().json()) as {
    set: { key: string[]; value: string }[];
    unset: string[][];
  };
  for (const { key, value } of body.set) {
    if (value.includes("{bad}")) return problem(422, "unknown placeholder bad; known: {limit}");
    file.values[key.join(".")] = value;
  }
  for (const key of body.unset) delete file.values[key.join(".")];
  file.etag = `${file.etag}+`;
  return Response.json({ layer, path: PATHS[layer], exists: true, text: "", etag: file.etag });
}

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
  layers = {
    user: { values: { "prompts.denied": "No." }, etag: "u1" },
    project: {
      values: { "prompts.system": "Work here.", "prompts.denied": "Not allowed." },
      etag: "p1",
    },
  };
  texts = { user: "", project: "[credits]\nmeta = 150\n" };
  projectExists = true;
  saves = [];
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
    if (url.pathname === "/api/prompts") return Response.json(promptsInfo());
    if (url.pathname === "/api/config")
      return Response.json({
        layers: (["user", "project"] as const).map((layer) => ({
          layer,
          path: PATHS[layer],
          exists: layer === "user" || projectExists,
          etag: layer === "user" || projectExists ? layers[layer].etag : null,
        })),
        error: null,
      });
    if (url.pathname === "/api/config/merged")
      return Response.json({ text: "meta = 150.0  # this project\n" });
    if (/^\/api\/config\/(user|project)\/check$/.test(url.pathname)) return checkLayer(request);
    const layer = /^\/api\/config\/(user|project)$/.exec(url.pathname)?.[1] as
      "user" | "project" | undefined;
    if (layer && request.method === "GET") {
      const exists = layer === "user" || projectExists;
      return Response.json({
        layer,
        path: PATHS[layer],
        exists,
        text: exists ? texts[layer] : "# AIMS settings\n",
        etag: exists ? layers[layer].etag : null,
      });
    }
    if (layer && request.method === "PUT") return putLayer(layer, request);
    if (layer && request.method === "PATCH") return patchLayer(layer, request);
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

beforeAll(() => {
  // the DOM here lays nothing out; CodeMirror measures text ranges
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect }) as DOMRect;
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

test("a check of a model profile follows its run", async () => {
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
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

const textRow = (name: string) =>
  screen.getByText(name, { selector: "li > span" }).closest("li") as HTMLElement;

test("prompts list every text with where it comes from and save into the project", async () => {
  renderAt("/settings/prompts");
  expect(await screen.findByText("Saves to")).toBeTruthy();
  expect(screen.getByRole("button", { name: "This project" }).getAttribute("aria-pressed")).toBe(
    "true",
  );
  expect(screen.getByText(PATHS.project)).toBeTruthy();
  for (const title of ["Agent texts", "Tool texts", "Monitor texts", "Monitor categories"]) {
    expect(screen.getByRole("region", { name: title })).toBeTruthy();
  }
  expect(within(textRow("system")).getByText("this project")).toBeTruthy();
  expect(within(textRow("budget_steps")).getByText("built in")).toBeTruthy();
  expect(within(textRow("bash.timeout")).getByText("Timed out after {timeout_s} s.")).toBeTruthy();

  fireEvent.change(screen.getByRole("textbox", { name: "Filter texts" }), {
    target: { value: "judge" },
  });
  expect(screen.queryByRole("region", { name: "Agent texts" })).toBeNull();
  expect(screen.getByRole("region", { name: "Monitor texts" })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Filter texts" }), {
    target: { value: "" },
  });

  fireEvent.click(screen.getByRole("button", { name: "Edit system" }));
  const box = await screen.findByRole("textbox", { name: "Text of system" });
  expect((box as HTMLTextAreaElement).value).toBe("Work here.");
  expect(screen.getByRole("button", { name: "Use the built-in text" })).toBeTruthy();
  fireEvent.change(box, { target: { value: "Work there." } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await within(textRow("system")).findByText("Work there.")).toBeTruthy();
  const sent = calls("PATCH", "/api/config/project")[0];
  expect(sent?.headers.get("If-Match")).toBe('"p1"');
  expect(await sent?.json()).toEqual({
    set: [{ key: ["prompts", "system"], value: "Work there." }],
    unset: [],
  });
});

test("on this machine an edit starts from its own text and can drop it", async () => {
  renderAt("/settings/prompts");
  fireEvent.click(await screen.findByRole("button", { name: "This machine" }));
  expect(await screen.findByText(PATHS.user)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Edit denied" }));
  const box = await screen.findByRole("textbox", { name: "Text of denied" });
  expect((box as HTMLTextAreaElement).value).toBe("No.");
  expect(screen.getByText(/This project's text wins here/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Use the built-in text" }));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Text of denied" })).toBeNull());
  const sent = calls("PATCH", "/api/config/user")[0];
  expect(sent?.headers.get("If-Match")).toBe('"u1"');
  expect(await sent?.json()).toEqual({ set: [], unset: [["prompts", "denied"]] });
  expect(layers.user.values).toEqual({});
});

test("a bad placeholder shows under the row and a changed file offers both ways", async () => {
  renderAt("/settings/prompts");
  fireEvent.click(await screen.findByRole("button", { name: "Edit budget_steps" }));
  const box = await screen.findByRole("textbox", { name: "Text of budget_steps" });
  expect(
    screen.getByText("Placeholders: {limit} {used}. Write a literal brace as {{ or }}."),
  ).toBeTruthy();
  fireEvent.change(box, { target: { value: "{bad} steps" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("unknown placeholder bad; known: {limit}")).toBeTruthy();

  layers.project.etag = "p9";
  fireEvent.change(box, { target: { value: "{used} of {limit}" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("button", { name: "Load theirs, drop mine" })).toBeTruthy();
  fireEvent.click(within(alert).getByRole("button", { name: "Keep mine" }));
  expect(await within(textRow("budget_steps")).findByText("{used} of {limit}")).toBeTruthy();
  expect(calls("PATCH", "/api/config/project").at(-1)?.headers.get("If-Match")).toBe('"p9"');
  expect(layers.project.values["prompts.budget_steps"]).toBe("{used} of {limit}");
});

test("viewers read the texts without a layer or edits", async () => {
  role = "viewer";
  renderAt("/settings/prompts");
  expect(await screen.findByText("Read only: operators edit these texts.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();
  expect(screen.queryByRole("group", { name: "Layer" })).toBeNull();
  expect(calls("GET", "/api/config")).toEqual([]);
});

async function configEditor(name: string): Promise<EditorView> {
  const view = EditorView.findFromDOM(await screen.findByLabelText(name));
  if (!view) throw new Error("no editor");
  return view;
}

function retype(view: EditorView, from: string, to: string) {
  const at = view.state.doc.toString().indexOf(from);
  act(() => view.dispatch({ changes: { from: at, to: at + from.length, insert: to } }));
}

test("settings open on the project's config, checked by line as you type", async () => {
  const router = renderAt("/settings");
  const view = await configEditor("Contents of aims.toml");
  expect(router.state.location.pathname).toBe("/settings/config");
  expect(screen.getByText("Saves to")).toBeTruthy();
  expect(view.state.doc.toString()).toBe(texts.project);
  retype(view, "150", "");
  const found = await screen.findByText("Invalid value (column 9)", {}, { timeout: 3000 });
  expect(found.closest("button")?.textContent).toBe("line 2: Invalid value (column 9)");
  expect(screen.getByText("1 error")).toBeTruthy();
  retype(view, "= \n", "= 151\n");
  await waitFor(() => expect(screen.queryByText("1 error")).toBeNull(), { timeout: 3000 });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saved");
  expect(saves).toEqual([
    { layer: "project", text: "[credits]\nmeta = 151\n", ifMatch: '"p1"', ifNoneMatch: null },
  ]);
  expect(texts.project).toBe("[credits]\nmeta = 151\n");
});

test("a save that does not load says why, and a changed file offers both ways", async () => {
  renderAt("/settings/config");
  const view = await configEditor("Contents of aims.toml");
  retype(view, "[credits]", "[nonsense]");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Not saved: it does not load.")).toBeTruthy();
  expect(screen.getByText("/work/aims.toml: unknown table(s) nonsense")).toBeTruthy();
  expect(texts.project).toBe("[credits]\nmeta = 150\n");

  retype(view, "[nonsense]", "[credits]\nfireworks = 5");
  layers.project.etag = "p9";
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect((await screen.findByRole("alert")).textContent).toContain("changed on disk");
  fireEvent.click(screen.getByRole("button", { name: "Keep mine, overwrite" }));
  await screen.findByText("Saved");
  expect(saves.at(-1)?.ifMatch).toBe('"p9"');
  expect(texts.project).toBe("[credits]\nfireworks = 5\nmeta = 150\n");
});

test("without a project file, saving This project creates one", async () => {
  projectExists = false;
  renderAt("/settings/config");
  await configEditor("Contents of config.toml");
  expect(screen.getByText("Saves to")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "This project" }));
  await configEditor("Contents of aims.toml");
  expect(screen.getByText("Saving creates")).toBeTruthy();
  expect(screen.getByText("not created yet")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await screen.findByText("Created");
  expect(saves).toEqual([
    { layer: "project", text: "# AIMS settings\n", ifMatch: null, ifNoneMatch: "*" },
  ]);
});

test("viewers read the files and the settings in use, without checks", async () => {
  role = "viewer";
  renderAt("/settings/config?view=merged");
  const merged = await configEditor("The settings in use");
  expect(merged.state.doc.toString()).toBe("meta = 150.0  # this project\n");
  expect(merged.state.readOnly).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "This project" }));
  const view = await configEditor("Contents of aims.toml");
  expect(view.state.readOnly).toBe(true);
  expect(screen.getByText("read only")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  await act(() => new Promise((r) => setTimeout(r, 500)));
  expect(posts()).toHaveLength(0);
});
