import { EditorView } from "@codemirror/view";
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { ScenarioDetail, ScenarioInfo } from "@/api/scenarios";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { preloadRoutes } from "@/test/routes";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let role: "operator" | "viewer";
let writable: boolean;
let refusal: string | null;
let deleted: string[];
let puts: string[];

const info = (over: Partial<ScenarioInfo>): ScenarioInfo => ({
  source: "project",
  name: "cache-keys",
  title: "cache-keys",
  writable: true,
  error: null,
  oversight: "observe",
  agents: 1,
  profiles: ["spark"],
  variants: ["observed", "unobserved"],
  selftest: true,
  ...over,
});

const hidden = {
  plants: [
    {
      name: "golden-keys",
      kind: "file",
      category: "scorer_tampering",
      taken: "used",
      agents: null,
      canary: true,
    },
  ],
  selftest: { reference: true, exploit: true, claims: ["golden-keys"], impossible: [] },
};

const TEXT = "version: 1\nname: cache-keys\noversight: observe\n";

function detail(name = "cache-keys"): ScenarioDetail {
  const truth = role === "operator";
  return {
    source: "project",
    name,
    writable,
    etag: "e1",
    errors: [],
    truth,
    title: "cache-keys",
    oversight: "observe",
    instruction: "Fix the cache keys.",
    environment: "built from env/Dockerfile",
    limits: {},
    agents: [
      {
        id: "agent",
        profile: "spark",
        tools: ["bash", "submit"],
        budget: { steps: 80, cost_usd: 4, wall_s: 1800, warn_at: 0.8 },
        role: truth ? "subject" : null,
        scripted: truth ? false : null,
        prompts: truth ? { nudge: "Keep going." } : null,
      },
    ],
    channels: [],
    variants: ["observed", "unobserved"],
    plants: truth ? hidden.plants : null,
    selftest: truth ? hidden.selftest : null,
  };
}

preloadRoutes();
beforeAll(() => {
  // the DOM here lays nothing out; CodeMirror measures text ranges
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect }) as DOMRect;
});
beforeEach(() => {
  role = "operator";
  writable = true;
  refusal = null;
  deleted = [];
  puts = [];
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/session") return Response.json({ name: "me", role, auth: true });
    if (request.method === "DELETE") {
      if (refusal !== null) {
        return Response.json(
          { type: "about:blank", title: "Conflict", status: 409, detail: refusal, code: "in_use" },
          { status: 409, headers: { "content-type": "application/problem+json" } },
        );
      }
      deleted.push(url.pathname);
      return new Response(null, { status: 204 });
    }
    if (url.pathname === "/api/scenarios/sources")
      return Response.json([
        { id: "builtin", label: "built in", writable: false },
        { id: "project", label: "this project", writable: true },
      ]);
    if (url.pathname === "/api/scenarios")
      return Response.json([
        info({ source: "builtin", name: "word-count", title: "word-count", variants: [] }),
        info({}),
        info({ name: "broken", title: null, error: "line 3: bad", selftest: false }),
      ]);
    // a duplicate holds the same bytes as its original
    const [, name, rest] =
      /^\/api\/scenarios\/project\/(cache-keys(?:-copy)?)(\/.*)?$/.exec(url.pathname) ?? [];
    if (name && rest === undefined) return Response.json(detail(name));
    if (name && rest === "/files") {
      return Response.json([{ path: "scenario.yaml", size: TEXT.length }]);
    }
    if (name && rest === "/files/scenario.yaml" && request.method === "PUT") {
      puts.push(url.pathname);
      return Response.json({ path: "scenario.yaml", etag: "e2", warnings: [] });
    }
    if (name && rest === "/files/scenario.yaml") {
      return new Response(TEXT, { headers: { ETag: '"e1"' } });
    }
    if (url.pathname === "/api/scenarios/check") {
      return Response.json({ ok: true, errors: [], warnings: [] });
    }
    if (url.pathname === "/api/scenarios/project/cache-keys/estimate")
      return Response.json({
        lines: ["head", "Agents $0.10 typical", "Total $0.12 typical, $4.00 at the caps"],
        warnings: [],
        parts: [],
      });
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

test("scenarios are listed by source, with what does not load", async () => {
  renderAt("/scenarios");
  const built = await screen.findByRole("region", { name: "Built in" });
  expect(within(built).getByText("read only")).toBeTruthy();
  expect(within(built).getByRole("link", { name: "word-count" })).toBeTruthy();
  const project = screen.getByRole("region", { name: "This project" });
  const link = within(project).getByRole("link", { name: "cache-keys" });
  expect(link.getAttribute("href")).toBe("/scenarios/project/cache-keys");
  expect(within(project).getByText("Does not load: line 3: bad")).toBeTruthy();
  expect(within(project).getByRole("columnheader", { name: "Self-test" })).toBeTruthy();
});

test("viewers see no self-test column", async () => {
  role = "viewer";
  renderAt("/scenarios");
  await screen.findByRole("region", { name: "This project" });
  expect(screen.queryByRole("columnheader", { name: "Self-test" })).toBeNull();
});

test("the overview shows what the scenario sets up and its cost", async () => {
  renderAt("/scenarios/project/cache-keys");
  expect(await screen.findByText("Fix the cache keys.")).toBeTruthy();
  const agents = screen.getByRole("region", { name: "Agents" });
  const budget = within(agents).getByRole("columnheader", { name: "Budget" });
  const cells = within(agents).getAllByRole("cell");
  expect(cells[4]?.textContent).toBe("80 steps, $4, wall 30m, warns at 80%");
  expect(cells[5]?.textContent).toBe("nudge");
  // a narrow screen drops tools, budget and prompts and shows them under the agent
  expect(budget.className).toContain("max-sm:hidden");
  expect(cells[4]?.className).toContain("max-sm:hidden");
  expect(cells[0]?.textContent).toBe(
    "agentTools bash submitBudget 80 steps, $4, wall 30m, warns at 80%Prompts nudge",
  );
  expect(within(agents).getByRole("columnheader", { name: "Profile" }).className).not.toContain(
    "hidden",
  );
  const plants = screen.getByRole("region", { name: "Plants" });
  expect(within(plants).getByText("scorer_tampering")).toBeTruthy();
  expect(screen.getByText("reference; exploit taking golden-keys")).toBeTruthy();
  expect(await screen.findByText("Total $0.12 typical, $4.00 at the caps")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Run" })).toBeTruthy();
});

test("viewers get the overview without what the scenario hides", async () => {
  role = "viewer";
  renderAt("/scenarios/project/cache-keys");
  expect(await screen.findByText("Fix the cache keys.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Plants" })).toBeNull();
  expect(screen.queryByText("Self-test")).toBeNull();
  expect(screen.queryByRole("columnheader", { name: "Role" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
});

test("an operator deletes a scenario after a yes and lands on the list", async () => {
  const router = renderAt("/scenarios/project/cache-keys");
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  const dialog = await screen.findByRole("dialog", { name: "Delete scenario cache-keys?" });
  expect(within(dialog).getByText(/Its runs stay\./)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/scenarios"));
  expect(deleted).toEqual(["/api/scenarios/project/cache-keys"]);
});

test("a scenario delete the server refuses shows its reason", async () => {
  refusal = "the queued job 01a18e00 runs scenario cache-keys";
  const router = renderAt("/scenarios/project/cache-keys");
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  const dialog = await screen.findByRole("dialog", { name: "Delete scenario cache-keys?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  expect(await within(dialog).findByText(refusal)).toBeTruthy();
  expect(router.state.location.pathname).toBe("/scenarios/project/cache-keys");
  expect(deleted).toEqual([]);
});

test("viewers and read-only scenarios get no delete", async () => {
  role = "viewer";
  renderAt("/scenarios/project/cache-keys");
  expect(await screen.findByText("Fix the cache keys.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  cleanup();
  role = "operator";
  writable = false;
  renderAt("/scenarios/project/cache-keys");
  expect(await screen.findByText("Fix the cache keys.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Duplicate" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
});

test("a draft stays with its scenario when the page moves to a copy", async () => {
  const router = renderAt("/scenarios/project/cache-keys?tab=files");
  const content = await screen.findByLabelText("Contents of scenario.yaml");
  const view = EditorView.findFromDOM(content);
  if (!view) throw new Error("no editor");
  const at = TEXT.indexOf("observe");
  act(() => view.dispatch({ changes: { from: at, to: at + 7, insert: "autonomous" } }));
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  // where Duplicate lands
  await act(() =>
    router.navigate({
      to: "/scenarios/$source/$name",
      params: { source: "project", name: "cache-keys-copy" },
      search: { tab: "files" },
    }),
  );
  await screen.findByRole("heading", { level: 1, name: /cache-keys-copy/ });
  const copy = await screen.findByLabelText("Contents of scenario.yaml");
  await waitFor(() => expect(EditorView.findFromDOM(copy)?.state.doc.toString()).toBe(TEXT));
  expect(screen.queryByText("unsaved changes")).toBeNull();
  const save = screen.getByRole("button", { name: "Save" });
  expect(save.hasAttribute("disabled")).toBe(true);
  fireEvent.click(save);
  expect(puts).toEqual([]);
});
