import { useSyncExternalStore } from "react";

import type { Schemas } from "@/api/client";
import { cn } from "@/lib/utils";

import { useLive, useLiveState } from "./live";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";

type RunnerInfo = Schemas["RunnerInfo"];

export function useRunners(): readonly RunnerInfo[] {
  const live = useLive();
  return useSyncExternalStore(live.watch, () => live.runners);
}

export const up = (r: RunnerInfo) => r.connected && !r.stale;

function describe(r: RunnerInfo): string {
  const parts = [r.name];
  if (r.version) parts.push(r.version);
  if (!up(r)) parts.push("offline");
  else parts.push(`${r.free} of ${r.slots} slots free`);
  const tracer = r.tracer?.alive ? "tracer on" : "tracer off";
  parts.push(r.tracer?.detail ? `${tracer} (${r.tracer.detail})` : tracer);
  return parts.join(" · ");
}

// Runner and kernel tracer state from the live stream, so a dead tracer is never a surprise.
export function Health() {
  const state = useLiveState();
  const runners = useRunners();
  if (state === "connecting") return null;
  const live = runners.filter(up);
  const tracer = live.some((r) => r.tracer?.alive);
  let label: string;
  if (state === "retrying") label = "Reconnecting";
  else if (live.length === 0) label = "No runner";
  else label = live.length === 1 ? (live[0]?.name ?? "") : `${live.length} runners`;
  const showTracer = state === "open" && live.length > 0;
  return (
    <Tooltip>
      <TooltipTrigger className="text-muted-foreground focus-visible:ring-ring/50 flex items-center gap-1.5 rounded-sm px-1 outline-none focus-visible:ring-2">
        <span
          aria-hidden
          className={cn(
            "border-foreground size-2 rounded-full border",
            live.length > 0 && state === "open" && "bg-foreground",
          )}
        />
        <span className="max-sm:sr-only">{label}</span>
        {showTracer && (
          <>
            <span aria-hidden className="max-sm:hidden">
              ·
            </span>
            <span className={cn("max-sm:sr-only", !tracer && "text-foreground")}>
              tracer {tracer ? "on" : "off"}
            </span>
          </>
        )}
      </TooltipTrigger>
      <TooltipContent side="bottom" align="end" className="flex-col items-start gap-0.5">
        {runners.length === 0 ? (
          <span>
            No runner has connected. Start one with <code>aims runner</code>.
          </span>
        ) : (
          runners.map((r) => <span key={r.name}>{describe(r)}</span>)
        )}
      </TooltipContent>
    </Tooltip>
  );
}
