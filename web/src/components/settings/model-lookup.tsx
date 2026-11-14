import { Link } from "@tanstack/react-router";
import { useRef, useState } from "react";

import { lookupModel, type ProfileDraft } from "@/api/launch";
import { isProblem } from "@/api/session";
import { Field, problemText } from "@/components/form";
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

// A draft with the name and alias it was asked with, for the matching command.
interface Found {
  draft: ProfileDraft;
  name: string;
  alias: boolean;
}

// A word the shell takes as written, such as an alias's leading ~.
const word = (s: string) => (/^[\w./:@-]+$/.test(s) ? s : `'${s.replaceAll("'", "'\\''")}'`);

const command = ({ draft, name, alias }: Found) =>
  [
    "aims models add openrouter",
    word(draft.slug),
    ...(name ? [`--name ${word(name)}`] : []),
    ...(alias ? ["--alias"] : []),
    "--write",
  ].join(" ");

// A profile drafted from OpenRouter's list of models, to paste into a config file. The dialog
// never writes the config itself.
export function ModelLookup() {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="xs" />}>
        Add from OpenRouter
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        {open && <Lookup />}
      </DialogContent>
    </Dialog>
  );
}

function Lookup() {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [alias, setAlias] = useState(false);
  // the id being looked up
  const [asking, setAsking] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [found, setFound] = useState<Found | null>(null);
  // Each lookup has the server read OpenRouter's list, so a second submit before the page shows
  // the first asks nothing.
  const busy = useRef(false);
  const id = slug.trim();

  const submit = async () => {
    if (id === "" || busy.current) return;
    busy.current = true;
    const ask = { name: name.trim(), alias };
    setAsking(id);
    setProblem(null);
    setFound(null);
    try {
      setFound({ draft: await lookupModel(id, ask.name, ask.alias), ...ask });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
    } finally {
      busy.current = false;
      setAsking(null);
    }
  };

  return (
    <form
      className="flex min-w-0 flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <DialogHeader>
        <DialogTitle>Add from OpenRouter</DialogTitle>
        <DialogDescription>
          The control plane reads OpenRouter&apos;s public list of models and drafts a profile.
          Nothing is saved.
        </DialogDescription>
      </DialogHeader>
      <Field label="Model id">
        <Input
          autoFocus
          value={slug}
          placeholder="author/model-name"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setSlug(e.target.value)}
        />
      </Field>
      <Field label="Profile name (optional)">
        <Input
          value={name}
          placeholder="openrouter-<model>"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={alias}
          onChange={(e) => setAlias(e.target.checked)}
          className="accent-foreground"
        />
        Allow an alias that OpenRouter moves to newer models
      </label>
      {(asking !== null || problem !== null || found !== null) && (
        <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
          {asking !== null ? (
            <p className="text-muted-foreground">Looking up {asking}</p>
          ) : problem !== null ? (
            <p className="font-medium">{problem}</p>
          ) : (
            found && <Notes draft={found.draft} />
          )}
        </div>
      )}
      {found && <Draft found={found} />}
      <DialogFooter>
        <Button type="submit" size="sm" disabled={id === "" || asking !== null}>
          Look up
        </Button>
      </DialogFooter>
    </form>
  );
}

function Notes({ draft }: { draft: ProfileDraft }) {
  return (
    <>
      {draft.exists && (
        <p className="font-medium">
          A profile named {draft.name} exists already. Pasting this would change it. Give another
          name to add a new one.
        </p>
      )}
      {draft.notes.length > 0 && (
        <ul className="list-disc pl-5">
          {draft.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </>
  );
}

function Draft({ found }: { found: Found }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  // missing outside a secure context, such as plain http on a LAN address
  const clipboard = (navigator as { clipboard?: Clipboard }).clipboard;
  const copy = async () => {
    try {
      await clipboard?.writeText(found.draft.toml);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {/* a tab stop, so keys can scroll lines that do not fit */}
      <pre
        tabIndex={0}
        aria-label={`Profile ${found.draft.name}`}
        className="focus-visible:ring-ring/50 overflow-x-auto rounded-sm border px-2 py-1.5 font-mono text-xs outline-none select-all focus-visible:ring-2"
      >
        {found.draft.toml}
      </pre>
      <div className="flex items-start gap-2">
        <p className="text-muted-foreground min-w-0 flex-1 break-words">
          Paste it into this project&apos;s aims.toml under{" "}
          <Link
            to="/settings/config"
            search={{ view: "project" }}
            className="text-foreground underline"
          >
            Config
          </Link>
          . The CLI adds it for you with <code className="text-foreground">{command(found)}</code>.
        </p>
        {clipboard && (
          <Button size="sm" variant="outline" onClick={() => void copy()}>
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </div>
      {copied === false && (
        <p className="text-muted-foreground">Copy failed. Select the text instead.</p>
      )}
    </div>
  );
}
