import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type ScenarioInfo = Schemas["ScenarioInfo"];

export const scenariosQuery = queryOptions({
  queryKey: ["scenarios"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/scenarios", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 60_000,
});
