import { type InfiniteData, QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";

import { FakeEventSource } from "@/test/event-source";
import { run } from "@/test/runs";

import { Live } from "./live";
import {
  applyRuns,
  filtersOf,
  matches,
  mergePages,
  parseRunsSearch,
  reviewsQuery,
  type RunPage,
  runsQuery,
  syncRuns,
} from "./runs";

afterEach(() => {
  vi.unstubAllGlobals();
});

const ids = (pages: readonly RunPage[] | null) => pages?.map((p) => p.items.map((r) => r.run_id));

test("search keeps known values and drops the rest", () => {
  expect(
    parseRunsSearch({
      kind: "monitor",
      scenario: " cache-keys ",
      severity: "3",
      category: "drift",
      series: "",
      variant: 4,
    }),
  ).toEqual({ kind: "monitor", scenario: "cache-keys", severity: 3, category: "drift" });
  expect(parseRunsSearch({ kind: "scenario", severity: 9, category: "nope" })).toEqual({});
  expect(parseRunsSearch({ kind: "bogus" })).toEqual({});
});

test("agent runs are the default kind and all lifts it", () => {
  expect(filtersOf({})).toEqual({ kind: "scenario" });
  expect(filtersOf({ kind: "all", scenario: "x" })).toEqual({ scenario: "x" });
  expect(filtersOf({ kind: "score" })).toEqual({ kind: "score" });
});

test("matches applies every server filter", () => {
  const r = run("b", { scenario: "s", max_severity: 3, categories: { drift: 2 }, series: "q" });
  expect(matches({ kind: "scenario", scenario: "s", severity: 3, category: "drift" }, r)).toBe(
    true,
  );
  expect(matches({ severity: 4 }, r)).toBe(false);
  expect(matches({ category: "scorer_tampering" }, r)).toBe(false);
  expect(matches({ kind: "monitor" }, r)).toBe(false);
  expect(matches({ series: "q", variant: "v" }, r)).toBe(false);
  expect(matches({ origin: "a" }, run("m", { kind: "monitor", origin: "a" }))).toBe(true);
});

test("a known run is replaced where it is", () => {
  const pages = [
    { items: [run("d"), run("c")], next: "c" },
    { items: [run("b")], next: null },
  ];
  const updated = run("b", { status: "completed" });
  const out = mergePages(pages, [updated], {});
  expect(ids(out)).toEqual([["d", "c"], ["b"]]);
  expect(out?.[1]?.items[0]).toBe(updated);
  expect(mergePages(out ?? [], [updated], {})).toBeNull();
});

test("a new run goes where its id sorts, only when it matches", () => {
  const pages = [
    { items: [run("f"), run("d")], next: "d" },
    { items: [run("c"), run("a")], next: "a" },
  ];
  expect(ids(mergePages(pages, [run("g"), run("e"), run("b")], {}))).toEqual([
    ["g", "f", "e", "d"],
    ["c", "b", "a"],
  ]);
  expect(mergePages(pages, [run("g", { kind: "score" })], { kind: "scenario" })).toBeNull();
});

test("a run below the loaded pages waits for its page unless none is left", () => {
  const more = [{ items: [run("d"), run("c")], next: "c" }];
  expect(mergePages(more, [run("a")], {})).toBeNull();
  const done = [{ items: [run("d"), run("c")], next: null }];
  expect(ids(mergePages(done, [run("a")], {}))).toEqual([["d", "c", "a"]]);
  expect(ids(mergePages([{ items: [], next: null }], [run("a")], {}))).toEqual([["a"]]);
});

test("updates reach every loaded list and review list", () => {
  const qc = new QueryClient();
  const list = runsQuery({ kind: "scenario" });
  qc.setQueryData<InfiniteData<RunPage, string | null>>(list.queryKey, {
    pages: [{ items: [run("b")], next: null }],
    pageParams: [null],
  });
  const reviews = reviewsQuery("b");
  qc.setQueryData(reviews.queryKey, { items: [], next: null });
  applyRuns(qc, [run("c"), run("m", { kind: "monitor", origin: "b" })]);
  expect(qc.getQueryData(list.queryKey)?.pages[0]?.items.map((r) => r.run_id)).toEqual(["c", "b"]);
  expect(qc.getQueryData(reviews.queryKey)?.items.map((r) => r.run_id)).toEqual(["m"]);
});

test("stream updates are applied once a frame, the last one per run winning", () => {
  const qc = new QueryClient();
  const list = runsQuery({});
  qc.setQueryData<InfiniteData<RunPage, string | null>>(list.queryKey, {
    pages: [{ items: [run("b")], next: null }],
    pageParams: [null],
  });
  const frames: (() => void)[] = [];
  const live = new Live({ runs: true });
  const stop = syncRuns(live, qc, (fn) => frames.push(fn));
  vi.stubGlobal("EventSource", FakeEventSource);
  live.start();
  const source = FakeEventSource.all.at(-1);
  source?.send({ kind: "run", run: run("b", { steps: 1 }) });
  source?.send({ kind: "run", run: run("b", { steps: 2 }) });
  expect(frames).toHaveLength(1);
  frames[0]?.();
  expect(qc.getQueryData(list.queryKey)?.pages[0]?.items[0]?.steps).toBe(2);
  stop();
  live.stop();
});
