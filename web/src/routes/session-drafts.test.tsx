// A session that ends under an open draft: the sign-in form shows over the page, which keeps the
// draft for the same one signing in again. Someone else starts from nothing.
import { EditorView } from "@codemirror/view";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { FakeEventSource } from "@/test/event-source";
import { renderAt } from "@/test/report";
import { preloadRoutes } from "@/test/routes";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
const TEXT = "version: 1\nname: cache-keys\noversight: observe\n";
// who the cookie names, null for nobody; a sign-in makes it `next`
let who: string | null;
let next: string;

const detail = {
  source: "project",
  name: "cache-keys",
  writable: true,
  etag: "e1",
  errors: [],
  truth: true,
  title: "cache-keys",
  oversight: "observe",
  instruction: "Fix the cache keys.",
  environment: "built from env/Dockerfile",
  limits: {},
  agents: [],
  channels: [],
  variants: [],
  plants: [],
  selftest: null,
};

preloadRoutes();
beforeAll(() => {
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect }) as DOMRect;
});

beforeEach(() => {
  who = "op";
  next = "op";
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session" && request.method === "POST") {
      who = next;
      return new Response(null, { status: 204 });
    }
    if (who === null) {
      return Response.json(
        { type: "about:blank", title: "Not signed in", status: 401 },
        { status: 401, headers: { "content-type": "application/problem+json" } },
      );
    }
    if (path === "/api/session") return Response.json({ name: who, role: "operator", auth: true });
    if (path === "/api/scenarios/sources") {
      return Response.json([{ id: "project", label: "this project", writable: true }]);
    }
    if (path === "/api/scenarios") return Response.json([]);
    if (path === "/api/scenarios/project/cache-keys") return Response.json(detail);
    if (path === "/api/scenarios/project/cache-keys/files") {
      return Response.json([{ path: "scenario.yaml", size: TEXT.length }]);
    }
    if (path === "/api/scenarios/project/cache-keys/files/scenario.yaml") {
      return new Response(TEXT, { headers: { ETag: '"e1"' } });
    }
    if (path === "/api/scenarios/check")
      return Response.json({ ok: true, errors: [], warnings: [] });
    if (path === "/api/models") return Response.json({ profiles: [], roles: {} });
    return Response.json({ items: [], next: null });
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

const LABEL = "Contents of scenario.yaml";
const text = async () =>
  EditorView.findFromDOM(await screen.findByLabelText(LABEL))?.state.doc.toString();
const usable = () => FakeEventSource.all.filter((s) => s.readyState !== FakeEventSource.CLOSED);

async function draft() {
  const router = renderAt("/scenarios/project/cache-keys?tab=files");
  const view = EditorView.findFromDOM(await screen.findByLabelText(LABEL));
  if (!view) throw new Error("no editor");
  act(() => view.dispatch({ changes: { from: 0, insert: "# mine\n" } }));
  await screen.findByText("unsaved changes");
  const source = FakeEventSource.all[0];
  if (!source) throw new Error("no stream");
  act(() => source.open());
  // the reads something on the page still follows
  const followed = () =>
    router.options.context.queryClient
      .getQueryCache()
      .getAll()
      .filter((q) => q.getObserversCount() > 0)
      .map((q) => q.queryKey);
  return { source, followed };
}

async function signIn(as: string) {
  next = as;
  fireEvent.change(await screen.findByLabelText("Token"), { target: { value: "aims_t" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

test.each([
  ["its stream closed", (source: FakeEventSource) => act(() => source.fail(true))],
  ["a read elsewhere", () => act(() => client.GET("/api/health"))],
])("a draft is still there when the same one signs in again after %s", async (_, end) => {
  const { source, followed } = await draft();
  who = null;
  await end(source);
  await screen.findByLabelText("Token");
  // the page waits hidden under the form, with no stream and no read but who is signed in
  expect(screen.queryByRole("tab", { name: "Files" })).toBeNull();
  await waitFor(() => expect(usable()).toHaveLength(0));
  expect(followed()).toEqual([["get", "/api/session"]]);
  await signIn("op");
  await screen.findByRole("tab", { name: "Files" });
  expect(screen.queryByLabelText("Token")).toBeNull();
  expect(await text()).toBe(`# mine\n${TEXT}`);
  expect(screen.getByText("unsaved changes")).toBeTruthy();
  // its stream opens again
  await waitFor(() => expect(usable()).toHaveLength(1));
});

test("someone else signing in after a 401 starts without the draft", async () => {
  const { source } = await draft();
  who = null;
  act(() => source.fail(true));
  await signIn("vera");
  expect(await screen.findByRole("button", { name: /vera/ })).toBeTruthy();
  await waitFor(async () => expect(await text()).toBe(TEXT));
  expect(screen.queryByText("unsaved changes")).toBeNull();
});
