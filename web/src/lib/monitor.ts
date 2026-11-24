import type { FlagInfo, StepInfo } from "@/api/run";
import type { RunSummary } from "@/api/runs";

import { stepOf } from "./timeline";

export type FlagStatus = FlagInfo["status"];
export type FlagLayer = FlagInfo["layer"];

// Reviewed findings first; the ones a review set aside last.
export const statuses: FlagStatus[] = [
  "confirmed",
  "open",
  "unclear",
  "tainted",
  "cleared",
  "rejected",
];

export const statusText: Record<FlagStatus, string> = {
  confirmed: "Confirmed",
  open: "Open",
  unclear: "Unclear",
  tainted: "Tainted",
  cleared: "Cleared",
  rejected: "Rejected",
};

export const layers: { value: FlagLayer; label: string }[] = [
  { value: "signal", label: "Signals" },
  { value: "sweep", label: "Sweeps" },
  { value: "judge", label: "Judge" },
];

export const categoryText = (c: string) => c.replaceAll("_", " ");

export const aside = (f: Pick<FlagInfo, "status">) =>
  f.status === "cleared" || f.status === "rejected";

// Red only where the rubric says misbehaviour is likely (3 and up), and only while no review
// has set the flag aside.
export function flagTone(f: Pick<FlagInfo, "importance" | "status">): {
  red: boolean;
  hollow: boolean;
} {
  const off = aside(f);
  return { red: f.importance >= 3 && !off, hollow: off };
}

// The first of `evidence` from the first step of `agent` on, where the agent acts. What came
// before, such as the task it was given, counts only when nothing later is cited.
export function firstCited(
  evidence: readonly number[],
  agent: string | null | undefined,
  steps: readonly StepInfo[],
): number | undefined {
  if (evidence.length === 0) return undefined;
  const start = (agent ? steps.find((s) => s.agent_id === agent) : undefined) ?? steps[0];
  const acted = start === undefined ? [] : evidence.filter((s) => s >= start.seq_from);
  return Math.min(...(acted.length > 0 ? acted : evidence));
}

// The event a flag points at first.
export const cited = (f: FlagInfo, steps: readonly StepInfo[]): number =>
  firstCited(f.evidence, f.agent_id, steps) ?? f.seq;

// Whether the loaded steps reach `seq`, so the step holding it is known.
export function reaches(steps: readonly StepInfo[], seq: number, more: boolean): boolean {
  return !more || (steps.at(-1)?.seq_to ?? -1) >= seq;
}

// The step of the flag's agent that holds the event it cites.
export function flagStep(f: FlagInfo, steps: readonly StepInfo[]): StepInfo | undefined {
  const own = f.agent_id === null ? [] : steps.filter((s) => s.agent_id === f.agent_id);
  const pool = own.length > 0 ? own : steps;
  return pool.length > 0 ? stepOf(pool, cited(f, steps)) : undefined;
}

export interface FlagFilters {
  layer?: FlagLayer | undefined;
  category?: string | undefined;
}

export function filterFlags(flags: readonly FlagInfo[], f: FlagFilters): FlagInfo[] {
  return flags.filter(
    (x) =>
      (f.layer === undefined || x.layer === f.layer) &&
      (f.category === undefined || x.category === f.category),
  );
}

// Flags by status in `statuses` order, highest importance first, then by the event they cite.
export function groupFlags(
  flags: readonly FlagInfo[],
  steps: readonly StepInfo[],
): [FlagStatus, FlagInfo[]][] {
  const at = new Map(flags.map((f) => [f.seq, cited(f, steps)]));
  const where = (f: FlagInfo) => at.get(f.seq) ?? f.seq;
  const out: [FlagStatus, FlagInfo[]][] = [];
  for (const status of statuses) {
    const own = flags
      .filter((f) => f.status === status)
      .toSorted((a, b) => b.importance - a.importance || where(a) - where(b) || a.seq - b.seq);
    if (own.length > 0) out.push([status, own]);
  }
  return out;
}

const modeText: Record<string, string> = {
  signals_only: "signals only",
  judge_only: "judge only",
};

// "standard", "standard, judge only": the monitor a run had or a review ran.
export function presetText(run: Pick<RunSummary, "preset" | "monitor_mode">): string | null {
  const parts = [run.preset, run.monitor_mode ? (modeText[run.monitor_mode] ?? null) : null].filter(
    Boolean,
  );
  return parts.length > 0 ? parts.join(", ") : null;
}
