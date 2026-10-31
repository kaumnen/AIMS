import { ChevronRight } from "lucide-react";

import { Input } from "@/components/ui/input";
import { type BudgetDraft, inForce, KEYS, LABELS, type Row, type RunCaps } from "@/lib/budget";
import { cn } from "@/lib/utils";

// The caps of a new run or fork, folded under its summary: each cell keeps the cap in force
// (its placeholder) when empty, sets it to a value, or lifts it with "none".
export function BudgetSection({
  caps,
  draft,
  problems,
  summary,
  note,
  onChange,
}: {
  caps: RunCaps | undefined;
  draft: BudgetDraft;
  problems: Record<string, string>;
  summary: string;
  note?: string | null;
  onChange: (draft: BudgetDraft) => void;
}) {
  const agents = Object.keys(caps?.agents ?? {});
  const several = agents.length > 1;
  const rows: { id: string; label: string; row: Row; set: (row: Row) => void }[] = [
    {
      id: "every",
      label: several ? "Every agent" : "Agent",
      row: draft.every,
      set: (row) => onChange({ ...draft, every: row }),
    },
    ...(several
      ? agents.map((a) => ({
          id: `agent:${a}`,
          label: a,
          row: draft.agents[a] ?? {},
          set: (row: Row) => onChange({ ...draft, agents: { ...draft.agents, [a]: row } }),
        }))
      : []),
    { id: "run", label: "Run", row: draft.run, set: (row) => onChange({ ...draft, run: row }) },
  ];
  const shown = Object.entries(problems);
  return (
    <details className="group sm:col-span-2" open={shown.length > 0 || undefined}>
      <summary className="flex cursor-default list-none items-center gap-2">
        <ChevronRight
          aria-hidden
          className="text-muted-foreground size-3.5 shrink-0 group-open:rotate-90"
        />
        <span className="text-muted-foreground">Budget</span>
        <span className="truncate">{summary}</span>
      </summary>
      <div className="mt-2 flex flex-col gap-2">
        {note && <p className="text-muted-foreground">{note}</p>}
        <div
          aria-hidden
          className="text-muted-foreground hidden grid-cols-[6rem_repeat(4,minmax(0,1fr))] gap-2 sm:grid"
        >
          <span />
          {KEYS.map((k) => (
            <span key={k}>{LABELS[k]}</span>
          ))}
        </div>
        {rows.map(({ id, label, row, set }) => (
          <div
            key={id}
            role="group"
            aria-label={label}
            className="grid grid-cols-2 gap-2 sm:grid-cols-[6rem_repeat(4,minmax(0,1fr))] sm:items-center"
          >
            <span className="col-span-2 truncate font-medium sm:col-span-1 sm:font-normal">
              {label}
            </span>
            {KEYS.map((k) => {
              const problem = problems[`${id}.${k}`];
              return (
                <label key={k} className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-muted-foreground sm:sr-only">{LABELS[k]}</span>
                  <Input
                    value={row[k] ?? ""}
                    placeholder={inForce(caps, id, k)}
                    aria-label={`${label} ${LABELS[k].toLowerCase()}`}
                    aria-invalid={problem ? true : undefined}
                    className={cn("h-7 font-mono md:text-sm", problem && "border-flag")}
                    onChange={(e) => set({ ...row, [k]: e.currentTarget.value })}
                  />
                </label>
              );
            })}
          </div>
        ))}
        {shown.map(([where, text]) => (
          <p key={where} className="text-flag">
            {problemWhere(where, several)}: {text}
          </p>
        ))}
        <p className="text-muted-foreground">
          Empty keeps the cap shown, none lifts it. Wall takes seconds, 30m or 2h.
        </p>
      </div>
    </details>
  );
}

function problemWhere(where: string, several: boolean): string {
  const [row = "", key = ""] = where.split(/\.(?=[^.]+$)/);
  const cap = LABELS[key as keyof typeof LABELS] ?? key;
  if (row === "run") return `Run ${cap.toLowerCase()}`;
  if (row === "every") return several ? `Every agent ${cap.toLowerCase()}` : cap;
  return `${row.slice("agent:".length)} ${cap.toLowerCase()}`;
}
