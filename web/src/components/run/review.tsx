import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { modelsQuery } from "@/api/launch";
import {
  type ReviewAsk,
  reviewEstimateQuery,
  reviewFields,
  type ReviewMode,
  type ReviewPreset,
  reviewPresets,
  startReview,
} from "@/api/review";
import { reviewsQuery, type RunSummary } from "@/api/runs";
import { isProblem } from "@/api/session";
import { DEFAULT, Field, Pick, problemText } from "@/components/form";
import { up, useRunners } from "@/components/health";
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
import { usd } from "@/lib/format";
import { confirmWhy } from "@/lib/launch";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

const MAX_SAMPLES = 100;

const modes: { value: ReviewMode; label: string }[] = [
  { value: "full", label: "Every layer" },
  { value: "signals_only", label: "Signals only, no model" },
  { value: "judge_only", label: "Judge only" },
];

// A sealed scenario run, which the monitor can watch again and a fork can replay.
export function sealedScenario(run: RunSummary): boolean {
  return run.kind === "scenario" && run.started_at !== null && run.status !== "running";
}

export function NewReview({
  run,
  onStarted,
}: {
  run: RunSummary;
  onStarted: (id: string) => void;
}) {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  if (!operator || !sealedScenario(run)) return null;
  const done = (id?: string) => {
    setOpen(false);
    if (id) onStarted(id);
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="sm" />}>Review</DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        {open && <ReviewForm runId={run.run_id} onDone={done} />}
      </DialogContent>
    </Dialog>
  );
}

function ReviewForm({ runId, onDone }: { runId: string; onDone: (id?: string) => void }) {
  const qc = useQueryClient();
  const [preset, setPreset] = useState(DEFAULT);
  const [mode, setMode] = useState<ReviewMode>("full");
  const [profile, setProfile] = useState(DEFAULT);
  const [budget, setBudget] = useState("");
  const [samples, setSamples] = useState(1);
  const [parallel, setParallel] = useState<number | null>(null);
  const [runner, setRunner] = useState(DEFAULT);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const runners = useRunners().filter((r) => up(r) && r.kinds.includes("monitor"));
  const models = useQuery(modelsQuery);

  const cap = Number(budget);
  const ask: ReviewAsk = {
    preset: (preset || undefined) as ReviewPreset | undefined,
    mode,
    profile: profile || undefined,
    budget: budget !== "" && Number.isFinite(cap) && cap >= 0 ? cap : undefined,
    samples,
  };
  const fields = reviewFields(ask);
  const key = JSON.stringify(fields);
  const base = useQuery(reviewEstimateQuery(runId, { mode: "full", samples: 1 }));
  const estimate = useQuery(reviewEstimateQuery(runId, ask));
  const why = estimate.data && !estimate.isPlaceholderData ? confirmWhy(estimate.data) : null;
  const confirm = confirmed === key;
  const signals = mode === "signals_only";

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const out = await startReview(runId, {
        ...fields,
        confirm,
        ...(runner ? { runner } : {}),
        ...(parallel && samples > 1 ? { parallel } : {}),
      });
      void qc.invalidateQueries({ queryKey: reviewsQuery(runId).queryKey });
      onDone(out.run_ids[0]);
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The review did not start.");
      setBusy(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>Review this run</DialogTitle>
        <DialogDescription>
          Runs the monitor again over the recorded events, into a new monitor run. Its actions are
          simulated; this run never changes.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Layers">
          <Pick
            label="Layers"
            value={mode}
            items={modes}
            onChange={(v) => setMode(modes.find((m) => m.value === v)?.value ?? "full")}
          />
        </Field>
        {!signals && (
          <Field label="Monitor">
            <Pick
              label="Monitor"
              value={preset}
              items={[
                {
                  value: DEFAULT,
                  label: base.data ? `From config (${base.data.preset})` : "From config",
                },
                ...reviewPresets.map((p) => ({ value: p, label: p })),
              ]}
              onChange={setPreset}
            />
          </Field>
        )}
        {!signals && (
          <Field label="Profile for every model layer" className="sm:col-span-2">
            <Pick
              label="Profile"
              value={profile}
              items={[
                { value: DEFAULT, label: "From config" },
                ...(models.data?.profiles ?? []).map((p) => ({
                  value: p.name,
                  label: `${p.name} · ${p.model}`,
                })),
              ]}
              onChange={setProfile}
            />
          </Field>
        )}
        {!signals && (
          <Field label="Spend cap per review, USD">
            <Input
              type="number"
              min={0}
              step="0.5"
              placeholder={
                base.data?.ceiling_usd == null
                  ? "From config"
                  : `From config, ${usd(base.data.ceiling_usd)}`
              }
              value={budget}
              className="h-7 md:text-sm"
              onChange={(e) => setBudget(e.currentTarget.value)}
            />
          </Field>
        )}
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
          <ul className={cn("flex flex-col gap-0.5", estimate.isPlaceholderData && "opacity-60")}>
            {estimate.data.lines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
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
        <Button variant="ghost" size="sm" onClick={() => onDone()}>
          Cancel
        </Button>
        <Button
          size="sm"
          disabled={
            busy || estimate.isPending || estimate.isPlaceholderData || (why !== null && !confirm)
          }
          onClick={() => void submit()}
        >
          {samples === 1 ? "Start review" : `Start ${samples} reviews`}
        </Button>
      </DialogFooter>
    </>
  );
}
