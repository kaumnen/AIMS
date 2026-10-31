import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Event } from "./events";

export type RunDetail = Schemas["RunDetail"];
export type StepInfo = Schemas["StepInfo"];
export type ToolInfo = Schemas["ToolInfo"];
export type ExecActivity = Schemas["ExecActivity"];
export type BlobRef = Schemas["BlobRef"];
export type SampleWindow = Schemas["SampleWindow"];
export type FlagInfo = Schemas["FlagInfo"];

const STEP_PAGE = 200;
const EXEC_PAGE = 200;
const RAW_PAGE = 200;
const MAX_EVENTS = 1000;
// The scrubber reads a whole run; this caps it at 50 pages of each kind.
const MAX_PAGES = 50;

// What a step's transcript shows; kernel records stay behind each exec's fold.
export const stepTypes = [
  "agent.input",
  "model.response",
  "tool.call",
  "agent.step",
  "exec.started",
  "exec.finished",
  "tool.result",
  "agent.finished",
  "agent.compacted",
  "agent.held",
  "monitor.flag",
  "monitor.action",
  "operator.action",
  "sys.denied",
  "plant.triggered",
  "replay.divergence",
  "task.scored",
];

const path = (runId: string) => ({ path: { run_id: runId } });

export function runQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "detail", runId] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}", {
        params: path(runId),
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
  });
}

export function stepsQuery(runId: string) {
  return infiniteQueryOptions({
    queryKey: ["runs", "steps", runId] as const,
    queryFn: async ({ pageParam, signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/steps", {
        params: {
          ...path(runId),
          query: { limit: STEP_PAGE, ...(pageParam === null ? {} : { after: pageParam }) },
        },
        signal,
      });
      if (error) throw error;
      return data;
    },
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next,
    staleTime: Infinity,
  });
}

// From just after the agent's previous step, so inputs given between steps come along; the
// last step runs on to the end, so the answer and the score come with it.
export function stepEventsQuery(runId: string, step: StepInfo, after: number, last: boolean) {
  return queryOptions({
    queryKey: ["runs", "step", runId, step.seq, step.seq_to, after, last] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/events", {
        params: {
          ...path(runId),
          query: {
            after,
            ...(last ? {} : { before: step.seq_to + 1 }),
            types: stepTypes,
            limit: MAX_EVENTS,
            ...(step.agent_id === null ? {} : { agent: step.agent_id }),
          },
        },
        signal,
      });
      if (error) throw error;
      return data.items;
    },
    staleTime: Infinity,
  });
}

export function activityQuery(runId: string, execs: readonly string[]) {
  return queryOptions({
    queryKey: ["runs", "activity", runId, ...execs] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/activity", {
        params: { ...path(runId), query: { exec: [...execs] } },
        signal,
      });
      if (error) throw error;
      return new Map(data.map((a) => [a.exec_id, a]));
    },
    enabled: execs.length > 0,
    staleTime: Infinity,
  });
}

export function execEventsQuery(runId: string, execId: string) {
  return infiniteQueryOptions({
    queryKey: ["runs", "exec", runId, execId] as const,
    queryFn: async ({ pageParam, signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/events", {
        params: {
          ...path(runId),
          query: {
            exec: [execId],
            limit: EXEC_PAGE,
            ...(pageParam === null ? {} : { after: pageParam }),
          },
        },
        signal,
      });
      if (error) throw error;
      return data;
    },
    initialPageParam: null as number | null,
    getNextPageParam: (last) => (last.next === null ? null : Number(last.next)),
    staleTime: Infinity,
  });
}

export interface RawFilters {
  types?: readonly string[];
  q?: string;
}

export function rawEventsQuery(runId: string, filters: RawFilters) {
  return infiniteQueryOptions({
    queryKey: ["runs", "events", runId, filters] as const,
    queryFn: async ({ pageParam, signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/events", {
        params: {
          ...path(runId),
          query: {
            limit: RAW_PAGE,
            ...(filters.types ? { types: [...filters.types] } : {}),
            ...(filters.q ? { q: filters.q } : {}),
            ...(pageParam === null ? {} : { after: pageParam }),
          },
        },
        signal,
      });
      if (error) throw error;
      return data;
    },
    initialPageParam: null as number | null,
    getNextPageParam: (last) => (last.next === null ? null : Number(last.next)),
    staleTime: Infinity,
  });
}

// Pages follow one another: each starts where the last one ended.
async function everyPage<T>(
  get: (cursor: number | undefined) => Promise<{ items: T[]; next?: number | string | null }>,
): Promise<T[]> {
  const out: T[] = [];
  let cursor: number | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    // oxlint-disable-next-line no-await-in-loop
    const { items, next } = await get(cursor);
    out.push(...items);
    if (next === null || next === undefined) break;
    cursor = Number(next);
  }
  return out;
}

// The resource timeline, every window: the scrubber spans the whole run.
export function samplesQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "samples", runId] as const,
    queryFn: ({ signal }) =>
      everyPage(async (from) => {
        const { data, error } = await client.GET("/api/runs/{run_id}/samples", {
          params: {
            ...path(runId),
            query: { limit: MAX_EVENTS, ...(from === undefined ? {} : { ts_from: from }) },
          },
          signal,
        });
        if (error) throw error;
        return data;
      }),
    staleTime: Infinity,
  });
}

function everyEvent(runId: string, types: string[], signal: AbortSignal): Promise<Event[]> {
  return everyPage(async (after) => {
    const { data, error } = await client.GET("/api/runs/{run_id}/events", {
      params: {
        ...path(runId),
        query: { types, limit: MAX_EVENTS, ...(after === undefined ? {} : { after }) },
      },
      signal,
    });
    if (error) throw error;
    return data;
  });
}

// What a self-test run recorded of each case that ended, in order.
export function selftestCasesQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "selftest", runId] as const,
    queryFn: ({ signal }) => everyEvent(runId, ["selftest.case"], signal),
    staleTime: Infinity,
  });
}

// Where each sandbox's boot clock sits on the wall clock.
export function sandboxesQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "sandboxes", runId] as const,
    queryFn: ({ signal }) => everyEvent(runId, ["sandbox.started"], signal),
    staleTime: Infinity,
  });
}

// A run's sweeps: the review ranges a monitor covered. `source` is the run that monitored.
export function sweepsQuery(source: string) {
  return queryOptions({
    queryKey: ["runs", "sweeps", source] as const,
    queryFn: ({ signal }) => everyEvent(source, ["monitor.sweep"], signal),
    staleTime: Infinity,
  });
}

export function flagsQuery(source: string) {
  return queryOptions({
    queryKey: ["runs", "flags", source] as const,
    queryFn: ({ signal }) =>
      everyPage(async (after) => {
        const { data, error } = await client.GET("/api/runs/{run_id}/flags", {
          params: {
            ...path(source),
            query: { limit: MAX_EVENTS, ...(after === undefined ? {} : { after }) },
          },
          signal,
        });
        if (error) throw error;
        return data;
      }),
    staleTime: Infinity,
  });
}

export function eventQuery(runId: string, seq: number) {
  return queryOptions({
    queryKey: ["runs", "event", runId, seq] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/events/{seq}", {
        params: { path: { run_id: runId, seq } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
  });
}

// Blobs never change: their name is their hash.
export function blobQuery(runId: string, blob: BlobRef) {
  return queryOptions({
    queryKey: ["blob", runId, blob.sha256] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/blobs/{sha256}", {
        params: { path: { run_id: runId, sha256: blob.sha256 } },
        parseAs: "text",
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  });
}

export function isBlob(v: unknown): v is BlobRef {
  return (
    typeof v === "object" &&
    v !== null &&
    "sha256" in v &&
    "size" in v &&
    typeof (v as BlobRef).sha256 === "string"
  );
}

export function byType<T extends string>(events: readonly Event[], type: T): Event[] {
  return events.filter((e) => e.type === type);
}
