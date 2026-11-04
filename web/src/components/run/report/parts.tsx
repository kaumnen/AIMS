import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";

import type { RunReport } from "@/api/report";
import type { RunDetail, StepInfo } from "@/api/run";
import { shortId } from "@/lib/format";
import { reaches } from "@/lib/monitor";
import type { PrintSeq } from "@/lib/print";
import { stepOf } from "@/lib/timeline";
import { cn } from "@/lib/utils";

export { StepLinks as Evidence } from "../monitor";

// What every section of the report gets. Seqs are events of the reported run unless a
// prop says otherwise.
export interface SectionProps {
  report: RunReport;
  runId: string;
  detail: RunDetail;
  // every step of the run, loaded page by page; `more` while pages are still to come
  steps: readonly StepInfo[];
  more: boolean;
  multi: boolean;
  // open the Steps tab at an event, under the report's monitor source
  onSeq: (seq: number) => void;
  // open the Steps tab on a flag of the report's monitor source, or of `holder`: `live` or a
  // review's run id
  onFlag: (seq: number, holder?: string) => void;
  // build the report from another monitor source: `live` or a review's run id
  onSource: (id: string) => void;
  // how a link to an event of `run`, the reported run unless given, prints
  print: (seq: number, run?: string) => PrintSeq;
}

type SectionPrint = Pick<SectionProps, "print">;

// Tables show this many rows, the rest folded below, as in the Markdown.
export const TABLE_ROWS = 30;
// Quoted text shows this many characters until someone asks for the rest, as the Markdown cuts.
export const PROSE_CHARS = 2000;

// Words more than one section says, as src/aims/report/texts.py has them.
export const UNKNOWN = "unknown";
export const LABELS_NOTE = "Labels sit beside the log. They are not in the hash chain.";
export const NO_VERDICT: Record<NonNullable<RunReport["headline"]["no_verdict"]>, string> = {
  running: "none yet, the run is in progress",
  no_monitor: "none, the run had no monitor",
  skipped: "skipped",
  missing: "no verdict",
};

// Bidi controls, zero-width and other format chars, control chars but tab and newline, lone
// surrogates and line or paragraph separators, so text cannot hide or reorder what it shows.
const HIDDEN = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/gu;
const point = (c: string) => {
  const n = c.codePointAt(0) ?? 0;
  const hex = n.toString(16).toUpperCase();
  return n > 0xffff ? `\\U${hex.padStart(8, "0")}` : `\\u${hex.padStart(4, "0")}`;
};

// Text from the log as the characters it holds, like escape.visible in the Markdown writer:
// each hidden char as the visible text `\uXXXX`. CR and CRLF become newline first.
export function visible(text: string): string {
  return text
    .replaceAll(/\r\n?/g, "\n")
    .replaceAll(HIDDEN, (c) => (c === "\n" || c === "\t" ? c : point(c)));
}

export function Section({
  title,
  intro,
  children,
  className,
}: {
  title: string;
  intro?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section aria-label={title} className={cn("flex flex-col gap-2", className)}>
      <h2 className="font-medium">{title}</h2>
      {intro && <p className="text-muted-foreground">{intro}</p>}
      {children}
    </section>
  );
}

// The step that holds `seq`, among `agent`'s steps when given. None before the first step.
export function stepFor(
  steps: readonly StepInfo[],
  seq: number,
  more: boolean,
  agent?: string | null,
): StepInfo | undefined {
  if (!reaches(steps, seq, more)) return undefined;
  const own = agent ? steps.filter((s) => s.agent_id === agent) : [];
  const step = stepOf(own.length > 0 ? own : steps, seq);
  return step && step.seq_from <= seq ? step : undefined;
}

// An event of the reported run, named by the step that holds it once that step is loaded.
export function SeqLink({
  seq,
  agent,
  steps,
  more,
  onSeq,
  print,
}: {
  seq: number;
  agent?: string | null;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
} & SectionPrint) {
  const step = stepFor(steps, seq, more, agent);
  return (
    <EventButton title={`Event ${seq}`} onClick={() => onSeq(seq)} print={print(seq)}>
      {step ? `step ${step.n}` : `seq ${seq}`}
    </EventButton>
  );
}

const LINK =
  "hover:text-foreground text-muted-foreground tabular-nums underline underline-offset-2";

// A button that opens an event or a flag. Its words sit in a span so print can swap them.
export function EventButton({
  title,
  onClick,
  print,
  className,
  children,
}: {
  title: string;
  onClick: () => void;
  print: PrintSeq;
  className?: string;
  children: ReactNode;
}) {
  return (
    <button
      {...print}
      type="button"
      title={title}
      onClick={onClick}
      className={cn(LINK, className)}
    >
      <span>{children}</span>
    </button>
  );
}

// An event of another run, such as a review: opens that run at the event.
export function OtherSeq({ run, seq, print }: { run: string; seq: number } & SectionPrint) {
  return (
    <Link
      {...print(seq, run)}
      to="/runs/$runId"
      params={{ runId: run }}
      search={{ tab: "raw", seq }}
      title={`Event ${seq} of run ${run}`}
      className={LINK}
    >
      <span>seq {seq}</span>
    </Link>
  );
}

export function RunLink({ run, className }: { run: string; className?: string }) {
  return (
    <Link
      to="/runs/$runId"
      params={{ runId: run }}
      title={run}
      className={cn(
        "hover:text-foreground font-mono text-xs underline underline-offset-2",
        className,
      )}
    >
      {shortId(run)}
    </Link>
  );
}

// Text an agent or the judge wrote, kept as written. React escapes it, so it can hold anything.
// Past PROSE_CHARS it is cut until someone asks for the rest.
export function Quote({ children, className }: { children: string; className?: string }) {
  const [whole, setWhole] = useState(false);
  // by code point, as Python counts, so a cut never splits an emoji
  const chars = Array.from(children);
  const rest = chars.length - PROSE_CHARS;
  const cut = rest > 0 && !whole;
  return (
    <div className="flex flex-col items-start gap-1">
      <blockquote
        className={cn(
          "border-grid w-full border-l-2 pl-3 break-words whitespace-pre-wrap",
          className,
        )}
      >
        {visible(cut ? chars.slice(0, PROSE_CHARS).join("") : children)}
      </blockquote>
      {cut && (
        <button
          type="button"
          onClick={() => setWhole(true)}
          className="text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          Cut, {count(rest, "more character")}.<span className="print:hidden"> Show all</span>
        </button>
      )}
    </div>
  );
}

// A part folded away until opened. Print opens every fold, see ReportTab.
export function Fold({
  summary,
  children,
  open,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
}) {
  return (
    <details open={open} className="group">
      <summary className="text-muted-foreground flex cursor-default list-none items-center gap-1">
        <ChevronRight aria-hidden className="size-3.5 shrink-0 group-open:rotate-90 print:hidden" />
        {summary}
      </summary>
      <div className="mt-1">{children}</div>
    </details>
  );
}

// A table of the report: the first rows shown, the rest folded below it. A wide table scrolls
// sideways on its own, so a phone keeps the page still; it takes focus so keys scroll it too.
export function ReportTable({
  label,
  head,
  rows,
}: {
  label: string;
  head: readonly string[];
  rows: readonly (readonly ReactNode[])[];
}) {
  const table = (part: readonly (readonly ReactNode[])[], from: number, name: string) => (
    <div
      role="region"
      aria-label={`${name} table`}
      tabIndex={0}
      className="focus-visible:ring-ring/50 overflow-x-auto rounded-sm outline-none focus-visible:ring-2 print:overflow-visible"
    >
      <table aria-label={name} className="w-full text-left">
        <thead className="text-muted-foreground">
          <tr className="border-b">
            {head.map((h) => (
              <th key={h} scope="col" className="py-1 pr-4 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {part.map((row, i) => (
            // rows are fixed for one report, so a row's place is its key
            // oxlint-disable-next-line react/no-array-index-key
            <tr key={from + i} className="border-b last:border-0">
              {row.map((c, j) => (
                // oxlint-disable-next-line react/no-array-index-key
                <td key={j} className="py-1.5 pr-4 align-top">
                  {typeof c === "string" ? visible(c) : c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  const rest = rows.length - TABLE_ROWS;
  return (
    <div className="flex flex-col gap-1">
      {table(rows.slice(0, TABLE_ROWS), 0, label)}
      {rest > 0 && (
        <Fold summary={count(rest, "more row")}>
          {table(rows.slice(TABLE_ROWS), TABLE_ROWS, `${label}, more rows`)}
        </Fold>
      )}
    </div>
  );
}

export const count = (n: number, word: string) =>
  `${n.toLocaleString("en")} ${word}${n === 1 ? "" : "s"}`;

export const importanceText = (n: number | null | undefined) =>
  n === null || n === undefined ? "importance unknown" : `importance ${n} of 5`;

export { categoryLabel as category } from "@/components/runs/filters";
