import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { type Oversight, oversights } from "@/api/fork";
import {
  checkModels,
  modelsQuery,
  type Preset,
  presets,
  type ProfileInfo,
  runEstimateQuery,
  startRuns,
} from "@/api/launch";
import { type ScenarioInfo, scenariosQuery } from "@/api/scenarios";
import { isProblem, whoami } from "@/api/session";
import { DEFAULT, Field, Pick, problemText } from "@/components/form";
import { up, useRunners } from "@/components/health";
import { useLive } from "@/components/live";
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
import { started } from "@/lib/format";
import { checkProblem, checkState, confirmWhy, oversightNote } from "@/lib/launch";
import { cn } from "@/lib/utils";

const MAX_SAMPLES = 100;

export function NewRun() {
  const me = useQuery(whoami);
  const [open, setOpen] = useState(false);
  if (me.data?.role !== "operator") return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" />}>New run</DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        {open && <NewRunForm onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  );
}

const keyOf = (s: ScenarioInfo) => `${s.source}/${s.name}`;

function NewRunForm({ onDone }: { onDone: () => void }) {
  const navigate = useNavigate();
  const scenarios = useQuery(scenariosQuery);
  const runnable = (scenarios.data ?? []).filter((s) => !s.error);
  const [chosen, setChosen] = useState(DEFAULT);
  const scenario = runnable.find((s) => keyOf(s) === chosen) ?? runnable[0];
  const [variant, setVariant] = useState(DEFAULT);
  const [profile, setProfile] = useState(DEFAULT);
  const [monitor, setMonitor] = useState(DEFAULT);
  const [oversight, setOversight] = useState(DEFAULT);
  const [samples, setSamples] = useState(1);
  const [parallel, setParallel] = useState<number | null>(null);
  const [runner, setRunner] = useState(DEFAULT);
  const [audit, setAudit] = useState<"kernel" | "snapshot">("kernel");
  const [score, setScore] = useState(false);
  const [keep, setKeep] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const runners = useRunners().filter((r) => up(r) && r.kinds.includes("scenario"));

  const estimate = useQuery({
    ...runEstimateQuery({
      source: scenario?.source ?? "",
      name: scenario?.name ?? "",
      variant: variant || undefined,
      samples,
      profile: profile || undefined,
      monitor: (monitor || undefined) as Preset | undefined,
    }),
    enabled: scenario !== undefined,
  });

  if (scenarios.isPending) return <p className="text-muted-foreground">Loading scenarios</p>;
  if (scenarios.isError) return <p>Scenarios did not load.</p>;
  if (scenario === undefined) {
    return (
      <DialogHeader>
        <DialogTitle>New run</DialogTitle>
        <DialogDescription>No scenario loads. Fix or add one under Scenarios.</DialogDescription>
      </DialogHeader>
    );
  }

  const effective = monitor || estimate.data?.parts[0]?.monitor_preset || undefined;
  const note = oversightNote(
    (oversight || scenario.oversight) as Oversight | null,
    effective ?? undefined,
  );
  const why = estimate.data ? confirmWhy(estimate.data) : null;
  const needsYes = why !== null && !confirm;

  const pickScenario = (key: string) => {
    setChosen(key);
    setVariant(DEFAULT);
    setConfirm(false);
  };
  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const out = await startRuns({
        scenario: { source: scenario.source, name: scenario.name },
        samples,
        audit,
        keep,
        score,
        confirm,
        ...(variant ? { variant } : {}),
        ...(profile ? { profile } : {}),
        ...(monitor ? { monitor: monitor as Preset } : {}),
        ...(oversight ? { oversight: oversight as Oversight } : {}),
        ...(runner ? { runner } : {}),
        ...(parallel && samples > 1 ? { parallel } : {}),
      });
      onDone();
      const [first] = out.run_ids;
      if (out.run_ids.length === 1 && first) {
        void navigate({ to: "/runs/$runId", params: { runId: first } });
      } else {
        void navigate({ to: "/runs", search: { scenario: scenario.title ?? scenario.name } });
      }
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The run did not start.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>New run</DialogTitle>
        <DialogDescription>
          Queues the scenario on a runner. The cost estimate below updates as you change it.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Scenario" className="sm:col-span-2">
          <Pick
            label="Scenario"
            value={keyOf(scenario)}
            items={runnable.map((s) => ({
              value: keyOf(s),
              label: `${s.title ?? s.name} (${s.source})`,
            }))}
            onChange={pickScenario}
          />
          <span className="text-muted-foreground">
            {scenario.agents === 1 ? "1 agent" : `${scenario.agents ?? "?"} agents`}
            {scenario.variants.length > 0 &&
              ` · ${scenario.variants.length} ${scenario.variants.length === 1 ? "variant" : "variants"}`}
          </span>
        </Field>
        {note && <OversightNote note={note} className="sm:col-span-2" />}
        {scenario.variants.length > 0 && (
          <Field label="Variant">
            <Pick
              label="Variant"
              value={variant}
              items={[
                { value: DEFAULT, label: "None" },
                ...scenario.variants.map((v) => ({ value: v, label: v })),
              ]}
              onChange={setVariant}
            />
          </Field>
        )}
        <ProfileField scenario={scenario} value={profile} onChange={setProfile} />
        <Field label="Monitor">
          <Pick
            label="Monitor"
            value={monitor}
            items={[
              {
                value: DEFAULT,
                label: estimate.data?.parts[0]?.monitor_preset
                  ? `From config (${estimate.data.parts[0].monitor_preset})`
                  : "From config",
              },
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
              {
                value: DEFAULT,
                label: scenario.oversight
                  ? `From the scenario (${scenario.oversight})`
                  : "From the scenario",
              },
              ...oversights.map((o) => ({ value: o, label: o })),
            ]}
            onChange={setOversight}
          />
        </Field>
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
                setConfirm(false);
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
        <div className="flex flex-col gap-1.5 sm:col-span-2">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={score}
              onChange={(e) => setScore(e.target.checked)}
              className="accent-foreground"
            />
            Score each run against its truth file when it ends
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
              onChange={(e) => setConfirm(e.target.checked)}
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
        <Button size="sm" disabled={busy || needsYes} onClick={() => void submit()}>
          {samples === 1 ? "Start run" : `Start ${samples} runs`}
        </Button>
      </DialogFooter>
    </>
  );
}

function ProfileField({
  scenario,
  value,
  onChange,
}: {
  scenario: ScenarioInfo;
  value: string;
  onChange: (value: string) => void;
}) {
  const models = useQuery(modelsQuery);
  const profiles = models.data?.profiles ?? [];
  const using = value ? [value] : scenario.profiles;
  const shown = profiles.filter((p) => using.includes(p.name));
  return (
    <div className="flex flex-col gap-1">
      <Field label="Profile">
        <Pick
          label="Profile"
          value={value}
          items={[
            { value: DEFAULT, label: "From the scenario" },
            ...profiles.map((p) => ({ value: p.name, label: `${p.name} · ${p.model}` })),
          ]}
          onChange={onChange}
        />
      </Field>
      <ProfileChecks profiles={shown} />
    </div>
  );
}

const STATE_TEXT = {
  ok: "ok",
  warn: "works with warnings",
  fail: "failed",
  running: "checking",
  none: "never checked",
} as const;

function ProfileChecks({ profiles }: { profiles: ProfileInfo[] }) {
  const qc = useQueryClient();
  const live = useLive();
  const [checking, setChecking] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (checking === null) return;
    return live.on("run", ({ run }) => {
      if (run.run_id !== checking || run.state !== "done") return;
      setChecking(null);
      void qc.invalidateQueries({ queryKey: modelsQuery.queryKey });
    });
  }, [live, qc, checking]);
  if (profiles.length === 0) return null;
  const check = async () => {
    setProblem(null);
    try {
      setChecking(await checkModels(profiles.map((p) => p.name)));
    } catch (e) {
      setProblem(isProblem(e) ? (e.detail ?? e.title) : "The check did not start.");
    }
  };
  return (
    <div className="text-muted-foreground flex flex-col gap-0.5">
      {profiles.map((p) => {
        const state = checking ? "running" : checkState(p.check);
        const bad = checking ? null : checkProblem(p.check);
        return (
          <p key={p.name}>
            <span className={cn(state === "fail" && "text-foreground font-medium")}>
              {p.name}: {STATE_TEXT[state]}
            </span>
            {p.check && !checking && ` · ${started(p.check.started_at)}`}
            {bad && (
              <span className="block truncate" title={bad}>
                {bad}
              </span>
            )}
            {p.runners.length === 0 && p.envs.length > 0 && (
              <span className="text-foreground block">
                No connected runner has {p.envs.join(" and ")}.
              </span>
            )}
          </p>
        );
      })}
      <span className="flex items-center gap-2">
        <Button
          variant="outline"
          size="xs"
          disabled={checking !== null}
          onClick={() => void check()}
        >
          Check
        </Button>
        <span>a few small paid calls per profile</span>
      </span>
      {problem && <span className="text-flag">{problem}</span>}
    </div>
  );
}
