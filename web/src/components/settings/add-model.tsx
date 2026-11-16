import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import {
  type AddInfo,
  addModel,
  type Backend,
  type LookupAsk,
  lookupModel,
  type ModelAdded,
  type ModelDraft,
  type ModelRole,
  modelsQuery,
  type ProfileInfo,
} from "@/api/launch";
import { isProblem } from "@/api/session";
import { CONFIG_DEPENDENT } from "@/api/settings";
import { DEFAULT, Field, Pick, problemText } from "@/components/form";
import { useLive } from "@/components/live";
import { CHECK_TEXT } from "@/components/model-check";
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
import { checkState } from "@/lib/launch";
import { cn } from "@/lib/utils";

// The fields as typed. `server` is DEFAULT for a base URL, else the profile whose server to use;
// `backend` is DEFAULT to keep the one the draft gets.
export interface Ask {
  server: string;
  baseUrl: string;
  keyEnv: string;
  model: string;
  name: string;
  litellm: string;
  backend: string;
}

// What the dialog holds, kept while it is closed so a typed field or an edited text survives.
export interface AddForm {
  ask: Ask;
  // the draft and the fields it was made from
  drafted: { ask: Ask; draft: ModelDraft } | null;
  // the profile text as edited, null while it is the draft's
  text: string | null;
  roles: ModelRole[];
  added: ModelAdded | null;
}

export const NEW_FORM: AddForm = {
  ask: { server: DEFAULT, baseUrl: "", keyEnv: "", model: "", name: "", litellm: "", backend: "" },
  drafted: null,
  text: null,
  roles: [],
  added: null,
};

const ROLES: readonly ModelRole[] = ["agent", "sweep", "judge"];

const ROLE_TEXT: Record<ModelRole, string> = {
  agent: "runs a scenario's agents",
  sweep: "reads a run as it goes",
  judge: "gives the verdict after a run",
};

const BACKENDS: { value: string; label: string }[] = [
  { value: DEFAULT, label: "As drafted" },
  { value: "chat", label: "chat (Chat Completions)" },
  { value: "responses", label: "responses (Responses API)" },
];

export function toLookup(a: Ask): LookupAsk {
  const name = a.name.trim();
  const litellm = a.litellm.trim();
  const key = a.keyEnv.trim();
  return {
    model: a.model.trim(),
    ...(a.server === DEFAULT
      ? { base_url: a.baseUrl.trim(), ...(key ? { key_env: key } : {}) }
      : { from: a.server }),
    ...(name ? { name } : {}),
    ...(litellm ? { litellm } : {}),
    ...(a.backend ? { backend: a.backend as Backend } : {}),
  };
}

const sameAsk = (a: Ask, b: Ask) => JSON.stringify(toLookup(a)) === JSON.stringify(toLookup(b));

const ready = (a: Ask) =>
  a.model.trim() !== "" && (a.server !== DEFAULT || a.baseUrl.trim() !== "");

// A word the shell takes as written.
const word = (s: string) => (/^[\w./:@-]+$/.test(s) ? s : `'${s.replaceAll("'", "'\\''")}'`);

export function command(q: LookupAsk, roles: readonly ModelRole[], check: boolean): string {
  const flag = (name: string, value: string | null | undefined) =>
    value ? [`--${name} ${word(value)}`] : [];
  return [
    "aims models add",
    word(q.model),
    ...flag("base-url", q.base_url),
    ...flag("key-env", q.key_env),
    ...flag("from", q.from),
    ...flag("name", q.name),
    ...flag("litellm", q.litellm),
    ...flag("backend", q.backend),
    ...roles.map((r) => `--role ${r}`),
    ...(check ? [] : ["--no-check"]),
    "--write",
  ].join(" ");
}

// The text's `reasoning` line, to tell whether an edit set it.
const reasoningLine = (text: string) => /^[ \t]*reasoning[ \t]*=.*$/m.exec(text)?.[0].trim();

// The check replaces the drafted reasoning with what it saw, unless the text set its own.
export const measures = (draft: ModelDraft, text: string) =>
  draft.guessed && reasoningLine(text) === reasoningLine(draft.toml);

// A server note as a sentence.
const sentence = (s: string) =>
  `${s.charAt(0).toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}`;

const failed = (e: unknown) => (isProblem(e) ? problemText(e) : "The request failed.");

// Drafts a profile for a model on any server and adds it to the project's config file. Nothing
// is saved until Add.
export function AddModel({
  open,
  onOpenChange,
  profiles,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profiles: readonly ProfileInfo[];
}) {
  const [form, setForm] = useState<AddForm>(NEW_FORM);
  // A finished add starts over when the dialog opens again, by its button or a link.
  const [shown, setShown] = useState(open);
  if (open !== shown) {
    setShown(open);
    if (open && form.added !== null) setForm(NEW_FORM);
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger render={<Button variant="outline" size="xs" />}>Add a model</DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        {open &&
          (form.added === null ? (
            <Drafting form={form} setForm={setForm} profiles={profiles} />
          ) : (
            <Added added={form.added} roles={form.roles} />
          ))}
      </DialogContent>
    </Dialog>
  );
}

function Drafting({
  form,
  setForm,
  profiles,
}: {
  form: AddForm;
  setForm: React.Dispatch<React.SetStateAction<AddForm>>;
  profiles: readonly ProfileInfo[];
}) {
  const { ask, drafted } = form;
  const [asking, setAsking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Each lookup has the server read a server's list of models, so a second submit before the
  // page shows the first asks nothing.
  const busy = useRef(false);
  const set = (change: Partial<Ask>) => setForm((f) => ({ ...f, ask: { ...f.ask, ...change } }));

  const draft = async (next: Ask) => {
    if (!ready(next) || busy.current) return;
    busy.current = true;
    setAsking(true);
    setProblem(null);
    try {
      const found = await lookupModel(toLookup(next));
      // what was typed meanwhile stays; the draft says it changed
      setForm((f) => ({
        ...f,
        drafted: { ask: next, draft: found },
        text: null,
        roles: f.roles.filter((r) => found.fits.includes(r)),
      }));
    } catch (e) {
      setProblem(failed(e));
    } finally {
      busy.current = false;
      setAsking(false);
    }
  };
  // a pick from the draft's suggestions drafts again with it
  const redraft = (change: Partial<Ask>) => {
    const next = { ...ask, ...change };
    set(change);
    void draft(next);
  };

  const servers = [
    { value: DEFAULT, label: "A base URL" },
    ...profiles.map((p) => ({ value: p.name, label: `The server of profile ${p.name}` })),
  ];
  const edited = form.text !== null;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <form
        className="flex min-w-0 flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void draft(ask);
        }}
      >
        <DialogHeader>
          <DialogTitle>Add a model</DialogTitle>
          <DialogDescription>
            The control plane reads the server&apos;s list of models with no key and drafts a
            profile. Nothing is saved until you add it.
          </DialogDescription>
        </DialogHeader>
        {profiles.length > 0 && (
          <Field label="Server">
            <Pick
              label="Server"
              value={ask.server}
              items={servers}
              onChange={(server) => set({ server })}
            />
          </Field>
        )}
        {ask.server === DEFAULT && (
          <div className="grid gap-3 sm:grid-cols-[3fr_2fr]">
            <Field label="Base URL">
              <Input
                autoFocus
                value={ask.baseUrl}
                inputMode="url"
                placeholder="https://api.example.com/v1"
                autoComplete="off"
                spellCheck={false}
                maxLength={2000}
                onChange={(e) => set({ baseUrl: e.target.value })}
              />
            </Field>
            <Field label="Key env var (optional)">
              <Input
                value={ask.keyEnv}
                placeholder="EXAMPLE_API_KEY"
                autoComplete="off"
                spellCheck={false}
                maxLength={100}
                onChange={(e) => set({ keyEnv: e.target.value })}
              />
            </Field>
          </div>
        )}
        <Field label="Model id">
          <Input
            value={ask.model}
            placeholder="example/model-name"
            autoComplete="off"
            spellCheck={false}
            maxLength={200}
            list="add-model-ids"
            onChange={(e) => set({ model: e.target.value })}
          />
        </Field>
        <datalist id="add-model-ids">
          {drafted?.draft.ids.map((id) => (
            <option key={id} value={id} />
          ))}
        </datalist>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Profile name (optional)">
            <Input
              value={ask.name}
              placeholder={drafted?.draft.name ?? "from the model id"}
              autoComplete="off"
              spellCheck={false}
              maxLength={200}
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <Field label="API">
            <Pick
              label="API"
              value={ask.backend}
              items={BACKENDS}
              onChange={(backend) => set({ backend })}
            />
          </Field>
        </div>
        <Field label="LiteLLM price row (optional)">
          <Input
            value={ask.litellm}
            placeholder="for the price, context size and output cap"
            autoComplete="off"
            spellCheck={false}
            maxLength={300}
            list="add-model-rows"
            onChange={(e) => set({ litellm: e.target.value })}
          />
        </Field>
        <datalist id="add-model-rows">
          {drafted?.draft.candidates.map((row) => (
            <option key={row} value={row} />
          ))}
        </datalist>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {drafted && !sameAsk(ask, drafted.ask) && (
            <p className="text-muted-foreground mr-auto">The fields changed after this draft.</p>
          )}
          <Button type="submit" size="sm" variant="outline" disabled={!ready(ask) || asking}>
            {drafted === null
              ? "Draft"
              : edited
                ? "Draft again, dropping your edits"
                : "Draft again"}
          </Button>
        </div>
      </form>
      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {asking ? (
          <p className="text-muted-foreground">Reading the server&apos;s list of models</p>
        ) : problem !== null ? (
          <p className="font-medium">{problem}</p>
        ) : null}
      </div>
      {drafted && (
        <Draft form={form} drafted={drafted} setForm={setForm} onPick={redraft} disabled={asking} />
      )}
    </div>
  );
}

function Draft({
  form,
  drafted: { ask, draft },
  setForm,
  onPick,
  disabled,
}: {
  form: AddForm;
  drafted: NonNullable<AddForm["drafted"]>;
  setForm: React.Dispatch<React.SetStateAction<AddForm>>;
  onPick: (change: Partial<Ask>) => void;
  disabled: boolean;
}) {
  const qc = useQueryClient();
  // the profile each role has now
  const now = useQuery(modelsQuery).data?.roles;
  const [adding, setAdding] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // An add with a check makes paid calls, so a second click before the page shows the first
  // adds nothing.
  const busy = useRef(false);
  const text = form.text ?? draft.toml;
  const edited = form.text !== null;
  const measure = measures(draft, text);
  const others = draft.ids.filter((id) => id !== draft.model);
  const listed = draft.ids.includes(draft.model);
  // the server refuses a name in use; an edit may rename it
  const taken = draft.exists && !edited;

  const add = async (check: boolean) => {
    if (busy.current) return;
    busy.current = true;
    setAdding(true);
    setProblem(null);
    try {
      const added = await addModel({ toml: text, roles: form.roles, check, measure });
      for (const queryKey of CONFIG_DEPENDENT) void qc.invalidateQueries({ queryKey });
      setForm((f) => ({ ...f, added }));
    } catch (e) {
      const why = failed(e);
      // a refusal comes before the file is written
      setProblem(isProblem(e) ? `Nothing was saved: ${why}` : why);
    } finally {
      busy.current = false;
      setAdding(false);
    }
  };

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {taken && (
        <p className="font-medium">
          A profile named {draft.name} exists already. Give another name to add this one.
        </p>
      )}
      {draft.notes.length > 0 && (
        <ul className="list-disc pl-5">
          {draft.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {!listed && others.length > 0 && (
        <Picks
          label="Ids on the server's list"
          items={others}
          disabled={disabled}
          onPick={(model) => onPick({ model })}
        />
      )}
      {draft.candidates.length > 0 && (
        <Picks
          label="LiteLLM rows that may price it"
          items={draft.candidates}
          disabled={disabled}
          onPick={(litellm) => onPick({ litellm })}
        />
      )}
      <Field label={edited ? "Profile (edited)" : "Profile"}>
        <Textarea
          value={text}
          // a draft on the way replaces it
          readOnly={disabled}
          spellCheck={false}
          autoComplete="off"
          className="max-h-80 font-mono text-xs md:text-xs"
          onChange={(e) => {
            const value = e.currentTarget.value;
            setForm((f) => ({ ...f, text: value === draft.toml ? null : value }));
          }}
        />
      </Field>
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1">Roles (optional)</legend>
        {ROLES.map((r) => {
          // an edit may change what it fits; the server says on Add
          const fits = edited || draft.fits.includes(r);
          const holder = now?.[r];
          return (
            <label
              key={r}
              className={cn("flex items-start gap-2", !fits && "text-muted-foreground")}
            >
              <input
                type="checkbox"
                checked={form.roles.includes(r)}
                disabled={!fits}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    roles: ROLES.filter((x) => (x === r ? e.target.checked : f.roles.includes(x))),
                  }))
                }
                className="accent-foreground mt-0.5"
              />
              <span>
                {r}{" "}
                <span className="text-muted-foreground">
                  {ROLE_TEXT[r]} ·{" "}
                  {!fits ? "needs structured output" : holder ? `now ${holder}` : "unset"}
                </span>
              </span>
            </label>
          );
        })}
      </fieldset>
      <div className="text-muted-foreground flex flex-col gap-1">
        <p>
          Add saves it in this project&apos;s aims.toml. With a check it gets the roles only once
          the check passes.
          {measure && " The check also sets its reasoning to what the model sent."}
          {draft.guessed && !measure && " The reasoning stays as you set it."}
        </p>
        {!edited && sameAsk(form.ask, ask) && (
          <p className="break-words">
            The CLI does the same with{" "}
            <code className="text-foreground">{command(toLookup(ask), form.roles, true)}</code>.
          </p>
        )}
      </div>
      <div aria-live="polite" className="empty:hidden">
        {adding ? (
          <p className="text-muted-foreground">Adding</p>
        ) : (
          problem !== null && <p className="font-medium">{problem}</p>
        )}
      </div>
      <DialogFooter>
        <Button
          size="sm"
          variant="outline"
          disabled={adding || disabled || taken}
          onClick={() => void add(false)}
        >
          Add without checking
        </Button>
        <Button size="sm" disabled={adding || disabled || taken} onClick={() => void add(true)}>
          Add and check (a few small paid calls)
        </Button>
      </DialogFooter>
    </div>
  );
}

function Picks({
  label,
  items,
  disabled,
  onPick,
}: {
  label: string;
  items: readonly string[];
  disabled: boolean;
  onPick: (item: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground">{label}</span>
      <ul className="flex flex-wrap gap-1">
        {items.map((item) => (
          <li key={item} className="min-w-0">
            <Button
              size="xs"
              variant="outline"
              disabled={disabled}
              className="h-auto max-w-full py-0.5 font-mono break-all whitespace-normal"
              onClick={() => onPick(item)}
            >
              {item}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// how often the models are read while an add waits for its check
const POLL_MS = 2000;

// The add's file and, with a check, how the check and the roles go.
function Added({ added, roles }: { added: ModelAdded; roles: readonly ModelRole[] }) {
  const run = added.run_id ?? null;
  const qc = useQueryClient();
  const live = useLive();
  // the run's news reads the models again; the poll covers the write after its end
  useEffect(() => {
    if (run === null) return;
    return live.on("run", (m) => {
      if (m.run.run_id === run) void qc.invalidateQueries({ queryKey: modelsQuery.queryKey });
    });
  }, [live, qc, run]);
  const models = useQuery({
    ...modelsQuery,
    enabled: run !== null,
    refetchInterval: (q) => {
      const p = q.state.data?.profiles.find((x) => x.name === added.name);
      return p && p.add?.run_id === run && p.add.outcome != null ? false : POLL_MS;
    },
  });
  const profile = models.data?.profiles.find((p) => p.name === added.name);
  const info = profile?.add?.run_id === run ? profile.add : null;
  const check = profile?.check?.run_id === run ? profile.check : null;
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <DialogHeader>
        <DialogTitle>Added {added.name}</DialogTitle>
        <DialogDescription>
          It is in <span className="font-mono break-all">{added.path}</span>.
          {run === null && roles.length > 0 && ` It has ${theRoles(roles)}.`}
        </DialogDescription>
      </DialogHeader>
      {added.notes.length > 0 && (
        <ul className="list-disc pl-5">
          {added.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {run !== null && (
        <div aria-live="polite" className="flex flex-col gap-2">
          <p>
            {info ? <AddLine info={info} /> : "Checking"}{" "}
            <Link
              to="/runs/$runId"
              params={{ runId: run }}
              className="text-muted-foreground underline"
            >
              check run
            </Link>
          </p>
          {check && check.checks.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {check.checks.map((c) => (
                <li key={c.name} className="flex gap-2">
                  <span className={cn("w-12 shrink-0", c.verdict === "fail" && "font-medium")}>
                    {c.verdict}
                  </span>
                  <span className="min-w-0 break-words">
                    {c.name}
                    <span className="text-muted-foreground">: {c.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {check && check.checks.length === 0 && checkState(check) !== "running" && (
            <p className="text-muted-foreground">{CHECK_TEXT[checkState(check)]}</p>
          )}
        </div>
      )}
    </div>
  );
}

// "the role agent" or "the roles sweep, judge"
function theRoles(roles: readonly string[], the = "the"): string {
  return `${the} role${roles.length > 1 ? "s" : ""} ${roles.join(", ")}`;
}

// How an add stands: waiting for a runner, checking, or how it went.
export function AddLine({ info }: { info: AddInfo }) {
  if (info.outcome === null) {
    const roles =
      info.roles.length > 0
        ? ` ${theRoles(info.roles, "The")} ${info.roles.length > 1 ? "are" : "is"} set once the check passes.`
        : "";
    return info.waiting !== null ? (
      <>
        The check waits: {info.waiting}.{roles}
      </>
    ) : (
      <>Checking.{roles}</>
    );
  }
  const note = info.note ? sentence(info.note) : "";
  return (
    <span className={cn(info.outcome !== "done" && "font-medium")}>
      {note || (info.outcome === "done" ? "Done." : `The add ${info.outcome}.`)}
    </span>
  );
}
