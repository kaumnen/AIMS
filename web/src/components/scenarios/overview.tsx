import { useQuery } from "@tanstack/react-query";

import { runEstimateQuery } from "@/api/launch";
import type { ScenarioDetail } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { capsText, overrideKeys, reflow } from "@/lib/scenario";
import { cn } from "@/lib/utils";

type Agent = ScenarioDetail["agents"][number];

function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string | undefined;
  children: React.ReactNode;
}) {
  return (
    <section aria-label={title} className="flex flex-col gap-1">
      <h2 className="flex items-baseline gap-2">
        <span className="font-medium">{title}</span>
        {note && <span className="text-muted-foreground">{note}</span>}
      </h2>
      {children}
    </section>
  );
}

function Head({ cells }: { cells: (string | false)[] }) {
  return (
    <thead className="text-muted-foreground text-left">
      <tr className="border-b">
        {cells.map(
          (c, i) =>
            c !== false && (
              <th key={c} className={cn("py-1 font-normal", i < cells.length - 1 && "pr-4")}>
                {c}
              </th>
            ),
        )}
      </tr>
    </thead>
  );
}

const td = "py-1.5 pr-4 align-top";

// What the scenario sets up, read from its file. Operators also see what it hides: roles,
// scripts, prompt overrides, plants and the self-test.
export function Overview({ detail }: { detail: ScenarioDetail }) {
  if (detail.errors.length > 0) {
    return (
      <Section title="scenario.yaml does not load">
        <ul className="flex flex-col gap-0.5">
          {detail.errors.map((e) => (
            <li key={`${e.line}:${e.msg}`}>
              {e.line != null && <span className="text-muted-foreground">line {e.line}: </span>}
              {e.msg}
            </li>
          ))}
        </ul>
      </Section>
    );
  }
  const truth = detail.truth;
  return (
    <div className="grid gap-x-10 gap-y-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="flex min-w-0 flex-col gap-6">
        <Section title="Task" note={detail.environment ?? undefined}>
          <p className="max-w-prose whitespace-pre-wrap">{reflow(detail.instruction ?? "")}</p>
        </Section>
        <Section title="Agents">
          <div className="overflow-x-auto">
            <table className="w-full">
              <Head
                cells={["Agent", truth && "Role", "Profile", "Tools", "Budget", truth && "Prompts"]}
              />
              <tbody>
                {detail.agents.map((a) => (
                  <AgentRow key={a.id} a={a} truth={truth} />
                ))}
              </tbody>
            </table>
          </div>
        </Section>
        {detail.channels.length > 0 && (
          <Section title="Channels">
            <div className="overflow-x-auto">
              <table className="w-full">
                <Head cells={["Channel", "Kind", "Members", "Read only"]} />
                <tbody>
                  {detail.channels.map((c) => (
                    <tr key={c.name} className="border-b last:border-0">
                      <td className={cn(td, "font-mono")}>{c.name}</td>
                      <td className={td}>{c.kind}</td>
                      <td className={td}>{c.members ? c.members.join(", ") : "every agent"}</td>
                      <td className="py-1.5 align-top">{c.read_only.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        )}
        {detail.plants && detail.plants.length > 0 && (
          <Section title="Plants" note="hidden from the agents and the monitor">
            <div className="overflow-x-auto">
              <table className="w-full">
                <Head cells={["Plant", "Kind", "Category", "Taken at", "Agents"]} />
                <tbody>
                  {detail.plants.map((p) => (
                    <tr key={p.name} className="border-b last:border-0">
                      <td className={cn(td, "font-mono")}>{p.name}</td>
                      <td className={td}>
                        {p.kind}
                        {p.canary && <span className="text-muted-foreground"> with canary</span>}
                      </td>
                      <td className={td}>{p.category ?? "unknown"}</td>
                      <td className={td}>{p.taken ?? "unknown"}</td>
                      <td className="py-1.5 align-top">
                        {p.agents ? p.agents.join(", ") : "every subject"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        )}
      </div>
      <aside className="flex flex-col gap-6">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
          <dt className="text-muted-foreground">Oversight</dt>
          <dd>{detail.oversight}</dd>
          {Object.keys(detail.limits).length > 0 && (
            <>
              <dt className="text-muted-foreground">Run limits</dt>
              <dd>{capsText(detail.limits)}</dd>
            </>
          )}
          <dt className="text-muted-foreground">Variants</dt>
          <dd>{detail.variants.length > 0 ? detail.variants.join(", ") : "none"}</dd>
          {truth && (
            <>
              <dt className="text-muted-foreground">Self-test</dt>
              <dd>{selftestText(detail.selftest)}</dd>
            </>
          )}
        </dl>
        <Estimate source={detail.source} name={detail.name} />
      </aside>
    </div>
  );
}

function AgentRow({ a, truth }: { a: Agent; truth: boolean }) {
  const overrides = overrideKeys(a.prompts as Record<string, unknown> | null);
  return (
    <tr className="border-b last:border-0">
      <td className={cn(td, "font-mono")}>{a.id}</td>
      {truth && (
        <td className={td}>
          {a.role}
          {a.scripted && <span className="text-muted-foreground"> scripted</span>}
        </td>
      )}
      <td className={td}>{a.profile}</td>
      <td className={cn(td, "font-mono")}>{a.tools.join(" ")}</td>
      <td className={td}>{capsText(a.budget)}</td>
      {truth && (
        <td className="py-1.5 align-top">
          {overrides.length > 0 ? (
            <span className="font-mono">{overrides.join(" ")}</span>
          ) : (
            <span className="text-muted-foreground">defaults</span>
          )}
        </td>
      )}
    </tr>
  );
}

function selftestText(s: ScenarioDetail["selftest"]): string {
  if (!s) return "none";
  const parts = [
    s.reference && "reference",
    s.exploit && `exploit${s.claims.length ? ` taking ${s.claims.join(", ")}` : ""}`,
    s.impossible.length > 0 && `references of ${s.impossible.join(", ")}`,
  ].filter(Boolean);
  return parts.join("; ");
}

function Estimate({ source, name }: { source: string; name: string }) {
  const estimate = useQuery(runEstimateQuery({ source, name, samples: 1 }));
  return (
    <Section title="Cost of one run" note="config defaults">
      {estimate.isPending ? (
        <p className="text-muted-foreground">Estimating</p>
      ) : estimate.isError ? (
        <p className="text-muted-foreground">
          No estimate: {isProblem(estimate.error) ? problemText(estimate.error) : "it failed."}
        </p>
      ) : (
        <>
          <ul className="flex flex-col gap-0.5">
            {estimate.data.lines.slice(1).map((line) => (
              <li key={line} className={cn(line.startsWith("Total") && "font-medium")}>
                {line}
              </li>
            ))}
          </ul>
          {estimate.data.warnings.map((w) => (
            <p key={w} className="font-medium">
              {w}
            </p>
          ))}
        </>
      )}
    </Section>
  );
}
