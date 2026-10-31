import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ScenarioDetail } from "@/api/scenarios";

import { PromptsTab } from "./prompts";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
let patches: { body: unknown; ifMatch: string | null }[];
// scenario.yaml's version on the server; each save makes a new one
let version: number;

beforeEach(() => {
  patches = [];
  version = 1;
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/prompts") {
      return Response.json({
        agent: {
          system: { text: "You are an agent.", source: "default" },
          nudge: { text: "Keep going.", source: "project" },
        },
        tools: {
          bash: { description: { text: "Run a command.", source: "default" } },
          edit: { description: { text: "Edit a file.", source: "default" } },
        },
        monitor: {},
        categories: {},
      });
    }
    if (url.pathname === "/api/scenarios/project/demo/prompts") {
      patches.push({ body: await request.json(), ifMatch: request.headers.get("If-Match") });
      version += 1;
      return Response.json({ path: "scenario.yaml", etag: `e${version}`, warnings: [] });
    }
    if (url.pathname === "/api/scenarios/project/demo") {
      return Response.json({ ...detail(), etag: `e${version}` });
    }
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function detail(writable = true): ScenarioDetail {
  return {
    source: "project",
    name: "demo",
    writable,
    etag: "e1",
    agents: [
      {
        id: "agent",
        profile: "spark",
        tools: ["bash", "submit"],
        budget: {},
        role: "subject",
        scripted: false,
        prompts: { tools: { bash: { description: "Run one command." } } },
      },
    ],
  } as unknown as ScenarioDetail;
}

function renderTab(writable = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PromptsTab detail={detail(writable)} />
    </QueryClientProvider>,
  );
}

const rowOf = (label: string) => {
  const cell = screen.getByText(label);
  const row = cell.closest("li");
  if (!row) throw new Error(`no row ${label}`);
  return within(row);
};

test("each text says where it comes from, the agent's own tools only", async () => {
  renderTab();
  await screen.findByText("system");
  expect(rowOf("system").getByText("built in")).toBeTruthy();
  expect(rowOf("nudge").getByText("this project")).toBeTruthy();
  expect(rowOf("bash.description").getByText("this scenario")).toBeTruthy();
  expect(rowOf("bash.description").getByText("Run one command.")).toBeTruthy();
  expect(screen.queryByText("edit.description")).toBeNull();
});

test("an override is written into the scenario against the version on the page", async () => {
  renderTab();
  await screen.findByText("system");
  fireEvent.click(rowOf("nudge").getByRole("button", { name: "Edit nudge" }));
  const box = await screen.findByLabelText("Text of nudge");
  fireEvent.change(box, { target: { value: "Go on." } });
  fireEvent.click(screen.getByRole("button", { name: "Save override" }));
  await vi.waitFor(() => expect(patches).toHaveLength(1));
  expect(patches[0]).toEqual({
    body: { set: [{ agent: "agent", key: ["nudge"], value: "Go on." }] },
    ifMatch: '"e1"',
  });
  await vi.waitFor(() => expect(screen.queryByLabelText("Text of nudge")).toBeNull());
  fireEvent.click(rowOf("bash.description").getByRole("button", { name: "Edit bash.description" }));
  fireEvent.click(await screen.findByRole("button", { name: "Use the config's text" }));
  await vi.waitFor(() => expect(patches).toHaveLength(2));
  expect(patches[1]?.body).toEqual({
    unset: [{ agent: "agent", key: ["tools", "bash", "description"] }],
  });
});

test("a second edit right after a save is made against the saved version", async () => {
  // the page still holds the first version: its refetch has not landed
  renderTab();
  await screen.findByText("system");
  fireEvent.click(rowOf("nudge").getByRole("button", { name: "Edit nudge" }));
  fireEvent.change(await screen.findByLabelText("Text of nudge"), { target: { value: "Go on." } });
  fireEvent.click(screen.getByRole("button", { name: "Save override" }));
  await vi.waitFor(() => expect(patches).toHaveLength(1));
  await vi.waitFor(() => expect(screen.queryByLabelText("Text of nudge")).toBeNull());
  fireEvent.click(rowOf("system").getByRole("button", { name: "Edit system" }));
  fireEvent.change(await screen.findByLabelText("Text of system"), { target: { value: "Hi." } });
  fireEvent.click(screen.getByRole("button", { name: "Save override" }));
  await vi.waitFor(() => expect(patches).toHaveLength(2));
  expect(patches.map((p) => p.ifMatch)).toEqual(['"e1"', '"e2"']);
});

test("a read-only source shows the texts without edits", async () => {
  renderTab(false);
  await screen.findByText("system");
  expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();
  expect(screen.getByText(/duplicate the scenario/)).toBeTruthy();
});
