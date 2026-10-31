import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";

export type CommandInfo = Schemas["CommandInfo"];

// Fetched again when the run's summary counts a new command or a new answer.
export function commandsQuery(runId: string) {
  return queryOptions({
    queryKey: ["runs", "commands", runId] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/runs/{run_id}/commands", {
        params: { path: { run_id: runId } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
  });
}
