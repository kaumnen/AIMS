import {
  type InfiniteData,
  infiniteQueryOptions,
  type QueryClient,
  type QueryKey,
  queryOptions,
} from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Live } from "./live";
import { readFinal, refreshLastSteps } from "./run";
import {
  pathsApiRunsGetParametersQueryCategoryAnyOf0Values as categoryValues,
  runSummaryKindValues,
} from "./schema.gen";
import type { SeriesInfo } from "./series";

export type RunSummary = Schemas["RunSummary"];
export type RunPage = Schemas["RunPage"];
type RunDetail = Schemas["RunDetail"];
export type RunKind = RunSummary["kind"];
export type Category = NonNullable<(typeof categoryValues)[number]>;

export const runKinds = runSummaryKindValues;
export const categories = categoryValues.filter((c): c is Category => c !== null);

// Whether a run's job will change no more: it ran and its log is sealed, or it was cancelled
// in the queue before it started.
export const ended = (run: Pick<RunSummary, "state">): boolean =>
  run.state === "done" || run.state === "cancelled";

// Each field is a GET /api/runs query parameter.
export interface RunFilters {
  kind?: RunKind;
  origin?: string;
  scenario?: string;
  series?: string;
  variant?: string;
  severity?: number;
  category?: Category;
}

// The list's URL search. No kind means agent runs; "all" lifts the kind filter.
export interface RunsSearch {
  kind?: RunKind | "all";
  scenario?: string;
  series?: string;
  variant?: string;
  severity?: number;
  category?: Category;
}

const text = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : undefined);

export function parseRunsSearch(raw: Record<string, unknown>): RunsSearch {
  const out: RunsSearch = {};
  const kind = raw["kind"];
  if (kind === "all" || runKinds.includes(kind as RunKind)) out.kind = kind as RunKind | "all";
  if (out.kind === "scenario") delete out.kind;
  for (const key of ["scenario", "series", "variant"] as const) {
    const v = text(raw[key]);
    if (v !== undefined) out[key] = v;
  }
  const severity = Number(raw["severity"]);
  if (Number.isInteger(severity) && severity >= 1 && severity <= 5) out.severity = severity;
  const category = raw["category"];
  if (categories.includes(category as Category)) out.category = category as Category;
  return out;
}

export function filtersOf(search: RunsSearch): RunFilters {
  const { kind, ...rest } = search;
  if (kind === "all") return rest;
  return { ...rest, kind: kind ?? "scenario" };
}

export const PAGE = 100;
const MAX_REVIEWS = 1000;

async function page(
  filters: RunFilters,
  before: string | null,
  limit: number,
  signal: AbortSignal,
) {
  const { data, error } = await client.GET("/api/runs", {
    params: { query: { ...filters, limit, ...(before === null ? {} : { before }) } },
    signal,
  });
  if (error) throw error;
  return data;
}

// The live stream keeps these current (syncRuns), so they never go stale on their own.
export function runsQuery(filters: RunFilters) {
  return infiniteQueryOptions({
    queryKey: ["runs", "list", filters] as const,
    queryFn: ({ pageParam, signal }) => page(filters, pageParam, PAGE, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next,
    staleTime: Infinity,
  });
}

// The case runs of a self-test run.
export function casesQuery(origin: string) {
  return queryOptions({
    queryKey: ["runs", "cases", origin] as const,
    queryFn: ({ signal }) => page({ kind: "selftest.case", origin }, null, MAX_REVIEWS, signal),
    staleTime: Infinity,
  });
}

export function reviewsQuery(origin: string) {
  return queryOptions({
    queryKey: ["runs", "reviews", origin] as const,
    queryFn: ({ signal }) => page({ kind: "monitor", origin }, null, MAX_REVIEWS, signal),
    staleTime: Infinity,
  });
}

// The server's filters, applied to one run, so a live update lands where a fetch would put it.
export function matches(f: RunFilters, s: RunSummary): boolean {
  return (
    (f.kind === undefined || s.kind === f.kind) &&
    (f.origin === undefined || s.origin === f.origin) &&
    (f.scenario === undefined || s.scenario === f.scenario) &&
    (f.series === undefined || s.series === f.series) &&
    (f.variant === undefined || s.variant === f.variant) &&
    (f.severity === undefined || s.max_severity >= f.severity) &&
    (f.category === undefined || (s.categories[f.category] ?? 0) > 0)
  );
}

// Pages hold runs newest first. A known run is replaced in place; a new one that matches goes
// where its id sorts, unless it sorts below the loaded pages and more are still to come.
export function mergePages(
  pages: readonly RunPage[],
  runs: readonly RunSummary[],
  filters: RunFilters,
): RunPage[] | null {
  const out = pages.map((p) => ({ ...p, items: [...p.items] }));
  const where = new Map<string, number>();
  out.forEach((p, i) => {
    for (const r of p.items) where.set(r.run_id, i);
  });
  const complete = (pages.at(-1)?.next ?? null) === null;
  let changed = false;
  for (const run of runs) {
    const at = where.get(run.run_id);
    if (at !== undefined) {
      const items = out[at]?.items ?? [];
      const i = items.findIndex((r) => r.run_id === run.run_id);
      // a message older than a page read after it would undo the page
      const older = (items[i]?.events ?? 0) > run.events;
      if (i >= 0 && items[i] !== run && !older) {
        items[i] = run;
        changed = true;
      }
      continue;
    }
    if (!matches(filters, run)) continue;
    let target = out.findIndex((p) => {
      const last = p.items.at(-1);
      return last !== undefined && last.run_id < run.run_id;
    });
    if (target < 0) {
      if (!complete) continue;
      target = Math.max(0, out.length - 1);
    }
    const p = out[target];
    if (p === undefined) continue;
    const i = p.items.findIndex((r) => r.run_id < run.run_id);
    p.items.splice(i < 0 ? p.items.length : i, 0, run);
    where.set(run.run_id, target);
    changed = true;
  }
  return changed ? out : null;
}

function applyList(qc: QueryClient, key: QueryKey, runs: readonly RunSummary[]): void {
  const filters = key[2] as RunFilters;
  qc.setQueryData<InfiniteData<RunPage, string | null>>(key, (data) => {
    if (!data) return data;
    const pages = mergePages(data.pages, runs, filters);
    return pages ? { ...data, pages } : data;
  });
}

const listFetching = (qc: QueryClient) =>
  qc
    .getQueryCache()
    .findAll({ queryKey: ["runs", "list"] })
    .some((q) => q.state.fetchStatus === "fetching");

// The lists of runs the stream keeps current; deleted runs leave each.
const runLists: ReadonlySet<unknown> = new Set(["list", "reviews", "scores", "cases"]);
const isRunList = (key: QueryKey) => key[0] === "runs" && runLists.has(key[1]);
const runListFetching = (qc: QueryClient) =>
  qc.isFetching({ queryKey: ["runs"], predicate: (q) => isRunList(q.queryKey) }) > 0;
// how many deleted runs a load in flight holds at most, the oldest dropped first
const MAX_GONE = 10_000;

export function applyRuns(qc: QueryClient, runs: readonly RunSummary[]): void {
  if (runs.length === 0) return;
  for (const q of qc.getQueryCache().findAll({ queryKey: ["runs"] })) {
    const [, what, arg] = q.queryKey as readonly [string, string, unknown];
    if (what === "list") {
      applyList(qc, q.queryKey, runs);
    } else if (what === "detail") {
      const run = runs.find((r) => r.run_id === arg);
      const data = qc.getQueryData<RunDetail>(q.queryKey);
      if (!run || !data || data.run === run) continue;
      qc.setQueryData<RunDetail>(q.queryKey, { ...data, run });
      if (
        data.run.commands !== run.commands ||
        data.run.commands_waiting !== run.commands_waiting
      ) {
        void qc.invalidateQueries({ queryKey: ["runs", "commands", run.run_id], exact: true });
      }
      // its start, end and verdict events only come with a fetch
      if (data.run.status !== run.status || data.run.verdict_severity !== run.verdict_severity) {
        void qc.invalidateQueries({ queryKey: q.queryKey, exact: true });
        void qc.invalidateQueries({ queryKey: ["runs", "steps", run.run_id], exact: true });
        refreshLastSteps(qc, run.run_id);
      }
    } else if (what === "series") {
      applySeries(qc, q.queryKey, runs);
    } else if (what === "reviews" || what === "scores" || what === "cases") {
      const kind = what === "reviews" ? "monitor" : what === "scores" ? "score" : "selftest.case";
      qc.setQueryData<RunPage>(q.queryKey, (data) => {
        if (!data) return data;
        const pages = mergePages([data], runs, { kind, origin: arg as string });
        return pages?.[0] ?? data;
      });
    }
  }
  // A sealed run's log takes nothing more, so what views polled of it is read a last time.
  for (const run of runs) {
    if (run.started_at !== null && run.status !== "running") readFinal(qc, run.run_id);
  }
}

// A run of the series that is new or changed status moves where the series goes on, so the
// series is read again; other changes land in place.
function applySeries(qc: QueryClient, key: QueryKey, runs: readonly RunSummary[]): void {
  const data = qc.getQueryData<SeriesInfo>(key);
  const mine = runs.filter((r) => r.series === key[2]);
  if (!data || mine.length === 0) return;
  const known = new Map(data.runs.map((r) => [r.run_id, r]));
  if (mine.some((r) => known.get(r.run_id)?.status !== r.status)) {
    void qc.invalidateQueries({ queryKey: key, exact: true });
    return;
  }
  const fresh = new Map(mine.map((r) => [r.run_id, r]));
  qc.setQueryData<SeriesInfo>(key, {
    ...data,
    runs: data.runs.map((r) => fresh.get(r.run_id) ?? r),
  });
}

// Run updates from the stream, applied at most once a frame.
export function syncRuns(
  live: Live,
  qc: QueryClient,
  frame: (fn: () => void) => void = requestAnimationFrame,
): () => void {
  let pending = new Map<string, RunSummary>();
  // A page that lands writes the pages as they were when its fetch began, so updates made
  // meanwhile are held and made again on top of it.
  let held = new Map<string, RunSummary>();
  // The same goes for runs deleted meanwhile, which leave each list again as it lands.
  let gone = new Set<string>();
  let scheduled = false;
  let stopped = false;
  const flush = () => {
    scheduled = false;
    if (stopped) return;
    const runs = [...pending.values()];
    pending = new Map();
    applyRuns(qc, runs);
    if (listFetching(qc)) for (const r of runs) held.set(r.run_id, r);
  };
  const unwatch = qc.getQueryCache().subscribe((event) => {
    if (held.size === 0 && gone.size === 0) return;
    if (event.type !== "updated" || !isRunList(event.query.queryKey)) return;
    const key = event.query.queryKey;
    const deleted = gone;
    if (deleted.size > 0 && !runListFetching(qc)) gone = new Set();
    if (event.action.type !== "success" || event.action.manual) return;
    const runs = key[1] === "list" ? [...held.values()] : [];
    if (runs.length > 0 && !listFetching(qc)) held = new Map();
    if (runs.length === 0 && deleted.size === 0) return;
    queueMicrotask(() => {
      if (stopped) return;
      if (runs.length > 0) applyList(qc, key, runs);
      if (deleted.size > 0) dropFrom(qc, key, deleted);
    });
  });
  const off = live.on("run", ({ run }) => {
    pending.set(run.run_id, run);
    if (!scheduled) {
      scheduled = true;
      frame(flush);
    }
  });
  const offDeleted = live.on("deleted", ({ run_id }) => {
    pending.delete(run_id);
    held.delete(run_id);
    if (runListFetching(qc)) {
      gone.add(run_id);
      if (gone.size > MAX_GONE) {
        const [oldest] = gone;
        if (oldest !== undefined) gone.delete(oldest);
      }
    }
    qc.setQueryData(deletedQuery.queryKey, (ids = []) => [...ids, run_id]);
    dropRuns(qc, [run_id]);
  });
  return () => {
    stopped = true;
    off();
    offDeleted();
    unwatch();
  };
}

// Runs deleted elsewhere while this page is open, so a page that shows one can say so. The
// stream fills it; it is never fetched.
export const deletedQuery = queryOptions({
  queryKey: ["deleted"] as const,
  queryFn: () => [] as string[],
  initialData: [] as string[],
  staleTime: Infinity,
  gcTime: Infinity,
});

// Deleted runs leave every list. Their own reads go once nothing shows them: a page that
// shows one leaves first, then calls this again.
export function dropRuns(qc: QueryClient, runIds: readonly string[]): void {
  const gone = new Set(runIds);
  for (const q of qc.getQueryCache().findAll({ queryKey: ["runs"] })) {
    const [, what, arg] = q.queryKey as readonly [string, string, unknown];
    if (isRunList(q.queryKey)) {
      dropFrom(qc, q.queryKey, gone);
    } else if (what === "series") {
      void qc.invalidateQueries({ queryKey: q.queryKey, exact: true });
    }
    if (typeof arg === "string" && gone.has(arg) && q.getObserversCount() === 0) {
      qc.removeQueries({ queryKey: q.queryKey, exact: true });
    }
  }
}

// Takes the runs out of one list of runs, if it holds any of them.
function dropFrom(qc: QueryClient, key: QueryKey, gone: ReadonlySet<string>): void {
  const holds = (items: readonly RunSummary[]) => items.some((r) => gone.has(r.run_id));
  const kept = (items: readonly RunSummary[]) => items.filter((r) => !gone.has(r.run_id));
  if (key[1] === "list") {
    qc.setQueryData<InfiniteData<RunPage, string | null>>(key, (data) =>
      data?.pages.some((p) => holds(p.items))
        ? { ...data, pages: data.pages.map((p) => ({ ...p, items: kept(p.items) })) }
        : data,
    );
  } else {
    qc.setQueryData<RunPage>(key, (data) =>
      data && holds(data.items) ? { ...data, items: kept(data.items) } : data,
    );
  }
}
