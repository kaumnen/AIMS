import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";

import type { StepInfo } from "@/api/run";
import { cn } from "@/lib/utils";

import { failed, toolLine } from "./tool-text";

const ROW = 28;
const NO_PENDING: readonly { agent: string | null; n: number }[] = [];

function mark(step: StepInfo): string | null {
  const tools = step.tools ?? [];
  if (tools.some((t) => t.denied)) return "denied";
  if (tools.some((t) => t.error)) return "error";
  const exit = tools.find((t) => (t.exit_code ?? 0) !== 0)?.exit_code;
  if (exit !== undefined && exit !== null) return `exit ${exit}`;
  if (step.status !== "completed") return step.status;
  return null;
}

export function stepLine(step: StepInfo): string {
  const first = step.tools?.[0];
  return first ? toolLine(first) : "no tool call";
}

// How many of the shown flags cite a step, and whether one of them reads red.
export interface StepFlags {
  count: number;
  red: boolean;
}

// `harm` holds the steps where a score says an agent first took a plant.
export function StepList({
  steps,
  selected,
  multi,
  flags,
  harm,
  pending = NO_PENDING,
  onSelect,
  onNearEnd,
  className,
}: {
  steps: readonly StepInfo[];
  selected: number | undefined;
  multi: boolean;
  flags: ReadonlyMap<number, StepFlags>;
  harm?: ReadonlySet<number>;
  // steps whose model is still writing them
  pending?: readonly { agent: string | null; n: number }[];
  onSelect: (seq: number) => void;
  onNearEnd: () => void;
  className?: string;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  // The compiler leaves this component alone; rows get plain values, so they still memoize.
  // oxlint-disable-next-line react/incompatible-library
  const v = useVirtualizer({
    count: steps.length + pending.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW,
    overscan: 16,
    getItemKey: (i) => steps[i]?.seq ?? `pending-${i}`,
  });
  const items = v.getVirtualItems();
  const last = items.at(-1)?.index ?? -1;
  useEffect(() => {
    if (last >= steps.length - 30) onNearEnd();
  }, [last, steps.length, onNearEnd]);
  const at = steps.findIndex((s) => s.seq === selected);
  useEffect(() => {
    if (at >= 0) v.scrollToIndex(at, { align: "auto" });
  }, [at, v]);

  return (
    <div ref={scroller} className={cn("min-h-0 overflow-auto rounded-sm border", className)}>
      <div
        role="listbox"
        aria-label="Steps"
        tabIndex={0}
        aria-activedescendant={selected === undefined ? undefined : `step-${selected}`}
        className="relative outline-none"
        style={{ height: v.getTotalSize() }}
      >
        {items.map((item) => {
          const step = steps[item.index];
          if (step === undefined) {
            const p = pending[item.index - steps.length];
            return p === undefined ? null : (
              <PendingRow key={item.key} n={p.n} agent={p.agent} top={item.start} multi={multi} />
            );
          }
          const f = flags.get(step.seq);
          return (
            <StepRow
              key={item.key}
              step={step}
              top={item.start}
              active={step.seq === selected}
              multi={multi}
              flagged={f?.count ?? 0}
              red={f?.red ?? false}
              harmed={harm?.has(step.seq) ?? false}
              onSelect={onSelect}
            />
          );
        })}
      </div>
    </div>
  );
}

function StepRow({
  step,
  top,
  active,
  multi,
  flagged,
  red,
  harmed,
  onSelect,
}: {
  step: StepInfo;
  top: number;
  active: boolean;
  multi: boolean;
  flagged: number;
  red: boolean;
  harmed: boolean;
  onSelect: (seq: number) => void;
}) {
  const tools = step.tools ?? [];
  const marked = mark(step);
  const bad = tools.some(failed) || step.status === "error";
  return (
    <div
      role="option"
      id={`step-${step.seq}`}
      aria-selected={active}
      tabIndex={-1}
      onClick={() => onSelect(step.seq)}
      className={cn(
        "absolute inset-x-0 top-0 flex h-7 cursor-default items-center gap-2 px-2",
        active ? "bg-accent" : "hover:bg-accent/60",
      )}
      style={{ transform: `translateY(${top}px)` }}
    >
      <span className="text-muted-foreground w-8 shrink-0 text-right tabular-nums">{step.n}</span>
      {multi && step.agent_id && (
        <span className="text-muted-foreground max-w-20 shrink-0 truncate">{step.agent_id}</span>
      )}
      <span className="min-w-0 flex-1 truncate font-mono text-xs">{stepLine(step)}</span>
      {tools.length > 1 && (
        <span className="text-muted-foreground shrink-0 tabular-nums">+{tools.length - 1}</span>
      )}
      {marked && (
        <span className={cn("shrink-0", bad ? "text-flag" : "text-muted-foreground")}>
          {marked}
        </span>
      )}
      {harmed && (
        <span data-harm className="text-flag shrink-0 font-medium">
          harm
        </span>
      )}
      {flagged > 0 && (
        <span
          role="img"
          aria-label={`${flagged} ${flagged === 1 ? "flag" : "flags"}`}
          className={cn(
            "flex shrink-0 items-center gap-0.5 tabular-nums",
            red ? "text-flag" : "text-foreground",
          )}
        >
          <svg aria-hidden width={8} height={7} viewBox="0 0 8 7" className="fill-current">
            <path d="M0 0H8L4 7Z" />
          </svg>
          {flagged}
        </span>
      )}
    </div>
  );
}

function PendingRow({
  n,
  agent,
  top,
  multi,
}: {
  n: number;
  agent: string | null;
  top: number;
  multi: boolean;
}) {
  return (
    <div
      role="option"
      aria-selected={false}
      aria-disabled
      className="text-muted-foreground absolute inset-x-0 top-0 flex h-7 items-center gap-2 px-2"
      style={{ transform: `translateY(${top}px)` }}
    >
      <span className="w-8 shrink-0 text-right tabular-nums">{n}</span>
      {multi && agent && <span className="max-w-20 shrink-0 truncate">{agent}</span>}
      <span className="min-w-0 flex-1 truncate">writing</span>
    </div>
  );
}
