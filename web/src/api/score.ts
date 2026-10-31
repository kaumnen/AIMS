import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Event } from "./events";

export type Label = Schemas["Label-Output"];
export type LabelKind = Label["label"];
export type ScoreEstimate = Schemas["ScoreEstimate"];
export type StartScore = Schemas["StartScore"];

const MAX_SCORES = 1000;

// What the Score tab reads of a score run; its model calls stay in Raw events.
export const scoreTypes = [
  "run.started",
  "plant.triggered",
  "score.unit",
  "score.answer",
  "score.awareness",
  "score.unexplained",
  "run.finished",
];

const path = (runId: string) => ({ path: { run_id: runId } });

// The scores of a run, queued ones too, newest first. The live stream keeps them current.
export function scoresQuery(origin: string) {
  return queryOptions({
    queryKey: ["runs", "scores", origin] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs", {
        params: { query: { kind: "score", origin, limit: MAX_SCORES } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
  });
}

// A sealed score never changes; `status` in the key reads it again once it seals.
export function scoreQuery(scoreId: string, status: string) {
  return queryOptions({
    queryKey: ["runs", "score", scoreId, status] as const,
    queryFn: async ({ signal }): Promise<Event[]> => {
      const { data, error } = await client.GET("/api/runs/{run_id}/events", {
        params: { ...path(scoreId), query: { types: scoreTypes, limit: MAX_SCORES } },
        signal,
      });
      if (error) throw error;
      return data.items;
    },
    staleTime: Infinity,
  });
}

// The latest label of each labelled flag of the run that holds the flags.
export function labelsQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "labels", runId] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/labels", {
        params: path(runId),
        signal,
      });
      if (error) throw error;
      return new Map(data.map((l) => [l.flag, l]));
    },
    staleTime: Infinity,
  });
}

export function scoreEstimateQuery(runId: string, awareness: boolean, answer: boolean) {
  return queryOptions({
    queryKey: ["runs", "score-estimate", runId, awareness, answer] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/score/estimate", {
        params: { ...path(runId), query: { awareness, answer } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 60_000,
  });
}

export async function startScore(runId: string, body: StartScore): Promise<string[]> {
  const { data, error } = await client.POST("/api/runs/{run_id}/score", {
    params: path(runId),
    body,
  });
  if (error) throw error;
  return data.run_ids;
}

export async function postLabel(runId: string, flag: number, label: LabelKind): Promise<Label> {
  const { data, error } = await client.POST("/api/runs/{run_id}/labels", {
    params: path(runId),
    body: { flag, label },
  });
  if (error) throw error;
  return data;
}
