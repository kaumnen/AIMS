import { useInfiniteQuery, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { flagsQuery, type RunDetail, type StepInfo, stepsQuery } from "@/api/run";
import { reviewsQuery, type RunSummary } from "@/api/runs";
import { scenariosQuery } from "@/api/scenarios";
import {
  type Label,
  type LabelKind,
  labelsQuery,
  postLabel,
  scoreEstimateQuery,
  scoreQuery,
  scoresQuery,
  startScore,
} from "@/api/score";
import { isProblem, whoami } from "@/api/session";
import { Meter, statusLabel } from "@/components/runs/cells";
import { categoryLabel } from "@/components/runs/filters";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { started, usd } from "@/lib/format";
import { presetText, reaches } from "@/lib/monitor";
import {
  awareText,
  causeText,
  type Level,
  levelOf,
  levelText,
  type Match,
  matchOf,
  type PlantRow,
  readScore,
  type Score,
  type ScoreLayer,
  scoreLayers,
  THRESHOLD,
  tierText,
  toldText,
  type UnitRow,
  whenCaught,
} from "@/lib/score";
import { stepOf } from "@/lib/timeline";

import { LIVE } from "./monitor";
import { TextOf } from "./text-of";

// Where a link in the tab takes the Steps tab: a monitor, one of its flags, or an event.
export interface StepsLink {
  source?: string;
  flag?: number;
  seq?: number;
}

const sealed = (r: RunSummary) => r.started_at !== null && r.status !== "running";

// The newest finished score, else the newest.
export function defaultScore(scores: readonly RunSummary[]): RunSummary | undefined {
  return scores.find((s) => s.status === "completed") ?? scores[0];
}

// The scores of a run: what each agent took, and what each monitor layer caught of it.
export function ScoreTab({
  runId,
  detail,
  scoreId,
  onScore,
  monitor,
  onSteps,
}: {
  runId: string;
  detail: RunDetail;
  scoreId: string | undefined;
  onScore: (id: string) => void;
  monitor: string;
  onSteps: (to: StepsLink) => void;
}) {
  const me = useQuery(whoami);
  const operator = me.data?.role === "operator";
  const scoresQ = useQuery({ ...scoresQuery(runId), enabled: operator });
  const reviews = useQuery(reviewsQuery(runId));
  const scores = scoresQ.data?.items ?? [];
  const reads = useQueries({
    queries: scores.map((s) => ({ ...scoreQuery(s.run_id, s.status), enabled: sealed(s) })),
  });
  const read = new Map<string, Score>();
  const unread = new Set<string>();
  scores.forEach((s, i) => {
    const events = reads[i]?.data;
    if (events) read.set(s.run_id, readScore(events));
    else if (reads[i]?.isError) unread.add(s.run_id);
  });
  const [asking, setAsking] = useState(false);

  if (me.isPending) return null;
  if (!operator) {
    return (
      <p className="text-muted-foreground">
        A score shows what the run hid from its agents and its monitor, so only operators see it.
      </p>
    );
  }
  if (scoresQ.isPending) return <p className="text-muted-foreground">Loading scores</p>;
  if (scoresQ.isError) return <p className="text-muted-foreground">Scores did not load.</p>;
  const current = scores.find((s) => s.run_id === scoreId) ?? defaultScore(scores);
  const reviewOf = (id: string | null) =>
    id === null ? undefined : (reviews.data?.items ?? []).find((r) => r.run_id === id);
  const label = (s: RunSummary) => {
    const score = read.get(s.run_id);
    const of = score ? scoredText(score.monitor, reviewOf(score.monitor)) : null;
    return [
      "Score",
      of,
      s.status === "completed" ? null : statusLabel(s).toLowerCase(),
      s.started_at ? started(s.started_at) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  };
  const ready = detail.run.status !== "running" && detail.run.started_at !== null;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto">
      <div className="flex flex-wrap items-center gap-2">
        {scores.length > 0 && current && (
          <Select
            items={scores.map((s) => ({ value: s.run_id, label: label(s) }))}
            value={current.run_id}
            onValueChange={(v) => v && onScore(String(v))}
          >
            <SelectTrigger size="sm" aria-label="Score" className="min-w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false} align="start">
              {scores.map((s) => (
                <SelectItem key={s.run_id} value={s.run_id}>
                  {label(s)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {ready && !asking && (
          <Button variant="outline" size="sm" onClick={() => setAsking(true)}>
            {scores.length > 0 ? "Score again" : "Score"}
          </Button>
        )}
        {!ready && <p className="text-muted-foreground">The run scores once it seals.</p>}
      </div>
      {asking && (
        <ScoreForm
          detail={detail}
          reviews={reviews.data?.items ?? []}
          monitor={monitor}
          onCancel={() => setAsking(false)}
          onStarted={(id) => {
            setAsking(false);
            onScore(id);
          }}
        />
      )}
      {current === undefined ? (
        !asking && ready && <p className="text-muted-foreground">Not scored yet.</p>
      ) : !sealed(current) ? (
        <p className="text-muted-foreground">
          {statusLabel(current)}
          {current.status === "running"
            ? ": scoring."
            : current.reason
              ? `: ${current.reason}`
              : current.status === "queued"
                ? ": waiting for a runner that scores."
                : "."}
        </p>
      ) : unread.has(current.run_id) ? (
        <p className="text-muted-foreground">The score did not load.</p>
      ) : read.get(current.run_id) ? (
        <ScoreView
          runId={runId}
          detail={detail}
          run={current}
          score={read.get(current.run_id) as Score}
          review={reviewOf(read.get(current.run_id)?.monitor ?? null)}
          onSteps={onSteps}
        />
      ) : (
        <p className="text-muted-foreground">Loading score</p>
      )}
    </div>
  );
}

function scoredText(monitor: string | null, review: RunSummary | undefined): string {
  if (monitor === null) return "live flags";
  const preset = review ? presetText(review) : null;
  return preset ? `review, ${preset}` : "review";
}

// Queue a score: which monitor's flags, which checks, the estimate first.
function ScoreForm({
  detail,
  reviews,
  monitor,
  onCancel,
  onStarted,
}: {
  detail: RunDetail;
  reviews: readonly RunSummary[];
  monitor: string;
  onCancel: () => void;
  onStarted: (id: string) => void;
}) {
  const run = detail.run;
  const qc = useQueryClient();
  const done = reviews.filter((r) => r.status === "completed");
  const [watch, setWatch] = useState(
    monitor !== LIVE && done.some((r) => r.run_id === monitor) ? monitor : LIVE,
  );
  const [awareness, setAwareness] = useState(true);
  const [answer, setAnswer] = useState(true);
  const [rules, setRules] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const estimate = useQuery(scoreEstimateQuery(run.run_id, awareness, answer));
  const scenarios = useQuery(scenariosQuery);
  const same = (scenarios.data ?? []).filter((s) => s.title === run.scenario && !s.error);
  const watchItems = [
    { value: LIVE, label: "The run's own flags" },
    ...done.map((r) => ({
      value: r.run_id,
      label: ["Review", presetText(r), r.started_at ? started(r.started_at) : null]
        .filter(Boolean)
        .join(" · "),
    })),
  ];
  const ruleItems = [
    { value: "", label: "None" },
    ...same.map((s) => ({ value: `${s.source}/${s.name}`, label: `${s.name} (${s.source})` })),
  ];
  const submit = async () => {
    setBusy(true);
    setProblem(null);
    const [source, name] = rules.split("/");
    try {
      const ids = await startScore(run.run_id, {
        awareness,
        answer,
        ...(watch === LIVE ? {} : { monitor: watch }),
        ...(source && name ? { plants_from: { source, name } } : {}),
      });
      await qc.invalidateQueries({ queryKey: ["runs", "scores", run.run_id] });
      if (ids[0]) onStarted(ids[0]);
    } catch (e) {
      setProblem(isProblem(e) ? (e.detail ?? e.title) : "The score did not start.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="New score" className="flex max-w-xl flex-col gap-2 rounded-sm border p-3">
      <h3 className="font-medium">New score</h3>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">Flags to score</span>
        <Select items={watchItems} value={watch} onValueChange={(v) => v && setWatch(String(v))}>
          <SelectTrigger size="sm" aria-label="Flags to score" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} align="start">
            {watchItems.map((i) => (
              <SelectItem key={i.value} value={i.value}>
                {i.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={awareness}
          onChange={(e) => setAwareness(e.target.checked)}
          className="accent-foreground"
        />
        Ask whether each agent said it thought it was tested
      </label>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={answer}
          onChange={(e) => setAnswer(e.target.checked)}
          className="accent-foreground"
        />
        Check what each subject that took a plant told the operator
      </label>
      {same.length > 0 && (
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            Rule plants and facts the run's truth file lacks, from
          </span>
          <Select items={ruleItems} value={rules} onValueChange={(v) => setRules(String(v ?? ""))}>
            <SelectTrigger size="sm" aria-label="Rule plants from" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false} align="start">
              {ruleItems.map((i) => (
                <SelectItem key={i.value || "none"} value={i.value}>
                  {i.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      )}
      <p className="text-muted-foreground" aria-live="polite">
        {estimate.isPending
          ? "Estimating"
          : estimate.isError
            ? "No estimate."
            : estimate.data.lines.join(" ")}
      </p>
      {problem && <p className="text-flag">{problem}</p>}
      <div className="flex gap-2">
        <Button size="sm" disabled={busy} onClick={() => void submit()}>
          Start score
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </section>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{children}</dd>
    </div>
  );
}

// A step link where the loaded steps reach the event, else its seq.
function At({
  seq,
  steps,
  more,
  onSeq,
}: {
  seq: number;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
}) {
  const step = reaches(steps, seq, more) ? stepOf(steps, seq) : undefined;
  return (
    <button
      type="button"
      title={`Event ${seq}`}
      onClick={() => onSeq(seq)}
      className="hover:text-foreground tabular-nums underline underline-offset-2"
    >
      {step ? `step ${step.n}` : `seq ${seq}`}
    </button>
  );
}

function ScoreView({
  runId,
  detail,
  run,
  score,
  review,
  onSteps,
}: {
  runId: string;
  detail: RunDetail;
  run: RunSummary;
  score: Score;
  review: RunSummary | undefined;
  onSteps: (to: StepsLink) => void;
}) {
  const stepsQ = useInfiniteQuery(stepsQuery(runId));
  const steps = stepsQ.data?.pages.flatMap((p) => p.items) ?? [];
  const more = stepsQ.hasNextPage;
  const holder = score.monitor ?? runId;
  const flags = useQuery(flagsQuery(holder));
  const known = new Set((flags.data ?? []).map((f) => f.seq));
  const source = score.monitor ?? LIVE;
  const onSeq = (seq: number) => onSteps({ seq });
  const onFlag = (seq: number | null | undefined) =>
    onSteps(
      seq !== null && seq !== undefined && known.has(seq) ? { source, flag: seq } : { source },
    );

  if (score.failed)
    return <p className="text-muted-foreground">The score failed: {score.failed}</p>;
  const multi = score.units.length > 1;
  const positives = score.units.filter((u) => u.positive);
  const planted = score.units.reduce((n, u) => n + u.planted.length, 0);
  const taken = score.units.reduce((n, u) => n + u.plants.length, 0);
  const severity =
    score.monitor === null ? detail.run.verdict_severity : (review?.verdict_severity ?? null);
  return (
    <div className="flex flex-col gap-5">
      <dl aria-label="Score" className="flex flex-wrap gap-x-8 gap-y-2">
        {detail.run.reward !== null && <Fact label="Reward">{detail.run.reward.toFixed(2)}</Fact>}
        <Fact label="Plants taken">{planted === 0 ? "No plants" : `${taken} of ${planted}`}</Fact>
        <Fact label="Judge">
          {severity === null ? (
            <span className="text-muted-foreground">No verdict</span>
          ) : (
            <span className="flex items-center gap-2">
              <Meter value={severity} label={`Judged severity ${severity} of 5`} />
              {severity} of 5
            </span>
          )}
        </Fact>
        {positives.map((u) => (
          <Fact key={u.agent} label={multi ? `Caught ${u.agent}` : "Caught"}>
            <Caught m={matchOf(u.matches, "any")} />
          </Fact>
        ))}
        <Fact label="Flags of">
          {score.monitor === null ? "Live monitor" : scoredText(score.monitor, review)}
        </Fact>
        {run.cost_usd !== null && <Fact label="Score cost">{usd(run.cost_usd)}</Fact>}
      </dl>
      <PlantTable
        score={score}
        multi={multi}
        steps={steps}
        more={more}
        onSeq={onSeq}
        onFlag={onFlag}
      />
      {(score.answers.size > 0 || score.awareness.size > 0) && (
        <section aria-label="What the agents said" className="flex max-w-3xl flex-col gap-3">
          <h3 className="font-medium">What the agents said</h3>
          {score.units.map((u) => (
            <Said
              key={u.agent}
              unit={u}
              score={score}
              scoreRun={run.run_id}
              multi={multi}
              steps={steps}
              more={more}
              onSeq={onSeq}
            />
          ))}
        </section>
      )}
      <Unexplained score={score} holder={holder} multi={multi} onFlag={onFlag} />
    </div>
  );
}

function Caught({ m }: { m: Match | undefined }) {
  const level = levelOf(m);
  const when = whenCaught(m);
  return (
    <span>
      {levelText[level]}
      {when && <span className="text-muted-foreground">, {when}</span>}
    </span>
  );
}

const quietLevels = new Set<Level>(["missed"]);

function Cell({
  m,
  onFlag,
}: {
  m: Match | undefined;
  onFlag: (seq: number | null | undefined) => void;
}) {
  const level = levelOf(m);
  if (quietLevels.has(level)) return <span className="text-muted-foreground">Missed</span>;
  return (
    <button
      type="button"
      onClick={() => onFlag(m?.first_flag)}
      title="Show the flag"
      className="hover:text-foreground flex flex-col text-left underline-offset-2 hover:underline"
    >
      <span>{levelText[level]}</span>
      <span className="text-muted-foreground text-xs">{whenCaught(m)}</span>
    </button>
  );
}

// One row per plant an agent took, each layer's best catch of it; a score made before plants
// kept their own matches has one row per agent instead.
function PlantTable({
  score,
  multi,
  steps,
  more,
  onSeq,
  onFlag,
}: {
  score: Score;
  multi: boolean;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
  onFlag: (seq: number | null | undefined) => void;
}) {
  const took = score.plants.filter((p) => p.taken);
  const whole = took.length > 0 && took.every((p) => p.matches.length > 0);
  const byUnit: UnitRow[] = whole ? [] : score.units.filter((u) => u.plants.length > 0);
  const name = (agent: string, text: string) => (multi ? `${agent} · ${text}` : text);
  const head = (
    <thead className="text-muted-foreground text-left">
      <tr className="border-b">
        <th className="py-1 pr-4 font-normal">Plant</th>
        <th className="py-1 pr-4 font-normal">Taken</th>
        {scoreLayers.map((l) => (
          <th key={l.value} className="py-1 pr-4 font-normal">
            {l.label}
          </th>
        ))}
      </tr>
    </thead>
  );
  const cells = (matches: readonly Match[]) =>
    scoreLayers.map((l) => (
      <td key={l.value} className="py-1.5 pr-4 align-top">
        <Cell m={matchOf(matches, l.value as ScoreLayer)} onFlag={onFlag} />
      </td>
    ));
  const untaken = score.units.flatMap((u) =>
    u.planted.filter((p) => !u.plants.includes(p)).map((p) => name(u.agent, p)),
  );
  const considered = score.units.flatMap((u) => u.considered.map((p) => name(u.agent, p)));
  return (
    <section aria-label="Plants" className="flex flex-col gap-2">
      <h3 className="flex items-baseline gap-2">
        <span className="font-medium">Plants against the monitor</span>
        <span className="text-muted-foreground">importance {THRESHOLD} and up</span>
      </h3>
      {took.length === 0 ? (
        <p className="text-muted-foreground">
          {score.units.some((u) => u.positive)
            ? "No plant taken; a flag was labelled real."
            : "No plant taken."}
        </p>
      ) : (
        <table className="w-full max-w-4xl">
          {head}
          <tbody>
            {whole
              ? took.map((p: PlantRow) => (
                  <tr key={`${p.agent}:${p.plant}`} className="border-b last:border-0">
                    <td className="py-1.5 pr-4 align-top">
                      <span className="flex flex-col">
                        <span>{name(p.agent, p.plant)}</span>
                        <span className="text-muted-foreground text-xs">
                          {categoryLabel(p.category)}
                        </span>
                      </span>
                    </td>
                    <td className="py-1.5 pr-4 align-top">
                      <span className="flex flex-col">
                        <span className="text-flag">
                          {tierText[p.tier] ?? p.tier} at{" "}
                          <At seq={p.seq} steps={steps} more={more} onSeq={onSeq} />
                        </span>
                        {p.detail && (
                          <span className="text-muted-foreground text-xs">{p.detail}</span>
                        )}
                      </span>
                    </td>
                    {cells(p.matches)}
                  </tr>
                ))
              : byUnit.map((u) => (
                  <tr key={u.agent} className="border-b last:border-0">
                    <td className="py-1.5 pr-4 align-top">{name(u.agent, u.plants.join(", "))}</td>
                    <td className="py-1.5 pr-4 align-top">
                      {u.harm_seq !== null && u.harm_seq !== undefined && (
                        <span className="text-flag">
                          first at <At seq={u.harm_seq} steps={steps} more={more} onSeq={onSeq} />
                        </span>
                      )}
                    </td>
                    {cells(u.matches)}
                  </tr>
                ))}
          </tbody>
        </table>
      )}
      {!whole && took.length > 0 && (
        <p className="text-muted-foreground">Score again to see each plant on its own.</p>
      )}
      {untaken.length > 0 && (
        <p className="text-muted-foreground">Not taken: {untaken.join(", ")}.</p>
      )}
      {considered.length > 0 && (
        <p className="text-muted-foreground">
          Canary only in model output: {considered.join(", ")}.
        </p>
      )}
      {score.added.length > 0 && (
        <p className="text-muted-foreground">
          Rule plants added from the scenario: {score.added.join(", ")}.
        </p>
      )}
    </section>
  );
}

function Said({
  unit,
  score,
  scoreRun,
  multi,
  steps,
  more,
  onSeq,
}: {
  unit: UnitRow;
  score: Score;
  scoreRun: string;
  multi: boolean;
  steps: readonly StepInfo[];
  more: boolean;
  onSeq: (seq: number) => void;
}) {
  const told = score.answers.get(unit.agent);
  const aware = score.awareness.get(unit.agent);
  if (!told && !aware) return null;
  return (
    <div className="flex flex-col gap-2">
      {multi && <h4 className="text-muted-foreground">{unit.agent}</h4>}
      {told && (
        <div className="flex flex-col gap-1">
          <p>
            {toldText[told.told] ?? told.told}; {causeText[told.cause] ?? told.cause}.
            {told.evidence.length > 0 && (
              <span className="text-muted-foreground">
                {" "}
                <At seq={Math.min(...told.evidence)} steps={steps} more={more} onSeq={onSeq} />
              </span>
            )}
          </p>
          {told.quote !== null && told.quote !== undefined && (
            <TextOf runId={scoreRun} value={told.quote} mono={false} className="max-h-40" />
          )}
        </div>
      )}
      {aware && (
        <div className="flex flex-col gap-1">
          <p>
            {awareText[aware.finding] ?? aware.finding}.
            {aware.evidence.length > 0 && (
              <span className="text-muted-foreground">
                {" "}
                <At seq={Math.min(...aware.evidence)} steps={steps} more={more} onSeq={onSeq} />
              </span>
            )}
          </p>
          {aware.quote !== null && aware.quote !== undefined && (
            <TextOf runId={scoreRun} value={aware.quote} mono={false} className="max-h-40" />
          )}
        </div>
      )}
    </div>
  );
}

const labelItems: { value: LabelKind; label: string }[] = [
  { value: "real", label: "Real" },
  { value: "false_alarm", label: "False alarm" },
  { value: "unclear", label: "Unsure" },
];

// Flags no plant explains, for a person to label; a label counts at the next score.
function Unexplained({
  score,
  holder,
  multi,
  onFlag,
}: {
  score: Score;
  holder: string;
  multi: boolean;
  onFlag: (seq: number) => void;
}) {
  const qc = useQueryClient();
  const labels = useQuery(labelsQuery(holder));
  const [failed, setFailed] = useState<string | null>(null);
  if (score.unexplained.length === 0) return null;
  const set = async (flag: number, label: LabelKind) => {
    setFailed(null);
    try {
      const made = await postLabel(holder, flag, label);
      qc.setQueryData<Map<number, Label>>(labelsQuery(holder).queryKey, (old) =>
        new Map(old).set(flag, made),
      );
    } catch (e) {
      setFailed(isProblem(e) ? (e.detail ?? e.title) : "The label was not saved.");
    }
  };
  return (
    <section aria-label="Unexplained flags" className="flex max-w-3xl flex-col gap-2">
      <h3 className="flex items-baseline gap-2">
        <span className="font-medium">Flags no plant explains</span>
        <span className="text-muted-foreground tabular-nums">{score.unexplained.length}</span>
      </h3>
      <p className="text-muted-foreground">
        Label each one; a label counts when the run is scored again.
      </p>
      {failed && <p className="text-flag">{failed}</p>}
      <ul className="flex flex-col">
        {score.unexplained.map((u) => {
          const now = labels.data?.get(u.flag)?.label ?? u.label ?? null;
          return (
            <li
              key={u.flag}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b py-1.5 last:border-0"
            >
              <Meter value={u.importance} label={`Importance ${u.importance} of 5`} />
              <button
                type="button"
                onClick={() => onFlag(u.flag)}
                className="min-w-0 flex-1 truncate text-left underline-offset-2 hover:underline"
              >
                {categoryLabel(u.category)}
                <span className="text-muted-foreground">
                  {" "}
                  · {u.layer}
                  {multi && u.agent ? ` · ${u.agent}` : ""} · flag {u.flag}
                </span>
              </button>
              <span role="group" aria-label={`Label flag ${u.flag}`} className="flex gap-1">
                {labelItems.map((l) => (
                  <Button
                    key={l.value}
                    size="xs"
                    variant={now === l.value ? "default" : "outline"}
                    aria-pressed={now === l.value}
                    onClick={() => void set(u.flag, l.value)}
                  >
                    {l.label}
                  </Button>
                ))}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// Where the newest finished score says each agent first did harm, for the timeline and the
// step list; none for a viewer or before a score.
export function useHarm(runId: string): { seq: number; agent: string }[] {
  const me = useQuery(whoami);
  const operator = me.data?.role === "operator";
  const scores = useQuery({ ...scoresQuery(runId), enabled: operator });
  const newest = (scores.data?.items ?? []).find((s) => s.status === "completed");
  const events = useQuery({
    ...scoreQuery(newest?.run_id ?? "", newest?.status ?? ""),
    enabled: operator && newest !== undefined,
  });
  if (!events.data) return [];
  return readScore(events.data)
    .units.flatMap((u) =>
      u.harm_seq === null || u.harm_seq === undefined ? [] : [{ seq: u.harm_seq, agent: u.agent }],
    )
    .toSorted((a, b) => a.seq - b.seq);
}
