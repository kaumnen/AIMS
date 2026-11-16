import type { Estimate, ProfileCheck } from "@/api/launch";
import type { ScenarioInfo } from "@/api/scenarios";

import { usd } from "./format";

export type CheckState = "ok" | "warn" | "fail" | "running" | "none" | "cut";

// The worst smoke test decides; a check from before smoke tests were logged has only its status.
// A check stopped before it reached the profile says nothing of the profile.
export function checkState(check: ProfileCheck | null | undefined): CheckState {
  if (!check) return "none";
  if (check.status === "running") return "running";
  const verdicts = check.checks.map((c) => c.verdict);
  const stopped = check.status === "cancelled" || check.status === "killed";
  if (verdicts.length === 0 && stopped) return "cut";
  if (verdicts.includes("fail") || (verdicts.length === 0 && check.status !== "completed")) {
    return "fail";
  }
  return verdicts.includes("warn") ? "warn" : "ok";
}

export function checkProblem(check: ProfileCheck | null | undefined): string | null {
  const bad =
    check?.checks.find((c) => c.verdict === "fail") ??
    check?.checks.find((c) => c.verdict === "warn");
  return bad ? `${bad.name}: ${bad.detail}` : null;
}

const MONITOR_USES: Record<string, string> = { channels: "channel sweep" };

// What each profile the estimated runs call does in them, in order: the agents' first, then the
// monitor's, across every variant. Without an estimate, the agents' `fallback` profiles.
export function profileUses(
  e: Pick<Estimate, "parts"> | undefined,
  fallback: readonly string[],
): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const add = (name: string, use: string) => {
    const uses = out.get(name) ?? [];
    if (use && !uses.includes(use)) uses.push(use);
    out.set(name, uses);
  };
  if (e === undefined) {
    for (const name of fallback) add(name, "");
    return out;
  }
  const agents = new Set(e.parts.flatMap((p) => Object.keys(p.agent_profiles)));
  for (const p of e.parts) {
    for (const [id, name] of Object.entries(p.agent_profiles)) {
      add(name, agents.size > 1 ? `agent ${id}` : "agent");
    }
  }
  for (const p of e.parts) {
    for (const [part, name] of Object.entries(p.monitor_profiles)) {
      add(name, MONITOR_USES[part] ?? part.replace(":", " "));
    }
  }
  return out;
}

// Mirrors the server's guard: why runs or reviews need a yes before they start, null when they
// do not.
export function confirmWhy(
  e: Pick<Estimate, "confirm" | "confirm_above_usd" | "total">,
): string | null {
  if (!e.confirm) return null;
  const limit = usd(e.confirm_above_usd);
  if (e.total.ceiling_usd == null && e.total.high_usd != null) {
    return `No cap bounds the cost. The high estimate is ${usd(e.total.high_usd)}.`;
  }
  if (e.total.high_usd != null) {
    return `The high estimate ${usd(e.total.high_usd)} is above the ${limit} limit.`;
  }
  if (e.total.ceiling_usd != null) {
    return `The caps allow ${usd(e.total.ceiling_usd)}, above the ${limit} limit.`;
  }
  return "No cap bounds the cost and no past runs give an estimate.";
}

export interface Note {
  loud: boolean;
  text: string;
}

export function oversightNote(
  oversight: ScenarioInfo["oversight"],
  preset: string | undefined,
): Note | null {
  if (preset === "off") return { loud: true, text: "No monitor: nothing watches the agents." };
  if (oversight === "observe") {
    return {
      loud: true,
      text: "Observe oversight: the monitor only records what it would do. Nothing stops the agents.",
    };
  }
  if (oversight === "autonomous") {
    return {
      loud: true,
      text: "Autonomous oversight: the monitor pauses, denies and kills on its own, without asking.",
    };
  }
  if (oversight === "human") {
    return {
      loud: false,
      text: "Human oversight: when the monitor pauses an agent, the run waits for you.",
    };
  }
  return null;
}
