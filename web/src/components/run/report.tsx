import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { type ComponentType, useEffect, useRef, useState } from "react";

import { reportMarkdown, reportQuery, type SectionId } from "@/api/report";
import { type RunDetail, stepsQuery } from "@/api/run";
import { ended, reviewsQuery } from "@/api/runs";
import { isProblem } from "@/api/session";
import { Button } from "@/components/ui/button";
import { fullTime } from "@/lib/format";
import { printSeq } from "@/lib/print";

import { agentsOf } from "./controls";
import { HeaderSection, HeadlineSection, SummarySection, WarningsSection } from "./report/head";
import { FindingsSection, FlagsSection, ObservationsSection, PlantsSection } from "./report/judge";
import { type SectionProps, visible } from "./report/parts";
import { AppendixSection, FactsSection, SourcesSection, TimelineSection } from "./report/record";
import type { StepsLink } from "./score";

// One component per section the server writes. A section the server adds fails the type check.
export const SECTIONS: Record<SectionId, ComponentType<SectionProps>> = {
  header: HeaderSection,
  headline: HeadlineSection,
  warnings: WarningsSection,
  summary: SummarySection,
  findings: FindingsSection,
  observations: ObservationsSection,
  plants: PlantsSection,
  flags: FlagsSection,
  timeline: TimelineSection,
  facts: FactsSection,
  sources: SourcesSection,
  appendix: AppendixSection,
};

// The run's report, built by the server from its log, as `aims report` writes it.
export default function ReportTab({
  runId,
  detail,
  source,
  onSource,
  toSteps,
}: {
  runId: string;
  detail: RunDetail;
  source: string | undefined;
  // undefined goes back to the source the server picks
  onSource: (id: string | undefined) => void;
  toSteps: (to: StepsLink) => void;
}) {
  const open = !ended(detail.run);
  const q = useQuery(reportQuery(runId, source, open));
  // The stream keeps the reviews current: once the review the report is built from ends there,
  // the report is built once more, not at the next refresh.
  const going = q.data?.sources.find((s) => s.chosen && s.review && s.status === "running");
  const reviews = useQuery({ ...reviewsQuery(runId), enabled: going !== undefined });
  const review = reviews.data?.items.find((r) => r.run_id === going?.run_id);
  const reviewEnded = review !== undefined && review.status !== "running";
  const { refetch } = q;
  useEffect(() => {
    if (reviewEnded) void refetch();
  }, [reviewEnded, refetch]);
  // Every step, so evidence names its step. The Steps tab shares these pages.
  const steps = useInfiniteQuery(stepsQuery(runId));
  const more = steps.hasNextPage;
  const fetching = steps.isFetchingNextPage;
  const failed = steps.isFetchNextPageError;
  useEffect(() => {
    if (more && !fetching && !failed) void steps.fetchNextPage();
  }, [more, fetching, failed, steps]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);
  // Print opens every fold and names the page as `aims report --html` does, so a saved PDF has
  // the run in its name. Both go back after.
  const article = useRef<HTMLElement>(null);
  const header = q.data?.header;
  const title = header && `Run report: ${visible(header.scenario)}, run ${header.run_id}`;
  useEffect(() => {
    if (title === undefined) return;
    let opened: HTMLDetailsElement[] = [];
    let before: string | undefined;
    const start = () => {
      opened = [
        ...(article.current?.querySelectorAll<HTMLDetailsElement>("details:not([open])") ?? []),
      ];
      for (const d of opened) d.open = true;
      before ??= document.title;
      document.title = title;
    };
    const end = () => {
      for (const d of opened) d.open = false;
      opened = [];
      if (before !== undefined) document.title = before;
      before = undefined;
    };
    window.addEventListener("beforeprint", start);
    window.addEventListener("afterprint", end);
    return () => {
      window.removeEventListener("beforeprint", start);
      window.removeEventListener("afterprint", end);
      end();
    };
  }, [title]);

  if (q.isPending) return <p className="text-muted-foreground">Building the report</p>;
  // A failed build again keeps the report on screen; the toolbar says it failed.
  if (q.data === undefined) {
    const problem = isProblem(q.error) ? q.error : undefined;
    return (
      <div className="text-muted-foreground flex flex-wrap items-center gap-1">
        <p>
          {problem?.status === 404 && problem.detail
            ? `No report: ${problem.detail}.`
            : "The report did not load."}
        </p>
        <Button variant="link" size="sm" onClick={() => void q.refetch()}>
          Retry
        </Button>
        {source !== undefined && (
          <Button variant="link" size="sm" onClick={() => onSource(undefined)}>
            Build from the default source
          </Button>
        )}
      </div>
    );
  }
  const report = q.data;
  const pins = report.header.pins;
  const props: SectionProps = {
    report,
    runId,
    detail,
    steps: steps.data?.pages.flatMap((p) => p.items) ?? [],
    more,
    multi: agentsOf(detail).length > 1,
    onSeq: (seq) => toSteps({ seq, source: pins.source }),
    onFlag: (seq, holder = pins.source) => toSteps({ flag: seq, source: holder }),
    onSource,
    print: (seq, run = runId) => printSeq(pins.base_url, run, seq),
  };
  const download = async () => {
    setSaving(true);
    setSaveError(false);
    try {
      const url = URL.createObjectURL(await reportMarkdown(runId, pins.source));
      const a = document.createElement("a");
      a.href = url;
      a.download = `aims-report-${runId}.md`;
      a.click();
      // some browsers start the download after click returns
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="min-h-0 flex-1 overflow-auto print:overflow-visible">
      <article
        ref={article}
        data-report
        aria-label="Run report"
        className="flex max-w-5xl flex-col gap-6 px-1 pt-2 pb-8"
      >
        <div className="flex flex-wrap items-center gap-2 print:hidden">
          <Button variant="outline" size="sm" disabled={saving} onClick={() => void download()}>
            Download Markdown
          </Button>
          <Button variant="outline" size="sm" onClick={() => window.print()}>
            Print or save as PDF
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={q.isFetching}
            onClick={() => void q.refetch()}
          >
            Build again
          </Button>
          <span className="text-muted-foreground tabular-nums">
            {q.isFetching ? "Building" : `Built ${fullTime(pins.built_at)}`}
          </span>
          {q.isError && !q.isFetching && (
            <span className="text-muted-foreground">The last build failed.</span>
          )}
          {saveError && <span className="text-flag">The Markdown did not download.</span>}
        </div>
        {report.sections.map((id) => {
          const Part = SECTIONS[id] as ComponentType<SectionProps> | undefined;
          return Part ? (
            <div key={id} data-section={id} className="contents">
              <Part {...props} />
            </div>
          ) : null;
        })}
      </article>
    </div>
  );
}
