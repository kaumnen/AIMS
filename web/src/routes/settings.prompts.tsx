import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";

import {
  PROMPT_SOURCES,
  type PromptKey,
  type PromptsInfo,
  promptsQuery,
  type PromptText,
} from "@/api/scenario-files";
import { isProblem } from "@/api/session";
import { configQuery, type Layer, layerQuery, patchConfig } from "@/api/settings";
import { problemText } from "@/components/form";
import { type TextFile, TextRow } from "@/components/text-row";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useOperator } from "@/lib/use-operator";

export const Route = createFileRoute("/settings/prompts")({
  staticData: { crumb: "Prompts" },
  component: PromptsPage,
});

interface Row {
  // the key path under the config file's top level
  key: PromptKey;
  label: string;
  text: PromptText;
}

interface Section {
  title: string;
  rows: Row[];
}

function entries(texts: Record<string, PromptText>, key: (k: string) => PromptKey): Row[] {
  return Object.entries(texts).map(([k, text]) => ({ key: key(k), label: k, text }));
}

function sections(info: PromptsInfo): Section[] {
  return [
    { title: "Agent texts", rows: entries(info.agent, (k) => ["prompts", k]) },
    {
      title: "Tool texts",
      rows: Object.entries(info.tools).flatMap(([tool, texts]) =>
        entries(texts, (k) => ["prompts", "tools", tool, k]).map((r) => ({
          ...r,
          label: `${tool}.${r.label}`,
        })),
      ),
    },
    { title: "Monitor texts", rows: entries(info.monitor, (k) => ["prompts", "monitor", k]) },
    {
      title: "Monitor categories",
      rows: entries(info.categories, (k) => ["prompts", "monitor", "categories", k]),
    },
  ];
}

const LAYERS: Record<Layer, string> = { user: "This machine", project: "This project" };

// The text a layer gives, its own or the one it inherits.
function inLayer(text: PromptText, layer: Layer): string {
  return layer === "project"
    ? (text.project ?? text.user ?? text.default)
    : (text.user ?? text.default);
}

function PromptsPage() {
  const operator = useOperator();
  const prompts = useQuery(promptsQuery);
  const config = useQuery({ ...configQuery, enabled: operator });
  const [chosen, setChosen] = useState<Layer | null>(null);
  const [filter, setFilter] = useState("");

  if (prompts.isPending) return <p className="text-muted-foreground">Loading texts</p>;
  if (prompts.isError) {
    return (
      <p className="text-muted-foreground flex flex-wrap items-center">
        The texts did not load
        {isProblem(prompts.error) ? `: ${problemText(prompts.error)}` : "."}
        <Button variant="link" size="sm" onClick={() => void prompts.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const layers = config.data?.layers ?? [];
  const project = layers.find((l) => l.layer === "project");
  const layer = chosen ?? (project?.exists ? "project" : "user");
  const file = layers.find((l) => l.layer === layer);
  const editable = operator && file !== undefined;

  const needle = filter.trim().toLowerCase();
  const shown = sections(prompts.data)
    .map((s) => ({
      ...s,
      rows: needle
        ? s.rows.filter(
            (r) =>
              r.label.toLowerCase().includes(needle) || r.text.text.toLowerCase().includes(needle),
          )
        : s.rows,
    }))
    .filter((s) => s.rows.length > 0);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        {operator ? (
          <div className="flex flex-wrap items-center gap-3">
            {layers.length > 1 && (
              <ToggleGroup
                aria-label="Layer"
                value={[layer]}
                onValueChange={(v: string[]) => v[0] && setChosen(v[0] as Layer)}
                variant="outline"
                size="sm"
              >
                {layers.map((l) => (
                  <ToggleGroupItem key={l.layer} value={l.layer}>
                    {LAYERS[l.layer]}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            )}
            {file && (
              <p className="text-muted-foreground min-w-0 break-all">
                {file.exists ? "Saves to " : "Saving creates "}
                <span className="font-mono">{file.path}</span>
              </p>
            )}
          </div>
        ) : (
          <p className="text-muted-foreground">Read only: operators edit these texts.</p>
        )}
        <p className="text-muted-foreground">
          A project's text wins over this machine's; a scenario can override both for its agents.
        </p>
      </div>
      <Input
        aria-label="Filter texts"
        placeholder="Filter by name or text"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="max-w-xs"
      />
      {shown.length === 0 && <p className="text-muted-foreground">No text matches.</p>}
      {shown.map((s) => (
        <section key={s.title} aria-label={s.title} className="flex flex-col gap-1">
          <h2 className="font-medium">
            {s.title} <span className="text-muted-foreground font-normal">{s.rows.length}</span>
          </h2>
          <ul className="flex flex-col">
            {s.rows.map((r) => (
              <ConfigText
                key={`${layer}:${r.key.join(".")}`}
                row={r}
                layer={layer}
                editable={editable}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function hint(placeholders: string[] | null | undefined): string | null {
  if (placeholders == null) return null;
  const names = placeholders.map((p) => `{${p}}`).join(" ");
  return `${names ? `Placeholders: ${names}.` : "No placeholders."} Write a literal brace as {{ or }}.`;
}

function ConfigText({ row, layer, editable }: { row: Row; layer: Layer; editable: boolean }) {
  const queryClient = useQueryClient();
  const { text } = row;
  const own = layer === "project" ? text.project : text.user;
  const below = layer === "project" && text.user != null ? "this machine's" : "the built-in";
  const shadowed = layer === "user" && text.project != null;
  const placeholders = hint(text.placeholders);

  const file: TextFile = {
    begin: async () => {
      const [current, fresh] = await Promise.all([
        queryClient.fetchQuery(layerQuery(layer)),
        queryClient.fetchQuery({ ...promptsQuery, staleTime: 0 }),
      ]);
      const now = sections(fresh)
        .flatMap((s) => s.rows)
        .find((r) => r.key.join(".") === row.key.join("."));
      return {
        value: inLayer(now?.text ?? text, layer),
        base: current.exists ? current.etag : null,
      };
    },
    save: async (value, base) => {
      const saved = await patchConfig(
        layer,
        value === null ? { unset: [row.key] } : { set: [{ key: row.key, value }] },
        base,
      );
      queryClient.setQueryData(layerQuery(layer).queryKey, saved);
      void queryClient.invalidateQueries({ queryKey: ["prompts"] });
      void queryClient.invalidateQueries({ queryKey: ["config"], exact: true });
      return null;
    },
    latest: async () => {
      const current = await queryClient.fetchQuery(layerQuery(layer));
      return current.exists ? current.etag : null;
    },
    reload: () => {
      void queryClient.invalidateQueries({ queryKey: ["prompts"] });
      void queryClient.invalidateQueries({ queryKey: ["config"] });
    },
  };

  return (
    <TextRow
      label={row.label}
      source={PROMPT_SOURCES[text.source]}
      own={text.source !== "default"}
      text={text.text}
      editable={editable}
      file={file}
      resetLabel={own != null ? `Use ${below} text` : undefined}
      hints={
        <>
          {placeholders && <p className="text-muted-foreground">{placeholders}</p>}
          {shadowed && (
            <p className="text-muted-foreground">
              This project's text wins here; this machine's applies in projects that set none.
            </p>
          )}
        </>
      }
    />
  );
}
