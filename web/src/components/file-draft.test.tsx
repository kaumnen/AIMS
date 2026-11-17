import { QueryClient, QueryClientProvider, queryOptions } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { LineProblem } from "@/components/code-editor";

import { type DraftOptions, useFileDraft } from "./file-draft";

interface Stored {
  text: string;
  etag: string | null;
}

// the file on disk; a read fails while `down`
let disk: Stored;
let down: boolean;
// each write's text and the version it named
let writes: { text: string; etag: string | null }[];

const problem = (status: number, detail: string, errors: { msg: string; line: number }[] = []) => ({
  type: "about:blank",
  title: "Refused",
  status,
  detail,
  errors,
});

const fileQuery = queryOptions({
  queryKey: ["file"] as const,
  queryFn: async (): Promise<Stored> => {
    if (down) throw problem(502, "the server is away");
    return { ...disk };
  },
  staleTime: 0,
});

const write = vi.fn<(text: string, etag: string | null) => Promise<Stored>>();

beforeEach(() => {
  disk = { text: "a", etag: "v1" };
  down = false;
  writes = [];
  write.mockReset();
  write.mockImplementation(async (text, etag) => {
    writes.push({ text, etag });
    if (etag !== disk.etag) throw problem(412, "the file changed since it was read");
    disk = { text, etag: `${disk.etag ?? "v0"}+` };
    return { ...disk };
  });
});

afterEach(cleanup);

function draftOf(options: Partial<DraftOptions<Stored>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const onSaved = vi.fn<() => void>();
  const hook = renderHook(
    ({ scope }: { scope: string }) =>
      useFileDraft(fileQuery, {
        scope,
        editable: true,
        check: null,
        write,
        onSaved,
        ...options,
      }),
    { wrapper, initialProps: { scope: "one" } },
  );
  return { ...hook, client, onSaved };
}

async function opened(options: Partial<DraftOptions<Stored>> = {}) {
  const draft = draftOf(options);
  await waitFor(() => expect(draft.result.current.opened.data).toBeDefined());
  return draft;
}

type Hook = Awaited<ReturnType<typeof opened>>;

// The file changes on disk and is read again.
async function reread(result: Hook["result"], client: QueryClient, now: Stored) {
  disk = now;
  await act(() => client.refetchQueries({ queryKey: ["file"] }));
  // the query tells the hook on its next tick
  await waitFor(() => expect(result.current.opened.data).toEqual(now));
}

test("a save names the version the draft began from, then the one it made", async () => {
  const { result, client, onSaved } = await opened();
  act(() => result.current.edit("b"));
  expect(result.current.dirty).toBe(true);
  // read again under the draft: the draft still began from v1
  await reread(result, client, { text: "theirs", etag: "v2" });
  expect(result.current.text).toBe("b");
  await act(() => result.current.save());
  expect(writes).toEqual([{ text: "b", etag: "v1" }]);
  expect(result.current.conflict).toBe("the file changed since it was read");
  expect(result.current.status).toBeNull();
  expect(onSaved).not.toHaveBeenCalled();

  await act(() => result.current.overwrite());
  expect(writes.at(-1)).toEqual({ text: "b", etag: "v2" });
  expect(result.current.conflict).toBeNull();
  expect(result.current.status).toBe("Saved");
  expect(result.current.dirty).toBe(false);
  expect(onSaved).toHaveBeenCalledOnce();

  act(() => result.current.edit("c"));
  await act(() => result.current.save());
  expect(writes.at(-1)).toEqual({ text: "c", etag: "v2+" });
});

test("a file not made yet saves as new and says what the caller says", async () => {
  disk = { text: "starter", etag: null };
  const { result } = await opened({ done: (was) => (was.etag === null ? "Created" : "Saved") });
  act(() => result.current.edit("made"));
  await act(() => result.current.save());
  expect(writes).toEqual([{ text: "made", etag: null }]);
  expect(result.current.status).toBe("Created");
});

test("load theirs drops the draft and reads the file again", async () => {
  const { result } = await opened();
  act(() => result.current.edit("mine"));
  disk = { text: "theirs", etag: "v2" };
  await act(() => result.current.save());
  expect(result.current.conflict).not.toBeNull();
  await act(() => result.current.reload());
  expect(result.current.conflict).toBeNull();
  // the query tells the hook on its next tick
  await waitFor(() => expect(result.current.text).toBe("theirs"));
  expect(result.current.dirty).toBe(false);
  // a new draft begins from what was read
  act(() => result.current.edit("again"));
  await act(() => result.current.save());
  expect(writes.at(-1)).toEqual({ text: "again", etag: "v2" });
});

test("a refusal with problems by line shows them; other failures say why", async () => {
  const { result } = await opened({ invalid: "Not saved: it does not load." });
  act(() => result.current.edit("bad"));
  write.mockRejectedValueOnce(problem(422, "no load", [{ msg: "line 1 is wrong", line: 1 }]));
  await act(() => result.current.save());
  expect(result.current.problems).toEqual([{ line: 1, msg: "line 1 is wrong", severity: "error" }]);
  expect(result.current.status).toBe("Not saved: it does not load.");

  write.mockRejectedValueOnce(problem(500, "disk full"));
  await act(() => result.current.save());
  expect(result.current.status).toBe("Not saved: disk full");

  write.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await act(() => result.current.save());
  expect(result.current.status).toBe("Not saved: the request failed.");

  down = true;
  await act(() => result.current.overwrite());
  expect(result.current.status).toBe("Not saved: the server is away");
  expect(result.current.dirty).toBe(true);
});

test("a read-only file saves nothing", async () => {
  const { result } = await opened({ editable: false });
  act(() => result.current.edit("b"));
  await act(() => result.current.save());
  expect(write).not.toHaveBeenCalled();
  expect(result.current.status).toBeNull();
});

test("a new scope drops the draft, its version, problems and conflict", async () => {
  const { result, rerender, client } = await opened();
  act(() => result.current.edit("mine"));
  write.mockRejectedValueOnce(problem(422, "no load", [{ msg: "line 1 is wrong", line: 1 }]));
  await act(() => result.current.save());
  act(() => result.current.setGoto({ line: 1 }));
  await reread(result, client, { text: "theirs", etag: "v2" });
  await act(() => result.current.save());
  expect(result.current.conflict).not.toBeNull();
  expect(result.current.problems).toHaveLength(1);
  rerender({ scope: "two" });
  expect(result.current.text).toBe("theirs");
  expect(result.current.dirty).toBe(false);
  expect(result.current.problems).toEqual([]);
  expect(result.current.conflict).toBeNull();
  expect(result.current.goto).toBeNull();
  // the next draft begins from the file as read now
  act(() => result.current.edit("fresh"));
  await act(() => result.current.save());
  expect(writes.at(-1)).toEqual({ text: "fresh", etag: "v2" });
});

test("the text is checked once it settles, and only the latest", async () => {
  vi.useFakeTimers();
  try {
    const check = vi.fn<(text: string, signal: AbortSignal) => Promise<LineProblem[]>>(
      async (text) => [{ line: 1, msg: `saw ${text}`, severity: "warning" }],
    );
    const { result } = draftOf({ check });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.opened.data).toBeDefined();
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(check.mock.calls.map(([text]) => text)).toEqual(["a"]);
    act(() => result.current.edit("ab"));
    await act(() => vi.advanceTimersByTimeAsync(200));
    act(() => result.current.edit("abc"));
    await act(() => vi.advanceTimersByTimeAsync(399));
    expect(check).toHaveBeenCalledOnce();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(check.mock.calls.map(([text]) => text)).toEqual(["a", "abc"]);
    expect(result.current.problems).toEqual([{ line: 1, msg: "saw abc", severity: "warning" }]);
  } finally {
    vi.useRealTimers();
  }
});

test("the page hears of a draft, and of none once the editor is gone", async () => {
  const onDirty = vi.fn<(dirty: boolean) => void>();
  const { result, unmount } = await opened({ onDirty });
  act(() => result.current.edit("b"));
  expect(onDirty).toHaveBeenLastCalledWith(true);
  act(() => result.current.edit("a"));
  expect(onDirty).toHaveBeenLastCalledWith(false);
  act(() => result.current.edit("b"));
  unmount();
  expect(onDirty).toHaveBeenLastCalledWith(false);
});
