import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Event } from "./events";

export type RunDetail = Schemas["RunDetail"];
export type StepInfo = Schemas["StepInfo"];
export type ToolInfo = Schemas["ToolInfo"];
export type ExecActivity = Schemas["ExecActivity"];
export type BlobRef = Schemas["BlobRef"];

const STEP_PAGE = 200;
const EXEC_PAGE = 200;
const RAW_PAGE = 200;
const MAX_EVENTS = 1000;

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
