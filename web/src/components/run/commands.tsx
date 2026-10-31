import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { type CommandInfo, commandsQuery } from "@/api/commands";
import type { RunDetail } from "@/api/run";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { fullTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type Run = RunDetail["run"];

const ACTION: Record<string, string> = {
  pause: "Pause",
  inject: "Inject",
  kill: "Kill",
  cancel: "Cancel",
  sweep: "Sweep",
};

const THEN: Record<string, string> = {
  continue: "Resume",
  deny: "Deny",
  inject: "Resume with a message",
  kill: "Kill at the hold",
};

const commandLabel = (c: CommandInfo): string =>
  c.action === "resume" ? (THEN[c.then ?? "continue"] ?? "Resume") : (ACTION[c.action] ?? c.action);

function answerOf(c: CommandInfo): string {
  if (c.state === "waiting") return "Waiting";
  const outcome = c.outcome ?? "done";
  if (c.state === "failed") return outcome.replace(/^failed: /, "");
  if (c.state === "dropped") return outcome.replace(/^dropped: /, "Dropped: ");
  return outcome === "done" ? "Done" : outcome;
}

const clock = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});
const dayClock = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

// Seconds count here: an operator often sends several commands within a minute.
function sentAt(s: number, now = new Date()): string {
  const d = new Date(s * 1000);
  return d.toDateString() === now.toDateString() ? clock.format(d) : dayClock.format(d);
}

const tookText = (s: number): string =>
  s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(1)} s`;

// The commands sent to a run and the runner's answers, from a button in the run's header.
export function RunCommands({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  const live = run.status === "running" && run.state === "running";
  if (run.commands === 0 && !live) return null;
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger render={<Button variant="outline" size="sm" />}>
        Commands
        {run.commands_waiting > 0 && (
          <span className="text-pen-1 tabular-nums">
            {run.commands_waiting}
            <span className="sr-only"> waiting</span>
          </span>
        )}
      </SheetTrigger>
      <SheetContent
        side="right"
        className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl"
      >
        <SheetHeader className="border-b">
          <SheetTitle>Commands</SheetTitle>
          <SheetDescription>
            Sent to this run through the control plane, newest first, with the runner's answers.
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <CommandList runId={run.run_id} />
        </div>
      </SheetContent>
    </Sheet>
  );
}

function CommandList({ runId }: { runId: string }) {
  const list = useQuery(commandsQuery(runId));
  if (list.isPending) return <p className="text-muted-foreground">Loading.</p>;
  if (list.isError) return <p className="text-muted-foreground">Commands did not load.</p>;
  if (list.data.length === 0) {
    return (
      <p className="text-muted-foreground">
        No commands sent yet. Answers given at a terminal are in the run's log as operator.action.
      </p>
    );
  }
  const rows = list.data.toReversed();
  return (
    <table className="w-full">
      <thead className="text-muted-foreground text-left">
        <tr className="border-b">
          <th className="pr-4 pb-1.5 font-normal">Sent</th>
          <th className="pr-4 pb-1.5 font-normal">By</th>
          <th className="pr-4 pb-1.5 font-normal">Command</th>
          <th className="pb-1.5 font-normal">Answer</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <Row key={c.id} c={c} />
        ))}
      </tbody>
    </table>
  );
}

function Row({ c }: { c: CommandInfo }) {
  const note = c.text ?? c.reason;
  const took = c.acked !== null && (c.state === "done" || c.state === "failed");
  return (
    <tr className="border-b align-top last:border-0">
      <td
        className="py-1.5 pr-4 whitespace-nowrap tabular-nums"
        title={fullTime(new Date(c.created * 1000).toISOString())}
      >
        {sentAt(c.created)}
      </td>
      <td className="py-1.5 pr-4">{c.by ?? "unknown"}</td>
      <td className="py-1.5 pr-4">
        <span className="flex flex-col">
          <span>
            {commandLabel(c)}
            {c.agent && <span className="text-muted-foreground"> {c.agent}</span>}
          </span>
          {note && (
            <span className="text-muted-foreground text-xs break-words whitespace-pre-wrap">
              {c.text ? note : `Reason: ${note}`}
            </span>
          )}
        </span>
      </td>
      <td className="py-1.5">
        <span className="flex flex-col">
          <span
            className={cn(
              c.state === "failed" && "text-flag",
              (c.state === "waiting" || c.state === "dropped") && "text-muted-foreground",
            )}
          >
            {c.state === "failed" ? `Refused: ${answerOf(c)}` : answerOf(c)}
          </span>
          {took && c.acked !== null && (
            <span className="text-muted-foreground text-xs tabular-nums">
              in {tookText(c.acked - c.created)}
            </span>
          )}
        </span>
      </td>
    </tr>
  );
}
