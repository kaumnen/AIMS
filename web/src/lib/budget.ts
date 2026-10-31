import type { Schemas } from "@/api/client";

export type BudgetOverride = Schemas["BudgetOverride-Input"];
export type Caps = Schemas["Caps-Input"];
export type RunCaps = Schemas["RunCaps"];
export type CapsInForce = Schemas["CapsInForce"];
export type BudgetChange = Schemas["BudgetChange"];
export type Key = BudgetChange["key"];

export const KEYS: readonly Key[] = ["steps", "output_tokens", "cost_usd", "wall_s"];

export const LABELS: Record<Key, string> = {
  steps: "Steps",
  output_tokens: "Output tokens",
  cost_usd: "Cost",
  wall_s: "Wall",
};

const NAMES: Record<Key, string> = {
  steps: "steps",
  output_tokens: "output tokens",
  cost_usd: "cost",
  wall_s: "wall",
};

// What the operator typed in one row of caps: empty keeps a cap, "none" lifts it, a value sets it.
export type Row = Partial<Record<Key, string>>;

export interface BudgetDraft {
  every: Row;
  agents: Record<string, Row>;
  run: Row;
}

export const EMPTY_DRAFT: BudgetDraft = { every: {}, agents: {}, run: {} };

const WALL = /^(\d+(?:\.\d+)?)\s*(s|m|h)?$/i;
const UNIT: Record<string, number> = { s: 1, m: 60, h: 3600 };

// The cap a cell gives: undefined keeps it, null lifts it; a string says why it does not read.
export function readCell(key: Key, text: string | undefined): number | null | undefined | string {
  const t = (text ?? "").trim();
  if (!t) return undefined;
  if (/^(none|no cap)$/i.test(t)) return null;
  if (key === "wall_s") {
    const m = WALL.exec(t);
    const n = m ? Number(m[1]) * (UNIT[(m[2] ?? "s").toLowerCase()] ?? 1) : NaN;
    return n > 0 ? n : "Write seconds, or 30m, 2h, or none.";
  }
  const n = Number((key === "cost_usd" ? t.replace(/^\$/, "") : t).replaceAll(/[,_]/g, ""));
  if (key === "cost_usd")
    return Number.isFinite(n) && n > 0 ? n : "Write dollars above 0, or none.";
  return Number.isInteger(n) && n >= 1 ? n : "Write a whole number from 1, or none.";
}

function readRow(row: Row, problems: Record<string, string>, where: string): Caps | undefined {
  const out: Caps = {};
  for (const key of KEYS) {
    const value = readCell(key, row[key]);
    if (typeof value === "string") problems[`${where}.${key}`] = value;
    else if (value !== undefined) out[key] = value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

// The override the draft asks for, undefined when it changes nothing, and the cells that do
// not read, by "every.steps", "agent:<id>.steps" or "run.steps".
export function readDraft(draft: BudgetDraft): {
  budget: BudgetOverride | undefined;
  problems: Record<string, string>;
} {
  const problems: Record<string, string> = {};
  const every = readRow(draft.every, problems, "every");
  const agents: Record<string, Caps> = {};
  for (const [id, row] of Object.entries(draft.agents)) {
    const caps = readRow(row, problems, `agent:${id}`);
    if (caps) agents[id] = caps;
  }
  const run = readRow(draft.run, problems, "run");
  const budget: BudgetOverride = {
    ...(every ? { every } : {}),
    ...(Object.keys(agents).length > 0 ? { agents } : {}),
    ...(run ? { run } : {}),
  };
  return { budget: Object.keys(budget).length > 0 ? budget : undefined, problems };
}

function duration(seconds: number): string {
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

// A cap in words: 200 steps reads "200", a cost "$4", a wall "30m", none "no cap".
export function capText(key: Key, value: number | null | undefined): string {
  if (value === null || value === undefined) return "no cap";
  if (key === "cost_usd") return `$${value}`;
  if (key === "wall_s") return duration(value);
  return value.toLocaleString("en-US");
}

// What the draft changes, for the folded section's summary: "steps 200, run cost $10".
export function draftText(budget: BudgetOverride | undefined, several: boolean): string | null {
  if (!budget) return null;
  const parts: string[] = [];
  const add = (who: string, caps: Caps | null | undefined) => {
    for (const key of KEYS) {
      if (caps && key in caps) parts.push(`${who}${NAMES[key]} ${capText(key, caps[key])}`);
    }
  };
  add(several ? "every agent " : "", budget.every);
  for (const [id, caps] of Object.entries(budget.agents ?? {})) add(`${id} `, caps);
  add("run ", budget.run);
  return parts.length > 0 ? parts.join(", ") : null;
}

// The caps a run's override changed, as the CLI prints them:
// "steps 200 (scenario 80), run cost no cap (scenario $10)".
export function changesText(changes: BudgetChange[], several: boolean): string | null {
  if (changes.length === 0) return null;
  return changes
    .map((c) => {
      const who = c.scope === "run" ? "run " : several ? `${c.agent} ` : "";
      return `${who}${NAMES[c.key]} ${capText(c.key, c.value)} (scenario ${capText(c.key, c.scenario)})`;
    })
    .join(", ");
}

// The placeholder of a cell: the cap in force, or for every agent the one they share.
export function inForce(caps: RunCaps | undefined, row: string, key: Key): string {
  if (!caps) return "";
  if (row === "run") return capText(key, caps.run[key]);
  if (row === "every") {
    const values = new Set(Object.values(caps.agents).map((c) => c[key] ?? null));
    if (values.size !== 1) return "varies";
    const [value] = values;
    return capText(key, value);
  }
  const agent = caps.agents[row.slice("agent:".length)];
  return agent ? capText(key, agent[key]) : "";
}
