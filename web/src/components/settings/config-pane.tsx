import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  checkConfig,
  CONFIG_DEPENDENT,
  type Layer,
  type LayerInfo,
  layerQuery,
  mergedQuery,
  saveConfig,
} from "@/api/settings";
import { CodeEditor, failed, Problems, useFileDraft } from "@/components/code-editor";
import { conflictText } from "@/components/form";
import { Button } from "@/components/ui/button";

export type View = Layer | "merged";

const EDITOR = "h-[calc(100dvh-17rem)] min-h-80 rounded-sm border";

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
  // The compiler skipped this until its draft moved to useFileDraft. Memoized, it adds 1.4 kB.
  "use no memo";
  const queryClient = useQueryClient();
  // the page asks before a draft is left, and once this file is gone there is none
  const {
    opened,
    text,
    dirty,
    problems,
    status,
    conflict,
    goto,
    setGoto,
    busy,
    edit,
    save,
    overwrite,
    reload,
  } = useFileDraft(layerQuery(layer), {
    scope: layer,
    editable,
    check: editable
      ? async (settled, signal) => {
          const r = await checkConfig(layer, settled, signal);
          return r.errors.map((e) => ({ line: e.line ?? null, msg: e.msg, severity: "error" }));
        }
      : null,
    write: (next, etag) => saveConfig(layer, next, etag),
    done: (was) => (was.exists ? "Saved" : "Created"),
    invalid: "Not saved: it does not load.",
    onSaved: () => {
      for (const queryKey of CONFIG_DEPENDENT) void queryClient.invalidateQueries({ queryKey });
    },
    onDirty,
  });
  const base = opened.data;
  const name = (file?.path ?? base?.path ?? "").split(/[\\/]/).pop() ?? "";

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
          <span className="font-medium">{conflictText(name, conflict)}</span>
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
