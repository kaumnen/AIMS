import { useQuery } from "@tanstack/react-query";

import { type AgentText, type Moment, REPORT_REFRESH_MS, type ReportSource } from "@/api/report";
import { flagsQuery, sweepsQuery } from "@/api/run";
import { Button } from "@/components/ui/button";
import { duration, fullTime, shortId, usd } from "@/lib/format";

import { useHarm } from "../harm";
import { LIVE } from "../monitor";
import { Scrubber } from "../scrubber";
import {
  category,
  count,
  Fold,
  Quote,
  ReportTable,
  RunLink,
  Section,
  type SectionProps,
  UNKNOWN,
  visible,
} from "./parts";

// The words of these sections, as `aims report` writes them.
const TIMELINE_INTRO =
  "Starts and ends, flags at importance 3 or more, monitor and operator actions, plants " +
  "reached and the events the judge cited.";
const MOMENT: Record<Moment["kind"], string> = {
  run_started: "Run started",
  agent_started: "Agent started",
  agent_finished: "Agent finished",
  run_finished: "Run finished",
  flag: "Flag",
  monitor_action: "Monitor action",
  operator_action: "Operator action",
  degraded: "Monitor degraded",
  plant: "Plant reached",
  report: "Report to the operator",
  scored: "Verifier reward",
  verdict: "Judge verdict",
  evidence: "Judge evidence",
};
const COSTS_TITLE = "Model calls by caller";
const KERNEL_TITLE = "Kernel events";
const NO_KERNEL = "No kernel events.";
const NONE = "None.";
const LIVE_SOURCE = "Live monitor";
const APPENDIX_SUMMARY = "Final answers, reports to the operator and judge disagreements";
const DISAGREEMENTS = "Judge disagreements";

const money = (value: number | null) => (value === null ? UNKNOWN : usd(value));
// An agent's cost when some of its calls had no price: what was priced is a floor.
const atLeast = (value: number) => (value === 0 ? UNKNOWN : `at least ${usd(value)}`);
// Steps and importance as plain numbers, counts with grouped digits, as in the Markdown.
const plain = (n: number) => <span className="tabular-nums">{n}</span>;
const grouped = (n: number) => <span className="tabular-nums">{n.toLocaleString("en")}</span>;
const mono = (value: string) => (
  <span className="font-mono text-xs break-all">{visible(value)}</span>
);

// An event of this run by its seq. The step sits beside it, as the server counted it.
function Seq({ seq, onSeq }: { seq: number; onSeq: (seq: number) => void }) {
  return (
    <button
      type="button"
      title={`Event ${seq}`}
      onClick={() => onSeq(seq)}
      className="hover:text-foreground text-muted-foreground whitespace-nowrap tabular-nums underline underline-offset-2"
    >
      seq {seq}
    </button>
  );
}

// The run on its time axis, with the flags and sweeps of the monitor the report is built from.
function RunScrubber({ report, runId, detail, steps, onSeq, onFlag }: SectionProps) {
  const source = report.header.pins.source;
  const watcher = source === LIVE ? runId : source;
  const chosen = report.sources.find((s) => s.chosen);
  const open = detail.run.state !== "done" || chosen?.status === "running";
  // the report of an open run is built again this often, so its flags keep pace with it
  const every = open ? REPORT_REFRESH_MS : false;
  const flags = useQuery({ ...flagsQuery(watcher), refetchInterval: every });
  const sweeps = useQuery({ ...sweepsQuery(watcher), refetchInterval: every });
  const harms = useHarm(runId);
  return (
    <Scrubber
      runId={runId}
      detail={detail}
      steps={steps}
      onSelect={onSeq}
      flags={flags.data ?? []}
      sweeps={sweeps.data ?? []}
      flag={undefined}
      onFlag={(f) => onFlag(f.seq)}
      harms={harms}
    />
  );
}

function What({ moment }: { moment: Moment }) {
  return (
    <span className="break-words">
      {MOMENT[moment.kind]}
      {moment.what && <>: {mono(visible(moment.what))}</>}
      {moment.detail && <span className="text-muted-foreground"> ({visible(moment.detail)})</span>}
    </span>
  );
}

export function TimelineSection(props: SectionProps) {
  const { report, steps, multi, onSeq } = props;
  const head = ["Seq", "Step", ...(multi ? ["Agent"] : []), "Event", "Categories", "Importance"];
  const rows = report.timeline.map((m) => [
    <Seq key="seq" seq={m.seq} onSeq={onSeq} />,
    m.step === null ? "" : plain(m.step),
    ...(multi
      ? [
          <span key="agent" className="break-all">
            {visible(m.agent ?? "")}
          </span>,
        ]
      : []),
    <What key="what" moment={m} />,
    m.categories.map(category).join(", "),
    m.importance === null ? "" : plain(m.importance),
  ]);
  return (
    <Section title="Timeline" intro={TIMELINE_INTRO}>
      {steps.length > 0 && <RunScrubber {...props} />}
      {rows.length > 0 && <ReportTable label="Timeline" head={head} rows={rows} />}
    </Section>
  );
}

export function FactsSection({ report }: SectionProps) {
  const f = report.facts;
  const review = report.header.pins.source !== LIVE;
  const counts = `${count(f.events, "event")}, ${count(f.steps, "step")}, ${count(f.flags, "flag")}.`;
  const costs =
    `Cost: agents ${money(f.agent_cost_usd)}. Live monitor ${money(f.monitor_cost_usd)}, ` +
    "judge included." +
    (review ? ` This review ${money(f.review_cost_usd)}, judge included.` : "");
  const kernel = Object.entries(f.kernel);
  return (
    <Section title="Run facts">
      {f.agents.length > 0 && (
        <ReportTable
          label="Agents"
          head={["Agent", "Profile", "Model", "Steps", "End", "Cost", "Time"]}
          rows={f.agents.map((a) => [
            <span key="agent" className="break-all">
              {visible(a.agent)}
            </span>,
            <span key="profile" className="break-all">
              {visible(a.profile)}
            </span>,
            mono(a.model),
            plain(a.steps),
            a.end ?? "",
            <span key="cost" className="tabular-nums">
              {a.cost_complete ? usd(a.cost_usd) : atLeast(a.cost_usd)}
            </span>,
            <span key="time" className="tabular-nums">
              {duration(a.wall_ms)}
            </span>,
          ])}
        />
      )}
      <p className="tabular-nums">
        {counts} {costs}
      </p>
      {f.costs.length > 0 && (
        <Fold summary={COSTS_TITLE}>
          <ReportTable
            label={COSTS_TITLE}
            head={["Caller", "Schema", "Calls", "Cost", "Input tokens", "Output tokens"]}
            rows={f.costs.map((c) => [
              c.role ?? "agent",
              c.schema_name ? mono(c.schema_name) : "",
              grouped(c.calls),
              <span key="cost" className="tabular-nums">
                {money(c.cost_usd)}
              </span>,
              grouped(c.input_tokens),
              grouped(c.output_tokens),
            ])}
          />
        </Fold>
      )}
      {kernel.length > 0 ? (
        <Fold summary={KERNEL_TITLE}>
          <ReportTable
            label={KERNEL_TITLE}
            head={["Type", "Count"]}
            rows={kernel.map(([type, n]) => [mono(type), grouped(n)])}
          />
        </Fold>
      ) : (
        <p className="text-muted-foreground">{NO_KERNEL}</p>
      )}
    </Section>
  );
}

function SourceName({ source }: { source: ReportSource }) {
  return source.review ? (
    <>
      Review <RunLink run={source.run_id} />
    </>
  ) : (
    LIVE_SOURCE
  );
}

export function SourcesSection({ report, onSource }: SectionProps) {
  const others = report.sources.filter((s) => !s.chosen);
  return (
    <Section title="Other monitor sources">
      {others.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ReportTable
          label="Other monitor sources"
          head={["Source", "Preset", "Status", "Importance", "Claim matched", "Cost", "Started"]}
          rows={others.map((s) => [
            <span key="source" className="flex flex-col items-start gap-1">
              <span>
                <SourceName source={s} />
              </span>
              <Button
                variant="outline"
                size="xs"
                aria-label={`Build from this source: ${s.review ? `review ${shortId(s.run_id)}` : "the live monitor"}`}
                onClick={() => onSource(s.run_id)}
              >
                Build from this source
              </Button>
            </span>,
            s.preset ?? "",
            s.status,
            s.importance === null ? "" : `${s.importance} of 5`,
            s.claim_matched ?? "",
            <span key="cost" className="tabular-nums">
              {money(s.cost_usd)}
            </span>,
            <span key="started" className="tabular-nums">
              {s.started_at === null ? UNKNOWN : fullTime(s.started_at)}
            </span>,
          ])}
        />
      )}
    </Section>
  );
}

// A final answer or a report to the operator: whose, where, then the text as written.
function Written({
  what,
  text,
  onSeq,
}: {
  what: string;
  text: AgentText;
  onSeq: (seq: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className="font-medium">
        {what} of <span className="font-mono text-sm">{visible(text.agent ?? "")}</span>
      </h3>
      <p className="text-muted-foreground tabular-nums">
        <Seq seq={text.seq} onSeq={onSeq} />
        {text.step === null ? "" : `, step ${text.step}`}.
      </p>
      <Quote>{text.text}</Quote>
    </div>
  );
}

export function AppendixSection({ report, onSeq }: SectionProps) {
  const a = report.appendix;
  const empty = a.answers.length + a.reports.length + a.disagreements.length === 0;
  return (
    <Section title="Appendix">
      {empty ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <Fold summary={APPENDIX_SUMMARY}>
          <div className="flex flex-col gap-4 pt-1">
            {a.answers.map((t) => (
              <Written key={`answer-${t.seq}`} what="Final answer" text={t} onSeq={onSeq} />
            ))}
            {a.reports.map((t) => (
              <Written key={`report-${t.seq}`} what="Report" text={t} onSeq={onSeq} />
            ))}
            {a.disagreements.length > 0 && (
              <div className="flex flex-col gap-2">
                <h3 className="font-medium">{DISAGREEMENTS}</h3>
                {a.disagreements.map((d, i) => (
                  // the list is fixed for one report, so a place is its key
                  // oxlint-disable-next-line react/no-array-index-key
                  <Quote key={i}>{d}</Quote>
                ))}
              </div>
            )}
          </div>
        </Fold>
      )}
    </Section>
  );
}
