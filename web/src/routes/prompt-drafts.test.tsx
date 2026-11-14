// A prompt edited in place, in Settings > Prompts and in a scenario's Prompts tab: a leave
// drops the draft, so it asks first, as the Files tab and Settings > Config do.
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { hasUnsaved } from "@/lib/unsaved";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { preloadRoutes } from "@/test/routes";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let questions: string[];
// what the person answers to a question
let yes: boolean;

const prompt = (text: string, project: string | null = null) => ({
  text: project ?? text,
  source: project !== null ? "project" : "default",
  default: text,
  user: null,
  project,
  placeholders: null,
});

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
  agents: [
    {
      id: "agent",
      profile: "spark",
      tools: ["bash", "submit"],
      budget: { steps: 80 },
      role: "subject",
      scripted: false,
      prompts: {},
    },
  ],
  channels: [],
  variants: [],
  plants: [],
  selftest: null,
};

preloadRoutes();
beforeEach(() => {
  questions = [];
  yes = false;
  vi.stubGlobal("confirm", (q: string) => {
    questions.push(q);
    return yes;
  });
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session") return Response.json({ name: "me", role: "operator", auth: true });
    if (path === "/api/prompts") {
      return Response.json({
        agent: { system: prompt("Work on the task.", "Work here.") },
        tools: {},
        monitor: {},
        categories: {},
      });
    }
    if (path === "/api/config") {
      return Response.json({
        layers: [
          { layer: "user", path: "/home/me/.config/aims/config.toml", exists: true, etag: "u1" },
          { layer: "project", path: "/work/aims.toml", exists: true, etag: "p1" },
        ],
        error: null,
      });
    }
    if (path === "/api/config/project") {
      return Response.json({
        layer: "project",
        path: "/work/aims.toml",
        exists: true,
        text: "",
        etag: "p1",
      });
    }
    if (path === "/api/scenarios/sources") {
      return Response.json([{ id: "project", label: "this project", writable: true }]);
    }
    if (path === "/api/scenarios") return Response.json([]);
    if (path === "/api/scenarios/project/cache-keys") return Response.json(detail);
    if (path.endsWith("/estimate"))
      return Response.json({ lines: ["est"], warnings: [], parts: [] });
    if (path === "/api/runs") return Response.json({ items: [], next: null });
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

async function editSystem() {
  fireEvent.click(await screen.findByRole("button", { name: "Edit system" }));
  const box = (await screen.findByRole("textbox", {
    name: "Text of system",
  })) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "My long careful rewrite of the system prompt." } });
  await waitFor(() => {
    if (box.value !== "My long careful rewrite of the system prompt.") throw new Error("no draft");
  });
}

const TEXT = "Text of system";
const draft = () => screen.queryByRole("textbox", { name: TEXT });

test("a link away from a prompt draft in Settings asks first", async () => {
  const router = renderAt("/settings/prompts");
  await editSystem();
  expect(hasUnsaved()).toBe(true);
  // a blocked navigation never settles
  act(() => void router.navigate({ to: "/runs" }));
  await waitFor(() => expect(questions).toEqual(["Discard your changes to system?"]));
  expect(router.state.location.pathname).toBe("/settings/prompts");
  expect(draft()).not.toBeNull();
  yes = true;
  await act(() => router.navigate({ to: "/runs" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/runs"));
  expect(hasUnsaved()).toBe(false);
});

test("a prompt editor opened and left as it was holds nothing to ask about", async () => {
  const router = renderAt("/settings/prompts");
  fireEvent.click(await screen.findByRole("button", { name: "Edit system" }));
  await screen.findByRole("textbox", { name: TEXT });
  expect(hasUnsaved()).toBe(false);
  await act(() => router.navigate({ to: "/runs" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/runs"));
  expect(questions).toEqual([]);
});

test("a prompt draft cancelled is no longer asked about", async () => {
  renderAt("/settings/prompts");
  await editSystem();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(draft()).toBeNull());
  expect(hasUnsaved()).toBe(false);
});

test("another tab of a scenario asks before it drops a prompt draft", async () => {
  const router = renderAt("/scenarios/project/cache-keys?tab=prompts");
  await editSystem();
  expect(hasUnsaved()).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
  await waitFor(() => expect(questions).toEqual(["Discard your changes to system?"]));
  expect(router.state.location.search).toEqual({ tab: "prompts" });
  expect(draft()).not.toBeNull();
  yes = true;
  fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
  await waitFor(() => expect(draft()).toBeNull());
  expect(hasUnsaved()).toBe(false);
});
