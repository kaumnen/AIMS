import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type SeriesInfo = Schemas["SeriesInfo"];

// Under "runs", so run updates from the stream reach it too.
export function seriesQuery(name: string) {
  return queryOptions({
    queryKey: ["runs", "series", name] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/series/{name}", {
        params: { path: { name } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 5_000,
  });
}
