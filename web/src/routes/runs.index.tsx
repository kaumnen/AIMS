import { useInfiniteQuery, useQueries } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import {
  casesQuery,
  filtersOf,
  parseRunsSearch,
  reviewsQuery,
  runsQuery,
  type RunsSearch,
} from "@/api/runs";
import { rememberSearch } from "@/components/crumbs";
import { clear, RunFilterBar } from "@/components/runs/filters";
import { NewRun } from "@/components/runs/new-run";
import { type Row, RunsTable } from "@/components/runs/table";
import { Button } from "@/components/ui/button";
import { useNow } from "@/lib/use-now";

export const Route = createFileRoute("/runs/")({
  validateSearch: parseRunsSearch,
  component: Runs,
});

function Runs() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const filters = filtersOf(search);
  useEffect(() => rememberSearch("/runs", { ...search }), [search]);
  const list = useInfiniteQuery(runsQuery(filters));
  // agent runs hold their reviews, self-tests their cases
  const nest = filters.kind === "scenario" || filters.kind === "selftest";
  const children = filters.kind === "selftest" ? casesQuery : reviewsQuery;
  const what = filters.kind === "selftest" ? "cases" : "reviews";
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const open = nest ? [...expanded] : [];
  const reviews = useQueries({ queries: open.map((id) => children(id)) });

  const runs = list.data?.pages.flatMap((p) => p.items) ?? [];
  const rows: Row[] = [];
  for (const run of runs) {
    rows.push({ kind: "run", run, child: false });
    const at = open.indexOf(run.run_id);
    if (at < 0) continue;
    const q = reviews[at];
    if (q === undefined || q.isPending) {
      rows.push({ kind: "note", key: `${run.run_id}:note`, text: `Loading ${what}` });
    } else if (q.isError) {
      rows.push({ kind: "note", key: `${run.run_id}:note`, text: `The ${what} did not load` });
    } else {
      for (const r of q.data.items) rows.push({ kind: "run", run: r, child: true });
    }
  }
  const now = useNow(rows.some((r) => r.kind === "run" && r.run.status === "running"));

  const toggle = (runId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (!next.delete(runId)) next.add(runId);
      return next;
    });
  const nearEnd = () => {
    if (list.hasNextPage && !list.isFetchingNextPage) void list.fetchNextPage();
  };
  const change = (update: (prev: RunsSearch) => RunsSearch) =>
    void navigate({ search: update, replace: true });

  const count = list.isSuccess
    ? `${runs.length}${list.hasNextPage ? "+" : ""} ${runs.length === 1 ? "run" : "runs"}`
    : "";
  return (
    <div className="flex h-full flex-col">
      <h1 className="sr-only">Runs</h1>
      <RunFilterBar search={search} onChange={change} count={count}>
        <NewRun />
      </RunFilterBar>
      {list.isError ? (
        <Message>
          Runs did not load.{" "}
          <Button variant="link" size="sm" onClick={() => void list.refetch()}>
            Retry
          </Button>
        </Message>
      ) : list.isSuccess && runs.length === 0 ? (
        <Message>
          {Object.keys(search).some((k) => k !== "kind") ? (
            <>
              No runs match these filters.{" "}
              <Button variant="link" size="sm" onClick={() => change(clear)}>
                Clear filters
              </Button>
            </>
          ) : (
            <>
              No runs yet. Start one with <code className="font-mono">aims run</code>.
            </>
          )}
        </Message>
      ) : (
        <RunsTable
          rows={rows}
          now={now}
          expanded={expanded}
          onToggle={toggle}
          onNearEnd={nearEnd}
          footer={
            list.isFetchingNextPage && (
              <p className="text-muted-foreground px-2 py-1.5">Loading more runs</p>
            )
          }
        />
      )}
    </div>
  );
}

function Message({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-muted-foreground flex items-center rounded-sm border px-3 py-6">
      {children}
    </p>
  );
}
