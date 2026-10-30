import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect } from "react";

import { type RunDetail, runQuery, stepsQuery } from "@/api/run";
import { isProblem } from "@/api/session";
import { RunHeader } from "@/components/run/header";
import { RAW_SEARCH_ID, RawEvents, type RawSearch } from "@/components/run/raw";
import { StepList } from "@/components/run/step-list";
import { Transcript } from "@/components/run/transcript";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { shortId } from "@/lib/format";

interface RunSearch extends RawSearch {
  tab?: "raw";
  step?: number;
}

const int = (v: unknown) => {
  const n = Number(v);
  return v !== undefined && v !== "" && Number.isInteger(n) && n >= 0 ? n : undefined;
};

function parseSearch(raw: Record<string, unknown>): RunSearch {
  const out: RunSearch = {};
  if (raw["tab"] === "raw") out.tab = "raw";
  const step = int(raw["step"]);
  if (step !== undefined) out.step = step;
  const seq = int(raw["seq"]);
  if (seq !== undefined) out.seq = seq;
  if (typeof raw["q"] === "string" && raw["q"].trim()) out.q = raw["q"].trim();
  if (typeof raw["group"] === "string" && raw["group"]) out.group = raw["group"];
  return out;
}

export const Route = createFileRoute("/runs/$runId")({
  staticData: { crumb: (p) => shortId(p["runId"] ?? "") },
  validateSearch: parseSearch,
  loader: ({ context, params }) => {
    void context.queryClient.prefetchQuery(runQuery(params.runId));
  },
  component: Run,
});

// A key typed into a field is text, not a shortcut.
function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return (
    e.metaKey ||
    e.ctrlKey ||
    e.altKey ||
    (t !== null && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)))
  );
}

function Run() {
  const { runId } = Route.useParams();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const detail = useQuery(runQuery(runId));
  const change = (update: (prev: RunSearch) => RunSearch) =>
    void navigate({ search: update, replace: true });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || typing(e)) return;
      e.preventDefault();
      void navigate({ search: (prev) => ({ ...prev, tab: "raw" }), replace: true }).then(() =>
        document.getElementById(RAW_SEARCH_ID)?.focus(),
      );
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  if (detail.isPending) return <p className="text-muted-foreground">Loading run</p>;
  if (detail.isError) {
    const missing = isProblem(detail.error) && detail.error.status === 404;
    return (
      <p className="text-muted-foreground flex items-center gap-1">
        {missing ? `No run ${runId}.` : "The run did not load."}
        {missing ? (
          <Button variant="link" size="sm" render={<Link to="/runs" />}>
            Back to runs
          </Button>
        ) : (
          <Button variant="link" size="sm" onClick={() => void detail.refetch()}>
            Retry
          </Button>
        )}
      </p>
    );
  }
  const tab = search.tab ?? "steps";
  return (
    <div className="flex h-full flex-col">
      <RunHeader detail={detail.data} />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          change((prev) => {
            const { tab: _, ...rest } = prev;
            return v === "raw" ? { ...rest, tab: "raw" } : rest;
          })
        }
        className="min-h-0 flex-1"
      >
        <TabsList variant="line" className="border-b">
          <TabsTrigger value="steps">Steps</TabsTrigger>
          <TabsTrigger value="raw">Raw events</TabsTrigger>
        </TabsList>
        <TabsContent value="steps" className="flex min-h-0 flex-col pt-1">
          <Steps runId={runId} detail={detail.data} selected={search.step} onChange={change} />
        </TabsContent>
        <TabsContent value="raw" className="flex min-h-0 flex-col pt-1">
          <RawEvents runId={runId} search={search} onChange={change} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Steps({
  runId,
  detail,
  selected,
  onChange,
}: {
  runId: string;
  detail: RunDetail;
  selected: number | undefined;
  onChange: (update: (prev: RunSearch) => RunSearch) => void;
}) {
  const q = useInfiniteQuery(stepsQuery(runId));
  const steps = q.data?.pages.flatMap((p) => p.items) ?? [];
  const at = Math.max(
    0,
    steps.findIndex((s) => s.seq === selected),
  );
  const select = (seq: number) => onChange((prev) => ({ ...prev, step: seq }));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e)) return;
      const move =
        e.key === "j" || e.key === "ArrowDown" ? 1 : e.key === "k" || e.key === "ArrowUp" ? -1 : 0;
      const next = steps[at + move];
      if (move === 0 || next === undefined) return;
      e.preventDefault();
      select(next.seq);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (q.isPending) return <p className="text-muted-foreground">Loading steps</p>;
  if (q.isError) return <p className="text-muted-foreground">Steps did not load.</p>;
  const current = steps[at];
  if (current === undefined) {
    return (
      <p className="text-muted-foreground flex items-center gap-1">
        This run has no agent steps. Its record is under
        <Button
          variant="link"
          size="sm"
          className="px-0"
          onClick={() => onChange((prev) => ({ ...prev, tab: "raw" }))}
        >
          Raw events
        </Button>
      </p>
    );
  }
  const prev = steps.slice(0, at).findLast((s) => s.agent_id === current.agent_id);
  const agents = detail.started?.data["agents"];
  const multi = Array.isArray(agents) && agents.length > 1;
  return (
    <div className="flex min-h-0 flex-1 gap-4 max-md:flex-col">
      <StepList
        steps={steps}
        selected={current.seq}
        multi={multi}
        onSelect={select}
        onNearEnd={() => {
          if (q.hasNextPage && !q.isFetchingNextPage) void q.fetchNextPage();
        }}
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <Transcript
          key={current.seq}
          runId={runId}
          step={current}
          after={prev ? prev.seq_to : -1}
          last={at === steps.length - 1 && !q.hasNextPage}
          multi={multi}
        />
      </div>
    </div>
  );
}
