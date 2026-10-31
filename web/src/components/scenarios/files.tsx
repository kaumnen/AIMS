import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";

import {
  checkScenario,
  deleteFile,
  fileQuery,
  filesQuery,
  MAX_EDIT_BYTES,
  type Opened,
  saveFile,
  SCENARIO_FILE,
  schemaQuery,
} from "@/api/scenario-files";
import type { ScenarioDetail } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { CodeEditor, type LineProblem, Problems } from "@/components/code-editor";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fileTree, type TreeNode } from "@/lib/scenario";
import { useDebounced } from "@/lib/use-debounced";
import { cn } from "@/lib/utils";

const CHECK_MS = 400;
const lineOf = (p: { line?: number | null }) => p.line ?? null;

// The scenario's files: a list, the open file in an editor, scenario.yaml checked as you type.
// Saves name the version they replace; a file changed on disk since it was opened is not
// overwritten unless you say so.
export default function FilesTab({
  detail,
  file,
  onFile,
}: {
  detail: ScenarioDetail;
  file: string;
  onFile: (file: string) => void;
}) {
  const { source, name, writable } = detail;
  const queryClient = useQueryClient();
  const files = useQuery(filesQuery(source, name));
  const opened = useQuery(fileQuery(source, name, file));
  const schema = useQuery(schemaQuery);
  const [draft, setDraft] = useState<string | null>(null);
  // the version the draft was started from: a save replaces that one or is refused, even if
  // the file was read again since
  const [from, setFrom] = useState<string | null>(null);
  const [problems, setProblems] = useState<LineProblem[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [goto, setGoto] = useState<{ line: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);

  const base = opened.data;
  const text = draft ?? base?.text ?? "";
  const dirty = draft !== null && draft !== base?.text;
  const isScenario = file === SCENARIO_FILE;
  const settled = useDebounced(isScenario ? text : null, CHECK_MS);

  useEffect(() => {
    if (settled === null || base === undefined || settled !== text) return;
    const ask = new AbortController();
    checkScenario(settled, ask.signal)
      .then((r) => {
        setProblems([
          ...r.errors.map((e) => ({ line: lineOf(e), msg: e.msg, severity: "error" as const })),
          ...r.warnings.map((w) => ({ line: lineOf(w), msg: w.msg, severity: "warning" as const })),
        ]);
      })
      .catch(() => undefined);
    return () => ask.abort();
  }, [settled, base, text]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["scenarios", "files", source, name] });
    void queryClient.invalidateQueries({ queryKey: ["scenarios", "detail", source, name] });
    void queryClient.invalidateQueries({ queryKey: ["scenarios"], exact: true });
  };

  const open = (next: string) => {
    if (next === file) return;
    if (dirty && !window.confirm(`Discard your changes to ${file}?`)) return;
    setDraft(null);
    setProblems([]);
    setStatus(null);
    setConflict(null);
    onFile(next);
  };

  const edit = (t: string) => {
    if (draft === null) setFrom(base?.etag ?? null);
    setDraft(t);
    setStatus(null);
  };

  const save = async (etag: string | null = from ?? base?.etag ?? null) => {
    if (!writable || busy || base === undefined || base.text === null) return;
    setBusy(true);
    setStatus("Saving");
    try {
      const saved = await saveFile(source, name, file, text, etag);
      const now: Opened = { text, size: new Blob([text]).size, etag: `"${saved.etag}"` };
      queryClient.setQueryData(fileQuery(source, name, file).queryKey, now);
      setDraft(null);
      setFrom(null);
      setConflict(null);
      setStatus("Saved");
      refresh();
    } catch (e) {
      if (isProblem(e) && e.status === 412) {
        setConflict(problemText(e));
        setStatus(null);
      } else if (isProblem(e) && e.status === 422 && e.errors) {
        setProblems(
          e.errors.map((p) => ({ line: p.line ?? null, msg: p.msg, severity: "error" as const })),
        );
        setStatus(`Not saved: ${problemText(e)}`);
      } else {
        setStatus(`Not saved: ${isProblem(e) ? problemText(e) : "the request failed."}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const overwrite = async () => {
    const latest = await queryClient.fetchQuery(fileQuery(source, name, file));
    await save(latest.etag);
  };

  const reload = async () => {
    setDraft(null);
    setFrom(null);
    setConflict(null);
    setStatus(null);
    await opened.refetch();
  };

  const remove = async () => {
    if (!base || !window.confirm(`Delete ${file}?`)) return;
    try {
      await deleteFile(source, name, file, base.etag);
      refresh();
      onFile(SCENARIO_FILE);
    } catch (e) {
      setStatus(`Not deleted: ${isProblem(e) ? problemText(e) : "the request failed."}`);
    }
  };

  const add = async (path: string) => {
    const clean = path.trim().replace(/^\/+/, "");
    if (!clean) return;
    try {
      await saveFile(source, name, clean, "", null);
      setAdding(null);
      refresh();
      open(clean);
    } catch (e) {
      setStatus(`Not created: ${isProblem(e) ? problemText(e) : "the request failed."}`);
    }
  };

  const errors = problems.filter((p) => p.severity === "error").length;
  const warnings = problems.length - errors;
  return (
    <div className="grid min-h-0 gap-4 md:grid-cols-[14rem_minmax(0,1fr)]">
      <nav aria-label="Files" className="flex min-w-0 flex-col gap-1">
        <FileTree
          nodes={fileTree((files.data ?? []).map((f) => f.path))}
          sizes={new Map((files.data ?? []).map((f) => [f.path, f.size]))}
          current={file}
          onOpen={open}
        />
        {files.isError && <p className="text-muted-foreground px-2">Files did not load.</p>}
        {writable &&
          (adding === null ? (
            <Button variant="ghost" size="sm" className="self-start" onClick={() => setAdding("")}>
              New file
            </Button>
          ) : (
            <form
              className="flex flex-col gap-1 px-1"
              onSubmit={(e) => {
                e.preventDefault();
                void add(adding);
              }}
            >
              <Input
                autoFocus
                aria-label="New file path"
                placeholder="selftest/exploit.sh"
                value={adding}
                onChange={(e) => setAdding(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && setAdding(null)}
              />
            </form>
          ))}
      </nav>
      <section aria-label={file} className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="font-mono">{file}</h2>
          <span className="text-muted-foreground" aria-live="polite">
            {!writable
              ? "read only"
              : (status ??
                (dirty
                  ? "unsaved changes"
                  : base
                    ? `${base.size.toLocaleString("en-US")} bytes`
                    : ""))}
          </span>
          {isScenario && (errors > 0 || warnings > 0) && (
            <span className={cn(errors > 0 && "font-medium")}>
              {[errors > 0 && plural(errors, "error"), warnings > 0 && plural(warnings, "warning")]
                .filter(Boolean)
                .join(", ")}
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            {writable && file !== SCENARIO_FILE && base && (
              <Button variant="ghost" size="sm" onClick={() => void remove()}>
                Delete
              </Button>
            )}
            {writable && (
              <Button size="sm" disabled={!dirty || busy} onClick={() => void save()}>
                Save
              </Button>
            )}
          </div>
        </div>
        {conflict && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-2 rounded-sm border px-3 py-2"
          >
            <span className="font-medium">
              {file} changed on disk since you opened it ({conflict}).
            </span>
            <Button variant="outline" size="sm" onClick={() => void reload()}>
              Load theirs, drop mine
            </Button>
            <Button variant="outline" size="sm" onClick={() => void overwrite()}>
              Keep mine, overwrite
            </Button>
          </div>
        )}
        <div className="h-[calc(100dvh-15rem)] min-h-80 rounded-sm border">
          {opened.isPending ? (
            <p className="text-muted-foreground p-3">Opening {file}</p>
          ) : opened.isError ? (
            <p className="text-muted-foreground p-3">
              {file} did not open:{" "}
              {isProblem(opened.error) ? problemText(opened.error) : "the request failed."}
            </p>
          ) : opened.data.text === null || opened.data.size > MAX_EDIT_BYTES ? (
            <p className="text-muted-foreground p-3">
              {opened.data.text === null ? "Not a text file" : "Too large to edit here"},{" "}
              {opened.data.size.toLocaleString("en-US")} bytes.
            </p>
          ) : (
            <CodeEditor
              key={file}
              text={text}
              language={/\.ya?ml$/.test(file) ? "yaml" : null}
              schema={isScenario ? schema.data : undefined}
              readOnly={!writable}
              problems={isScenario ? problems : []}
              onChange={edit}
              onSave={() => void save()}
              label={`Contents of ${file}`}
              goto={goto}
            />
          )}
        </div>
        {isScenario && <Problems problems={problems} onGoto={(line) => setGoto({ line })} />}
      </section>
    </div>
  );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Folders open on the way to the current file; the rest open on a click.
function FileTree({
  nodes,
  sizes,
  current,
  onOpen,
}: {
  nodes: TreeNode[];
  sizes: ReadonlyMap<string, number>;
  current: string;
  onOpen: (path: string) => void;
}) {
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const toggle = (key: string) =>
    setOpened((prev) => {
      const next = new Set(prev);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  const rows = (list: TreeNode[], depth: number, prefix: string): React.ReactNode[] =>
    list.flatMap((n) => {
      const pad = { paddingLeft: `${depth * 0.75 + 0.5}rem` };
      if (n.path !== null) {
        const size = sizes.get(n.path) ?? 0;
        return [
          <li key={n.path}>
            <button
              type="button"
              onClick={() => onOpen(n.path ?? "")}
              aria-current={n.path === current ? "true" : undefined}
              className={cn(
                "hover:bg-muted/60 w-full truncate rounded-sm py-0.5 pr-2 text-left font-mono",
                n.path === current ? "bg-muted text-foreground" : "text-muted-foreground",
              )}
              style={pad}
              title={`${n.path}, ${size.toLocaleString("en-US")} bytes`}
            >
              {n.name}
            </button>
          </li>,
        ];
      }
      const key = `${prefix}${n.name}/`;
      const open = opened.has(key) !== current.startsWith(key);
      return [
        <li key={key}>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => toggle(key)}
            className="hover:bg-muted/60 flex w-full items-center gap-1 rounded-sm py-0.5 pr-2 text-left font-mono"
            style={pad}
          >
            <ChevronRight className={cn("size-3.5 shrink-0", open && "rotate-90")} aria-hidden />
            <span className="truncate">{n.name}</span>
          </button>
        </li>,
        ...(open ? rows(n.children, depth + 1, key) : []),
      ];
    });
  return <ul className="flex flex-col">{rows(nodes, 0, "")}</ul>;
}
