import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { patchPrompt, type PromptKey, promptsQuery } from "@/api/scenario-files";
import type { ScenarioDetail } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";

type Agent = ScenarioDetail["agents"][number];

interface Row {
  key: PromptKey;
  label: string;
  text: string;
  // where the text comes from: this scenario, or the config layer below it
  source: string;
}

const SOURCES: Record<string, string> = {
  default: "built in",
  user: "this machine",
  project: "this project",
};

function override(agent: Agent, key: PromptKey): string | undefined {
  let node: unknown = agent.prompts ?? {};
  for (const part of key) {
    if (!node || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

// Every text the agent reads from the harness, and where it comes from. An override written
// here goes into this scenario's file, under the agent; the config files hold the rest.
export function PromptsTab({ detail }: { detail: ScenarioDetail }) {
  const prompts = useQuery(promptsQuery);
  const [agentId, setAgentId] = useState(detail.agents[0]?.id ?? "");
  const agent = detail.agents.find((a) => a.id === agentId) ?? detail.agents[0];
  if (prompts.isPending) return <p className="text-muted-foreground">Loading texts</p>;
  if (prompts.isError || !agent) {
    return <p className="text-muted-foreground">The texts did not load.</p>;
  }
  const row = (key: PromptKey, label: string, layer: { text: string; source: string }): Row => {
    const own = override(agent, key);
    return own === undefined
      ? { key, label, text: layer.text, source: SOURCES[layer.source] ?? layer.source }
      : { key, label, text: own, source: "this scenario" };
  };
  const agentRows = Object.entries(prompts.data.agent).map(([k, t]) => row([k], k, t));
  const toolRows = agent.tools.flatMap((tool) =>
    Object.entries(prompts.data.tools[tool] ?? {}).map(([k, t]) =>
      row(["tools", tool, k], `${tool}.${k}`, t),
    ),
  );
  return (
    <div className="flex max-w-5xl flex-col gap-5">
      <div className="flex flex-wrap items-center gap-3">
        {detail.agents.length > 1 && (
          <ToggleGroup
            aria-label="Agent"
            value={[agent.id]}
            onValueChange={(v: string[]) => v[0] && setAgentId(v[0])}
            variant="outline"
            size="sm"
          >
            {detail.agents.map((a) => (
              <ToggleGroupItem key={a.id} value={a.id} className="font-mono">
                {a.id}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        )}
        <p className="text-muted-foreground">
          {detail.writable
            ? "An override is written into scenario.yaml under this agent and applies to this scenario only."
            : "Read only: duplicate the scenario to change its texts."}
        </p>
      </div>
      <TextTable title="Agent texts" rows={agentRows} detail={detail} agent={agent.id} />
      <TextTable title="Tool texts" rows={toolRows} detail={detail} agent={agent.id} />
    </div>
  );
}

function TextTable({
  title,
  rows,
  detail,
  agent,
}: {
  title: string;
  rows: Row[];
  detail: ScenarioDetail;
  agent: string;
}) {
  return (
    <section aria-label={title} className="flex flex-col gap-1">
      <h2 className="font-medium">{title}</h2>
      <ul className="flex flex-col">
        {rows.map((r) => (
          <TextRow key={r.key.join(".")} row={r} detail={detail} agent={agent} />
        ))}
      </ul>
    </section>
  );
}

function TextRow({ row, detail, agent }: { row: Row; detail: ScenarioDetail; agent: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const overridden = row.source === "this scenario";

  const write = async (value: string | null) => {
    setBusy(true);
    setNote(null);
    try {
      const saved = await patchPrompt(
        detail.source,
        detail.name,
        agent,
        row.key,
        value,
        detail.etag,
      );
      setDraft(null);
      setNote(saved.warnings.length ? saved.warnings.map((w) => w.msg).join(" ") : null);
      void queryClient.invalidateQueries({
        queryKey: ["scenarios", "detail", detail.source, detail.name],
      });
      void queryClient.invalidateQueries({
        queryKey: ["scenarios", "file", detail.source, detail.name],
      });
    } catch (e) {
      if (isProblem(e) && e.status === 412) {
        setNote("scenario.yaml changed since this page loaded; it is loaded again, try again.");
        void queryClient.invalidateQueries({
          queryKey: ["scenarios", "detail", detail.source, detail.name],
        });
      } else {
        setNote(isProblem(e) ? problemText(e) : "The request failed.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="grid gap-x-4 border-b py-1.5 last:border-0 md:grid-cols-[14rem_8rem_minmax(0,1fr)]">
      <span className="font-mono">{row.label}</span>
      <span className={cn(overridden ? "font-medium" : "text-muted-foreground")}>{row.source}</span>
      {draft === null ? (
        <div className="flex min-w-0 items-start gap-2">
          <p className="line-clamp-3 min-w-0 flex-1 whitespace-pre-wrap">
            {row.text || <span className="text-muted-foreground">empty</span>}
          </p>
          {detail.writable && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(row.text)}>
              Edit
            </Button>
          )}
        </div>
      ) : (
        <form
          className="flex min-w-0 flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void write(draft);
          }}
        >
          <Textarea
            aria-label={`Text of ${row.label}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={Math.min(14, Math.max(3, draft.split("\n").length + 1))}
            className="font-mono"
            autoFocus
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={busy || draft === row.text}>
              Save override
            </Button>
            {overridden && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => void write(null)}
              >
                Use the config's text
              </Button>
            )}
            <Button type="button" variant="ghost" size="sm" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {note && <p className="text-muted-foreground md:col-start-3">{note}</p>}
    </li>
  );
}
