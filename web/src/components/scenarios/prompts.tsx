import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  patchPrompt,
  PROMPT_SOURCES,
  type PromptKey,
  promptsQuery,
  type PromptText,
} from "@/api/scenario-files";
import { type ScenarioDetail, scenarioQuery } from "@/api/scenarios";
import { type TextFile, TextRow } from "@/components/text-row";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

type Agent = ScenarioDetail["agents"][number];

interface Row {
  key: PromptKey;
  label: string;
  text: string;
  // where the text comes from: this scenario, or the config layer below it
  source: string;
  // the config's text, which applies without an override
  inherited: string;
}

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
  const row = (key: PromptKey, label: string, layer: PromptText): Row => {
    const own = override(agent, key);
    const inherited = layer.text;
    return own === undefined
      ? { key, label, text: inherited, source: PROMPT_SOURCES[layer.source], inherited }
      : { key, label, text: own, source: "this scenario", inherited };
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
          <ScenarioText key={r.key.join(".")} row={r} detail={detail} agent={agent} />
        ))}
      </ul>
    </section>
  );
}

function ScenarioText({ row, detail, agent }: { row: Row; detail: ScenarioDetail; agent: string }) {
  const queryClient = useQueryClient();
  const scenario = scenarioQuery(detail.source, detail.name);
  // the file as it is now: a save just made may not have reached the page yet
  const fresh = () => queryClient.fetchQuery({ ...scenario, staleTime: 0 });

  const file: TextFile = {
    begin: async () => {
      const now = await fresh();
      const own = now.agents.find((a) => a.id === agent);
      return { value: (own && override(own, row.key)) ?? row.inherited, base: now.etag };
    },
    save: async (value, base) => {
      const saved = await patchPrompt(
        detail.source,
        detail.name,
        agent,
        row.key,
        value,
        base ?? detail.etag,
      );
      void queryClient.invalidateQueries({ queryKey: scenario.queryKey });
      void queryClient.invalidateQueries({
        queryKey: ["scenarios", "file", detail.source, detail.name],
      });
      return saved.warnings.length ? saved.warnings.map((w) => w.msg).join(" ") : null;
    },
    latest: async () => (await fresh()).etag,
    reload: () => void queryClient.invalidateQueries({ queryKey: scenario.queryKey }),
  };

  const overridden = row.source === "this scenario";
  return (
    <TextRow
      label={row.label}
      source={row.source}
      own={overridden}
      text={row.text}
      editable={detail.writable}
      file={file}
      saveLabel="Save override"
      resetLabel={overridden ? "Use the config's text" : undefined}
    />
  );
}
