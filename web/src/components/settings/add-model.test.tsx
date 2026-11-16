import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AddInfo, ModelDraft, ProfileInfo } from "@/api/launch";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { pick } from "@/test/pick";
import { preloadRoutes } from "@/test/routes";
import { run } from "@/test/runs";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
const CHECK = "01a19300-0000-7000-8000-000000000001";
const TOML =
  '[profiles.gem-9]\nbackend = "chat"\nbase_url = "https://api.example.com/v1"\n' +
  'model = "example/gem-9"\nweights = "example/gem-9"\napi_key_env = "EXAMPLE_API_KEY"\n' +
  'reasoning = "none"\nmax_output_tokens = 16384\n';

let profiles: ProfileInfo[];
let roles: Record<string, string | null>;
// the answer to a lookup and to an add
let lookup: (url: URL) => Response | Promise<Response>;
let added: (body: unknown) => Response | Promise<Response>;
// the project file's version, for a removal
let etag: string;

const profile = (over: Partial<ProfileInfo>): ProfileInfo => ({
  name: "alpha",
  backend: "chat",
  model: "example/alpha",
  weights: "example/alpha",
  reasoning: "none",
  roles: ["agent"],
  envs: ["EXAMPLE_API_KEY"],
  runners: ["local"],
  check: null,
  price: null,
  price_key: null,
  add: null,
  ...over,
});

const draft = (over: Partial<ModelDraft> = {}): ModelDraft => ({
  name: "gem-9",
  model: "example/gem-9",
  toml: TOML,
  notes: ["Its context size is not known, so context_tokens is unset."],
  exists: false,
  fits: ["agent", "sweep", "judge"],
  guessed: true,
  candidates: [],
  ids: ["example/gem-9"],
  listed: 3,
  ...over,
});

const addInfo = (over: Partial<AddInfo> = {}): AddInfo => ({
  run_id: CHECK,
  roles: ["agent"],
  measure: true,
  waiting: null,
  outcome: null,
  note: null,
  ...over,
});

const problem = (status: number, detail: string, code?: string) =>
  Response.json({ status, title: "Refused", detail, ...(code ? { code } : {}) }, { status });

const calls = (method: string, path: string) =>
  fetch.mock.calls
    .map(([r]) => r)
    .filter((r) => r.method === method && new URL(r.url).pathname === path);

const lookups = () =>
  calls("GET", "/api/models/lookup").map((r) =>
    Object.fromEntries(new URL(r.url).searchParams.entries()),
  );

const adds = () =>
  Promise.all(calls("POST", "/api/models/add").map((r) => r.clone().json() as Promise<unknown>));

preloadRoutes();
beforeEach(() => {
  profiles = [profile({})];
  roles = { agent: "alpha", sweep: null, judge: null };
  lookup = () => Response.json(draft());
  added = () => Response.json({ name: "gem-9", path: "/work/aims.toml", run_id: CHECK, notes: [] });
  etag = "p1";
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/session") return Response.json({ name: "me", role: "operator" });
    if (url.pathname === "/api/models") return Response.json({ profiles, roles });
    if (url.pathname === "/api/models/lookup") return lookup(url);
    if (url.pathname === "/api/models/add") return added(await request.clone().json());
    if (url.pathname === "/api/config/project" && request.method === "GET") {
      return Response.json({
        layer: "project",
        path: "/work/aims.toml",
        exists: true,
        text: "",
        etag,
      });
    }
    if (url.pathname === "/api/config/project" && request.method === "PATCH") {
      if (request.headers.get("If-Match") !== `"${etag}"`) {
        return problem(412, "the file changed since it was read");
      }
      etag = `${etag}+`;
      return Response.json({
        layer: "project",
        path: "/work/aims.toml",
        exists: true,
        text: "",
        etag,
      });
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

async function openDialog() {
  renderAt("/settings/models");
  fireEvent.click(await screen.findByRole("button", { name: "Add a model" }));
  return within(await screen.findByRole("dialog", { name: "Add a model" }));
}

type Dialog = Awaited<ReturnType<typeof openDialog>>;

const type = (dialog: Dialog, name: string, value: string) =>
  fireEvent.change(dialog.getByLabelText(name), { target: { value } });

async function drafted(dialog: Dialog) {
  type(dialog, "Base URL", " https://api.example.com/v1 ");
  type(dialog, "Model id", "example/gem-9");
  fireEvent.click(dialog.getByRole("button", { name: "Draft" }));
  return (await dialog.findByRole("textbox", { name: "Profile" })) as HTMLTextAreaElement;
}

test("a draft asks the server only on submit, once, with the fields as typed", async () => {
  const answer = Promise.withResolvers<Response>();
  lookup = () => answer.promise;
  const dialog = await openDialog();
  const button = dialog.getByRole("button", { name: "Draft" });
  expect(button).toHaveProperty("disabled", true);
  type(dialog, "Base URL", " https://api.example.com/v1 ");
  type(dialog, "Key env var (optional)", "EXAMPLE_API_KEY");
  type(dialog, "Model id", " example/gem-9 ");
  type(dialog, "Profile name (optional)", "gem");
  type(dialog, "LiteLLM price row (optional)", "example/gem-9");
  await pick(dialog, "API", /^responses/);
  // typing reaches no server
  expect(lookups()).toHaveLength(0);
  const model = dialog.getByLabelText("Model id");
  act(() => {
    fireEvent.submit(model);
    fireEvent.submit(model);
  });
  expect(await dialog.findByText("Reading the server's list of models")).toBeTruthy();
  expect(button).toHaveProperty("disabled", true);
  expect(lookups()).toEqual([
    {
      model: "example/gem-9",
      base_url: "https://api.example.com/v1",
      key_env: "EXAMPLE_API_KEY",
      name: "gem",
      litellm: "example/gem-9",
      backend: "responses",
    },
  ]);
  answer.resolve(Response.json(draft()));
  const text = (await dialog.findByRole("textbox", { name: "Profile" })) as HTMLTextAreaElement;
  expect(text.value).toBe(TOML);
  expect(dialog.getByText(/^Its context size is not known/)).toBeTruthy();
  expect(
    dialog.getByText(
      "aims models add example/gem-9 --base-url https://api.example.com/v1 --key-env" +
        " EXAMPLE_API_KEY --name gem --litellm example/gem-9 --backend responses --write",
    ),
  ).toBeTruthy();
  expect(calls("POST", "/api/models/add")).toHaveLength(0);
});

test("a draft can take the server of a profile", async () => {
  const dialog = await openDialog();
  await pick(dialog, "Server", "The server of profile alpha");
  expect(dialog.queryByRole("textbox", { name: "Base URL" })).toBeNull();
  type(dialog, "Model id", "example/gem-9");
  fireEvent.click(dialog.getByRole("button", { name: "Draft" }));
  await dialog.findByRole("textbox", { name: "Profile" });
  expect(lookups()).toEqual([{ model: "example/gem-9", from: "alpha" }]);
  expect(dialog.getByText("aims models add example/gem-9 --from alpha --write")).toBeTruthy();
});

test("the ids on the server's list and LiteLLM's rows draft again on a click", async () => {
  lookup = (url) => {
    const model = url.searchParams.get("model") ?? "";
    return Response.json(
      draft({
        model,
        ids: model === "example/gem" ? ["example/gem-9", "example/gem-9-mini"] : [model],
        candidates: url.searchParams.has("litellm") ? [] : ["vendor/gem-9", "other/gem-9"],
      }),
    );
  };
  const dialog = await openDialog();
  type(dialog, "Base URL", "https://api.example.com/v1");
  type(dialog, "Model id", "example/gem");
  fireEvent.click(dialog.getByRole("button", { name: "Draft" }));
  const ids = await dialog.findByText("Ids on the server's list");
  fireEvent.click(
    within(ids.parentElement as HTMLElement).getByRole("button", { name: "example/gem-9" }),
  );
  await waitFor(() => expect(lookups()).toHaveLength(2));
  expect(lookups()[1]?.["model"]).toBe("example/gem-9");
  await waitFor(() => expect(dialog.queryByText("Ids on the server's list")).toBeNull());
  expect(dialog.getByLabelText("Model id")).toHaveProperty("value", "example/gem-9");

  const rows = dialog.getByText("LiteLLM rows that may price it");
  fireEvent.click(
    within(rows.parentElement as HTMLElement).getByRole("button", { name: "other/gem-9" }),
  );
  await waitFor(() => expect(lookups()).toHaveLength(3));
  expect(lookups()[2]).toMatchObject({ model: "example/gem-9", litellm: "other/gem-9" });
  await waitFor(() => expect(dialog.queryByText("LiteLLM rows that may price it")).toBeNull());
});

test("a refused draft says why", async () => {
  lookup = () => problem(422, "'http://' is not a base URL with a host", "connection");
  const dialog = await openDialog();
  type(dialog, "Base URL", "http://");
  type(dialog, "Model id", "example/gem-9");
  fireEvent.click(dialog.getByRole("button", { name: "Draft" }));
  expect(await dialog.findByText("'http://' is not a base URL with a host")).toBeTruthy();
  expect(dialog.queryByRole("textbox", { name: "Profile" })).toBeNull();
});

test("an edited profile is kept until you draft again, and a reasoning you set stays", async () => {
  roles = { agent: "alpha", sweep: null, judge: null };
  lookup = () => Response.json(draft({ fits: ["agent"] }));
  const dialog = await openDialog();
  const text = await drafted(dialog);
  // a draft that fits no sweep cannot take the role
  const sweep = dialog.getByRole("checkbox", { name: /^sweep/ });
  expect(sweep).toHaveProperty("disabled", true);
  expect(sweep.closest("label")?.textContent).toBe(
    "sweep reads a run as it goes · needs structured output",
  );
  expect(dialog.getByRole("checkbox", { name: /^agent/ }).closest("label")?.textContent).toBe(
    "agent runs a scenario's agents · now alpha",
  );
  expect(
    dialog.getByText(/The check also sets its reasoning to what the model sent\./),
  ).toBeTruthy();

  fireEvent.change(text, { target: { value: TOML.replace('"none"', '"full"') } });
  const edited = dialog.getByRole("textbox", { name: "Profile (edited)" });
  expect(dialog.getByRole("button", { name: "Draft again, dropping your edits" })).toBeTruthy();
  // the server judges an edit's roles
  expect(dialog.getByRole("checkbox", { name: /^sweep/ })).toHaveProperty("disabled", false);
  expect(dialog.getByText(/The reasoning stays as you set it\./)).toBeTruthy();
  // no CLI command makes an edited text
  expect(dialog.queryByText(/^aims models add/)).toBeNull();

  // Escape in the text closes the dialog and keeps the edit
  fireEvent.keyDown(edited, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Add a model" }));
  const again = within(await screen.findByRole("dialog", { name: "Add a model" }));
  expect(again.getByRole("textbox", { name: "Profile (edited)" })).toHaveProperty(
    "value",
    (edited as HTMLTextAreaElement).value,
  );
  fireEvent.click(again.getByRole("checkbox", { name: /^sweep/ }));
  fireEvent.click(again.getByRole("button", { name: "Add and check (a few small paid calls)" }));
  await again.findByText(/^It is in/);
  expect(await adds()).toEqual([
    { toml: TOML.replace('"none"', '"full"'), roles: ["sweep"], check: true, measure: false },
  ]);
});

test("drafting again drops the edit, and an edit back to the draft is none", async () => {
  const dialog = await openDialog();
  const text = await drafted(dialog);
  fireEvent.change(text, { target: { value: `${TOML}context_tokens = 128000\n` } });
  fireEvent.click(dialog.getByRole("button", { name: "Draft again, dropping your edits" }));
  await waitFor(() => expect(lookups()).toHaveLength(2));
  const fresh = await dialog.findByRole("textbox", { name: "Profile" });
  expect(fresh).toHaveProperty("value", TOML);
  fireEvent.change(fresh, { target: { value: `${TOML} ` } });
  fireEvent.change(dialog.getByRole("textbox", { name: "Profile (edited)" }), {
    target: { value: TOML },
  });
  expect(dialog.getByRole("textbox", { name: "Profile" })).toBeTruthy();
});

test("a name in use stops the add until the text renames it", async () => {
  lookup = () => Response.json(draft({ exists: true, name: "alpha" }));
  const dialog = await openDialog();
  const text = await drafted(dialog);
  expect(dialog.getByText(/^A profile named alpha exists already/)).toBeTruthy();
  const check = dialog.getByRole("button", { name: "Add and check (a few small paid calls)" });
  expect(check).toHaveProperty("disabled", true);
  expect(dialog.getByRole("button", { name: "Add without checking" })).toHaveProperty(
    "disabled",
    true,
  );
  fireEvent.change(text, { target: { value: TOML.replace("gem-9]", "gem-10]") } });
  expect(dialog.queryByText(/exists already/)).toBeNull();
  expect(check).toHaveProperty("disabled", false);
});

test("an add with a check is sent once and follows the check to its end", async () => {
  const answer = Promise.withResolvers<Response>();
  added = () => answer.promise;
  const dialog = await openDialog();
  await drafted(dialog);
  fireEvent.click(dialog.getByRole("checkbox", { name: /^agent/ }));
  const button = dialog.getByRole("button", { name: "Add and check (a few small paid calls)" });
  act(() => {
    button.click();
    button.click();
  });
  expect(await dialog.findByText("Adding")).toBeTruthy();
  expect(calls("POST", "/api/models/add")).toHaveLength(1);
  profiles = [
    profile({}),
    profile({
      name: "gem-9",
      roles: [],
      add: addInfo({ waiting: "no runner is connected" }),
    }),
  ];
  answer.resolve(
    Response.json({ name: "gem-9", path: "/work/aims.toml", run_id: CHECK, notes: [] }),
  );
  const done = within(await screen.findByRole("dialog", { name: "Added gem-9" }));
  expect(done.getByText("/work/aims.toml")).toBeTruthy();
  expect(
    await done.findByText(
      "The check waits: no runner is connected. The role agent is set once the check passes.",
    ),
  ).toBeTruthy();
  expect(done.getByRole("link", { name: "check run" }).getAttribute("href")).toBe(`/runs/${CHECK}`);
  expect(await adds()).toEqual([{ toml: TOML, roles: ["agent"], check: true, measure: true }]);

  profiles = [
    profile({ roles: [] }),
    profile({
      name: "gem-9",
      check: {
        run_id: CHECK,
        started_at: "2026-11-16T10:00:00+00:00",
        status: "completed",
        checks: [
          { name: "plain", verdict: "ok", detail: "answered", reasoning: "full" },
          { name: "tools", verdict: "warn", detail: "slow", reasoning: null },
        ],
      },
      add: addInfo({
        outcome: "done",
        note: "the check passed; set reasoning full and the role agent",
      }),
    }),
  ];
  act(() => {
    FakeEventSource.all[0]?.open();
    FakeEventSource.all[0]?.send({
      kind: "run",
      run: run(CHECK, { kind: "models.check", status: "completed", state: "done" }),
    });
  });
  expect(
    await done.findByText("The check passed; set reasoning full and the role agent."),
  ).toBeTruthy();
  expect(done.getByText("plain")).toBeTruthy();
  expect(done.getByText(": slow")).toBeTruthy();

  // the next add starts over
  fireEvent.click(done.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Add a model" }));
  const fresh = within(await screen.findByRole("dialog", { name: "Add a model" }));
  expect(fresh.getByLabelText("Model id")).toHaveProperty("value", "");
});

test("a field typed while a draft is on the way stays, and the draft says so", async () => {
  const dialog = await openDialog();
  const text = await drafted(dialog);
  const answer = Promise.withResolvers<Response>();
  lookup = () => answer.promise;
  fireEvent.click(dialog.getByRole("button", { name: "Draft again" }));
  await dialog.findByText("Reading the server's list of models");
  // the text waits for the draft
  expect(text).toHaveProperty("readOnly", true);
  type(dialog, "Model id", "example/gem-10");
  answer.resolve(Response.json(draft()));
  expect(await dialog.findByText("The fields changed after this draft.")).toBeTruthy();
  expect(dialog.getByLabelText("Model id")).toHaveProperty("value", "example/gem-10");
  expect(text).toHaveProperty("readOnly", false);
  expect(lookups()[1]?.["model"]).toBe("example/gem-9");
});

test("a link opens a fresh dialog after an add", async () => {
  added = () => Response.json({ name: "gem-9", path: "/work/aims.toml", run_id: null, notes: [] });
  const dialog = await openDialog();
  await drafted(dialog);
  fireEvent.click(dialog.getByRole("button", { name: "Add without checking" }));
  const done = within(await screen.findByRole("dialog", { name: "Added gem-9" }));
  fireEvent.click(done.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(await screen.findByRole("button", { name: "add a model with them" }));
  const fresh = within(await screen.findByRole("dialog", { name: "Add a model" }));
  expect(fresh.getByLabelText("Model id")).toHaveProperty("value", "");
});

test("an add without a check sets the roles at once", async () => {
  added = () => Response.json({ name: "gem-9", path: "/work/aims.toml", run_id: null, notes: [] });
  const dialog = await openDialog();
  await drafted(dialog);
  fireEvent.click(dialog.getByRole("checkbox", { name: /^judge/ }));
  fireEvent.click(dialog.getByRole("button", { name: "Add without checking" }));
  const done = within(await screen.findByRole("dialog", { name: "Added gem-9" }));
  expect(done.getByText(/It has the role judge\./)).toBeTruthy();
  expect(done.queryByRole("link", { name: "check run" })).toBeNull();
  expect(await adds()).toEqual([{ toml: TOML, roles: ["judge"], check: false, measure: true }]);
});

test("a refused add saves nothing, and a check that was not queued says so", async () => {
  added = () => problem(409, "no runner is connected", "no_runner");
  const dialog = await openDialog();
  await drafted(dialog);
  fireEvent.click(dialog.getByRole("button", { name: "Add and check (a few small paid calls)" }));
  expect(await dialog.findByText("Nothing was saved: no runner is connected")).toBeTruthy();
  expect(dialog.getByRole("textbox", { name: "Profile" })).toBeTruthy();

  const note = "Its check was not queued: boom. Check it under Settings > Models.";
  added = () =>
    Response.json({ name: "gem-9", path: "/work/aims.toml", run_id: null, notes: [note] });
  fireEvent.click(dialog.getByRole("button", { name: "Add and check (a few small paid calls)" }));
  const done = within(await screen.findByRole("dialog", { name: "Added gem-9" }));
  expect(done.getByText(note)).toBeTruthy();
  expect(done.queryByRole("link", { name: "check run" })).toBeNull();
});

test("a profile's row says how its add stands, and one that failed can be removed", async () => {
  profiles = [
    profile({}),
    profile({
      name: "waits",
      roles: [],
      add: addInfo({ roles: ["agent", "judge"], waiting: "every runner is busy" }),
    }),
    profile({
      name: "broke",
      roles: [],
      add: addInfo({
        outcome: "failed",
        note: "the check did not pass: it failed tools. The profile has no role",
      }),
    }),
    profile({ name: "fine", add: addInfo({ outcome: "done", note: "the check passed" }) }),
    // a role given by hand since, and a check after the add's own, say more than the add
    profile({ name: "given", add: addInfo({ outcome: "failed", note: "it failed" }) }),
    profile({
      name: "rechecked",
      roles: [],
      check: {
        run_id: "01a19300-0000-7000-8000-000000000009",
        started_at: "2026-11-16T11:00:00+00:00",
        status: "completed",
        checks: [],
      },
      add: addInfo({ outcome: "failed", note: "it failed" }),
    }),
  ];
  renderAt("/settings/models");
  const table = await screen.findByRole("table");
  const row = (name: string) => within(table).getByText(name).closest("tr") as HTMLElement;
  expect(
    within(row("waits")).getByText(
      "The check waits: every runner is busy. The roles agent, judge are set once the check passes.",
    ),
  ).toBeTruthy();
  expect(within(row("waits")).queryByRole("button", { name: "Remove waits" })).toBeNull();
  expect(within(row("fine")).queryByText(/check passed/)).toBeNull();
  expect(within(row("given")).queryByText("It failed.")).toBeNull();
  expect(within(row("rechecked")).queryByText("It failed.")).toBeNull();
  expect(within(row("rechecked")).queryByRole("button", { name: "Remove rechecked" })).toBeNull();
  const broke = row("broke");
  expect(
    within(broke).getByText("The check did not pass: it failed tools. The profile has no role."),
  ).toBeTruthy();
  fireEvent.click(within(broke).getByRole("button", { name: "Remove broke" }));
  // asks before it writes
  expect(calls("PATCH", "/api/config/project")).toHaveLength(0);
  fireEvent.click(within(broke).getByRole("button", { name: "Keep" }));
  fireEvent.click(within(broke).getByRole("button", { name: "Remove broke" }));
  fireEvent.click(within(broke).getByRole("button", { name: "Remove broke from aims.toml" }));
  await waitFor(() => expect(calls("PATCH", "/api/config/project")).toHaveLength(1));
  const patch = calls("PATCH", "/api/config/project")[0];
  expect(patch?.headers.get("If-Match")).toBe('"p1"');
  expect(await patch?.clone().json()).toEqual({ set: [], unset: [["profiles", "broke"]] });
});
