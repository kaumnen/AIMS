import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type RunnerInfo = Schemas["RunnerInfo"];
export type ProviderCredit = Schemas["ProviderCredit"];
export type TokenInfo = Schemas["TokenInfo"];
export type TokenRole = Schemas["TokenCreate"]["role"];
export type LayerInfo = Schemas["LayerInfo"];
export type Layer = LayerInfo["layer"];
export type ConfigFile = Schemas["ConfigFile"];
export type ConfigPatch = Schemas["ConfigPatch"];

// Every runner the control plane has seen, connected or not; the live stream updates it.
export const runnersQuery = queryOptions({
  queryKey: ["runners"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/runners", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 30_000,
});

export const spendQuery = queryOptions({
  queryKey: ["spend"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/spend", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 10_000,
});

// Operators only: the list answers 403 to anyone else.
export const tokensQuery = queryOptions({
  queryKey: ["tokens"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/tokens", { signal });
    if (error) throw error;
    return data;
  },
});

// Its value comes back this once.
export async function createToken(name: string, role: TokenRole): Promise<string> {
  const { data, error } = await client.POST("/api/tokens", { body: { name, role } });
  if (error) throw error;
  return data.token;
}

export async function revokeToken(name: string): Promise<void> {
  const { error } = await client.DELETE("/api/tokens/{name}", { params: { path: { name } } });
  if (error) throw error;
}

// The live copy of a runner wins over the listed one; runners only the list knows stay.
export function mergeRunners(
  listed: readonly RunnerInfo[],
  live: readonly RunnerInfo[],
): RunnerInfo[] {
  const byName = new Map(listed.map((r) => [r.name, r]));
  for (const r of live) byName.set(r.name, r);
  return [...byName.values()].toSorted((a, b) => a.name.localeCompare(b.name));
}

// The config files read, lowest layer first, and whether they load together.
export const configQuery = queryOptions({
  queryKey: ["config"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/config", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 10_000,
});

export function layerQuery(layer: Layer) {
  return queryOptions({
    queryKey: ["config", layer] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/config/{layer}", {
        params: { path: { layer } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 0,
  });
}

// Set or unset keys in a layer. `etag` names the version the change is made against, null for
// a file that does not exist yet.
export async function patchConfig(
  layer: Layer,
  change: Partial<ConfigPatch>,
  etag: string | null,
): Promise<ConfigFile> {
  const { data, error } = await client.PATCH("/api/config/{layer}", {
    params: {
      path: { layer },
      header: etag === null ? { "If-None-Match": "*" } : { "If-Match": `"${etag}"` },
    },
    body: { set: [], unset: [], ...change },
  });
  if (error) throw error;
  return data;
}

// Every layer merged, as TOML naming where each value comes from.
export const mergedQuery = queryOptions({
  queryKey: ["config", "merged"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/config/merged", { signal });
    if (error) throw error;
    return data.text;
  },
  staleTime: 0,
});

// Whether the layers would load with `text` in place of `layer`'s file.
export async function checkConfig(layer: Layer, text: string, signal: AbortSignal) {
  const { data, error } = await client.POST("/api/config/{layer}/check", {
    params: { path: { layer } },
    body: { text },
    signal,
  });
  if (error) throw error;
  return data;
}

// Replace a layer's text. `etag` as in patchConfig.
export async function saveConfig(
  layer: Layer,
  text: string,
  etag: string | null,
): Promise<ConfigFile> {
  const { data, error } = await client.PUT("/api/config/{layer}", {
    params: {
      path: { layer },
      header: etag === null ? { "If-None-Match": "*" } : { "If-Match": `"${etag}"` },
    },
    body: { text },
  });
  if (error) throw error;
  return data;
}

// What a change to the config files can change.
export const CONFIG_DEPENDENT = [["config"], ["prompts"], ["models"], ["spend"], ["scenarios"]];
