import { useQuery } from "@tanstack/react-query";

import { modelsQuery } from "@/api/launch";
import { CHECK_TEXT, useModelCheck } from "@/components/model-check";
import { Button } from "@/components/ui/button";
import { started } from "@/lib/format";
import { checkProblem, checkState } from "@/lib/launch";
import { cn } from "@/lib/utils";

const NONE = new Map<string, string[]>();

// The profiles a run calls with what each does in it, `uses`, and their checks. While `stale`
// the list may be of other picks, so it cannot be checked. `recorded` names the models a fork
// calls on a profile as its run recorded it, where that is not what this config's profile of the
// name holds, which is what the check tests.
export function ProfileChecks({
  uses,
  stale,
  recorded = NONE,
}: {
  uses: Map<string, string[]>;
  stale: boolean;
  recorded?: Map<string, string[]>;
}) {
  const models = useQuery(modelsQuery);
  const { checking, problem, check } = useModelCheck();
  const known = new Map((models.data?.profiles ?? []).map((p) => [p.name, p]));
  const profiles = [...uses.keys()].flatMap((name) => known.get(name) ?? []);
  if (profiles.length === 0) return null;
  return (
    <section aria-label="Model checks" className="text-muted-foreground flex flex-col gap-0.5">
      <h3 className="text-foreground font-medium">Models</h3>
      <div className={cn("flex flex-col gap-0.5", stale && "opacity-60")}>
        {profiles.map((p) => {
          const busy = checking?.includes(p.name) ?? false;
          const state = busy ? "running" : checkState(p.check);
          const bad = busy ? null : checkProblem(p.check);
          const doing = uses.get(p.name) ?? [];
          const ran = recorded.get(p.name) ?? [];
          return (
            <p key={p.name}>
              <span className={cn(state === "fail" && "text-foreground font-medium")}>
                {p.name}
                {doing.length > 0 && ` (${doing.join(", ")})`}: {CHECK_TEXT[state]}
              </span>
              {p.check && !busy && ` · ${started(p.check.started_at)}`}
              {bad && (
                <span className="block truncate" title={bad}>
                  {bad}
                </span>
              )}
              {ran.length > 0 && (
                <span className="text-foreground block">
                  {ran.every((m) => m === p.model)
                    ? "The fork uses the settings the run recorded; the check uses this config's."
                    : `The fork calls ${ran.join(", ")} as the run recorded it; the check calls ${p.model}.`}
                </span>
              )}
              {p.runners.length === 0 && p.entry !== null && (
                <span className="text-foreground block break-all">
                  No connected runner allows {p.entry}.
                </span>
              )}
            </p>
          );
        })}
      </div>
      <span className="flex items-center gap-2">
        <Button
          variant="outline"
          size="xs"
          disabled={checking !== null || stale}
          onClick={() => void check(profiles.map((p) => p.name))}
        >
          Check
        </Button>
        <span>a few small paid calls per profile</span>
      </span>
      {problem && <span className="text-flag">{problem}</span>}
    </section>
  );
}
