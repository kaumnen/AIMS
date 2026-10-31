import { EditorView } from "@codemirror/view";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { ScenarioDetail } from "@/api/scenarios";

import FilesTab from "./files";

const fetch = vi.fn<(request: Request) => Promise<Response>>();
const BASE = "/api/scenarios/project/demo";
const TEXT = "version: 1\nname: demo\noversight: observe\n";
let onDisk: { text: string; etag: string };
let puts: { body: string; ifMatch: string | null }[];
let putStatus: number;

beforeAll(() => {
  // jsdom lays nothing out; CodeMirror measures text ranges
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect }) as DOMRect;
});

beforeEach(() => {
  onDisk = { text: TEXT, etag: '"e1"' };
  puts = [];
  putStatus = 200;
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === `${BASE}/files`) {
      return Response.json([
        { path: "scenario.yaml", size: onDisk.text.length },
        { path: "selftest/exploit.sh", size: 12 },
      ]);
    }
    if (url.pathname === `${BASE}/files/scenario.yaml` && request.method === "GET") {
      return new Response(onDisk.text, { headers: { ETag: onDisk.etag } });
    }
    if (url.pathname === `${BASE}/files/scenario.yaml` && request.method === "PUT") {
      const body = await request.text();
      const ifMatch = request.headers.get("If-Match");
      puts.push({ body, ifMatch });
      if (putStatus === 500) {
        return Response.json({ status: 500, title: "Error", detail: "disk full" }, { status: 500 });
      }
      if (putStatus === 412 || ifMatch !== onDisk.etag) {
        return Response.json(
          { status: 412, title: "Precondition failed", detail: "the file changed" },
          { status: 412, headers: { "Content-Type": "application/problem+json" } },
        );
      }
      onDisk = { text: body, etag: '"e2"' };
      return Response.json({ path: "scenario.yaml", etag: "e2", warnings: [] });
    }
    if (url.pathname === "/api/scenarios/schema") return Response.json({ properties: {} });
    if (url.pathname === "/api/scenarios/check") {
      const { text } = (await request.json()) as { text: string };
      return Response.json(
        text.includes("nope")
          ? {
              ok: false,
              errors: [{ msg: "Input should be 'observe'", line: 3, loc: [] }],
              warnings: [],
            }
          : { ok: true, errors: [], warnings: [] },
      );
    }
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

const detail = (writable = true) =>
  ({ source: "project", name: "demo", writable }) as unknown as ScenarioDetail;

function renderTab(writable = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onFile = vi.fn<(file: string) => void>();
  render(
    <QueryClientProvider client={client}>
      <FilesTab detail={detail(writable)} file="scenario.yaml" onFile={onFile} />
    </QueryClientProvider>,
  );
  return { onFile, client };
}

async function editor(): Promise<EditorView> {
  const content = await screen.findByLabelText("Contents of scenario.yaml");
  const view = EditorView.findFromDOM(content);
  if (!view) throw new Error("no editor");
  return view;
}

function type(view: EditorView, from: string, to: string) {
  const text = view.state.doc.toString();
  const at = text.indexOf(from);
  act(() => view.dispatch({ changes: { from: at, to: at + from.length, insert: to } }));
}

test("the tree opens folders on a click and the open file sits first", async () => {
  const { onFile } = renderTab();
  const folder = await screen.findByRole("button", { name: "selftest" });
  expect(folder.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(folder);
  fireEvent.click(screen.getByRole("button", { name: "exploit.sh" }));
  expect(onFile).toHaveBeenCalledWith("selftest/exploit.sh");
});

test("scenario.yaml is checked as you type and problems point at their line", async () => {
  renderTab();
  const view = await editor();
  type(view, "observe", "nope");
  expect(await screen.findByText("Input should be 'observe'", {}, { timeout: 3000 })).toBeTruthy();
  expect(screen.getByText("1 error")).toBeTruthy();
  type(view, "nope", "observe");
  await waitFor(() => expect(screen.queryByText("1 error")).toBeNull(), { timeout: 3000 });
});

test("a save names the version it replaces", async () => {
  renderTab();
  const view = await editor();
  const save = screen.getByRole("button", { name: "Save" });
  expect((save as HTMLButtonElement).disabled).toBe(true);
  type(view, "demo", "demo2");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  fireEvent.click(save);
  await screen.findByText("Saved");
  expect(puts).toEqual([{ body: TEXT.replace("demo", "demo2"), ifMatch: '"e1"' }]);
});

test("a save that fails says so over the unsaved changes", async () => {
  renderTab();
  const view = await editor();
  type(view, "demo", "mine");
  putStatus = 500;
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Not saved: disk full")).toBeTruthy();
  type(view, "mine", "mine2");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
});

test("a file changed on disk is not overwritten unless you say so", async () => {
  renderTab();
  const view = await editor();
  type(view, "demo", "mine");
  putStatus = 412;
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("changed on disk");
  onDisk = { text: TEXT.replace("demo", "theirs"), etag: '"e9"' };
  putStatus = 200;
  fireEvent.click(screen.getByRole("button", { name: "Keep mine, overwrite" }));
  await screen.findByText("Saved");
  expect(puts.at(-1)).toEqual({ body: TEXT.replace("demo", "mine"), ifMatch: '"e9"' });
});

test("a file read again under a draft still saves against the version the draft began from", async () => {
  const { client } = renderTab();
  const view = await editor();
  type(view, "demo", "mine");
  onDisk = { text: TEXT.replace("demo", "theirs"), etag: '"e9"' };
  await act(() => client.refetchQueries({ queryKey: ["scenarios", "file"] }));
  // the query tells the page on its next tick
  await act(() => new Promise((r) => setTimeout(r, 20)));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect((await screen.findByRole("alert")).textContent).toContain("changed on disk");
  expect(puts.at(-1)?.ifMatch).toBe('"e1"');
});

test("a read-only source opens files without a way to save", async () => {
  renderTab(false);
  const view = await editor();
  expect(view.state.readOnly).toBe(true);
  expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  expect(screen.getByText("read only")).toBeTruthy();
});
