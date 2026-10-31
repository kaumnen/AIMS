import { client } from "./client";

// Queue the scenario's self-test; it answers with the run that records it.
export async function startSelftest(source: string, name: string): Promise<string> {
  const { data, error } = await client.POST("/api/scenarios/{source}/{name}/selftest", {
    params: { path: { source, name } },
    body: {},
  });
  if (error) throw error;
  const runId = data.run_ids[0];
  if (!runId) throw new Error("no run was queued");
  return runId;
}
