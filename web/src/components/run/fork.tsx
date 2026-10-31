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
import { DEFAULT, Field, Pick, problemText } from "@/components/form";
import { up, useRunners } from "@/components/health";
import { OversightNote } from "@/components/oversight-note";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { changesText, draftText, EMPTY_DRAFT, readDraft } from "@/lib/budget";
import { confirmWhy, oversightNote } from "@/lib/launch";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

import { agentsOf } from "./controls";
import { sealedScenario } from "./review";

const MAX_SAMPLES = 100;

const recorded = (detail: RunDetail): Oversight | undefined => {
  const value = detail.started?.data["oversight"];
  return oversights.find((o) => o === value);
};

// Replay the run up to this step and go on live in a new run, for an operator on a sealed run.
export function ForkHere({ detail, step }: { detail: RunDetail; step: StepInfo }) {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  if (!operator || !sealedScenario(detail.run) || !step.agent_id) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="xs" className="ml-auto" />}>
        Fork from here
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        {open && <ForkForm detail={detail} step={step} onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function ForkForm({
  detail,
  step,
  onDone,
}: {
  detail: RunDetail;
  step: StepInfo;
  onDone: () => void;
}) {
  const navigate = useNavigate();
  const runId = detail.run.run_id;
  const agent = step.agent_id ?? "";
  const multi = agentsOf(detail).length > 1;
  const [keepStep, setKeepStep] = useState(true);
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
      <DialogHeader>
        <DialogTitle>Fork this run</DialogTitle>
        <DialogDescription>
          Replays the recorded steps up to the cut at no model cost, then goes on live in a new run.
          This run never changes.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Cut" className="sm:col-span-2">
          <Pick
            label="Cut"
            value={keepStep ? "after" : "before"}
            items={[
              { value: "after", label: `After ${who}: keep it, go live from the next` },
              { value: "before", label: `Before ${who}: make it again live` },
            ]}
            onChange={(v) => setKeepStep(v !== "before")}
          />
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
            <Input
              type="number"
              min={1}
              max={MAX_SAMPLES}
              value={samples}
              className="h-7 md:text-sm"
              onChange={(e) => {
                const n = Math.round(Number(e.currentTarget.value));
                setSamples(Number.isFinite(n) ? Math.min(MAX_SAMPLES, Math.max(1, n)) : 1);
              }}
            />
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
