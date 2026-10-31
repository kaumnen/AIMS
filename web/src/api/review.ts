import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Started } from "./launch";

export type StartMonitor = Schemas["StartMonitor"];
export type ReviewEstimate = Schemas["ReviewEstimate"];
export type ReviewPreset = NonNullable<StartMonitor["preset"]>;
export type ReviewMode = ReviewEstimate["mode"];

export const reviewPresets: readonly ReviewPreset[] = ["signals", "standard", "research"];

export interface ReviewAsk {
  preset?: ReviewPreset | undefined;
  mode: ReviewMode;
  profile?: string | undefined;
  budget?: number | undefined;
  samples: number;
}

// The fields of a review request, as the estimate and the start both take them.
export function reviewFields(ask: ReviewAsk) {
  return {
    samples: ask.samples,
    ...(ask.mode === "signals_only" ? { signals_only: true } : {}),
    ...(ask.mode === "judge_only" ? { judge_only: true } : {}),
    ...(ask.preset && ask.mode !== "signals_only" ? { preset: ask.preset } : {}),
    ...(ask.profile && ask.mode !== "signals_only" ? { profile: ask.profile } : {}),
    ...(ask.budget !== undefined && ask.mode !== "signals_only" ? { budget_usd: ask.budget } : {}),
  };
}

// The previous estimate stays on screen while a changed field is estimated again.
export function reviewEstimateQuery(runId: string, ask: ReviewAsk) {
  const query = reviewFields(ask);
  return queryOptions({
    queryKey: ["runs", "review-estimate", runId, query] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/monitor/estimate", {
        params: { path: { run_id: runId }, query },
        signal,
      });
      if (error) throw error;
      return data;
    },
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

export async function startReview(runId: string, body: StartMonitor): Promise<Started> {
  const { data, error } = await client.POST("/api/runs/{run_id}/monitor", {
    params: { path: { run_id: runId } },
    body,
  });
  if (error) throw error;
  return data;
}
