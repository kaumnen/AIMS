import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";

import type { StepInfo } from "@/api/run";
import { cn } from "@/lib/utils";

import { failed, toolLine } from "./tool-text";

const ROW = 28;

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

export function StepList({
  steps,
  selected,
  multi,
  onSelect,
  onNearEnd,
}: {
  steps: readonly StepInfo[];
  selected: number | undefined;
  multi: boolean;
  onSelect: (seq: number) => void;
  onNearEnd: () => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  // The compiler leaves this component alone; rows get plain values, so they still memoize.
  // oxlint-disable-next-line react/incompatible-library
  const v = useVirtualizer({
    count: steps.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW,
    overscan: 16,
    getItemKey: (i) => steps[i]?.seq ?? i,
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
    <div
      ref={scroller}
      className="min-h-0 overflow-auto rounded-sm border max-md:max-h-64 md:w-96 md:shrink-0"
    >
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
          if (step === undefined) return null;
          return (
            <StepRow
              key={item.key}
              step={step}
              top={item.start}
              active={step.seq === selected}
              multi={multi}
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
  onSelect,
}: {
  step: StepInfo;
  top: number;
  active: boolean;
  multi: boolean;
  onSelect: (seq: number) => void;
}) {
  const tools = step.tools ?? [];
  const flagged = mark(step);
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
      {flagged && (
        <span className={cn("shrink-0", bad ? "text-flag" : "text-muted-foreground")}>
          {flagged}
        </span>
      )}
    </div>
  );
}
