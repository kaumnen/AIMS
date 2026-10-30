import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "./api/client";
import { makeRouter } from "./router";
import { FakeEventSource, runnerInfo } from "./test/event-source";

let me = { name: "op", role: "operator", auth: true };
const fetch = vi.fn<(request: Request) => Promise<Response>>();
let signedIn = true;

const unauthorized = () =>
  Response.json(
    { type: "about:blank", title: "Not signed in", status: 401, code: "unauthenticated" },
    { status: 401, headers: { "content-type": "application/problem+json" } },
  );

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
  expect(screen.getByRole("link", { name }).dataset["status"]).toBe("active");
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
