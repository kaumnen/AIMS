import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { modelsQuery, type ProfileInfo } from "@/api/launch";
import { CHECK_TEXT, useModelCheck } from "@/components/model-check";
import { Button } from "@/components/ui/button";
import { started } from "@/lib/format";
import { checkProblem, checkState } from "@/lib/launch";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/settings/models")({
  staticData: { crumb: "Models" },
  component: Models,
});

const running = (profiles: readonly ProfileInfo[]) =>
  profiles.some((p) => p.check?.status === "running");

function Models() {
  const models = useQuery({
    ...modelsQuery,
    refetchInterval: (q) => (q.state.data && running(q.state.data.profiles) ? 3000 : false),
  });
  const operator = useOperator();
  const { checking, problem, check } = useModelCheck();
  if (models.isPending) return <p className="text-muted-foreground">Loading profiles</p>;
  if (models.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Profiles did not load.
        <Button variant="link" size="sm" onClick={() => void models.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const { profiles } = models.data;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="font-medium">Model profiles</h2>
        <span className="text-muted-foreground">
          {operator
            ? "Set in the config files; a check makes a few small paid calls per profile."
            : "Set in the config files."}
        </span>
        {operator && profiles.length > 0 && (
          <Button
            variant="outline"
            size="xs"
            className="ml-auto"
            disabled={checking !== null}
            onClick={() => void check(profiles.map((p) => p.name))}
          >
            Check all
          </Button>
        )}
      </div>
      {problem && <p className="text-flag">{problem}</p>}
      <table className="w-full">
        <thead className="text-muted-foreground text-left">
          <tr className="border-b">
            <th className="py-1 pr-4 font-normal">Profile</th>
            <th className="py-1 pr-4 font-normal max-sm:hidden">Model</th>
            <th className="py-1 pr-4 font-normal max-md:hidden">Keys</th>
            <th className="py-1 pr-4 font-normal whitespace-nowrap">Last check</th>
            {operator && <th className="py-1 font-normal" aria-label="Actions" />}
          </tr>
        </thead>
        <tbody>
          {profiles.map((p) => (
            <Row
              key={p.name}
              p={p}
              checking={checking?.includes(p.name) ?? false}
              busy={checking !== null}
              operator={operator}
              onCheck={() => void check([p.name])}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Row({
  p,
  checking,
  busy,
  operator,
  onCheck,
}: {
  p: ProfileInfo;
  checking: boolean;
  busy: boolean;
  operator: boolean;
  onCheck: () => void;
}) {
  const state = checking ? "running" : checkState(p.check);
  const bad = checking ? null : checkProblem(p.check);
  return (
    <tr className="border-b align-top last:border-0">
      <td className="py-1.5 pr-4">
        <span className="font-medium">{p.name}</span>
        {p.roles.length > 0 && (
          <span className="text-muted-foreground ml-2">{p.roles.join(", ")}</span>
        )}
        <span className="text-muted-foreground block sm:hidden">{p.model}</span>
      </td>
      <td className="py-1.5 pr-4 max-sm:hidden">
        <span className="font-mono">{p.model}</span>
        <span className="text-muted-foreground block">
          {p.backend} · weights {p.weights} · reasoning {p.reasoning}
        </span>
      </td>
      <td className="py-1.5 pr-4 max-md:hidden">
        {p.envs.length === 0 ? (
          <span className="text-muted-foreground">none needed</span>
        ) : (
          <>
            <span className="font-mono">{p.envs.join(", ")}</span>
            <span className={cn("block", p.runners.length > 0 && "text-muted-foreground")}>
              {p.runners.length > 0
                ? `set on ${p.runners.join(", ")}`
                : "no connected runner has them"}
            </span>
          </>
        )}
      </td>
      <td className="py-1.5 pr-4">
        <span className={cn("sm:whitespace-nowrap", state === "fail" && "font-medium")}>
          {CHECK_TEXT[state]}
        </span>
        {p.check && !checking && (
          <Link
            to="/runs/$runId"
            params={{ runId: p.check.run_id }}
            className="text-muted-foreground hover:text-foreground block sm:whitespace-nowrap"
          >
            {started(p.check.started_at)}
          </Link>
        )}
        {bad && (
          <span className="text-muted-foreground block max-w-md truncate" title={bad}>
            {bad}
          </span>
        )}
      </td>
      {operator && (
        <td className="py-1.5 text-right">
          <Button
            variant="ghost"
            size="xs"
            aria-label={`Check ${p.name}`}
            disabled={busy}
            onClick={onCheck}
          >
            Check
          </Button>
        </td>
      )}
    </tr>
  );
}
