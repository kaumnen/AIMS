import type { ReactNode } from "react";

import type { ReportFlag, RunReport } from "@/api/report";
import type { StepInfo } from "@/api/run";
import { Meter } from "@/components/runs/cells";
import { aside, statusText } from "@/lib/monitor";
import { awareText, causeText, matchOf, type ScoreLayer, THRESHOLD, toldText } from "@/lib/score";
import { cn } from "@/lib/utils";

import { LIVE } from "../monitor";
import { Cell } from "../score";
import {
  category,
  EventButton,
  Evidence,
  importanceText,
  LABELS_NOTE,
  NO_VERDICT,
  Quote,
  ReportTable,
  RunLink,
  Section,
  type SectionProps,
  SeqLink,
  stepFor,
  visible,
} from "./parts";

type AnomalyKind = RunReport["observations"][number]["kind"];

// The words of src/aims/report/texts.py, so the tab says what the Markdown says.
const FINDINGS_INTRO = "The judge's view. Importance runs from 1 to 5.";
const NO_FINDINGS = "The judge found none of its categories.";
const OBSERVATIONS_INTRO =
  "Things the judge saw that fit no category. Also signs the scenario or sandbox is broken.";
const NO_OBSERVATIONS = "None.";
const ANOMALY_KIND: Record<AnomalyKind, string> = {
  behaviour: "Behaviour",
  environment: "Environment",
};
const PLANTS_INTRO = "Ground truth from the run's truth file. The monitor never sees it.";
const NO_PLANTS = "No agent reached a plant.";
const FLAGS_NOTE = "The judge's review and a person's label sit side by side.";
const NO_FLAGS = "No flags.";

const LAYERS: readonly [ScoreLayer, string][] = [
  ["signal", "Signal"],
  ["sweep", "Sweep"],
  ["judge", "Judge"],
];
const labelText: Record<string, string> = {
  real: "Real",
  false_alarm: "False alarm",
  unclear: "Unclear",
};

const upper = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// Text from the log in a table cell: a few lines, all of it on hover.
function Clamp({ text, className }: { text: string; className?: string }) {
  return (
    <span
      title={visible(text)}
      className={cn("line-clamp-3 wrap-break-word print:line-clamp-none", className)}
    >
      {visible(text)}
    </span>
  );
}

function Lines({ main, sub, wide }: { main: ReactNode; sub?: string | null; wide?: boolean }) {
  return (
    <span className={cn("flex flex-col", wide && "min-w-32")}>
      <span>{main}</span>
      {sub?.trim() ? <Clamp text={sub} className="text-muted-foreground" /> : null}
    </span>
  );
}

// One finding or observation: its heading, the events it cites, the judge's words.
function Entry({
  title,
  meter,
  evidence,
  text,
  steps,
  more,
  onSeq,
  print,
}: {
  title: string;
  meter?: ReactNode;
  evidence: readonly number[];
  text: string;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
  print: SectionProps["print"];
}) {
  return (
    <div className="flex flex-col gap-1.5 pt-1">
      <div className="flex flex-wrap items-center gap-x-2">
        <h3 className="font-medium">{title}</h3>
        {meter}
      </div>
      {evidence.length > 0 && (
        <div className="flex flex-wrap gap-x-2">
          <span className="text-muted-foreground">Evidence</span>
          <Evidence evidence={evidence} steps={steps} more={more} onSeq={onSeq} print={print} />
        </div>
      )}
      {text.trim() !== "" && <Quote>{text}</Quote>}
    </div>
  );
}

const Empty = ({ children }: { children: string }) => (
  <p className="text-muted-foreground">{children}</p>
);

export function FindingsSection({ report, steps, more, onSeq, print }: SectionProps) {
  const why = report.headline.no_verdict;
  if (why !== null) return <Section title="Findings" intro={`Judge: ${NO_VERDICT[why]}.`} />;
  if (report.findings.length === 0) return <Section title="Findings" intro={NO_FINDINGS} />;
  return (
    <Section title="Findings" intro={FINDINGS_INTRO}>
      {report.findings.map((f) => {
        const level = f.present === "unclear" ? "unclear" : importanceText(f.importance);
        return (
          <Entry
            key={f.category}
            title={`${category(f.category)}: ${level}`}
            meter={
              f.importance !== null && (
                <Meter value={f.importance} label={`Importance ${f.importance} of 5`} />
              )
            }
            evidence={f.evidence}
            text={f.justification}
            steps={steps}
            more={more}
            onSeq={onSeq}
            print={print}
          />
        );
      })}
    </Section>
  );
}

export function ObservationsSection({ report, steps, more, onSeq, print }: SectionProps) {
  if (report.observations.length === 0) {
    return (
      <Section title="Other observations">
        <Empty>{NO_OBSERVATIONS}</Empty>
      </Section>
    );
  }
  return (
    <Section title="Other observations" intro={OBSERVATIONS_INTRO}>
      {report.observations.map((a, i) => (
        <Entry
          // observations are fixed for one report and have no id, so their place is their key
          // oxlint-disable-next-line react/no-array-index-key
          key={i}
          title={ANOMALY_KIND[a.kind] + (a.agent === null ? "" : ` of ${visible(a.agent)}`)}
          evidence={a.evidence}
          text={a.note}
          steps={steps}
          more={more}
          onSeq={onSeq}
          print={print}
        />
      ))}
    </Section>
  );
}

// Ground truth, for operators. A viewer's report has none, so the section is left out.
export function PlantsSection({ report, steps, more, multi, onSeq, onFlag, print }: SectionProps) {
  const p = report.plants;
  if (p === null) return null;
  if (p.error !== null) {
    return (
      <Section title="Plants" intro={PLANTS_INTRO}>
        <p>The plants could not be read: {visible(p.error)}</p>
      </Section>
    );
  }
  const run = report.header.run_id;
  const source = report.header.pins.source;
  const scored = p.score !== null;
  const command = `aims score ${run}${source === LIVE ? "" : ` --monitor ${source}`}`;
  // A catch can rest on the verdict or on a raw re-flag, whose seq is no flag of the source:
  // that opens the plant's own event.
  const flagged = new Set(report.flags.map((f) => f.seq));
  const open = (seq: number | null | undefined, plant: number) =>
    typeof seq === "number" && flagged.has(seq) ? onFlag(seq) : onSeq(plant);
  const head = [
    ...(multi ? ["Agent"] : []),
    "Plant",
    "Kind",
    "Category",
    "Reached",
    "At",
    ...(scored ? LAYERS.map(([, name]) => name) : []),
  ];
  const rows = p.rows.map((t) => [
    ...(multi ? [t.agent] : []),
    t.plant,
    t.kind,
    category(t.category),
    <span key="reached" className={t.taken ? "text-flag" : "text-muted-foreground"}>
      {t.tier}
      {t.taken ? "" : ", not taken"}
    </span>,
    <span key="at" className="whitespace-nowrap">
      <SeqLink seq={t.seq} agent={t.agent} steps={steps} more={more} onSeq={onSeq} print={print} />
    </span>,
    ...(scored
      ? LAYERS.map(([layer]) => {
          const m = matchOf(t.matches, layer);
          return m ? <Cell key={layer} m={m} onFlag={(seq) => open(seq, t.seq)} /> : null;
        })
      : []),
  ]);
  const units = p.units.map((u) => [
    u.agent,
    u.role,
    u.plants.length > 0 ? u.plants.join(", ") : u.positive === null ? "none" : "-",
    u.eval_aware === null ? "" : (awareText[u.eval_aware] ?? u.eval_aware),
    u.told === null ? "" : (toldText[u.told] ?? u.told),
    u.cause === null ? "" : upper(causeText[u.cause] ?? u.cause),
  ]);
  return (
    <Section title="Plants" intro={PLANTS_INTRO}>
      {p.score === null ? (
        <p className="text-muted-foreground">
          No score of this monitor source yet.{" "}
          <code className="text-foreground font-mono text-xs wrap-anywhere">{command}</code> shows
          what each layer caught.
        </p>
      ) : (
        <p className="text-muted-foreground">
          Caught, from score <RunLink run={p.score} />, at importance {THRESHOLD} or more.
        </p>
      )}
      {rows.length === 0 ? (
        <Empty>{NO_PLANTS}</Empty>
      ) : (
        <ReportTable label="Plants reached" head={head} rows={rows} />
      )}
      {units.length > 0 && (
        <ReportTable
          label="What each agent took"
          head={[
            "Agent",
            "Role",
            "Took",
            "Said it was tested",
            "Told the operator",
            "Reasons held",
          ]}
          rows={units}
        />
      )}
    </Section>
  );
}

// The step of the flag's agent that holds the first event it cites, once that step is loaded.
function flagStep(f: ReportFlag, steps: readonly StepInfo[], more: boolean) {
  if (f.evidence.length === 0) return undefined;
  return stepFor(steps, Math.min(...f.evidence), more, f.agent);
}

export function FlagsSection({ report, steps, more, multi, onFlag, print }: SectionProps) {
  const source = report.header.pins.source;
  const review = source !== LIVE;
  const intro = (
    <>
      {review ? (
        <>
          Flags of review <RunLink run={source} />.
        </>
      ) : (
        "Flags of the live monitor."
      )}{" "}
      {FLAGS_NOTE} {LABELS_NOTE}
    </>
  );
  if (report.flags.length === 0) {
    return (
      <Section title="Flags" intro={intro}>
        <Empty>{NO_FLAGS}</Empty>
      </Section>
    );
  }
  const head = [
    "Flag",
    ...(multi ? ["Agent"] : []),
    "Layer",
    "Category",
    "Importance",
    "Status",
    "Judge",
    "Person",
    "Why",
  ];
  const rows = report.flags.map((f) => {
    const step = flagStep(f, steps, more);
    const status = statusText[f.status] + (f.tainted && f.status !== "tainted" ? ", tainted" : "");
    return [
      <EventButton
        key="flag"
        title={review ? `Flag at event ${f.seq} of the review` : `Flag at event ${f.seq}`}
        onClick={() => onFlag(f.seq)}
        print={print(f.seq, f.run_id)}
        className="whitespace-nowrap"
      >
        {step ? `step ${step.n}` : `seq ${f.seq}`}
      </EventButton>,
      ...(multi ? [f.agent ?? ""] : []),
      <span key="layer" className="whitespace-nowrap">
        {f.layer}
        {f.count > 1 && <span className="text-muted-foreground tabular-nums"> {f.count}×</span>}
      </span>,
      category(f.category),
      <span key="importance" className="flex items-center gap-1.5">
        <Meter value={f.importance} label={`Importance ${f.importance} of 5`} quiet={aside(f)} />
        <span className="tabular-nums">{f.importance}</span>
      </span>,
      <Lines key="status" main={status} sub={f.status_reason} />,
      f.review ? (
        <Lines key="judge" main={statusText[f.review.status]} sub={f.review.reason} wide />
      ) : null,
      f.label ? (
        <Lines
          key="person"
          main={`${labelText[f.label.label] ?? f.label.label}, by ${visible(f.label.by)}`}
          sub={f.label.note}
          wide
        />
      ) : null,
      <Clamp key="why" text={f.justification} className="min-w-40" />,
    ];
  });
  return (
    <Section title="Flags" intro={intro}>
      <ReportTable label="Flags" head={head} rows={rows} />
    </Section>
  );
}
