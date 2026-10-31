import { type ReactNode, useState } from "react";

import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

// Where a text is written. Each edit starts from the file as it is now, so a save made in
// between never turns the next edit into a conflict.
export interface TextFile {
  // the draft's first value and the file version it is made against (null: no file yet)
  begin: () => Promise<{ value: string; base: string | null }>;
  // writes `value`, or drops it with null; resolves with a note to show, if any
  save: (value: string | null, base: string | null) => Promise<string | null>;
  // the file's version now, to write over a change made meanwhile
  latest: () => Promise<string | null>;
  // drops the draft and shows what the file holds now
  reload: () => void;
}

interface Props {
  label: string;
  source: string;
  // the source is not the built-in default
  own: boolean;
  text: string;
  editable: boolean;
  file: TextFile;
  saveLabel?: string;
  // the button that drops this layer's text, absent when it has none
  resetLabel?: string | undefined;
  hints?: ReactNode;
}

// One harness text in a table of them: label, source, text, and an inline editor.
export function TextRow({
  label,
  source,
  own,
  text,
  editable,
  file,
  saveLabel = "Save",
  resetLabel,
  hints,
}: Props) {
  const [draft, setDraft] = useState<string | null>(null);
  const [start, setStart] = useState("");
  const [base, setBase] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  // the value a save tried to write when the file had changed under it
  const [conflict, setConflict] = useState<{ value: string | null } | null>(null);
  // a tool's texts break after the tool's name
  const dot = label.indexOf(".");

  const failed = (e: unknown) => setNote(isProblem(e) ? problemText(e) : "The request failed.");

  const begin = async () => {
    setBusy(true);
    setNote(null);
    try {
      const first = await file.begin();
      setBase(first.base);
      setStart(first.value);
      setDraft(first.value);
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  };

  const write = async (value: string | null, against: string | null) => {
    setBusy(true);
    setNote(null);
    setConflict(null);
    try {
      setNote(await file.save(value, against));
      setDraft(null);
    } catch (e) {
      if (isProblem(e) && e.status === 412) setConflict({ value });
      else failed(e);
    } finally {
      setBusy(false);
    }
  };

  const keepMine = async (value: string | null) => {
    try {
      await write(value, await file.latest());
    } catch (e) {
      failed(e);
    }
  };

  const close = () => {
    setDraft(null);
    setConflict(null);
    setNote(null);
  };

  return (
    <li className="grid gap-x-4 border-b py-1.5 last:border-0 md:grid-cols-[14rem_8rem_minmax(0,1fr)]">
      <span className="font-mono wrap-break-word">
        {dot < 0 ? (
          label
        ) : (
          <>
            {label.slice(0, dot + 1)}
            <wbr />
            {label.slice(dot + 1)}
          </>
        )}
      </span>
      <span className={cn(own ? "font-medium" : "text-muted-foreground")}>{source}</span>
      {draft === null ? (
        <div className="flex min-w-0 items-start gap-2">
          <p className="line-clamp-3 min-w-0 flex-1 whitespace-pre-wrap">
            {text || <span className="text-muted-foreground">empty</span>}
          </p>
          {editable && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              aria-label={`Edit ${label}`}
              onClick={() => void begin()}
            >
              Edit
            </Button>
          )}
        </div>
      ) : (
        <form
          className="flex min-w-0 flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void write(draft, base);
          }}
        >
          <Textarea
            aria-label={`Text of ${label}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={Math.min(14, Math.max(3, draft.split("\n").length + 1))}
            className="font-mono"
            autoFocus
          />
          {hints}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={busy || draft === start}>
              {saveLabel}
            </Button>
            {resetLabel && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => void write(null, base)}
              >
                {resetLabel}
              </Button>
            )}
            <Button type="button" variant="ghost" size="sm" onClick={close}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {conflict && (
        <div role="alert" className="flex flex-wrap items-center gap-2 py-1 md:col-start-3">
          <span className="font-medium">The file changed since you began this edit.</span>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => {
              close();
              file.reload();
            }}
          >
            Load theirs, drop mine
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => void keepMine(conflict.value)}
          >
            Keep mine
          </Button>
        </div>
      )}
      {note && <p className="text-muted-foreground md:col-start-3">{note}</p>}
    </li>
  );
}
