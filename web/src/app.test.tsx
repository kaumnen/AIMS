import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { reconnectLive } from "./api/channel";
import { client } from "./api/client";
import { whoami } from "./api/session";
import { makeRouter } from "./router";
import { FakeEventSource, runnerInfo } from "./test/event-source";
import { stubHeight } from "./test/layout";
import { preloadRoutes } from "./test/routes";
import { run } from "./test/runs";

vi.mock("./api/channel", async (actual) => {
  const channel = await actual<typeof import("./api/channel")>();
  return { ...channel, reconnectLive: vi.fn<() => void>(channel.reconnectLive) };
});

let me = { name: "op", role: "operator", auth: true };
const fetch = vi.fn<(request: Request) => Promise<Response>>();
let signedIn = true;

const unauthorized = () =>
  Response.json(
    { type: "about:blank", title: "Not signed in", status: 401, code: "unauthenticated" },
    { status: 401, headers: { "content-type": "application/problem+json" } },
  );

preloadRoutes();
stubHeight();
beforeEach(() => {
  signedIn = true;
  me = { name: "op", role: "operator", auth: true };
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session" && request.method === "DELETE") {
      signedIn = false;
      return new Response(null, { status: 204 });
    }
    if (path === "/api/session" && request.method === "POST") {
      const body = (await request.json()) as { token?: string };
      if (body.token !== "good") return unauthorized();
      signedIn = true;
      return new Response(null, { status: 204 });
    }
    if (!signedIn) return unauthorized();
    if (path === "/api/session") return Response.json(me);
    if (path === "/api/runs") return Response.json({ items: [], next: null });
    if (path === "/api/scenarios") return Response.json([]);
    if (path === "/api/models") return Response.json({ profiles: [], roles: {} });
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

async function renderAt(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

test("root redirects to runs", async () => {
  const router = await renderAt("/");
  expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();
  expect(router.state.location.pathname).toBe("/runs");
  expect(document.title).toBe("Runs · AIMS");
});

test.each(["Scenarios", "Settings"])("%s route renders", async (name) => {
  await renderAt(`/${name.toLowerCase()}`);
  expect(await screen.findByRole("heading", { name })).toBeTruthy();
  const nav = within(screen.getByRole("banner"));
  expect(nav.getByRole("link", { name }).dataset["status"]).toBe("active");
});

test("signed out shows the sign-in form, which keeps the page", async () => {
  signedIn = false;
  const router = await renderAt("/scenarios");
  const field = await screen.findByLabelText("Token");
  expect(FakeEventSource.all).toHaveLength(0);
  fireEvent.change(field, { target: { value: "bad" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect((await screen.findByRole("alert")).textContent).toBe("That token is unknown or revoked.");
  fireEvent.change(field, { target: { value: " good " } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("heading", { name: "Scenarios" })).toBeTruthy();
  expect(router.state.location.pathname).toBe("/scenarios");
});

test("a 401 elsewhere brings the sign-in form back", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  signedIn = false;
  await act(() => client.GET("/api/health"));
  expect(await screen.findByLabelText("Token")).toBeTruthy();
});

test("runs that ended while the control plane was out of reach show it once it is back", async () => {
  let down = false;
  let r1 = run("r1");
  const base = fetch.getMockImplementation();
  if (!base) throw new Error("no fetch");
  fetch.mockImplementation(async (request) => {
    if (down) throw new TypeError("offline");
    if (new URL(request.url).pathname === "/api/runs") {
      return Response.json({ items: [r1], next: null });
    }
    return base(request);
  });
  await renderAt("/runs");
  expect(await screen.findByText("Running")).toBeTruthy();
  down = true;
  act(() => FakeEventSource.all[0]?.fail(true));
  const again = await screen.findByRole("button", { name: "Try again" });
  down = false;
  r1 = run("r1", { status: "completed", state: "done" });
  fireEvent.click(again);
  expect(await screen.findByText("Completed")).toBeTruthy();
  expect(screen.queryByText("Running")).toBeNull();
});

// The server lists score runs to an operator only; `me` says who the cookie belongs to.
function serveScoreRuns() {
  const base = fetch.getMockImplementation();
  if (!base) throw new Error("no fetch");
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (signedIn && url.pathname === "/api/runs" && url.searchParams.get("kind") === "score") {
      const items =
        me.role === "operator" ? [run("s1", { kind: "score", scenario: "operator-only" })] : [];
      return Response.json({ items, next: null });
    }
    return base(request);
  });
}

async function endWithA401() {
  await screen.findByText("operator-only");
  signedIn = false;
  await act(() => client.GET("/api/health"));
  return screen.findByLabelText("Token");
}

test("who signs in after a 401 sees nothing the last session loaded", async () => {
  serveScoreRuns();
  await renderAt("/runs?kind=score");
  const field = await endWithA401();
  me = { name: "vera", role: "viewer", auth: true };
  fireEvent.change(field, { target: { value: "good" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("button", { name: /vera/ })).toBeTruthy();
  expect(await screen.findByText(/No runs yet/)).toBeTruthy();
  expect(screen.queryByText("operator-only")).toBeNull();
  expect(reconnectLive).toHaveBeenCalled();
});

test("a session started in another tab shows nothing the last one loaded", async () => {
  serveScoreRuns();
  const router = await renderAt("/runs?kind=score");
  await endWithA401();
  // another tab signs in, and this one asks again who it is
  signedIn = true;
  me = { name: "vera", role: "viewer", auth: true };
  await act(() => router.options.context.queryClient.refetchQueries(whoami));
  expect(await screen.findByRole("button", { name: /vera/ })).toBeTruthy();
  expect(await screen.findByText(/No runs yet/)).toBeTruthy();
  expect(screen.queryByText("operator-only")).toBeNull();
});

const sessionReads = () =>
  fetch.mock.calls.filter(([r]) => r.method === "GET" && new URL(r.url).pathname === "/api/session")
    .length;

test("a tab that comes back after another signed in as someone else shows the new one", async () => {
  serveScoreRuns();
  await renderAt("/runs?kind=score");
  await screen.findByText("operator-only");
  vi.mocked(reconnectLive).mockClear();
  // another tab signs in as vera, and this one never saw its own session end
  me = { name: "vera", role: "viewer", auth: true };
  act(() => void window.dispatchEvent(new Event("focus")));
  expect(await screen.findByRole("button", { name: /vera/ })).toBeTruthy();
  expect(await screen.findByText(/No runs yet/)).toBeTruthy();
  expect(screen.queryByText("operator-only")).toBeNull();
  expect(reconnectLive).toHaveBeenCalled();
});

test("a tab that comes back to the same session keeps what it holds", async () => {
  serveScoreRuns();
  await renderAt("/runs?kind=score");
  await screen.findByText("operator-only");
  const reads = sessionReads();
  const all = fetch.mock.calls.length;
  act(() => void document.dispatchEvent(new Event("visibilitychange")));
  await waitFor(() => expect(sessionReads()).toBe(reads + 1));
  await act(() => Promise.resolve());
  expect(fetch.mock.calls.length).toBe(all + 1);
  expect(screen.getByText("operator-only")).toBeTruthy();
  expect(screen.getByRole("button", { name: /op/ })).toBeTruthy();
});

test("a tab that comes back after its session ended elsewhere asks to sign in", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  signedIn = false;
  act(() => void window.dispatchEvent(new Event("focus")));
  expect(await screen.findByLabelText("Token")).toBeTruthy();
});

test("the header shows runner and tracer health from the stream", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  const [source] = FakeEventSource.all;
  expect(source?.url).toBe("/api/stream?runs=true");
  act(() => source?.open());
  expect(await screen.findByText("No runner")).toBeTruthy();
  const runner = runnerInfo({ tracer: { alive: false, detail: "bpftrace exited", restarts: 1 } });
  act(() => source?.send({ kind: "runner", runner }));
  expect(await screen.findByText("local")).toBeTruthy();
  expect(screen.getByText("tracer off")).toBeTruthy();
  act(() => source?.send({ kind: "runner", runner: runnerInfo() }));
  expect(await screen.findByText("tracer on")).toBeTruthy();
  act(() => source?.fail(false));
  expect(await screen.findByText("Reconnecting")).toBeTruthy();
});

test("sign out ends the session and the stream", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  fireEvent.click(screen.getByRole("button", { name: /op/ }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  expect(await screen.findByLabelText("Token")).toBeTruthy();
  expect(FakeEventSource.all.every((s) => s.readyState === FakeEventSource.CLOSED)).toBe(true);
});

test("without auth there is nobody to sign out", async () => {
  me = { name: "anonymous", role: "operator", auth: false };
  await renderAt("/runs");
  fireEvent.click(await screen.findByRole("button", { name: /No sign-in/ }));
  expect(await screen.findByRole("menuitemradio", { name: "System" })).toBeTruthy();
  expect(screen.queryByRole("menuitem", { name: "Sign out" })).toBeNull();
});

test("the skip link moves focus to the page", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  const skip = screen.getByRole("link", { name: "Skip to content" });
  expect(document.querySelector("[data-shell]")?.querySelector("a")).toBe(skip);
  fireEvent.click(skip);
  expect(document.activeElement).toBe(screen.getByRole("main"));
});

test("? shows the shortcuts and Escape gives focus back", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  const field = screen.getAllByRole("textbox")[0]!;
  field.focus();
  fireEvent.keyDown(field, { key: "?" });
  expect(screen.queryByRole("dialog")).toBeNull();

  const menu = screen.getByRole("button", { name: /op/ });
  menu.focus();
  fireEvent.keyDown(menu, { key: "?" });
  const sheet = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
  expect(within(sheet).getByRole("region", { name: "Runs" })).toBeTruthy();
  fireEvent.keyDown(sheet, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(document.activeElement).toBe(menu));

  fireEvent.click(menu);
  fireEvent.click(await screen.findByRole("menuitem", { name: /Shortcuts/ }));
  expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeTruthy();
});

test("the health tooltip opens on keyboard focus", async () => {
  await renderAt("/runs");
  await screen.findByRole("heading", { name: "Runs" });
  act(() => FakeEventSource.all[0]?.open());
  const trigger = (await screen.findByText("No runner")).closest("button");
  if (!trigger) throw new Error("no health trigger");
  act(() => trigger.focus());
  expect(await screen.findByText(/No runner has connected/)).toBeTruthy();
});
