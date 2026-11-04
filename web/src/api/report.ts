import { queryOptions } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import { runReportSectionsValues } from "./schema.gen";

// The server sends every field of a report, defaults too; the schema marks those optional.
type Full<T> = T extends readonly (infer U)[]
  ? Full<U>[]
  : T extends object
    ? { [K in keyof T]-?: Full<T[K]> }
    : T;

export type RunReport = Full<Schemas["RunReport"]>;
export type SectionId = RunReport["sections"][number];
export type ReportFlag = Full<Schemas["Flag"]>;
export type ReportWarning = Full<Schemas["Warning"]>;
export type ReportPlant = Full<Schemas["Plant"]>;
export type ReportSource = Full<Schemas["Source"]>;
export type Moment = Full<Schemas["Moment"]>;
export type AgentText = Full<Schemas["AgentText"]>;

// Every section of a report, in the order the server writes them.
export const reportSections: readonly SectionId[] = runReportSectionsValues;

// Kinds the server writes no report for: a report is about the run they look at.
export const unreported: ReadonlySet<string> = new Set([
  "monitor",
  "score",
  "models.check",
  "selftest",
]);

// Each build hashes the whole log, so an open run's report is built again only this often.
export const REPORT_REFRESH_MS = 15_000;

// The report of a run from `source`, else from the monitor the server picks. `open` is in the
// key so the report is built once more when the run ends. While the run or the review it is
// built from is still going, it is built again every REPORT_REFRESH_MS; else only on demand.
export function reportQuery(runId: string, source: string | undefined, open: boolean) {
  const going = (r: RunReport | undefined) =>
    open || (r?.sources.some((s) => s.chosen && s.status === "running") ?? false);
  return queryOptions({
    queryKey: ["runs", "report", runId, source ?? null, open] as const,
    queryFn: async ({ signal }): Promise<RunReport> => {
      const { data, error } = await client.GET("/api/runs/{run_id}/report", {
        params: { path: { run_id: runId }, query: source ? { source } : {} },
        signal,
      });
      if (error) throw error;
      return data as RunReport;
    },
    refetchInterval: (q) => (going(q.state.data) ? REPORT_REFRESH_MS : false),
    staleTime: (q) => (going(q.state.data) ? REPORT_REFRESH_MS : Infinity),
    refetchOnWindowFocus: false,
  });
}

// The same report as the Markdown file `aims report` writes.
export async function reportMarkdown(runId: string, source: string): Promise<Blob> {
  const { data, error } = await client.GET("/api/runs/{run_id}/report", {
    params: { path: { run_id: runId }, query: { source, format: "md" } },
    parseAs: "blob",
  });
  if (error) throw error;
  return data;
}
