import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type ScenarioInfo = Schemas["ScenarioInfo"];
export type ScenarioDetail = Schemas["ScenarioDetail"];
export type SourceInfo = Schemas["SourceInfo"];

export const scenariosQuery = queryOptions({
  queryKey: ["scenarios"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/scenarios", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 60_000,
});

export const sourcesQuery = queryOptions({
  queryKey: ["scenarios", "sources"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/scenarios/sources", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 60_000,
});

export function scenarioQuery(source: string, name: string) {
  return queryOptions({
    queryKey: ["scenarios", "detail", source, name] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/scenarios/{source}/{name}", {
        params: { path: { source, name } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 10_000,
  });
}

// "this project" reads as a place; the list heads each group with it.
export function sourceLabel(sources: readonly SourceInfo[] | undefined, id: string): string {
  const label = sources?.find((s) => s.id === id)?.label ?? id;
  return label.charAt(0).toUpperCase() + label.slice(1);
}
