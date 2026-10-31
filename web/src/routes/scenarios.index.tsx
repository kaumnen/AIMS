import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { type ScenarioInfo, scenariosQuery, sourceLabel, sourcesQuery } from "@/api/scenarios";
import { useOperator } from "@/components/run/controls";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/scenarios/")({
  component: Scenarios,
});

function Scenarios() {
  const list = useQuery(scenariosQuery);
  const sources = useQuery(sourcesQuery);
  const operator = useOperator();
  if (list.isPending) return <p className="text-muted-foreground">Loading scenarios</p>;
  if (list.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Scenarios did not load.
        <Button variant="link" size="sm" onClick={() => void list.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const groups = new Map<string, ScenarioInfo[]>();
  for (const s of list.data) groups.set(s.source, [...(groups.get(s.source) ?? []), s]);
  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <h1 className="sr-only">Scenarios</h1>
      {groups.size === 0 && (
        <p className="text-muted-foreground rounded-sm border px-3 py-6">
          No scenarios yet. A scenario is a folder with a scenario.yaml under a scenario source.
        </p>
      )}
      {[...groups].map(([source, items]) => (
        <section key={source} aria-label={sourceLabel(sources.data, source)}>
          <h2 className="mb-1 flex items-baseline gap-2">
            <span className="font-medium">{sourceLabel(sources.data, source)}</span>
            {!(sources.data?.find((x) => x.id === source)?.writable ?? items[0]?.writable) && (
              <span className="text-muted-foreground">read only</span>
            )}
          </h2>
          <table className="w-full">
            <thead className="text-muted-foreground text-left">
              <tr className="border-b">
                <th className="py-1 pr-4 font-normal">Scenario</th>
                <th className="py-1 pr-4 font-normal">Oversight</th>
                <th className="py-1 pr-4 font-normal max-sm:hidden">Agents</th>
                <th className="py-1 pr-4 font-normal max-sm:hidden">Profiles</th>
                <th className="py-1 pr-4 font-normal max-md:hidden">Variants</th>
                {operator && <th className="py-1 font-normal max-md:hidden">Self-test</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <Row key={s.name} s={s} operator={operator} />
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}

function Row({ s, operator }: { s: ScenarioInfo; operator: boolean }) {
  return (
    <tr className="hover:bg-muted/50 relative border-b last:border-0">
      <td className="py-1.5 pr-4 align-top">
        <Link
          to="/scenarios/$source/$name"
          params={{ source: s.source, name: s.name }}
          className="font-medium after:absolute after:inset-0"
        >
          {s.name}
        </Link>
        {s.title && s.title !== s.name && (
          <span className="text-muted-foreground ml-2">{s.title}</span>
        )}
        {s.error && <p className="text-muted-foreground">Does not load: {s.error}</p>}
      </td>
      <td className="py-1.5 pr-4 align-top">{s.oversight ?? ""}</td>
      <td className="py-1.5 pr-4 align-top tabular-nums max-sm:hidden">{s.agents ?? ""}</td>
      <td className="py-1.5 pr-4 align-top max-sm:hidden">{s.profiles.join(", ")}</td>
      <td className="py-1.5 pr-4 align-top max-md:hidden">{s.variants.join(", ")}</td>
      {operator && <td className="py-1.5 align-top max-md:hidden">{s.selftest ? "yes" : ""}</td>}
    </tr>
  );
}
