import { useQuery } from "@tanstack/react-query";

import { $api } from "@/api/client";
import { categories, type Category, type RunKind, type RunsSearch } from "@/api/runs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

const kinds: { value: RunKind | "all"; label: string }[] = [
  { value: "scenario", label: "Agent runs" },
  { value: "monitor", label: "Reviews" },
  { value: "score", label: "Scores" },
  { value: "models.check", label: "Model checks" },
  { value: "all", label: "All" },
];

const ANY = "any";

export const clear = (prev: RunsSearch): RunsSearch =>
  prev.kind === undefined ? {} : { kind: prev.kind };

export function categoryLabel(c: string): string {
  const s = c.replaceAll("_", " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function Choice({
  name,
  label,
  value,
  items,
  onChange,
}: {
  name: string;
  label: string;
  value: string | undefined;
  items: { value: string; label: string }[];
  onChange: (value: string | undefined) => void;
}) {
  const all = [{ value: ANY, label }, ...items];
  return (
    <Select
      items={all}
      value={value ?? ANY}
      onValueChange={(v) => onChange(v === null || v === ANY ? undefined : String(v))}
    >
      <SelectTrigger size="sm" aria-label={name} className="min-w-32">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start">
        {all.map((i) => (
          <SelectItem key={i.value} value={i.value}>
            {i.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// Applied on Enter or leaving the field; Clear resets it through the key.
function Text({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
}) {
  const commit = (raw: string) => {
    const v = raw.trim() || undefined;
    if (v !== value) onChange(v);
  };
  return (
    <Input
      key={value ?? ""}
      aria-label={label}
      placeholder={label}
      defaultValue={value ?? ""}
      className="h-7 w-28 text-sm md:text-sm"
      onBlur={(e) => commit(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit(e.currentTarget.value);
      }}
    />
  );
}

export function RunFilterBar({
  search,
  onChange,
  count,
}: {
  search: RunsSearch;
  onChange: (update: (prev: RunsSearch) => RunsSearch) => void;
  count: string;
}) {
  const scenarios = useQuery($api.queryOptions("get", "/api/scenarios"));
  const names = new Set((scenarios.data ?? []).map((s) => s.title ?? s.name));
  if (search.scenario) names.add(search.scenario);
  const set = <K extends keyof RunsSearch>(key: K, value: RunsSearch[K] | undefined) =>
    onChange((prev) => {
      const next = { ...prev };
      if (value === undefined) delete next[key];
      else next[key] = value;
      return next;
    });
  const filtered =
    search.scenario ?? search.series ?? search.variant ?? search.severity ?? search.category;
  return (
    <div className="flex flex-wrap items-center gap-2 pb-2">
      <ToggleGroup
        aria-label="Kind"
        size="sm"
        spacing={0}
        variant="outline"
        value={[search.kind ?? "scenario"]}
        onValueChange={(v) => {
          const kind = v[0] as RunKind | "all" | undefined;
          if (kind !== undefined) set("kind", kind === "scenario" ? undefined : kind);
        }}
      >
        {kinds.map((k) => (
          <ToggleGroupItem key={k.value} value={k.value}>
            {k.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <Choice
        name="Scenario"
        label="Any scenario"
        value={search.scenario}
        items={[...names].toSorted().map((n) => ({ value: n, label: n }))}
        onChange={(v) => set("scenario", v)}
      />
      <Choice
        name="Severity"
        label="Any severity"
        value={search.severity?.toString()}
        items={[1, 2, 3, 4, 5].map((n) => ({
          value: String(n),
          label: n === 5 ? "Severity 5" : `Severity ${n}+`,
        }))}
        onChange={(v) => set("severity", v === undefined ? undefined : Number(v))}
      />
      <Choice
        name="Category"
        label="Any category"
        value={search.category}
        items={categories.map((c) => ({ value: c, label: categoryLabel(c) }))}
        onChange={(v) => set("category", v as Category | undefined)}
      />
      <Text label="Series" value={search.series} onChange={(v) => set("series", v)} />
      <Text label="Variant" value={search.variant} onChange={(v) => set("variant", v)} />
      {filtered !== undefined && (
        <Button variant="ghost" size="sm" onClick={() => onChange(clear)}>
          Clear
        </Button>
      )}
      <span className="text-muted-foreground ml-auto tabular-nums">{count}</span>
    </div>
  );
}
