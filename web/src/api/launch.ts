import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type StartRun = Schemas["StartRun"];
export type Estimate = Schemas["Estimate"];
export type ProfileInfo = Schemas["ProfileInfo"];
export type ProfileCheck = Schemas["ProfileCheck"];
export type Preset = NonNullable<StartRun["monitor"]>;
export type Started = Schemas["Started"];

export const presets: readonly Preset[] = ["off", "signals", "standard", "research"];

export const modelsQuery = queryOptions({
  queryKey: ["models"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/models", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 30_000,
});

export interface EstimateAsk {
  source: string;
  name: string;
  variant?: string | undefined;
  samples: number;
  profile?: string | undefined;
  monitor?: Preset | undefined;
}

// The previous estimate stays on screen while a changed field is estimated again.
export function runEstimateQuery(ask: EstimateAsk) {
  const { source, name, variant, samples, profile, monitor } = ask;
  return queryOptions({
    queryKey: ["estimate", source, name, variant, samples, profile, monitor] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/scenarios/{source}/{name}/estimate", {
        params: {
          path: { source, name },
          query: {
            samples,
            ...(variant ? { variant } : {}),
            ...(profile ? { profile } : {}),
            ...(monitor ? { monitor } : {}),
          },
        },
        signal,
      });
      if (error) throw error;
      return data;
    },
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

export async function startRuns(body: StartRun): Promise<Started> {
  const { data, error } = await client.POST("/api/runs", { body });
  if (error) throw error;
  return data;
}

export async function checkModels(profiles: string[]): Promise<string> {
  const { data, error } = await client.POST("/api/models/check", { body: { profiles } });
  if (error) throw error;
  const [id] = data.run_ids;
  if (id === undefined) throw new Error("no check run");
  return id;
}
