import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { isProblem } from "@/api/session";
import {
  checkConfig,
  CONFIG_DEPENDENT,
  type Layer,
  type LayerInfo,
  layerQuery,
  mergedQuery,
  saveConfig,
} from "@/api/settings";
import { CodeEditor, type LineProblem, Problems } from "@/components/code-editor";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import { useDebounced } from "@/lib/use-debounced";

export type View = Layer | "merged";

const CHECK_MS = 400;
const EDITOR = "h-[calc(100dvh-17rem)] min-h-80 rounded-sm border";

const failed = (e: unknown) => (isProblem(e) ? problemText(e) : "the request failed.");

export default function ConfigPane({
  view,
  file,
  editable,
  onDirty,
}: {
  view: View;
  file: LayerInfo | undefined;
  editable: boolean;
  onDirty: (dirty: boolean) => void;
}) {
  if (view === "merged") return <Merged />;
  return <LayerFile layer={view} file={file} editable={editable} onDirty={onDirty} />;
}

function Merged() {
  const merged = useQuery(mergedQuery);
  return (
    <div className={EDITOR}>
      {merged.isPending ? (
        <p className="text-muted-foreground p-3">Merging the layers</p>
      ) : merged.isError ? (
        <p className="text-muted-foreground p-3">
          The settings do not load: {failed(merged.error)}
        </p>
      ) : (
        <CodeEditor
          text={merged.data}
          language="toml"
          readOnly
          problems={[]}
          label="The settings in use"
        />
      )}
    </div>
  );
}

// One layer's file, checked as you type. A save names the version it replaces; a file changed
// on disk since it was opened is not overwritten unless you say so.
function LayerFile({
  layer,
  file,
  editable,
  onDirty,
}: {
  layer: Layer;
  file: LayerInfo | undefined;
  editable: boolean;
  onDirty: (dirty: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const opened = useQuery(layerQuery(layer));
  const [draft, setDraft] = useState<string | null>(null);
  // the version the draft began from, null when there was no file; a save replaces that one
  const [from, setFrom] = useState<string | null | undefined>(undefined);
  const [problems, setProblems] = useState<LineProblem[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [goto, setGoto] = useState<{ line: number } | null>(null);
  const [busy, setBusy] = useState(false);

  const base = opened.data;
  const text = draft ?? base?.text ?? "";
  const dirty = draft !== null && draft !== base?.text;
  const settled = useDebounced(editable ? text : null, CHECK_MS);
  const name = (file?.path ?? base?.path ?? "").split(/[\\/]/).pop() ?? "";

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  useEffect(() => {
    if (settled === null || base === undefined || settled !== text) return;
    const ask = new AbortController();
    checkConfig(layer, settled, ask.signal)
      .then((r) => {
        setProblems(r.errors.map((e) => ({ line: e.line ?? null, msg: e.msg, severity: "error" })));
      })
      .catch(() => undefined);
    return () => ask.abort();
  }, [settled, base, text, layer]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const edit = (t: string) => {
    if (draft === null) setFrom(base?.exists ? base.etag : null);
    setDraft(t);
    setStatus(null);
  };

  const save = async (etag: string | null = from !== undefined ? from : (base?.etag ?? null)) => {
    if (!editable || busy || base === undefined) return;
    setBusy(true);
    setStatus("Saving");
    try {
      const saved = await saveConfig(layer, text, etag);
      queryClient.setQueryData(layerQuery(layer).queryKey, saved);
      setDraft(null);
      setFrom(undefined);
      setConflict(null);
      setStatus(base.exists ? "Saved" : "Created");
      for (const queryKey of CONFIG_DEPENDENT) void queryClient.invalidateQueries({ queryKey });
    } catch (e) {
      if (isProblem(e) && e.status === 412) {
        setConflict(problemText(e));
        setStatus(null);
      } else if (isProblem(e) && e.status === 422 && e.errors?.length) {
        setProblems(e.errors.map((p) => ({ line: p.line ?? null, msg: p.msg, severity: "error" })));
        setStatus("Not saved: it does not load.");
      } else {
        setStatus(`Not saved: ${failed(e)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const overwrite = async () => {
    try {
      const latest = await queryClient.fetchQuery(layerQuery(layer));
      await save(latest.exists ? latest.etag : null);
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

  const errors = problems.length;
  return (
    <section aria-label={name} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="font-mono">{name}</h2>
        <span className="text-muted-foreground" aria-live="polite">
          {!editable
            ? "read only"
            : (status ??
              (dirty ? "unsaved changes" : base && !base.exists ? "not created yet" : ""))}
        </span>
        {errors > 0 && (
          <span className="font-medium">
            {errors} {errors === 1 ? "error" : "errors"}
          </span>
        )}
        {editable && base && (
          <Button
            size="sm"
            className="ml-auto"
            disabled={(!dirty && base.exists) || busy}
            onClick={() => void save()}
          >
            {base.exists ? "Save" : "Create"}
          </Button>
        )}
      </div>
      {conflict && (
        <div role="alert" className="flex flex-wrap items-center gap-2 rounded-sm border px-3 py-2">
          <span className="font-medium">
            {name} changed on disk since you opened it ({conflict}).
          </span>
          <Button variant="outline" size="sm" onClick={() => void reload()}>
            Load theirs, drop mine
          </Button>
          <Button variant="outline" size="sm" onClick={() => void overwrite()}>
            Keep mine, overwrite
          </Button>
        </div>
      )}
      <div className={EDITOR}>
        {opened.isPending ? (
          <p className="text-muted-foreground p-3">Opening {name}</p>
        ) : opened.isError ? (
          <p className="text-muted-foreground p-3">
            {name} did not open: {failed(opened.error)}
          </p>
        ) : (
          <CodeEditor
            text={text}
            language="toml"
            readOnly={!editable}
            problems={problems}
            onChange={edit}
            onSave={() => void save()}
            label={`Contents of ${name}`}
            goto={goto}
          />
        )}
      </div>
      <Problems problems={problems} onGoto={(line) => setGoto({ line })} />
    </section>
  );
}
