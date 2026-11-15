import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import type { BudgetOverride } from "@/lib/budget";

import { client, type Schemas } from "./client";

export type StartRun = Schemas["StartRun"];
export type Estimate = Schemas["Estimate"];
export type ProfileInfo = Schemas["ProfileInfo"];
export type ProfileCheck = Schemas["ProfileCheck"];
export type ProfileDraft = Schemas["ProfileDraft"];
export type ModelPrices = Schemas["ModelPrices"];
export type PriceRow = Schemas["PriceRow"];
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

// What LiteLLM's price map says of the profiles that set no price, and its rows whose key holds
// `search`. The server reads the map only for such a profile or a search, and keeps its copy a
// day.
export function modelPricesQuery(search = "") {
  return queryOptions({
    queryKey: ["models", "prices", search] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/models/prices", {
        params: { query: search ? { search } : {} },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });
}

export interface EstimateAsk {
  source: string;
  name: string;
  variant?: string | undefined;
  samples: number;
  profile?: string | undefined;
  monitor?: Preset | undefined;
  budget?: BudgetOverride | undefined;
}

// The previous estimate stays on screen while a changed field is estimated again.
export function runEstimateQuery(ask: EstimateAsk) {
  const { source, name, variant, samples, profile, monitor } = ask;
  const budget = ask.budget ? JSON.stringify(ask.budget) : undefined;
  return queryOptions({
    queryKey: ["estimate", source, name, variant, samples, profile, monitor, budget] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/scenarios/{source}/{name}/estimate", {
        params: {
          path: { source, name },
          query: {
            samples,
            ...(variant ? { variant } : {}),
            ...(profile ? { profile } : {}),
            ...(monitor ? { monitor } : {}),
            ...(budget ? { budget } : {}),
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

// A profile for OpenRouter model `slug`, as config file text. The server reads OpenRouter's list
// when its copy is old and saves nothing. An empty `name` leaves the name to the server.
export async function lookupModel(
  slug: string,
  name: string,
  alias: boolean,
): Promise<ProfileDraft> {
  const { data, error } = await client.GET("/api/models/lookup", {
    params: {
      query: { slug, source: "openrouter", ...(name ? { name } : {}), ...(alias ? { alias } : {}) },
    },
  });
  if (error) throw error;
  return data;
}

// Add the profile a lookup with the same arguments shows to the project's config file. Returns
// the file's path.
export async function addModel(slug: string, name: string, alias: boolean): Promise<string> {
  const { data, error } = await client.POST("/api/models/add", {
    body: { slug, source: "openrouter", ...(name ? { name } : {}), ...(alias ? { alias } : {}) },
  });
  if (error) throw error;
  return data.path;
}
