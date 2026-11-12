import { lazy, Suspense, useState } from "react";

import type { RunDetail, StepInfo } from "@/api/run";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useOperator } from "@/lib/use-operator";

import { agentsOf } from "./controls";
import { sealedScenario } from "./review";

const ForkForm = lazy(() => import("./fork-form"));

// Replay the run up to this step and go on live in a new run, for an operator on a sealed run.
export function ForkHere({ detail, step }: { detail: RunDetail; step: StepInfo }) {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  if (!operator || !sealedScenario(detail.run) || !step.agent_id) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="xs" className="ml-auto" />}>
        Fork from here
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Fork this run</DialogTitle>
          <DialogDescription>
            Replays the recorded steps up to the cut at no model cost, then goes on live in a new
            run. This run never changes.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <Suspense fallback={<p className="text-muted-foreground">Loading the form</p>}>
            <ForkForm
              detail={detail}
              step={step}
              multi={agentsOf(detail).length > 1}
              onDone={() => setOpen(false)}
            />
          </Suspense>
        )}
      </DialogContent>
    </Dialog>
  );
}
