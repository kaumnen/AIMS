import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { scenarioQuery, sourceLabel, sourcesQuery } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { NewRun } from "@/components/runs/new-run";
import { Overview } from "@/components/scenarios/overview";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/scenarios/$source/$name")({
  staticData: { crumb: (p) => p["name"] ?? "" },
  loader: ({ context, params }) => {
    void context.queryClient.prefetchQuery(scenarioQuery(params.source, params.name));
  },
  component: ScenarioPage,
});

function ScenarioPage() {
  const { source, name } = Route.useParams();
  const detail = useQuery(scenarioQuery(source, name));
  const sources = useQuery(sourcesQuery);
  if (detail.isPending) return <p className="text-muted-foreground">Loading scenario</p>;
  if (detail.isError) {
    const missing = isProblem(detail.error) && detail.error.status === 404;
    return (
      <p className="text-muted-foreground flex items-center">
        {missing ? `No scenario ${name} here.` : "The scenario did not load."}
        <Button variant="link" size="sm" render={<Link to="/scenarios" />}>
          Back to scenarios
        </Button>
      </p>
    );
  }
  const d = detail.data;
  return (
    <div className="flex max-w-6xl flex-col gap-4">
      <header className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div className="min-w-0">
          <h1 className="flex items-baseline gap-2 text-lg font-medium">
            <span className="truncate">{d.name}</span>
            {d.title && d.title !== d.name && (
              <span className="text-muted-foreground text-sm">{d.title}</span>
            )}
          </h1>
          <p className="text-muted-foreground">
            {sourceLabel(sources.data, d.source)}
            {!d.writable && ", read only: duplicate it to edit"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            render={<Link to="/runs" search={{ scenario: d.title ?? d.name }} />}
          >
            Runs
          </Button>
          {d.errors.length === 0 && (
            <NewRun scenario={{ source: d.source, name: d.name }} label="Run" />
          )}
        </div>
      </header>
      <Overview detail={d} />
    </div>
  );
}
