import { createElement, Fragment, type ReactNode } from "react";

import type { ReportWarning, RunReport } from "@/api/report";
import { Meter } from "@/components/runs/cells";
import { duration, fullTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { LIVE } from "../monitor";
import {
  count,
  importanceText,
  LABELS_NOTE,
  NO_VERDICT,
  OtherSeq,
  Quote,
  RunLink,
  Section,
  type SectionProps,
  SeqLink,
  UNKNOWN,
  visible,
} from "./parts";

// The words of src/aims/report/texts.py these sections use, kept as the Markdown has them.
const ACCESS: Record<RunReport["header"]["pins"]["role"], string> = {
  local: "local runs directory. Labels are only the ones written here.",
  operator: "operator. Plants and scores included.",
  viewer: "viewer. Plants, ground truth and scores left out.",
};
const NO_LINKS = "none. Evidence shows as seq N.";
const JUDGE_VIEW = "The judge's view, in its own words.";
const NO_SUMMARY = "No judge summary.";

type Level = "note" | "warning" | "caution";
const LEVELS: Record<Level, string> = { note: "Note", warning: "Warning", caution: "Caution" };
const WARNINGS: Record<ReportWarning["kind"], readonly [Level, string]> = {
  in_progress: ["note", "In progress. This report shows the log so far."],
  verify_failed: ["caution", "The hash chain did not verify. Treat every fact below as unproven."],
  monitor_degraded: ["warning", "{who}'s {part} stopped at {at}. {then}"],
  audit_lost: ["warning", "The kernel audit missed {lost}, first at {at}."],
  log_truncated: [
    "warning",
    "The log had a torn last line at {at}. The writer cut it and kept the bytes.",
  ],
  replay_divergence: [
    "warning",
    "The replay differed from its recording {count} times, first at {at}.",
  ],
  cost_incomplete: ["note", "The cost of {agent} is incomplete. Some calls had no price."],
  tainted: ["warning", "Flags raised while the verifier ran: {seqs}. The reward stands as given."],
};
const BUDGET = "{who} ran out of budget at {at}. {then}";
const TAINTED_REVIEW = "{who} flagged events of the verifier: {seqs}. The reward stands as given.";
const WHO = "The monitor";
const WHO_LIVE = "The live monitor";
const WHO_REVIEW = "Review {run}";
const PART: Partial<Record<string, string>> = {
  sweep: "sweeps",
  judge: "judge",
  review: "review",
  signal: "signals",
  budget: "budget",
};
const THEN: Partial<Record<string, string>> = {
  continue: "It went on without that part.",
  pause: "It paused the agents.",
  signals_only: "From there it ran signals only.",
  skip: "It skipped that part.",
};
const LOG_SAYS = "The log says:";
const LOST_EVENTS = "{n} events";
const LOST_UNKNOWN = "events";
// Links a list of seqs shows at most, as in the Markdown.
const MAX_LINKS = 12;

// A template of texts.py with its `{name}` slots filled with text or links, trimmed at both
// ends as Python's strip() trims the Markdown line.
function fill(template: string, values: Record<string, ReactNode>): ReactNode {
  const parts: ReactNode[] = [];
  template.split(/\{(\w+)\}/).forEach((piece, i) => {
    const value = i % 2 === 0 ? piece : values[piece];
    const last = parts.at(-1);
    if (typeof value === "string" && typeof last === "string")
      parts[parts.length - 1] = last + value;
    else parts.push(value);
  });
  const first = parts[0];
  if (typeof first === "string") parts[0] = first.trimStart();
  const end = parts.at(-1);
  if (typeof end === "string") parts[parts.length - 1] = end.trimEnd();
  return createElement(Fragment, null, ...parts);
}

// A hash shortened to its first characters, the whole of it in the title.
function Hash({ value }: { value: string | null }) {
  if (!value) return UNKNOWN;
  return (
    <span title={value} className="font-mono text-xs">
      {value.slice(0, 12)}
    </span>
  );
}

const Mono = ({ children }: { children: string }) => (
  <span className="font-mono text-xs break-all">{children}</span>
);

const timeText = (iso: string | null) => (iso === null ? UNKNOWN : fullTime(iso));

// The report's header: the run and every input the report was built from.
export function HeaderSection({ report }: SectionProps) {
  const h = report.header;
  const p = h.pins;
  const ms = report.facts.duration_ms;
  const head = (
    <>
      , head <Hash value={p.head} />
    </>
  );
  const chain = p.verified ? (
    <>
      verified, {count(p.events, "event")}
      {head}
    </>
  ) : (
    <>
      <span className="text-flag">not verified</span>
      {p.verify_error ? ` (${visible(p.verify_error)})` : ""}
      {head}
    </>
  );
  const monitor =
    p.source === LIVE ? (
      p.monitor === null ? (
        "none"
      ) : (
        "live monitor"
      )
    ) : (
      <>
        review <RunLink run={p.source} />, head <Hash value={p.source_head} />
      </>
    );
  const score =
    p.role === "viewer" ? (
      "left out"
    ) : p.score === null ? (
      "none"
    ) : (
      <>
        <RunLink run={p.score} />, head <Hash value={p.score_head} />
      </>
    );
  const pins: [string, ReactNode][] = [
    ["Run", <Mono key="run">{h.run_id}</Mono>],
    ["Scenario", visible(h.variant === null ? h.scenario : `${h.scenario}, variant ${h.variant}`)],
    ["Status", h.reason === null ? h.status : `${h.status} (${visible(h.reason)})`],
    ["Started", timeText(h.started_at)],
    ["Duration", ms === null ? UNKNOWN : duration(ms)],
    ["Hash chain", chain],
    [
      "Monitor",
      <>
        {monitor}
        {p.monitor !== null && (
          <>
            , identity <Hash value={p.monitor} />
          </>
        )}
      </>,
    ],
    ["Score", score],
    [
      "Labels",
      <>
        {p.labels === 0 ? "none" : `${p.labels}, latest ${timeText(p.labels_at)}`}.{" "}
        <span className="text-muted-foreground">{LABELS_NOTE}</span>
      </>,
    ],
    ["Access", ACCESS[p.role]],
    ["Links", p.base_url === null ? NO_LINKS : <Mono key="links">{p.base_url}</Mono>],
    [
      "Built",
      <>
        AIMS <span className="font-mono text-xs">{p.version}</span> at {fullTime(p.built_at)}
      </>,
    ],
  ];
  return (
    <Section title={`Run report: ${visible(h.scenario)}`}>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-0.5">
        {pins.map(([label, value]) => (
          <Fragment key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0 break-words tabular-nums">{value}</dd>
          </Fragment>
        ))}
      </dl>
    </Section>
  );
}

// Python's `{:g}`: six significant digits, no trailing zeros, an exponent below 1e-4 or from
// 1e6 up.
function rewardText(n: number): string {
  const exp = n === 0 ? 0 : Math.floor(Math.log10(Math.abs(n)));
  if (exp >= -4 && exp < 6) return String(Number(n.toPrecision(6)));
  const [digits = "", power = "0"] = n.toExponential(5).split("e");
  const p = Number(power);
  return `${digits.replace(/\.?0+$/, "")}e${p < 0 ? "-" : "+"}${String(Math.abs(p)).padStart(2, "0")}`;
}

// The verifier's reward beside the judge's verdict.
export function HeadlineSection({ report }: SectionProps) {
  const h = report.headline;
  let reward: string;
  if (!h.scored || h.reward === null) {
    reward = h.scored && h.reward_error ? "Reward: none (verifier error)" : "Reward: none";
  } else {
    const tests = h.passed !== null && h.total !== null ? ` (${h.passed} of ${h.total} tests)` : "";
    reward = `Reward ${rewardText(h.reward)}${tests}`;
  }
  const judge =
    h.no_verdict !== null ? (
      `Judge: ${NO_VERDICT[h.no_verdict]}`
    ) : h.importance ? (
      <span className="inline-flex items-center gap-1.5">
        {"Judge: "}
        <span aria-hidden className="inline-flex">
          <Meter value={h.importance} label={importanceText(h.importance)} />
        </span>
        {importanceText(h.importance)}
      </span>
    ) : (
      "Judge: nothing found"
    );
  const parts: [string, ReactNode][] = [
    ["reward", reward],
    ["judge", judge],
  ];
  if (h.claim_matched !== null) parts.push(["claim", `Claim matched: ${h.claim_matched}`]);
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-base font-medium tabular-nums">
      {parts.map(([key, part], i) => (
        // The dot goes with the part after it, so a wrapped line never ends on one. The
        // spaces only keep the text the same as the Markdown line; flex lays it out.
        <Fragment key={key}>
          {i > 0 && " "}
          <span className="inline-flex items-center gap-x-2">
            {i > 0 && <span className="text-muted-foreground font-normal">·</span>}
            {i > 0 && " "}
            {part}
          </span>
        </Fragment>
      ))}
    </p>
  );
}

// The flags a warning names, in the order of their seqs, each opening that flag in the monitor
// that raised it: the live monitor for the reported run, else the review.
function flagLinks(props: SectionProps, run: string, seqs: readonly number[]): ReactNode {
  const holder = run === props.runId ? LIVE : run;
  const shown = seqs.toSorted((a, b) => a - b);
  const rest = shown.length - MAX_LINKS;
  return (
    <>
      {shown.slice(0, MAX_LINKS).map((seq, i) => (
        <Fragment key={seq}>
          {i > 0 && ", "}
          <button
            type="button"
            title={`Flag at event ${seq}`}
            onClick={() => props.onFlag(seq, holder)}
            className="hover:text-foreground text-muted-foreground tabular-nums underline underline-offset-2"
          >
            seq {seq}
          </button>
        </Fragment>
      ))}
      {rest > 0 && ` (${rest} more)`}
    </>
  );
}

// One warning in the words of the Markdown writer's `_warning`.
function warningLine(props: SectionProps, w: ReportWarning): ReactNode {
  const { report, runId, steps, more, onSeq } = props;
  const own = w.run_id === runId;
  let template = WARNINGS[w.kind][1];
  let who: ReactNode;
  if (!own) {
    who = fill(WHO_REVIEW, { run: <RunLink run={w.run_id} /> });
    if (w.kind === "tainted") template = TAINTED_REVIEW;
  } else {
    who = report.header.pins.source === LIVE ? WHO : WHO_LIVE;
  }
  if (w.kind === "monitor_degraded" && w.part === "budget") template = BUDGET;
  const at =
    w.seq === null ? (
      ""
    ) : own ? (
      <SeqLink seq={w.seq} steps={steps} more={more} onSeq={onSeq} />
    ) : (
      <OtherSeq run={w.run_id} seq={w.seq} />
    );
  return fill(template, {
    who,
    at,
    part: PART[w.part ?? ""] ?? w.part ?? "",
    // the `{then}` slot of the templates, never awaited
    // oxlint-disable-next-line unicorn/no-thenable
    then: THEN[w.then ?? ""] ?? "",
    lost: w.lost === null ? LOST_UNKNOWN : LOST_EVENTS.replace("{n}", w.lost.toLocaleString("en")),
    count: String(w.count),
    agent: <span className="font-mono text-xs">{visible(w.agent ?? "")}</span>,
    seqs: flagLinks(props, w.run_id, w.seqs),
  });
}

// What limits what the report can say, as callouts like GitHub's alerts.
export function WarningsSection(props: SectionProps) {
  const { warnings } = props.report;
  if (warnings.length === 0) return null;
  return (
    <ul aria-label="Warnings" className="flex flex-col gap-2">
      {warnings.map((w, i) => {
        const level = WARNINGS[w.kind][0];
        return (
          <li
            // the warnings are fixed for one report, so a warning's place is its key
            // oxlint-disable-next-line react/no-array-index-key
            key={i}
            data-level={level}
            className={cn(
              "flex flex-col gap-0.5 border-l-2 pl-3",
              level === "caution"
                ? "border-flag"
                : level === "warning"
                  ? "border-muted-foreground"
                  : "border-grid",
            )}
          >
            <p className="break-words">
              <span
                className={cn(
                  "font-medium",
                  level === "caution" ? "text-flag" : "text-muted-foreground",
                )}
              >
                {LEVELS[level]}
              </span>{" "}
              {warningLine(props, w)}
            </p>
            {w.detail && (
              <p className="text-muted-foreground break-words">
                {LOG_SAYS} {visible(w.detail)}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// The judge's summary of the run, as it wrote it.
export function SummarySection({ report }: SectionProps) {
  const blank = !report.summary.trim();
  return (
    <Section title="Judge summary" intro={blank ? undefined : JUDGE_VIEW}>
      {blank ? (
        <p className="text-muted-foreground">{NO_SUMMARY}</p>
      ) : (
        <Quote>{report.summary}</Quote>
      )}
    </Section>
  );
}
