import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import { useEffect, useRef } from "react";

import type { Event } from "@/api/events";
import { type FlagInfo, type RunDetail, runQuery, type StepInfo } from "@/api/run";
import { reviewsQuery, type RunSummary } from "@/api/runs";
import { Meter, statusLabel } from "@/components/runs/cells";
import { categoryLabel, Choice } from "@/components/runs/filters";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { summary } from "@/lib/event-text";
import { started, usd } from "@/lib/format";
import {
  aside,
  cited,
  type FlagFilters,
  type FlagStatus,
  flagStep,
  groupFlags,
  layers,
  presetText,
  reaches,
  statusText,
} from "@/lib/monitor";
import { stepOf } from "@/lib/timeline";
import { cn } from "@/lib/utils";

import { NewReview } from "./review";
import { TextOf } from "./text-of";

export const LIVE = "live";
const MAX_EVIDENCE = 20;

export interface Source {
  id: string;
  run: RunSummary;
  review: boolean;
}

// The run's own monitor, then each review of it, newest first.
export function useSources(
  runId: string,
  detail: RunDetail,
): { sources: Source[]; ready: boolean } {
  const reviews = useQuery(reviewsQuery(runId));
  const own: Source = { id: LIVE, run: detail.run, review: false };
  const listed = (reviews.data?.items ?? []).map((r) => ({ id: r.run_id, run: r, review: true }));
  return { sources: [own, ...listed], ready: !reviews.isPending };
}

// The newest finished review, else the run's own monitor.
export function defaultSource(sources: readonly Source[]): string {
  return sources.find((s) => s.review && s.run.status === "completed")?.id ?? LIVE;
}

function sourceLabel(s: Source): string {
  const preset = presetText(s.run);
  if (!s.review) return preset ? `Live monitor, ${preset}` : "Live monitor";
  return [
    "Review",
    preset,
    s.run.cost_usd === null ? null : usd(s.run.cost_usd),
    s.run.status === "completed" ? null : statusLabel(s.run).toLowerCase(),
    s.run.started_at ? started(s.run.started_at) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

// A review that has not started: why it waits, or that it ended first.
function waitText(run: RunSummary): string {
  if (run.state === "done")
    return run.reason ? `Ended before it started: ${run.reason}.` : "Ended before it started.";
  return run.reason ? `Waiting to start: ${run.reason}.` : "Waiting for a runner to start it.";
}

type Data = Record<string, unknown>;
const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const list = (v: unknown): Data[] =>
  Array.isArray(v) ? v.filter((x): x is Data => typeof x === "object" && x !== null) : [];
const seqs = (v: unknown): number[] =>
  Array.isArray(v) ? v.filter((x): x is number => typeof x === "number") : [];

// Evidence as the steps that hold it; past the loaded steps, as bare seqs.
function StepLinks({
  evidence,
  steps,
  more,
  onSeq,
}: {
  evidence: readonly number[];
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
}) {
  const links = new Map<string, { label: string; seq: number }>();
  for (const seq of evidence.toSorted((a, b) => a - b)) {
    const step = reaches(steps, seq, more) ? stepOf(steps, seq) : undefined;
    const key = step ? `s${step.seq}` : `e${seq}`;
    if (!links.has(key)) links.set(key, { label: step ? `step ${step.n}` : `seq ${seq}`, seq });
  }
  return (
    <span className="flex flex-wrap gap-x-2">
      {[...links.values()].map((l) => (
        <button
          key={l.seq}
          type="button"
          title={`Event ${l.seq}`}
          onClick={() => onSeq(l.seq)}
          className="hover:text-foreground text-muted-foreground tabular-nums underline underline-offset-2"
        >
          {l.label}
        </button>
      ))}
    </span>
  );
}

const claimText: Record<string, string> = {
  yes: "Final claim matches",
  no: "Final claim does not match",
  unclear: "Final claim unclear",
};

function VerdictCard({
  verdict,
  source,
  pending,
  steps,
  more,
  onSeq,
}: {
  verdict: Event | null | undefined;
  source: Source;
  pending: boolean;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
}) {
  if (pending) return null;
  if (!verdict) {
    const preset = source.run.preset;
    const why =
      source.run.status === "running" || source.run.started_at === null
        ? "No verdict yet."
        : source.run.monitor_mode === "signals_only" || preset === "signals" || preset === "off"
          ? "No verdict: this monitor has no judge."
          : "No verdict.";
    return <p className="text-muted-foreground">{why}</p>;
  }
  const d = verdict.data as Data;
  const severity = num(d["max_severity"]) ?? 0;
  const claim = typeof d["claim_matched"] === "string" ? claimText[d["claim_matched"]] : null;
  const found = list(d["categories"]).filter((c) => c["present"] !== "no");
  const clear = list(d["categories"]).length - found.length;
  const disagreements = Array.isArray(d["disagreements"]) ? d["disagreements"].map(String) : [];
  const failed = list(d["rollouts"]).filter((r) => typeof r["error"] === "string");
  const cost = num(d["cost_usd"]);
  return (
    <section aria-label="Verdict" className="flex flex-col gap-1.5">
      <h3 className="flex items-center gap-2">
        <span className="font-medium">Verdict</span>
        <Meter value={severity} label={`Judged severity ${severity} of 5`} />
        <span className="tabular-nums">{severity} of 5</span>
        {cost !== undefined && cost > 0 && (
          <span className="text-muted-foreground ml-auto tabular-nums">{usd(cost)}</span>
        )}
      </h3>
      {claim && <p className="text-muted-foreground">{claim}.</p>}
      <TextOf runId={source.run.run_id} value={d["summary"]} mono={false} className="max-h-48" />
      {found.map((c) => {
        const importance = num(c["importance"]);
        return (
          <details key={String(c["category"])} className="group">
            <summary className="flex cursor-default list-none items-center gap-2">
              <ChevronRight
                aria-hidden
                className="text-muted-foreground size-3.5 shrink-0 group-open:rotate-90"
              />
              <span>{categoryLabel(String(c["category"]))}</span>
              {c["present"] === "unclear" && <span className="text-muted-foreground">unclear</span>}
              {importance !== undefined && (
                <Meter value={importance} label={`Importance ${importance} of 5`} />
              )}
            </summary>
            <div className="mt-1 flex flex-col gap-1 pl-5">
              <p className="text-muted-foreground">{String(c["justification"] ?? "")}</p>
              <StepLinks evidence={seqs(c["evidence"])} steps={steps} more={more} onSeq={onSeq} />
            </div>
          </details>
        );
      })}
      {clear > 0 && (
        <p className="text-muted-foreground">
          {found.length === 0 ? "Nothing found in" : "Not found in"} the other {clear}{" "}
          {clear === 1 ? "category" : "categories"}.
        </p>
      )}
      {disagreements.length > 0 && (
        <details>
          <summary className="text-muted-foreground cursor-default">
            Rollouts disagreed on {disagreements.length}
          </summary>
          <ul className="text-muted-foreground mt-1 list-disc pl-5">
            {disagreements.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </details>
      )}
      {failed.length > 0 && (
        <p className="text-muted-foreground">
          {failed.length} of {list(d["rollouts"]).length} judge rollouts failed:{" "}
          {String(failed[0]?.["error"])}
        </p>
      )}
    </section>
  );
}

function FlagRow({
  flag,
  sourceRun,
  steps,
  more,
  open,
  multi,
  evidence,
  onFlag,
  onSeq,
}: {
  flag: FlagInfo;
  sourceRun: string;
  steps: readonly StepInfo[];
  more: boolean;
  open: boolean;
  multi: boolean;
  evidence: ReadonlyMap<number, Event>;
  onFlag: (f: FlagInfo) => void;
  onSeq: (seq: number) => void;
}) {
  const step = reaches(steps, cited(flag), more) ? flagStep(flag, steps) : undefined;
  const where = step ? `step ${step.n}` : `seq ${cited(flag)}`;
  const origin = [
    flag.layer,
    flag.source,
    flag.lane,
    multi ? flag.agent_id : null,
    flag.count > 1 ? `${flag.count}×` : null,
  ].filter(Boolean);
  return (
    <li
      data-flag-row={flag.seq}
      className={cn("rounded-sm", open ? "bg-accent" : "hover:bg-accent/60")}
    >
      <button
        type="button"
        aria-pressed={open}
        onClick={() => onFlag(flag)}
        className="focus-visible:ring-ring/50 flex w-full flex-col gap-0.5 rounded-sm px-1.5 py-1 text-left outline-none focus-visible:ring-2"
      >
        <span className="flex w-full items-center gap-2">
          <Meter
            value={flag.importance}
            label={`Importance ${flag.importance} of 5`}
            quiet={aside(flag)}
          />
          <span className="min-w-0 flex-1 truncate">{categoryLabel(flag.category)}</span>
          <span className="text-muted-foreground shrink-0 tabular-nums">{where}</span>
        </span>
        <span className="text-muted-foreground truncate text-xs">{origin.join(" · ")}</span>
        {!open && flag.justification && (
          <span className="text-muted-foreground line-clamp-2">{flag.justification}</span>
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-1.5 px-1.5 pb-2">
          {flag.justification && <p>{flag.justification}</p>}
          {flag.quote !== null && (
            <div>
              <p className="text-muted-foreground">
                Quote{flag.quote_verified === false ? ", not found in the cited events" : ""}
              </p>
              <TextOf runId={sourceRun} value={flag.quote} className="max-h-40" />
            </div>
          )}
          {flag.status !== "open" && (
            <p className="text-muted-foreground">
              {statusText[flag.status]}
              {flag.status_source ? ` by ${flag.status_source}` : ""}
              {flag.status_reason ? `: ${flag.status_reason}` : "."}
            </p>
          )}
          {flag.reflags > 0 && (
            <p className="text-muted-foreground">
              Flagged again {flag.reflags} {flag.reflags === 1 ? "time" : "times"}.
            </p>
          )}
          <p className="text-muted-foreground">Evidence</p>
          <ul className="flex flex-col">
            {flag.evidence
              .toSorted((a, b) => a - b)
              .slice(0, MAX_EVIDENCE)
              .map((seq) => {
                const e = evidence.get(seq);
                const at = reaches(steps, seq, more) ? stepOf(steps, seq) : undefined;
                return (
                  <li key={seq}>
                    <button
                      type="button"
                      onClick={() => onSeq(seq)}
                      title={`Event ${seq}`}
                      className="hover:bg-background/60 flex w-full gap-2 rounded-sm px-1 text-left"
                    >
                      <span className="text-muted-foreground w-14 shrink-0 tabular-nums">
                        {at ? `step ${at.n}` : `seq ${seq}`}
                      </span>
                      <span className="min-w-0 flex-1 truncate font-mono text-xs leading-5">
                        {e ? `${e.type} ${summary(e)}` : seq}
                      </span>
                    </button>
                  </li>
                );
              })}
          </ul>
          {flag.evidence.length > MAX_EVIDENCE && (
            <p className="text-muted-foreground">
              and {flag.evidence.length - MAX_EVIDENCE} more events.
            </p>
          )}
        </div>
      )}
    </li>
  );
}

const quiet = new Set<FlagStatus>(["cleared", "rejected"]);

// The monitor's view of the run: which monitor, its verdict, its flags. A flag click jumps the
// steps to what it cites.
export function MonitorPanel({
  sources,
  source,
  onSource,
  flags,
  loading,
  shown,
  filters,
  onFilters,
  selected,
  onFlag,
  steps,
  more,
  multi,
  evidence,
  onSeq,
  className,
}: {
  sources: readonly Source[];
  source: Source;
  onSource: (id: string) => void;
  flags: readonly FlagInfo[];
  loading: boolean;
  shown: readonly FlagInfo[];
  filters: FlagFilters;
  onFilters: (f: FlagFilters) => void;
  selected: number | undefined;
  onFlag: (f: FlagInfo) => void;
  steps: readonly StepInfo[];
  more: boolean;
  multi: boolean;
  evidence: ReadonlyMap<number, Event>;
  onSeq: (seq: number) => void;
  className?: string;
}) {
  const detail = useQuery(runQuery(source.run.run_id));
  const items = sources.map((s) => ({ value: s.id, label: sourceLabel(s) }));
  const categories = [...new Set(flags.map((f) => f.category))].toSorted();
  const none = sources.length === 1 && source.run.preset === null && !loading && flags.length === 0;
  const own = sources.find((s) => !s.review);
  const waiting = source.review && source.run.started_at === null;
  const box = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selected === undefined || loading) return;
    box.current
      ?.querySelector(`[data-flag-row="${selected}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [selected, loading]);
  return (
    <aside
      ref={box}
      aria-label="Monitor"
      className={cn("flex min-h-0 flex-col gap-3 overflow-auto rounded-sm border p-2", className)}
    >
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <Select items={items} value={source.id} onValueChange={(v) => v && onSource(String(v))}>
              <SelectTrigger size="sm" aria-label="Monitor" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false} align="start">
                {items.map((i) => (
                  <SelectItem key={i.value} value={i.value}>
                    {i.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {own && <NewReview run={own.run} onStarted={onSource} />}
        </div>
        {source.review && (
          <Link
            to="/runs/$runId"
            params={{ runId: source.run.run_id }}
            className="text-muted-foreground hover:text-foreground self-start underline underline-offset-2"
          >
            Open monitor run
          </Link>
        )}
      </div>
      {waiting ? (
        <p className="text-muted-foreground">{waitText(source.run)}</p>
      ) : none ? (
        <p className="text-muted-foreground">No monitor watched this run.</p>
      ) : (
        <>
          <VerdictCard
            verdict={detail.data?.verdict}
            source={source}
            pending={detail.isPending}
            steps={steps}
            more={more}
            onSeq={onSeq}
          />
          <section aria-label="Flags" className="flex flex-col gap-2 border-t pt-2">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-medium">Flags</h3>
              {!loading && (
                <span className="text-muted-foreground tabular-nums">
                  {shown.length === flags.length
                    ? flags.length
                    : `${shown.length} of ${flags.length}`}
                </span>
              )}
            </div>
            {flags.length > 0 && (
              <div className="flex flex-wrap gap-2">
                <Choice
                  name="Layer"
                  label="Any layer"
                  value={filters.layer}
                  items={layers}
                  className="min-w-28"
                  onChange={(v) =>
                    onFilters({ ...filters, layer: v as FlagFilters["layer"] | undefined })
                  }
                />
                <Choice
                  name="Category"
                  label="Any category"
                  value={filters.category}
                  items={categories.map((c) => ({ value: c, label: categoryLabel(c) }))}
                  className="min-w-28"
                  onChange={(v) => onFilters({ ...filters, category: v })}
                />
              </div>
            )}
            {loading ? (
              <p className="text-muted-foreground">Loading flags</p>
            ) : shown.length === 0 ? (
              <p className="text-muted-foreground">
                {flags.length === 0 ? "No flags." : "No flags match."}
              </p>
            ) : (
              groupFlags(shown).map(([status, group]) => {
                const rows = (
                  <ul className="flex flex-col gap-0.5">
                    {group.map((f) => (
                      <FlagRow
                        key={f.seq}
                        flag={f}
                        sourceRun={source.run.run_id}
                        steps={steps}
                        more={more}
                        open={f.seq === selected}
                        multi={multi}
                        evidence={evidence}
                        onFlag={onFlag}
                        onSeq={onSeq}
                      />
                    ))}
                  </ul>
                );
                const title = (
                  <span>
                    <span className="text-foreground">{statusText[status]}</span>{" "}
                    <span className="tabular-nums">{group.length}</span>
                    {quiet.has(status) ? ", set aside" : ""}
                  </span>
                );
                return quiet.has(status) ? (
                  <details
                    key={status}
                    open={group.some((f) => f.seq === selected) || undefined}
                    className="group"
                  >
                    <summary className="text-muted-foreground flex cursor-default list-none items-center gap-1">
                      <ChevronRight aria-hidden className="size-3.5 group-open:rotate-90" />
                      {title}
                    </summary>
                    <div className="mt-1">{rows}</div>
                  </details>
                ) : (
                  <div key={status} className="flex flex-col gap-1">
                    <h4 className="text-muted-foreground">{title}</h4>
                    {rows}
                  </div>
                );
              })
            )}
          </section>
        </>
      )}
    </aside>
  );
}
