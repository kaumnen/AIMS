import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import {
  type ForkAsk,
  forkEstimateQuery,
  forkFields,
  type Oversight,
  oversights,
  startFork,
} from "@/api/fork";
import { modelsQuery, type Preset, presets } from "@/api/launch";
import type { RunDetail, StepInfo } from "@/api/run";
import { isProblem } from "@/api/session";
import { BudgetSection } from "@/components/budget";
import { CountInput, DEFAULT, Field, Pick, problemText } from "@/components/form";
import { up, useRunners } from "@/components/health";
import { OversightNote } from "@/components/oversight-note";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { changesText, draftText, EMPTY_DRAFT, readDraft } from "@/lib/budget";
import { confirmWhy, oversightNote } from "@/lib/launch";
import { cn } from "@/lib/utils";

const MAX_SAMPLES = 100;

const recorded = (detail: RunDetail): Oversight | undefined => {
  const value = detail.started?.data["oversight"];
  return oversights.find((o) => o === value);
};

// The fork dialog's form, in its own chunk: the run page shows only the button that opens it.
export default function ForkForm({
  detail,
  step,
  multi,
  onDone,
}: {
  detail: RunDetail;
  step: StepInfo;
  multi: boolean;
  onDone: () => void;
}) {
  const navigate = useNavigate();
  const runId = detail.run.run_id;
  const agent = step.agent_id ?? "";
  // A fork keeps the step only when it can replay it: the run gave each of its tool calls a result.
  const whole = step.n <= (detail.replayable[agent] ?? step.n);
  const [keepStep, setKeepStep] = useState(whole);
  const [inject, setInject] = useState("");
  const [profile, setProfile] = useState(DEFAULT);
  const [keepOthers, setKeepOthers] = useState(false);
  const [oversight, setOversight] = useState(DEFAULT);
  const [monitor, setMonitor] = useState(DEFAULT);
  const [runner, setRunner] = useState(DEFAULT);
  const [samples, setSamples] = useState(1);
  const [parallel, setParallel] = useState<number | null>(null);
  const [audit, setAudit] = useState<"kernel" | "snapshot">("kernel");
  const [strict, setStrict] = useState(false);
  const [keep, setKeep] = useState(false);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const runners = useRunners().filter((r) => up(r) && r.kinds.includes("scenario"));
  const models = useQuery(modelsQuery);

  const cut = keepStep ? step.n : step.n - 1;
  const { budget, problems } = readDraft(draft);
  const ask: ForkAsk = {
    ...(multi ? { at: `${agent}:${cut}` } : { step: cut }),
    keepOthers,
    samples,
    profile: profile || undefined,
    monitor: (monitor || undefined) as Preset | undefined,
    budget,
  };
  const fields = forkFields(ask);
  const key = JSON.stringify(fields);
  const estimate = useQuery(forkEstimateQuery(runId, ask));
  const why = estimate.data && !estimate.isPlaceholderData ? confirmWhy(estimate.data) : null;
  const confirm = confirmed === key;
  const preset = estimate.data?.parts[0]?.monitor_preset;
  const before = recorded(detail);
  const caps = estimate.data?.parts[0]?.caps;
  const kept = changesText(detail.budget, multi);
  const unread = Object.keys(problems).length > 0;
  const note = oversightNote(
    ((oversight || before) as Oversight | undefined) ?? null,
    monitor || preset || undefined,
  );

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const out = await startFork(runId, {
        ...fields,
        confirm,
        audit,
        strict,
        keep,
        ...(inject.trim() ? { inject: inject.trim() } : {}),
        ...(oversight ? { oversight: oversight as Oversight } : {}),
        ...(runner ? { runner } : {}),
        ...(parallel && samples > 1 ? { parallel } : {}),
      });
      onDone();
      const [first] = out.run_ids;
      if (out.run_ids.length === 1 && first) {
        void navigate({ to: "/runs/$runId", params: { runId: first } });
      } else {
        const scenario = detail.run.scenario;
        void navigate({ to: "/runs", search: scenario ? { scenario } : {} });
      }
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The fork did not start.");
      setBusy(false);
    }
  };

  const who = multi ? `${agent}'s step ${step.n}` : `step ${step.n}`;
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Cut" className="sm:col-span-2">
          <Pick
            label="Cut"
            value={keepStep ? "after" : "before"}
            items={[
              ...(whole
                ? [{ value: "after", label: `After ${who}: keep it, go live from the next` }]
                : []),
              { value: "before", label: `Before ${who}: make it again live` },
            ]}
            onChange={(v) => setKeepStep(v !== "before")}
          />
          {!whole && (
            <span className="text-muted-foreground">
              The run ended before the tool calls of {who} returned, so a fork cannot keep it.
            </span>
          )}
        </Field>
        <Field label="Message to the agent at the fork" className="sm:col-span-2">
          <Textarea
            value={inject}
            rows={3}
            placeholder="None"
            onChange={(e) => setInject(e.currentTarget.value)}
          />
        </Field>
        <Field label="Profile after the fork">
          <Pick
            label="Profile"
            value={profile}
            items={[
              { value: DEFAULT, label: "As recorded" },
              ...(models.data?.profiles ?? []).map((p) => ({
                value: p.name,
                label: `${p.name} · ${p.model}`,
              })),
            ]}
            onChange={setProfile}
          />
        </Field>
        <Field label="Monitor">
          <Pick
            label="Monitor"
            value={monitor}
            items={[
              { value: DEFAULT, label: preset ? `From config (${preset})` : "From config" },
              ...presets.map((p) => ({ value: p, label: p })),
            ]}
            onChange={setMonitor}
          />
        </Field>
        <Field label="Oversight">
          <Pick
            label="Oversight"
            value={oversight}
            items={[
              { value: DEFAULT, label: before ? `As recorded (${before})` : "As recorded" },
              ...oversights.map((o) => ({ value: o, label: o })),
            ]}
            onChange={setOversight}
          />
        </Field>
        {note && <OversightNote note={note} className="sm:col-span-2" />}
        <Field label="Runner">
          <Pick
            label="Runner"
            value={runner}
            items={[
              { value: DEFAULT, label: "Any runner" },
              ...runners.map((r) => ({
                value: r.name,
                label: `${r.name} · ${r.free} of ${r.slots} free`,
              })),
            ]}
            onChange={setRunner}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Samples">
            <CountInput value={samples} max={MAX_SAMPLES} onChange={setSamples} />
          </Field>
          <Field label="At once">
            <Input
              type="number"
              min={1}
              placeholder="All"
              value={parallel ?? ""}
              disabled={samples === 1}
              className="h-7 md:text-sm"
              onChange={(e) => {
                const n = Math.round(Number(e.currentTarget.value));
                setParallel(e.currentTarget.value && n >= 1 ? n : null);
              }}
            />
          </Field>
        </div>
        <Field label="Audit">
          <Pick
            label="Audit"
            value={audit}
            items={[
              { value: "kernel", label: "Kernel trace and snapshots" },
              { value: "snapshot", label: "Snapshots only" },
            ]}
            onChange={(v) => setAudit(v === "snapshot" ? "snapshot" : "kernel")}
          />
        </Field>
        <BudgetSection
          caps={caps}
          draft={draft}
          problems={problems}
          summary={draftText(budget, multi) ?? (kept ? `As this run: ${kept}` : "Scenario caps")}
          note={`${kept ? `This run ran with ${kept}. ` : ""}The replayed steps keep this run's caps; changes apply from the cut.`}
          onChange={setDraft}
        />
        <div className="flex flex-col gap-1.5 sm:col-span-2">
          {multi && (
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={keepOthers}
                onChange={(e) => setKeepOthers(e.target.checked)}
                className="accent-foreground"
              />
              The other agents keep their recorded answers until their own inputs change
            </label>
          )}
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={strict}
              onChange={(e) => setStrict(e.target.checked)}
              className="accent-foreground"
            />
            Stop at the first tool output that differs from the recording
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={keep}
              onChange={(e) => setKeep(e.target.checked)}
              className="accent-foreground"
            />
            Keep the containers and volumes afterwards
          </label>
        </div>
      </div>
      <section aria-label="Cost estimate" aria-live="polite" className="flex flex-col gap-1">
        <h3 className="font-medium">Cost</h3>
        {estimate.isPending ? (
          <p className="text-muted-foreground">Estimating</p>
        ) : estimate.isError ? (
          <p className="text-muted-foreground">
            No estimate: {isProblem(estimate.error) ? problemText(estimate.error) : "it failed."}
          </p>
        ) : (
          <>
            <ul className={cn("flex flex-col gap-0.5", estimate.isPlaceholderData && "opacity-60")}>
              {estimate.data.lines.slice(1).map((line) => (
                <li key={line} className={cn(line.startsWith("Total") && "font-medium")}>
                  {line}
                </li>
              ))}
            </ul>
            {estimate.data.warnings.map((w) => (
              <p key={w} className="font-medium">
                {w}
              </p>
            ))}
          </>
        )}
        {why && (
          <label className="flex items-start gap-2 font-medium">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => setConfirmed(e.target.checked ? key : null)}
              className="accent-foreground mt-0.5"
            />
            {why} Start anyway.
          </label>
        )}
      </section>
      {problem && <p className="text-flag">{problem}</p>}
      <DialogFooter>
        <Button variant="ghost" size="sm" onClick={onDone}>
          Cancel
        </Button>
        <Button
          size="sm"
          disabled={
            busy ||
            unread ||
            estimate.isPending ||
            estimate.isPlaceholderData ||
            (why !== null && !confirm)
          }
          onClick={() => void submit()}
        >
          {samples === 1 ? "Start fork" : `Start ${samples} forks`}
        </Button>
      </DialogFooter>
    </>
  );
}
