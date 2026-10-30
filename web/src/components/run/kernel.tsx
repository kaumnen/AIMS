import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { type ExecActivity, execEventsQuery } from "@/api/run";
import { Button } from "@/components/ui/button";
import { summary } from "@/lib/event-text";
import { cn } from "@/lib/utils";

import type { Tone } from "./transcript";

const opWords: Record<string, [string, string]> = {
  read: ["read", "reads"],
  write: ["write", "writes"],
  delete: ["delete", "deletes"],
  rename: ["rename", "renames"],
  denied: ["denied open", "denied opens"],
  mkdir: ["mkdir", "mkdirs"],
  symlink: ["symlink", "symlinks"],
  link: ["link", "links"],
};

const typeWords: Record<string, [string, string]> = {
  "proc.exec": ["proc", "procs"],
  "net.connect": ["connect", "connects"],
  "ipc.event": ["ipc", "ipc"],
  "file.write": ["file write", "file writes"],
  "file.setattr": ["setattr", "setattrs"],
  "sys.denied": ["denied call", "denied calls"],
  "ptrace.event": ["ptrace", "ptraces"],
  "ns.event": ["namespace change", "namespace changes"],
  "proc.signal": ["signal", "signals"],
  "mem.exec": ["memory exec", "memory execs"],
};

const words = (n: number, [one, many]: [string, string]) => `${n} ${n === 1 ? one : many}`;

// "120 reads, 3 procs, 1 connect": what the kernel saw an exec do.
export function activityText(a: ExecActivity): string {
  const parts: string[] = [];
  for (const [op, n] of Object.entries(a.file_ops).toSorted((x, y) => y[1] - x[1])) {
    parts.push(words(n, opWords[op] ?? [op, op]));
  }
  for (const [type, w] of Object.entries(typeWords)) {
    const n = a.types[type];
    if (n) parts.push(words(n, w));
  }
  if (a.changes) parts.push(words(a.changes, ["file change", "file changes"]));
  return parts.length ? parts.join(", ") : `${a.events} kernel records`;
}

const NONE: readonly number[] = [];

// Opens by itself when a chosen flag cites one of its records.
export function KernelFold({
  runId,
  execId,
  activity,
  focus = NONE,
  tone = "ink",
}: {
  runId: string;
  execId: string;
  activity: ExecActivity | undefined;
  focus?: readonly number[];
  tone?: Tone;
}) {
  const [asked, setAsked] = useState<boolean | null>(null);
  const open = asked ?? focus.length > 0;
  const setOpen = (v: boolean) => setAsked(v);
  if (!activity) return null;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 flex items-start gap-1 rounded-sm text-left outline-none focus-visible:ring-2"
      >
        <ChevronRight aria-hidden className={cn("mt-0.5 size-3.5 shrink-0", open && "rotate-90")} />
        Kernel: {activityText(activity)}
      </button>
      {open && <KernelTable runId={runId} execId={execId} focus={focus} tone={tone} />}
    </div>
  );
}

function KernelTable({
  runId,
  execId,
  focus,
  tone,
}: {
  runId: string;
  execId: string;
  focus: readonly number[];
  tone: Tone;
}) {
  const q = useInfiniteQuery(execEventsQuery(runId, execId));
  const box = useRef<HTMLDivElement>(null);
  const shown = useRef(-1);
  const want = Math.max(-1, ...focus);
  const reached = q.data?.pages.at(-1)?.items.at(-1)?.seq ?? -1;
  const more = q.hasNextPage && !q.isFetchingNextPage;
  const next = q.fetchNextPage;
  // Page on until the cited record is loaded, then bring it into view once.
  useEffect(() => {
    if (want > reached && more) void next();
    else if (want >= 0 && want <= reached && shown.current !== want) {
      shown.current = want;
      box.current?.querySelector("[data-evidence]")?.scrollIntoView?.({ block: "center" });
    }
  }, [want, reached, more, next]);
  if (q.isPending) return <p className="text-muted-foreground py-1 pl-5">Loading records</p>;
  if (q.isError) return <p className="text-muted-foreground py-1 pl-5">Records did not load.</p>;
  const events = q.data.pages.flatMap((p) => p.items);
  return (
    <div ref={box} className="mt-1 ml-5 rounded-sm border">
      <table className="w-full table-fixed text-xs">
        <thead className="text-muted-foreground border-b text-left">
          <tr>
            <th className="w-16 px-2 py-1 font-normal">Seq</th>
            <th className="w-28 px-2 py-1 font-normal">Type</th>
            <th className="px-2 py-1 font-normal">Record</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {events.map((e) => (
            <tr
              key={e.seq}
              data-evidence={focus.includes(e.seq) ? "" : undefined}
              className={cn(
                "border-grid border-b last:border-0",
                focus.includes(e.seq) && (tone === "red" ? "bg-flag/10" : "bg-foreground/10"),
              )}
            >
              <td className="text-muted-foreground px-2 py-0.5 tabular-nums">{e.seq}</td>
              <td className="text-muted-foreground truncate px-2 py-0.5">{e.type}</td>
              <td className="truncate px-2 py-0.5" title={summary(e)}>
                {summary(e)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {q.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          disabled={q.isFetchingNextPage}
          onClick={() => void q.fetchNextPage()}
        >
          Load more records
        </Button>
      )}
    </div>
  );
}
