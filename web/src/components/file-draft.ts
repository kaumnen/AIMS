import {
  type DataTag,
  type QueryKey,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { isProblem } from "@/api/session";
import type { LineProblem } from "@/components/code-editor";
import { problemText } from "@/components/form";
import { useDebounced } from "@/lib/use-debounced";

const CHECK_MS = 400;

export const failed = (e: unknown) => (isProblem(e) ? problemText(e) : "the request failed.");

// A file as read: `text` is null for bytes that are not text, `etag` null for no file yet.
interface Version {
  text: string | null;
  etag: string | null;
}

export interface DraftOptions<T extends Version> {
  // a new scope, such as another file opened in this one's place, drops the draft
  scope: string;
  editable: boolean;
  // the problems of the text as typed; null checks nothing
  check: ((text: string, signal: AbortSignal) => Promise<LineProblem[]>) | null;
  // writes `text` over the version `etag` names and resolves with the file as it then is
  write: (text: string, etag: string | null, base: T) => Promise<T>;
  // what a save that went through says
  done?: (base: T) => string;
  // what a save refused with problems by line says
  invalid?: string;
  onSaved: () => void;
  // whether there is a draft, so the page asks before it is left
  onDirty?: ((dirty: boolean) => void) | undefined;
}

// A draft of one file over the file as read. A save names the version the draft began from;
// a file changed since is not overwritten unless you say so.
export function useFileDraft<T extends Version, K extends QueryKey>(
  query: UseQueryOptions<T, Error, T, K> & { queryKey: DataTag<K, T, Error> },
  options: DraftOptions<T>,
) {
  const { scope, editable, write, done, invalid, onSaved, onDirty } = options;
  const queryClient = useQueryClient();
  const opened = useQuery(query);
  const [draft, setDraft] = useState<string | null>(null);
  // the draft as last rendered, for a save that ends after more was typed
  const typed = useRef(draft);
  useLayoutEffect(() => {
    typed.current = draft;
  });
  // the version the draft began from, undefined while there is none: a save replaces that one
  // or is refused, even if the file was read again since
  const [from, setFrom] = useState<string | null | undefined>(undefined);
  const [problems, setProblems] = useState<LineProblem[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [goto, setGoto] = useState<{ line: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState(scope);
  if (shown !== scope) {
    setShown(scope);
    setDraft(null);
    setFrom(undefined);
    setProblems([]);
    setStatus(null);
    setConflict(null);
    setGoto(null);
  }

  const base = opened.data;
  const text = draft ?? base?.text ?? "";
  const dirty = draft !== null && draft !== base?.text;
  const settled = useDebounced(options.check ? text : null, CHECK_MS);
  // the check as last rendered: a new function each render asks nothing new
  const check = useRef(options.check);
  useLayoutEffect(() => {
    check.current = options.check;
  });

  useEffect(() => {
    if (settled === null || base === undefined || settled !== text) return;
    const ask = new AbortController();
    check
      .current?.(settled, ask.signal)
      .then(setProblems)
      .catch(() => undefined);
    return () => ask.abort();
  }, [settled, base, text]);

  useEffect(() => {
    onDirty?.(dirty);
    return () => onDirty?.(false);
  }, [dirty, onDirty]);

  const edit = (t: string) => {
    if (draft === null) setFrom(base?.etag ?? null);
    setDraft(t);
    setStatus(null);
  };

  const save = async (etag: string | null = from !== undefined ? from : (base?.etag ?? null)) => {
    if (!editable || busy || base === undefined || base.text === null) return;
    setBusy(true);
    setStatus("Saving");
    try {
      const now = await write(text, etag, base);
      queryClient.setQueryData<T>(query.queryKey, now);
      // what was typed while it saved stays a draft over the saved version
      const more = typed.current !== null && typed.current !== text;
      setDraft((d) => (d === text ? null : d));
      setFrom(now.etag);
      setConflict(null);
      setStatus(more ? null : (done?.(base) ?? "Saved"));
      onSaved();
    } catch (e) {
      if (isProblem(e) && e.status === 412) {
        setConflict(problemText(e));
        setStatus(null);
      } else if (isProblem(e) && e.status === 422 && e.errors?.length) {
        setProblems(e.errors.map((p) => ({ line: p.line ?? null, msg: p.msg, severity: "error" })));
        setStatus(invalid ?? `Not saved: ${problemText(e)}`);
      } else {
        setStatus(`Not saved: ${failed(e)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const overwrite = async () => {
    try {
      const latest = await queryClient.fetchQuery(query);
      await save(latest.etag);
    } catch (e) {
      setStatus(`Not saved: ${failed(e)}`);
    }
  };

  const reload = async () => {
    setDraft(null);
    setFrom(undefined);
    setConflict(null);
    setStatus(null);
    await opened.refetch();
  };

  return {
    opened,
    text,
    dirty,
    problems,
    status,
    setStatus,
    conflict,
    busy,
    goto,
    setGoto,
    edit,
    save,
    overwrite,
    reload,
  };
}
