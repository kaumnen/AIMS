import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Preset, Started } from "./launch";

export type StartFork = Schemas["StartFork"];
export type Oversight = NonNullable<StartFork["oversight"]>;

export const oversights: readonly Oversight[] = ["observe", "autonomous", "human"];

export interface ForkAsk {
  // the last step replayed of a run of one agent, else the cut as agent:N
  step?: number | undefined;
  at?: string | undefined;
  keepOthers: boolean;
  samples: number;
  profile?: string | undefined;
  monitor?: Preset | undefined;
}

// The fields of a fork request that change its cost, as the estimate and the start take them.
export function forkFields(ask: ForkAsk) {
  return {
    samples: ask.samples,
    ...(ask.at !== undefined ? { at: ask.at } : ask.step !== undefined ? { step: ask.step } : {}),
    ...(ask.at !== undefined && ask.keepOthers ? { keep_others: true } : {}),
    ...(ask.profile ? { profile: ask.profile } : {}),
    ...(ask.monitor ? { monitor: ask.monitor } : {}),
  };
}

// The previous estimate stays on screen while a changed field is estimated again.
export function forkEstimateQuery(runId: string, ask: ForkAsk) {
  const query = forkFields(ask);
  return queryOptions({
    queryKey: ["runs", "fork-estimate", runId, query] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/forks/estimate", {
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

export async function startFork(runId: string, body: StartFork): Promise<Started> {
  const { data, error } = await client.POST("/api/runs/{run_id}/forks", {
    params: { path: { run_id: runId } },
    body,
  });
  if (error) throw error;
  return data;
}
