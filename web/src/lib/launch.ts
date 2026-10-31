import type { Estimate, ProfileCheck } from "@/api/launch";
import type { ScenarioInfo } from "@/api/scenarios";

import { usd } from "./format";

export type CheckState = "ok" | "warn" | "fail" | "running" | "none";

// The worst smoke test decides; a check from before smoke tests were logged has only its status.
export function checkState(check: ProfileCheck | null | undefined): CheckState {
  if (!check) return "none";
  if (check.status === "running") return "running";
  const verdicts = check.checks.map((c) => c.verdict);
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

// Mirrors the server's guard: why the runs need a yes before they start, null when they do not.
export function confirmWhy(e: Estimate): string | null {
  if (!e.confirm) return null;
  const limit = usd(e.confirm_above_usd);
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
