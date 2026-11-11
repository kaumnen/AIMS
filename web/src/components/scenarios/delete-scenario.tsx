import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { deleteScenario } from "@/api/scenarios";
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
import { useOperator } from "@/lib/use-operator";

// Deletes a scenario of a source that takes edits, for an operator, after a yes.
export function DeleteScenario({
  source,
  name,
  writable,
}: {
  source: string;
  name: string;
  writable: boolean;
}) {
  const operator = useOperator();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!operator || !writable) return null;
  const remove = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await deleteScenario(source, name);
      // a draft of its files went with it, so leaving asks nothing
      await navigate({ to: "/scenarios", ignoreBlocker: true });
      qc.removeQueries({ queryKey: ["scenarios", "detail", source, name] });
      void qc.invalidateQueries({ queryKey: ["scenarios"], exact: true });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setProblem(null);
      }}
    >
      <DialogTrigger render={<Button size="sm" variant="outline" />}>Delete</DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete scenario {name}?</DialogTitle>
          <DialogDescription>
            Its folder and every file in it are deleted for good. Its runs stay.
          </DialogDescription>
        </DialogHeader>
        {problem && <p className="font-medium">{problem}</p>}
        <DialogFooter>
          <DialogClose render={<Button size="sm" variant="outline" />}>Cancel</DialogClose>
          <Button size="sm" variant="destructive" disabled={busy} onClick={() => void remove()}>
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
