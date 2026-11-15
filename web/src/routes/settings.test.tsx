import { EditorView } from "@codemirror/view";
import { QueryClientProvider } from "@tanstack/react-query";
import { createBrowserHistory, createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { ModelPrices, PriceRow, ProfileDraft, ProfileInfo } from "@/api/launch";
import type { PromptText } from "@/api/scenario-files";
import type { AuditEntry, ProviderCredit, TokenInfo } from "@/api/settings";
import { makeRouter } from "@/router";
import { FakeEventSource, runnerInfo } from "@/test/event-source";
import { pick } from "@/test/pick";
import { preloadRoutes } from "@/test/routes";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: "operator" | "viewer";
let checked: ProfileInfo["check"];
let credits: ProviderCredit[];
let tokens: TokenInfo[];
// the audit log, newest first
let audit: AuditEntry[];
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
// the answer to a lookup of an OpenRouter model
let lookup: (url: URL) => Response | Promise<Response>;
// the answer to adding a looked-up profile to the project's config
let added: () => Response;
// profiles the models list holds after alpha and gamma
let more: ProfileInfo[];
// the profile of each role, null when the role is unset
let modelRoles: Record<string, string | null>;
// the answer to a read of the price map, by the search text
let prices: (search: string) => ModelPrices;

const profile = (over: Partial<ProfileInfo>): ProfileInfo => ({
  name: "alpha",
  backend: "responses",
  model: "alpha-1.3",
  weights: "closed",
  reasoning: "medium",
  roles: ["agent"],
  envs: ["META_API_KEY"],
  runners: ["local"],
  check: null,
  price: { input: 1.25, cached: 0.15, output: 4.25 },
  price_key: null,
  ...over,
});

const priceRow = (key: string, over: Partial<PriceRow> = {}): PriceRow => ({
  key,
  input_usd: 0.35,
  cached_usd: 0.04,
  output_usd: 1.5,
  cached_listed: true,
  tier_from: null,
  ...over,
});

const DAY = "2026-11-15T11:59:06+00:00";

const mapSays = (over: Partial<ModelPrices> = {}): ModelPrices => ({
  source: "LiteLLM",
  checked: DAY,
  problem: null,
  profiles: [],
  found: [],
  ...over,
});

const TOML = '[profiles."openrouter-gem-9"]\nbackend = "chat"\nmodel = "acme/gem-9"\n';

const draft = (over: Partial<ProfileDraft> = {}): ProfileDraft => ({
  name: "openrouter-gem-9",
  slug: "acme/gem-9",
  toml: TOML,
  notes: ["OpenRouter lists no context size for it, so context_tokens is unset."],
  exists: false,
  fits: ["agent", "sweep", "judge"],
  ...over,
});

const credit = (over: Partial<ProviderCredit>): ProviderCredit => ({
  provider: "acme",
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
  last_used: null,
  ui: false,
  ...over,
});

const auditRow = (id: number, over: Partial<AuditEntry> = {}): AuditEntry => ({
  id,
  at: new Date(Date.UTC(2026, 10, 9, 12, 0, 0) + id * 1000).toISOString(),
  token: "me",
  role: "operator",
  auth: "cookie",
  action: "token.create",
  target: `t${id}`,
  outcome: "ok",
  status: 201,
  ip: "127.0.0.1",
  detail: "role viewer",
  ...over,
});

function auditPage(url: URL) {
  const q = url.searchParams;
  const before = Number(q.get("before") ?? Infinity);
  const limit = Number(q.get("limit") ?? 100);
  const action = q.get("action");
  const match = audit.filter(
    (r) =>
      r.id < before &&
      (!action || r.action === action || r.action.startsWith(`${action}.`)) &&
      (!q.get("outcome") || r.outcome === q.get("outcome")) &&
      (!q.get("token") || r.token === q.get("token")),
  );
  const items = match.slice(0, limit);
  return Response.json({ items, next: items.length === limit ? (items.at(-1)?.id ?? null) : null });
}

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

preloadRoutes();
beforeEach(() => {
  role = "operator";
  checked = null;
  credits = [
    credit({ provider: "provider-b", spent_usd: 1.5, credit_usd: null }),
    credit({ provider: "acme", spent_usd: 145, credit_usd: 150, left_usd: 5 }),
    credit({ provider: "provider-c", spent_usd: 21, credit_usd: 20, left_usd: -1 }),
  ];
  tokens = [
    token({ name: "old", role: "viewer", created: 1_761_800_000, revoked: 1_761_850_000 }),
    token({ name: "me", last_used: 1_761_950_000 }),
    token({ name: "ui", ui: true }),
  ];
  audit = Array.from({ length: 150 }, (_, i) => auditRow(150 - i));
  audit[0] = auditRow(150, {
    token: null,
    role: null,
    auth: "token",
    action: "session.start",
    target: null,
    outcome: "refused",
    status: 401,
    detail: "token",
  });
  layers = {
    user: { values: { "prompts.denied": "No." }, etag: "u1" },
    project: {
      values: { "prompts.system": "Work here.", "prompts.denied": "Not allowed." },
      etag: "p1",
    },
  };
  texts = { user: "", project: "[credits]\nacme = 150\n" };
  projectExists = true;
  saves = [];
  lookup = () => Response.json(draft());
  added = () => Response.json({ name: "openrouter-gem-9", path: "/work/aims.toml" });
  more = [];
  modelRoles = { agent: "alpha", sweep: "alpha", judge: "alpha" };
  prices = () => mapSays();
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
          profile({ name: "gamma", roles: [], envs: ["GAMMA_KEY"], runners: [], price: null }),
          ...more,
        ],
        roles: modelRoles,
      });
    if (url.pathname === "/api/models/prices") {
      return Response.json(prices(url.searchParams.get("search") ?? ""));
    }
    if (url.pathname === "/api/models/check") {
      checked = {
        run_id: CHECK,
        started_at: "2026-10-31T10:00:00+00:00",
        status: "completed",
        checks: [{ name: "tools", verdict: "warn", detail: "slow" }],
      };
      return Response.json({ run_ids: [CHECK] }, { status: 202 });
    }
    if (url.pathname === "/api/models/add") {
      if (role !== "operator") return problem(403, "operators only");
      return added();
    }
    if (url.pathname === "/api/models/lookup") {
      if (role !== "operator") return problem(403, "operators only");
      return lookup(url);
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
      return Response.json({ text: "acme = 150.0  # this project\n" });
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
    if (url.pathname === "/api/audit") {
      if (role !== "operator") return problem(403, "operators only");
      return auditPage(url);
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
  const alpha = within(table).getByText("alpha").closest("tr") as HTMLElement;
  expect(within(alpha).getByText("agent")).toBeTruthy();
  expect(within(alpha).getByText("set on local")).toBeTruthy();
  expect(within(alpha).getByText("never checked")).toBeTruthy();
  const gamma = within(table).getByText("gamma").closest("tr") as HTMLElement;
  expect(within(gamma).getByText("no connected runner has them")).toBeTruthy();

  fireEvent.click(within(alpha).getByRole("button", { name: "Check alpha" }));
  expect(await within(alpha).findByText("checking")).toBeTruthy();
  expect(await posts()[0]?.json()).toEqual({ profiles: ["alpha"] });
  expect(screen.getByRole("button", { name: "Check all" })).toHaveProperty("disabled", true);

  act(() => {
    FakeEventSource.all[0]?.open();
    FakeEventSource.all[0]?.send({
      kind: "run",
      run: run(CHECK, { kind: "models.check", status: "completed", state: "done" }),
    });
  });
  expect(await within(alpha).findByText("works with warnings")).toBeTruthy();
  expect(within(alpha).getByText("tools: slow")).toBeTruthy();
  expect(within(alpha).getByRole("link").getAttribute("href")).toBe(`/runs/${CHECK}`);
});

test("quick clicks start one model check, and one that did not start can be tried again", async () => {
  const base = fetch.getMockImplementation();
  if (!base) throw new Error("no fetch");
  let refuse = true;
  fetch.mockImplementation(async (request) => {
    if (refuse && new URL(request.url).pathname === "/api/models/check") {
      refuse = false;
      return problem(503, "no runner has the keys");
    }
    return base(request);
  });
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  const alpha = within(table).getByText("alpha").closest("tr") as HTMLElement;
  const button = within(alpha).getByRole("button", { name: "Check alpha" });
  // both in one tick, before the page shows the first
  const twice = () =>
    act(() => {
      button.click();
      button.click();
    });
  twice();
  expect(await screen.findByText("no runner has the keys")).toBeTruthy();
  expect(posts()).toHaveLength(1);
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  twice();
  expect(await within(alpha).findByText("checking")).toBeTruthy();
  expect(posts()).toHaveLength(2);
});

test("the models page names the roles no profile has and how to set them", async () => {
  renderAt("/settings/models");
  await screen.findByRole("table");
  expect(screen.queryByText(/Unset roles/)).toBeNull();
  cleanup();
  modelRoles = { agent: "alpha", sweep: null, judge: null };
  renderAt("/settings/models");
  const line = (await screen.findByText(/^Unset roles/)) as HTMLElement;
  expect(line.textContent).toBe(
    "Unset roles: sweep, judge. Set them under [roles] in aims.toml, or add a model with them.",
  );
  expect(within(line).getByRole("link", { name: "aims.toml" }).getAttribute("href")).toBe(
    "/settings/config?view=project",
  );
  fireEvent.click(within(line).getByRole("button", { name: "add a model with them" }));
  expect(await screen.findByRole("dialog", { name: "Add from OpenRouter" })).toBeTruthy();
  cleanup();
  role = "viewer";
  renderAt("/settings/models");
  expect((await screen.findByText(/^Unset roles/)).textContent).toBe(
    "Unset roles: sweep, judge. Set them under [roles] in aims.toml.",
  );
});

test("a models page with no profiles says how to add one", async () => {
  const real = fetch.getMockImplementation();
  fetch.mockImplementation(async (request) =>
    new URL(request.url).pathname === "/api/models"
      ? Response.json({ profiles: [], roles: { agent: null, sweep: null, judge: null } })
      : real!(request),
  );
  renderAt("/settings/models");
  const line = (await screen.findByText(/^No profiles yet/)) as HTMLElement;
  expect(line.textContent).toBe(
    "No profiles yet. Write one under [profiles] in aims.toml, or add one from OpenRouter.",
  );
  // no role can name a profile yet, so the line about them waits
  expect(screen.queryByText(/Unset roles/)).toBeNull();
  expect(screen.queryByRole("table")).toBeNull();
  fireEvent.click(within(line).getByRole("button", { name: "add one from OpenRouter" }));
  expect(await screen.findByRole("dialog", { name: "Add from OpenRouter" })).toBeTruthy();
});

test("a link to the models page can open the lookup", async () => {
  const router = renderAt("/settings/models?add=true");
  const dialog = within(await screen.findByRole("dialog", { name: "Add from OpenRouter" }));
  fireEvent.click(dialog.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(router.state.location.search).toEqual({});
});

test("viewers see the profiles without checks or a lookup", async () => {
  role = "viewer";
  renderAt("/settings/models");
  await screen.findByRole("table");
  expect(screen.queryByRole("button", { name: /Check/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "Add from OpenRouter" })).toBeNull();
});

const priceReads = () => calls("GET", "/api/models/prices").map((r) => new URL(r.url));

const LISTED = {
  profile: "listed",
  source: "LiteLLM",
  key: "cloud/eu/model-a",
  checked: DAY,
  input_usd: 2.75,
  cached_usd: 2.75,
  output_usd: 11,
  cached_listed: false,
  tier_from: 200_000,
  problem: "the map did not answer in 5 s",
  text: "LiteLLM list price for cloud/eu/model-a, fetched 2026-11-15",
};

function keyedProfiles() {
  more = [
    profile({ name: "listed", roles: [], price: null, price_key: "cloud/eu/model-a" }),
    profile({ name: "missed", roles: [], price: null, price_key: "vendor-c/nope" }),
    profile({ name: "bare", model: "router/gamma", roles: [], price: null }),
  ];
  prices = (search) =>
    mapSays({
      profiles: [
        { name: "bare", listed: null, unlisted: null, candidates: [] },
        { name: "listed", listed: LISTED, unlisted: null, candidates: [] },
        {
          name: "missed",
          listed: null,
          unlisted: "LiteLLM lists no key vendor-c/nope",
          candidates: [priceRow("vendor-c/model-c", { cached_usd: 0.002 })],
        },
      ],
      found: search === "gamma" ? [priceRow("provider/gamma-30b")] : [],
    });
}

const profileRow = (table: HTMLElement, name: string) =>
  within(table).getByText(name).closest("tr") as HTMLTableRowElement;

// The Price column's cell. A narrow screen shows the same in the profile's cell instead, which
// jsdom renders too.
const priceCell = (table: HTMLElement, name: string) =>
  within(profileRow(table, name).cells[2] as HTMLElement);

test("the models page shows each price and where it comes from", async () => {
  keyedProfiles();
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  const alpha = priceCell(table, "alpha");
  expect(alpha.getByText("$1.25 in · $0.15 cached · $4.25 out")).toBeTruthy();
  expect(alpha.getByText("own price")).toBeTruthy();
  const listed = priceCell(table, "listed");
  expect(listed.getByText("Reading the price map")).toBeTruthy();
  expect(await listed.findByText("$2.75 in · $2.75 cached · $11.00 out")).toBeTruthy();
  expect(listed.getByText("cloud/eu/model-a")).toBeTruthy();
  expect(
    listed.getByText(
      "fetched 2026-11-15 · no cached price listed, so cached input counts as input · more past 200,000 tokens · could not be read again",
    ),
  ).toBeTruthy();
  const missed = priceCell(table, "missed");
  expect(missed.getByText("LiteLLM lists no key vendor-c/nope")).toBeTruthy();
  expect(missed.getByRole("button", { name: "Find a price key for missed" })).toBeTruthy();
  const gamma = priceCell(table, "gamma");
  expect(
    gamma.getByText("estimates leave out its calls; caps count only reported costs"),
  ).toBeTruthy();
  expect(gamma.getByRole("button", { name: "Find a price key for gamma" })).toBeTruthy();
  // a narrow screen shows the price and the button under the profile's name
  const narrow = within(profileRow(table, "gamma").cells[0] as HTMLElement);
  expect(narrow.getByRole("button", { name: "Find a price key for gamma" })).toBeTruthy();
  expect(priceReads()).toHaveLength(1);
});

test("a page with no profile that names a key reads no price map", async () => {
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  expect(priceCell(table, "gamma").getByText("none")).toBeTruthy();
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
  expect(priceReads()).toHaveLength(0);
});

test("a viewer finds a price key, copies its line and searches the map", async () => {
  role = "viewer";
  keyedProfiles();
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  fireEvent.click(
    await priceCell(table, "missed").findByRole("button", { name: "Find a price key for missed" }),
  );
  const dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByText("vendor-c/model-c")).toBeTruthy();
  expect(dialog.getByText("$0.35 in · $0.002 cached · $1.50 out")).toBeTruthy();
  expect(
    dialog.getByText("From LiteLLM's price map, fetched 2026-11-15, in USD per million tokens."),
  ).toBeTruthy();
  fireEvent.click(
    dialog.getByRole("button", { name: "Copy the price_key line for vendor-c/model-c" }),
  );
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('price_key = "vendor-c/model-c"'));
  expect(await dialog.findByText("Copied")).toBeTruthy();

  const search = dialog.getByRole("textbox", { name: "Search keys" });
  fireEvent.change(search, { target: { value: " gamma " } });
  fireEvent.click(dialog.getByRole("button", { name: "Search" }));
  expect(await dialog.findByText("provider/gamma-30b")).toBeTruthy();
  expect(priceReads().map((u) => u.searchParams.get("search"))).toEqual([null, "gamma"]);
  fireEvent.change(search, { target: { value: "nothing" } });
  fireEvent.click(dialog.getByRole("button", { name: "Search" }));
  expect(await dialog.findByText("No row's key holds nothing.")).toBeTruthy();
});

test("a profile with no candidate rows asks for a search, and a missing map says why", async () => {
  keyedProfiles();
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  fireEvent.click(
    priceCell(table, "bare").getByRole("button", { name: "Find a price key for bare" }),
  );
  let dialog = within(await screen.findByRole("dialog"));
  expect(await dialog.findByText("No row's key ends in gamma. Search instead.")).toBeTruthy();
  cleanup();

  const why = "no copy of the LiteLLM price map is at hand (the map did not answer in 5 s)";
  prices = () =>
    mapSays({
      checked: null,
      problem: "the map did not answer in 5 s",
      profiles: [
        { name: "bare", listed: null, unlisted: null, candidates: [] },
        { name: "listed", listed: null, unlisted: why, candidates: [] },
      ],
    });
  renderAt("/settings/models");
  const again = await screen.findByRole("table");
  expect(await priceCell(again, "listed").findByText(why)).toBeTruthy();
  fireEvent.click(
    priceCell(again, "bare").getByRole("button", { name: "Find a price key for bare" }),
  );
  dialog = within(await screen.findByRole("dialog"));
  expect(
    await dialog.findByText(
      "No copy of LiteLLM's price map is at hand: the map did not answer in 5 s",
    ),
  ).toBeTruthy();
  expect(dialog.queryByText(/Search instead/)).toBeNull();
});

const lookups = () => calls("GET", "/api/models/lookup").map((r) => new URL(r.url));

async function openLookup() {
  renderAt("/settings/models");
  await screen.findByRole("table");
  fireEvent.click(screen.getByRole("button", { name: "Add from OpenRouter" }));
  const dialog = within(await screen.findByRole("dialog"));
  return { dialog, id: dialog.getByRole("textbox", { name: "Model id" }) };
}

test("a lookup asks only on submit and shows the profile to copy and where it goes", async () => {
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
  const { dialog, id } = await openLookup();
  const button = dialog.getByRole("button", { name: "Look up" });
  expect(button).toHaveProperty("disabled", true);

  fireEvent.change(id, { target: { value: " acme/gem-9 " } });
  expect(lookups()).toHaveLength(0);
  // what Enter in the field does
  fireEvent.submit(id);
  expect(await dialog.findByText(/^OpenRouter lists no context size/)).toBeTruthy();
  expect(lookups().map((u) => Object.fromEntries(u.searchParams))).toEqual([
    { slug: "acme/gem-9", source: "openrouter" },
  ]);
  expect(dialog.getByLabelText("Profile openrouter-gem-9").textContent).toBe(TOML);
  expect(dialog.queryByText(/exists already/)).toBeNull();
  expect(dialog.getByText("aims models add openrouter acme/gem-9 --write")).toBeTruthy();
  expect(dialog.getByRole("link", { name: "Config" }).getAttribute("href")).toBe(
    "/settings/config?view=project",
  );
  expect(calls("POST", "/api/models/add")).toHaveLength(0);

  fireEvent.click(dialog.getByRole("button", { name: "Copy" }));
  expect(await dialog.findByRole("button", { name: "Copied" })).toBeTruthy();
  expect(writeText).toHaveBeenCalledWith(TOML);
  expect(lookups()).toHaveLength(1);
});

test("a lookup sends the name and alias it is given and warns of a name in use", async () => {
  // plain http on a LAN address has no clipboard
  vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
  lookup = (url) =>
    Response.json(
      draft({
        slug: url.searchParams.get("slug") ?? "",
        name: url.searchParams.get("name") ?? "",
        notes: [],
        exists: true,
      }),
    );
  const { dialog, id } = await openLookup();
  fireEvent.change(id, { target: { value: "~acme/gem-latest" } });
  fireEvent.change(dialog.getByRole("textbox", { name: "Profile name (optional)" }), {
    target: { value: "alpha" },
  });
  fireEvent.click(
    dialog.getByRole("checkbox", { name: "Allow an alias that OpenRouter moves to newer models" }),
  );
  fireEvent.click(dialog.getByRole("button", { name: "Look up" }));
  expect(await dialog.findByText(/^A profile named alpha exists already\./)).toBeTruthy();
  expect(Object.fromEntries(lookups()[0]?.searchParams ?? [])).toEqual({
    slug: "~acme/gem-latest",
    source: "openrouter",
    name: "alpha",
    alias: "true",
  });
  expect(
    dialog.getByText("aims models add openrouter '~acme/gem-latest' --name alpha --alias --write"),
  ).toBeTruthy();
  expect(dialog.getByLabelText("Profile alpha")).toBeTruthy();
  expect(dialog.queryByRole("button", { name: "Copy" })).toBeNull();
  // the server would refuse a name in use
  expect(dialog.getByRole("button", { name: "Add to project config" })).toHaveProperty(
    "disabled",
    true,
  );
});

test("a looked-up profile is added to the project config on request", async () => {
  added = () =>
    Response.json(
      { status: 422, title: "Config does not load", detail: "aims.toml: unknown table(s) nope" },
      { status: 422 },
    );
  lookup = (url) => Response.json(draft({ slug: url.searchParams.get("slug") ?? "", name: "gem" }));
  const { dialog, id } = await openLookup();
  fireEvent.change(id, { target: { value: "~acme/gem-latest" } });
  fireEvent.change(dialog.getByRole("textbox", { name: "Profile name (optional)" }), {
    target: { value: "gem" },
  });
  fireEvent.click(dialog.getByRole("checkbox", { name: /^Allow an alias/ }));
  fireEvent.submit(id);
  const add = await dialog.findByRole("button", { name: "Add to project config" });
  fireEvent.click(add);
  expect(await dialog.findByText("aims.toml: unknown table(s) nope")).toBeTruthy();
  expect(await calls("POST", "/api/models/add")[0]?.json()).toEqual({
    slug: "~acme/gem-latest",
    source: "openrouter",
    name: "gem",
    alias: true,
  });

  added = () => Response.json({ name: "gem", path: "/work/aims.toml" });
  const lists = calls("GET", "/api/models").filter(
    (r) => new URL(r.url).pathname === "/api/models",
  );
  fireEvent.click(add);
  expect(await dialog.findByText("/work/aims.toml")).toBeTruthy();
  expect(dialog.queryByText("aims.toml: unknown table(s) nope")).toBeNull();
  expect(dialog.queryByRole("button", { name: "Add to project config" })).toBeNull();
  // the profile list is read again
  await waitFor(() =>
    expect(
      calls("GET", "/api/models").filter((r) => new URL(r.url).pathname === "/api/models").length,
    ).toBeGreaterThan(lists.length),
  );
});

test("a lookup shows it is under way and why the server refused it", async () => {
  const answer = Promise.withResolvers<Response>();
  lookup = () => answer.promise;
  const { dialog, id } = await openLookup();
  const button = dialog.getByRole("button", { name: "Look up" });
  fireEvent.change(id, { target: { value: "acme/gem-0" } });
  act(() => {
    fireEvent.submit(id);
    fireEvent.submit(id);
  });
  expect(await dialog.findByText("Looking up acme/gem-0")).toBeTruthy();
  expect(button).toHaveProperty("disabled", true);
  expect(lookups()).toHaveLength(1);
  answer.resolve(
    Response.json(
      {
        status: 404,
        title: "Not Found",
        detail: "no model acme/gem-0 on OpenRouter; close ids: acme/gem-9",
        code: "missing",
      },
      { status: 404 },
    ),
  );
  expect(
    await dialog.findByText("no model acme/gem-0 on OpenRouter; close ids: acme/gem-9"),
  ).toBeTruthy();
  expect(dialog.queryByText(/^Looking up/)).toBeNull();
  expect(document.querySelector("[role=dialog] pre")).toBeNull();

  // a problem with no detail says its title
  lookup = () =>
    Response.json({ status: 502, title: "Bad Gateway", detail: null }, { status: 502 });
  fireEvent.click(button);
  expect(await dialog.findByText("Bad Gateway")).toBeTruthy();
  expect(lookups()).toHaveLength(2);
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
  expect(within(row("provider-b")).getByText("not set")).toBeTruthy();
  expect(within(row("provider-b")).queryByRole("meter")).toBeNull();
  const first = row("acme");
  expect(within(first).getByText("$5.00").className).toContain("text-flag");
  expect(within(first).getByRole("meter").getAttribute("aria-valuenow")).toBe("97");
  expect(within(row("provider-c")).getByText("over by $1.00")).toBeTruthy();
});

test("spend is asked again when a run ends", async () => {
  renderAt("/settings/spend");
  await screen.findByRole("table");
  credits = [credit({ provider: "acme", spent_usd: 146, credit_usd: 150, left_usd: 4 })];
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
  expect(within(rowOf(table, "me")).queryByText("never")).toBeNull();
  expect(within(rowOf(table, "ui")).getByText("aims ui")).toBeTruthy();
  expect(within(rowOf(table, "ui")).getByText("never")).toBeTruthy();
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

test("revoking warns about your own token, the ui token and runner names", async () => {
  tokens = [...tokens, token({ name: "mac", role: "runner" })];
  renderAt("/settings/tokens");
  const table = await screen.findByRole("table");

  fireEvent.click(within(table).getByRole("button", { name: "Revoke ui" }));
  let dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByText(/until aims ui starts again/)).toBeTruthy();
  expect(dialog.queryByText(/signs you out/)).toBeNull();
  expect(dialog.queryByText(/runner names free up/)).toBeNull();
  fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(calls("DELETE", "/api/tokens/")).toHaveLength(0);

  fireEvent.click(within(table).getByRole("button", { name: "Revoke mac" }));
  dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByText(/runner names free up/)).toBeTruthy();
  fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

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

const lastAudit = () => new URL(calls("GET", "/api/audit").at(-1)?.url ?? "");

test("the audit log pages newest first, filters on the server and exports what matches", async () => {
  renderAt("/settings/audit");
  const table = await screen.findByRole("table");
  const first = within(table).getAllByRole("row")[1] as HTMLElement;
  expect(within(first).getByText("session.start")).toBeTruthy();
  expect(within(first).getByText("none")).toBeTruthy();
  expect(within(first).getByText("Refused 401")).toBeTruthy();
  const second = within(table).getAllByRole("row")[2] as HTMLElement;
  expect(within(second).getByText("t149")).toBeTruthy();
  expect(within(second).getByText("OK 201")).toBeTruthy();
  expect(screen.getByText("100 rows")).toBeTruthy();
  expect(lastAudit().searchParams.get("limit")).toBe("100");

  fireEvent.click(screen.getByRole("button", { name: "Show more" }));
  expect(await screen.findByText("150 rows")).toBeTruthy();
  expect(lastAudit().searchParams.get("before")).toBe("51");
  expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();

  await pick(within(document.body), "Outcome", "Refused");
  expect(await screen.findByText("1 row")).toBeTruthy();
  expect(lastAudit().searchParams.get("outcome")).toBe("refused");
  const link = screen.getByRole("link", { name: "Export JSON Lines" });
  expect(link.hasAttribute("download")).toBe(true);
  const href = new URL(link.getAttribute("href") ?? "", "http://aims.test");
  expect(href.pathname).toBe("/api/audit");
  expect(Object.fromEntries(href.searchParams)).toEqual({ format: "jsonl", outcome: "refused" });

  await pick(within(document.body), "Outcome", "Any outcome");
  await pick(within(document.body), "Action", "Tokens");
  fireEvent.change(screen.getByPlaceholderText("Any token"), { target: { value: "nobody" } });
  expect(await screen.findByText("No rows match.")).toBeTruthy();
  expect(Object.fromEntries(lastAudit().searchParams)).toEqual({
    action: "token",
    token: "nobody",
    limit: "100",
  });
});

test("a time that is no date the log can take bounds nothing and is marked", async () => {
  renderAt("/settings/audit");
  await screen.findByRole("table");
  const from = screen.getByLabelText("From");
  // Chrome's date field takes a year of up to six digits; this DOM keeps four, so the field
  // holds the value as Chrome would
  Object.defineProperty(from, "value", {
    configurable: true,
    get: () => "202611-01-01T10:00",
    set: () => {},
  });
  fireEvent.change(from);
  expect(screen.getByText("100 rows")).toBeTruthy();
  expect(from.getAttribute("aria-invalid")).toBe("true");
  expect(lastAudit().searchParams.has("since")).toBe(false);
  Reflect.deleteProperty(from, "value");
  fireEvent.change(from, { target: { value: "2026-11-01T10:00" } });
  await waitFor(() => expect(lastAudit().searchParams.has("since")).toBe(true));
  expect(from.getAttribute("aria-invalid")).toBe("false");
});

test("viewers have no audit page", async () => {
  role = "viewer";
  renderAt("/settings/audit");
  expect(await screen.findByText("Only operators see the audit log.")).toBeTruthy();
  const nav = screen.getByRole("navigation", { name: "Settings" });
  expect(within(nav).queryByText("Audit")).toBeNull();
  expect(calls("GET", "/api/audit")).toHaveLength(0);
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
    { layer: "project", text: "[credits]\nacme = 151\n", ifMatch: '"p1"', ifNoneMatch: null },
  ]);
  expect(texts.project).toBe("[credits]\nacme = 151\n");
});

test("a save that does not load says why, and a changed file offers both ways", async () => {
  renderAt("/settings/config");
  const view = await configEditor("Contents of aims.toml");
  const hint = view.contentDOM.getAttribute("aria-describedby") ?? "";
  expect(document.getElementById(hint)?.textContent).toBe("Esc then Tab leaves the editor");
  retype(view, "[credits]", "[nonsense]");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Not saved: it does not load.")).toBeTruthy();
  expect(screen.getByText("/work/aims.toml: unknown table(s) nonsense")).toBeTruthy();
  expect(texts.project).toBe("[credits]\nacme = 150\n");

  retype(view, "[nonsense]", "[credits]\nprovider-b = 5");
  layers.project.etag = "p9";
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const alert = await screen.findByRole("alert");
  // the control plane's own words are not said twice
  expect(alert.querySelector("span")?.textContent).toMatch(
    /aims\.toml changed on disk since you opened it\.$/,
  );
  fireEvent.click(screen.getByRole("button", { name: "Keep mine, overwrite" }));
  await screen.findByText("Saved");
  expect(saves.at(-1)?.ifMatch).toBe('"p9"');
  expect(texts.project).toBe("[credits]\nprovider-b = 5\nacme = 150\n");
});

// The questions the page asks with window.confirm, and the answer it gets.
function confirms(yes: boolean) {
  const asked = { questions: [] as string[], yes };
  vi.stubGlobal("confirm", (question: string) => {
    asked.questions.push(question);
    return asked.yes;
  });
  return asked;
}

test("a config draft asks once before a layer or a link drops it", async () => {
  const asked = confirms(false);
  const router = renderAt("/settings/config");
  const view = await configEditor("Contents of aims.toml");
  retype(view, "150", "151");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  const question = "Discard your changes to /work/aims.toml?";
  fireEvent.click(screen.getByRole("button", { name: "This machine" }));
  await waitFor(() => expect(asked.questions).toEqual([question]));
  fireEvent.click(screen.getByRole("link", { name: "Models" }));
  await waitFor(() => expect(asked.questions).toEqual([question, question]));
  expect(router.state.location.pathname).toBe("/settings/config");
  expect(router.state.location.search).toEqual({});
  expect(view.state.doc.toString()).toContain("151");
  expect(screen.getByText("unsaved changes")).toBeTruthy();

  asked.yes = true;
  fireEvent.click(screen.getByRole("button", { name: "This machine" }));
  await configEditor("Contents of config.toml");
  expect(router.state.location.search).toEqual({ view: "user" });
  expect(asked.questions).toHaveLength(3);
  // the draft is gone, so nothing more asks
  fireEvent.click(screen.getByRole("button", { name: "In use" }));
  await screen.findByLabelText("The settings in use");
  fireEvent.click(screen.getByRole("link", { name: "Models" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/settings/models"));
  expect(asked.questions).toHaveLength(3);
});

test("sign out asks before it drops a config draft", async () => {
  const asked = confirms(false);
  renderAt("/settings/config");
  const view = await configEditor("Contents of aims.toml");
  retype(view, "150", "151");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /^me/ }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() =>
    expect(asked.questions).toEqual(["Discard your changes to /work/aims.toml?"]),
  );
  const ended = fetch.mock.calls.filter(([r]) => r.method === "DELETE");
  expect(ended).toEqual([]);
  expect(view.state.doc.toString()).toContain("151");
});

test("a config with no draft never asks", async () => {
  const asked = confirms(false);
  const router = renderAt("/settings/config");
  await configEditor("Contents of aims.toml");
  fireEvent.click(screen.getByRole("button", { name: "This machine" }));
  await configEditor("Contents of config.toml");
  fireEvent.click(screen.getByRole("link", { name: "Models" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/settings/models"));
  expect(asked.questions).toEqual([]);
});

// Whether closing the browser tab would ask first.
function unload(): boolean {
  const e = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(e);
  return e.defaultPrevented;
}

test("in the browser a config draft holds the tab open and Back asks first", async () => {
  window.history.replaceState(null, "", "/settings/runners");
  const history = createBrowserHistory();
  const router = makeRouter(history);
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await act(() => router.navigate({ to: "/settings/config" }));
  const view = await configEditor("Contents of aims.toml");
  expect(unload()).toBe(false);
  retype(view, "150", "151");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  expect(unload()).toBe(true);

  const asked = confirms(false);
  act(() => window.history.back());
  await waitFor(() => expect(asked.questions).toHaveLength(1));
  await act(() => new Promise((r) => setTimeout(r, 50)));
  expect(router.state.location.pathname).toBe("/settings/config");
  expect(window.location.pathname).toBe("/settings/config");
  expect(screen.getByText("unsaved changes")).toBeTruthy();
  history.destroy();
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
  expect(merged.state.doc.toString()).toBe("acme = 150.0  # this project\n");
  expect(merged.state.readOnly).toBe(true);
  // read only, CodeMirror drops contenteditable; a tab stop still lets keys scroll it
  expect(merged.contentDOM.getAttribute("contenteditable")).toBe("false");
  expect(merged.contentDOM.tabIndex).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "This project" }));
  const view = await configEditor("Contents of aims.toml");
  expect(view.state.readOnly).toBe(true);
  // Tab leaves a read-only editor, so there is no hint on how to
  expect(view.contentDOM.getAttribute("aria-describedby")).toBeNull();
  expect(screen.getByText("read only")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  await act(() => new Promise((r) => setTimeout(r, 500)));
  expect(posts()).toHaveLength(0);
});

test("a looked-up profile can take the roles it fits", async () => {
  modelRoles = { agent: "alpha", sweep: null, judge: "alpha" };
  lookup = () => Response.json(draft({ fits: ["agent"] }));
  const { dialog, id } = await openLookup();
  fireEvent.change(id, { target: { value: "acme/gem-9" } });
  fireEvent.submit(id);
  const agent = await dialog.findByRole("checkbox", { name: /^agent/ });
  expect(agent.closest("label")?.textContent).toBe("agent runs a scenario's agents · now alpha");
  const sweep = dialog.getByRole("checkbox", { name: /^sweep/ });
  expect(sweep).toHaveProperty("disabled", true);
  expect(sweep.closest("label")?.textContent).toBe(
    "sweep reads a run as it goes · OpenRouter lists no structured output for it",
  );
  fireEvent.click(agent);
  expect(
    dialog.getByText("aims models add openrouter acme/gem-9 --write --role agent"),
  ).toBeTruthy();
  fireEvent.click(dialog.getByRole("button", { name: "Add to project config" }));
  expect(await dialog.findByText("/work/aims.toml")).toBeTruthy();
  expect(await calls("POST", "/api/models/add")[0]?.json()).toEqual({
    slug: "acme/gem-9",
    source: "openrouter",
    roles: ["agent"],
  });
  expect(agent).toHaveProperty("disabled", true);
  cleanup();

  modelRoles = { agent: null, sweep: null, judge: null };
  lookup = () => Response.json(draft());
  const again = await openLookup();
  fireEvent.change(again.id, { target: { value: "acme/gem-9" } });
  fireEvent.submit(again.id);
  const judge = await again.dialog.findByRole("checkbox", { name: /^judge/ });
  expect(judge.closest("label")?.textContent).toBe("judge gives the verdict after a run · unset");
  fireEvent.click(judge);
  fireEvent.click(again.dialog.getByRole("checkbox", { name: /^agent/ }));
  // the flags keep the roles' order, whatever the order they were ticked in
  expect(
    again.dialog.getByText(
      "aims models add openrouter acme/gem-9 --write --role agent --role judge",
    ),
  ).toBeTruthy();
});
