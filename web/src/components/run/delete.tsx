import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { deleteRun, dependentsQuery } from "@/api/run";
import { dropRuns, type RunSummary } from "@/api/runs";
import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { shortId } from "@/lib/format";
import { useOperator } from "@/lib/use-operator";

const ENDED = new Set(["done", "cancelled"]);
const NAMES: Record<string, string> = { monitor: "monitor run", score: "score run" };

// "2 monitor runs and 1 score run": how many runs of each kind go with a run.
export function goesWith(kinds: readonly string[]): string {
  const counts = new Map<string, number>();
  for (const kind of kinds) {
    const name = NAMES[kind] ?? "other run";
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const parts = ["monitor run", "score run", "other run"]
    .filter((name) => counts.has(name))
    .map((name) => {
      const n = counts.get(name) ?? 0;
      return `${n} ${name}${n === 1 ? "" : "s"}`;
    });
  const last = parts.pop();
  return parts.length > 0 ? `${parts.join(", ")} and ${last}` : (last ?? "");
}

// Deletes an ended run with the runs made from it, for an operator, after a yes. A run marked
// running whose runner is gone may be abandoned, so it gets the button too.
export function DeleteRun({ run, title }: { run: RunSummary; title: string }) {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  if (!operator || !(ENDED.has(run.state) || run.stale)) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="sm" />}>Delete</DialogTrigger>
      <DialogContent className="sm:max-w-md">
        {open && <Confirm run={run} title={title} />}
      </DialogContent>
    </Dialog>
  );
}

function Confirm({ run, title }: { run: RunSummary; title: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const family = useQuery(dependentsQuery(run.run_id));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const kinds = family.data?.runs.map((r) => r.kind) ?? [];
  const blocked = family.data?.blocked ?? null;
  const offline = family.data?.offline ?? null;
  const what = family.isPending
    ? "Looking for the runs made from it."
    : kinds.length > 0
      ? `It goes with ${goesWith(kinds)} made from it. Their logs, files and labels are deleted for good.`
      : "No other run was made from it. Its log, files and labels are deleted for good.";
  const remove = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const gone = await deleteRun(run.run_id, offline !== null);
      dropRuns(qc, gone);
      await navigate({ to: "/runs" });
      dropRuns(qc, gone);
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
      setBusy(false);
    }
  };
  return (
    <>
      <DialogHeader>
        <DialogTitle>
          Delete {title} {shortId(run.run_id)}?
        </DialogTitle>
        <DialogDescription>{what}</DialogDescription>
      </DialogHeader>
      {family.isError && <p>The runs made from it did not load.</p>}
      {blocked && <p className="font-medium">Not now: {blocked}.</p>}
      {!blocked && offline && (
        <p className="font-medium">
          It looks abandoned: {offline}. Its runner drops its copy if it comes back.
        </p>
      )}
      {problem && <p className="font-medium">{problem}</p>}
      <DialogFooter>
        <DialogClose render={<Button size="sm" variant="outline" />}>Cancel</DialogClose>
        <Button
          size="sm"
          variant="destructive"
          disabled={busy || !family.isSuccess || blocked !== null}
          onClick={() => void remove()}
        >
          {offline ? "Delete anyway" : "Delete"}
        </Button>
      </DialogFooter>
    </>
  );
}
