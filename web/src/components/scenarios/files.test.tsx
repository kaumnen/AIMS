import { insertNewline } from "@codemirror/commands";
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
// a save answers once this settles
let putGate: Promise<void>;

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
  putGate = Promise.resolve();
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
      // as the bytes are, a byte order mark too
      const body = new TextDecoder("utf-8", { ignoreBOM: true }).decode(
        await request.arrayBuffer(),
      );
      const ifMatch = request.headers.get("If-Match");
      puts.push({ body, ifMatch });
      await putGate;
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
    if (decodeURIComponent(url.pathname) === `${BASE}/files/selftest/exploit.sh`) {
      return new Response("echo hi\n", { headers: { ETag: '"x1"' } });
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
  const tab = (file: string) => (
    <QueryClientProvider client={client}>
      <FilesTab detail={detail(writable)} file={file} onFile={onFile} />
    </QueryClientProvider>
  );
  const { rerender } = render(tab("scenario.yaml"));
  return { onFile, client, open: (file: string) => rerender(tab(file)) };
}

async function editor(file = "scenario.yaml"): Promise<EditorView> {
  const content = await screen.findByLabelText(`Contents of ${file}`);
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
  // a detail the page has no words for shows as it came
  expect(alert.textContent).toContain(
    "scenario.yaml changed on disk since you opened it (the file changed).",
  );
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

test("a file opened any way but the tree, such as Back, leaves the last one's draft behind", async () => {
  const { open } = renderTab();
  type(await editor(), "demo", "mine");
  expect(await screen.findByText("unsaved changes")).toBeTruthy();
  open("selftest/exploit.sh");
  await waitFor(async () =>
    expect((await editor("selftest/exploit.sh")).state.doc.toString()).toBe("echo hi\n"),
  );
  expect(screen.queryByText("unsaved changes")).toBeNull();
  open("scenario.yaml");
  await waitFor(async () => expect((await editor()).state.doc.toString()).toBe(TEXT));
  expect(screen.queryByText("unsaved changes")).toBeNull();
});

test("what is typed while a save is under way stays a draft over the saved version", async () => {
  renderTab();
  const view = await editor();
  type(view, "demo", "mine");
  const held = Promise.withResolvers<void>();
  putGate = held.promise;
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(puts).toHaveLength(1));
  type(view, "mine", "mine2");
  await act(async () => held.resolve());
  await screen.findByText("unsaved changes");
  expect(view.state.doc.toString()).toBe(TEXT.replace("demo", "mine2"));
  putGate = Promise.resolve();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(puts).toHaveLength(2));
  expect(puts.at(-1)).toEqual({ body: TEXT.replace("demo", "mine2"), ifMatch: '"e2"' });
});

test("a file read again with no edits shows each new version and makes no draft", async () => {
  const { client } = renderTab();
  const view = await editor();
  const reread = async (text: string, etag: string) => {
    onDisk = { text, etag };
    await act(() => client.refetchQueries({ queryKey: ["scenarios", "file"] }));
    await waitFor(() => expect(view.state.doc.toString()).toBe(text));
    expect(screen.queryByText("unsaved changes")).toBeNull();
  };
  await reread(TEXT.replace("demo", "theirs"), '"e8"');
  await reread(TEXT.replace("demo", "later"), '"e9"');
});

// Saves the file after one edit, and returns what was sent.
async function saveEdit(view: EditorView, from: string, to: string): Promise<string> {
  type(view, from, to);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saved");
  return puts.at(-1)?.body ?? "";
}

test.each([
  ["CRLF", "\r\n"],
  ["CR", "\r"],
])(
  "a %s file keeps its line endings, and an edit taken back leaves it unchanged",
  async (_, eol) => {
    const text = TEXT.replaceAll("\n", eol);
    onDisk = { text, etag: '"e1"' };
    renderTab();
    const view = await editor();
    expect(view.state.doc.lines).toBe(4);
    type(view, "demo", "demo2");
    expect(await screen.findByText("unsaved changes")).toBeTruthy();
    type(view, "demo2", "demo");
    await waitFor(() => expect(screen.queryByText("unsaved changes")).toBeNull());
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    // Enter after "observe" starts a line with the file's ending
    const at = view.state.doc.toString().indexOf("observe") + "observe".length;
    act(() => view.dispatch({ selection: { anchor: at } }));
    act(() => void insertNewline(view));
    expect(await saveEdit(view, "observe", "observe!")).toBe(
      text.replace("observe", `observe!${eol}`),
    );
  },
);

test("a file with mixed line endings saves back unchanged but for the edit", async () => {
  const text = "version: 1\r\nname: demo\noversight: observe\r\n# a\rb\n";
  onDisk = { text, etag: '"e1"' };
  renderTab();
  const view = await editor();
  // LF ends the lines; a CR stays where it is
  expect(view.state.doc.lines).toBe(5);
  expect(await saveEdit(view, "demo", "demo2")).toBe(text.replace("demo", "demo2"));
});

test("a byte order mark stays on save", async () => {
  onDisk = { text: `\uFEFF${TEXT}`, etag: '"e1"' };
  renderTab();
  const view = await editor();
  // the editor leaves it out
  expect(view.state.doc.toString()).toBe(TEXT);
  expect(await saveEdit(view, "demo", "demo2")).toBe(`\uFEFF${TEXT.replace("demo", "demo2")}`);
});

test("pasted text takes the file's line ending", async () => {
  const text = TEXT.replaceAll("\n", "\r\n");
  onDisk = { text, etag: '"e1"' };
  renderTab();
  const view = await editor();
  act(() => view.dispatch({ selection: { anchor: view.state.doc.length } }));
  fireEvent.paste(view.contentDOM, { clipboardData: { getData: () => "a: 1\nb: 2\r\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saved");
  expect(puts.at(-1)?.body).toBe(`${text}a: 1\r\nb: 2\r\n`);
});
