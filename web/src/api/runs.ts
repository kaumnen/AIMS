import {
  type InfiniteData,
  infiniteQueryOptions,
  type QueryClient,
  queryOptions,
} from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Live } from "./live";
import {
  pathsApiRunsGetParametersQueryCategoryAnyOf0Values as categoryValues,
  runSummaryKindValues,
} from "./schema.gen";

export type RunSummary = Schemas["RunSummary"];
export type RunPage = Schemas["RunPage"];
type RunDetail = Schemas["RunDetail"];
export type RunKind = RunSummary["kind"];
export type Category = NonNullable<(typeof categoryValues)[number]>;

export const runKinds = runSummaryKindValues;
export const categories = categoryValues.filter((c): c is Category => c !== null);

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
      if (i >= 0 && items[i] !== run) {
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

export function applyRuns(qc: QueryClient, runs: readonly RunSummary[]): void {
  if (runs.length === 0) return;
  for (const q of qc.getQueryCache().findAll({ queryKey: ["runs"] })) {
    const [, what, arg] = q.queryKey as readonly [string, string, unknown];
    if (what === "list") {
      qc.setQueryData<InfiniteData<RunPage, string | null>>(q.queryKey, (data) => {
        if (!data) return data;
        const pages = mergePages(data.pages, runs, arg as RunFilters);
        return pages ? { ...data, pages } : data;
      });
    } else if (what === "detail") {
      const run = runs.find((r) => r.run_id === arg);
      const data = qc.getQueryData<RunDetail>(q.queryKey);
      if (!run || !data || data.run === run) continue;
      qc.setQueryData<RunDetail>(q.queryKey, { ...data, run });
      // its start, end and verdict events only come with a fetch
      if (data.run.status !== run.status || data.run.verdict_severity !== run.verdict_severity) {
        void qc.invalidateQueries({ queryKey: q.queryKey, exact: true });
        void qc.invalidateQueries({ queryKey: ["runs", "steps", run.run_id], exact: true });
      }
    } else if (what === "reviews") {
      qc.setQueryData<RunPage>(q.queryKey, (data) => {
        if (!data) return data;
        const pages = mergePages([data], runs, { kind: "monitor", origin: arg as string });
        return pages?.[0] ?? data;
      });
    }
  }
}

// Run updates from the stream, applied at most once a frame.
export function syncRuns(
  live: Live,
  qc: QueryClient,
  frame: (fn: () => void) => void = requestAnimationFrame,
): () => void {
  let pending = new Map<string, RunSummary>();
  let scheduled = false;
  let stopped = false;
  const flush = () => {
    scheduled = false;
    if (stopped) return;
    const runs = [...pending.values()];
    pending = new Map();
    applyRuns(qc, runs);
  };
  const off = live.on("run", ({ run }) => {
    pending.set(run.run_id, run);
    if (!scheduled) {
      scheduled = true;
      frame(flush);
    }
  });
  return () => {
    stopped = true;
    off();
  };
}
