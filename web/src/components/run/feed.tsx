import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import type { RunDetail, StepInfo } from "@/api/run";
import type { InFlight } from "@/api/tail";
import { Button } from "@/components/ui/button";

import { ForkHere } from "./fork";
import { InFlightStep } from "./live-tail";
import { StepCells, type StepFlags } from "./step-list";
import { Transcript } from "./transcript";

// How many of the latest steps the feed lists; the step list holds every step.
export const FEED_STEPS = 50;

// A live run as it goes: the newest step open, each step before it one line that opens in place.
// While the reader is at the end, the step that was newest closes when the next one lands. A
// reader scrolled up keeps it open, so the text they are reading stays.
export function Feed({
  runId,
  detail,
  steps,
  more,
  multi,
  flags,
  harm,
  writing,
  atEnd,
  follows,
}: {
  runId: string;
  detail: RunDetail;
  steps: readonly StepInfo[];
  more: boolean;
  multi: boolean;
  flags: ReadonlyMap<number, StepFlags>;
  harm: ReadonlySet<number>;
  // each agent's next step while its model writes it
  writing: readonly { draft: InFlight; n: number }[];
  atEnd: boolean;
  // counts presses of Follow latest; each one closes every step but the newest
  follows: number;
}) {
  const newest = steps.at(-1);
  // steps the reader opened or closed, by seq
  const [open, setOpen] = useState<ReadonlyMap<number, boolean>>(new Map());
  const [last, setLast] = useState(newest?.seq);
  if (newest !== undefined && newest.seq !== last) {
    setLast(newest.seq);
    if (!atEnd && last !== undefined && !open.has(last)) setOpen(new Map(open).set(last, true));
  }
  const [seenFollows, setSeenFollows] = useState(follows);
  if (follows !== seenFollows) {
    setSeenFollows(follows);
    setOpen(new Map());
  }
  const toggle = (seq: number, to: boolean) => setOpen(new Map(open).set(seq, to));

  // A step's own events start after the step before it of the same agent.
  const after = new Map<number, number>();
  const ends = new Map<string, number>();
  const latest = new Map<string, number>();
  for (const s of steps) {
    const agent = s.agent_id ?? "";
    after.set(s.seq, ends.get(agent) ?? -1);
    ends.set(agent, s.seq_to);
    latest.set(agent, s.seq);
  }

  const shown = steps.slice(-FEED_STEPS);
  const hidden = steps.length - shown.length;
  return (
    <div className="flex flex-col">
      {hidden > 0 && (
        <p className="text-muted-foreground pb-2">
          {hidden} earlier {hidden === 1 ? "step is" : "steps are"} in the step list.
        </p>
      )}
      {shown.map((s) => {
        const f = flags.get(s.seq);
        if (!(open.get(s.seq) ?? s === newest)) {
          return (
            <button
              key={s.seq}
              type="button"
              aria-expanded={false}
              onClick={() => toggle(s.seq, true)}
              className="hover:bg-accent/60 focus-visible:ring-ring/50 flex h-7 w-full shrink-0 items-center gap-2 rounded-sm px-1 text-left outline-none focus-visible:ring-2"
            >
              <ChevronRight aria-hidden className="text-muted-foreground size-3.5 shrink-0" />
              <StepCells
                step={s}
                multi={multi}
                flagged={f?.count ?? 0}
                red={f?.red ?? false}
                harmed={harm.has(s.seq)}
                named
              />
            </button>
          );
        }
        return (
          <div key={s.seq} className="pt-3">
            <Transcript
              runId={runId}
              step={s}
              after={after.get(s.seq) ?? -1}
              last={latest.get(s.agent_id ?? "") === s.seq && !more}
              multi={multi}
              actions={
                <div className="ml-auto flex items-center gap-1">
                  <ForkHere detail={detail} step={s} />
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Collapse step ${s.n}`}
                    aria-expanded
                    onClick={() => toggle(s.seq, false)}
                  >
                    <ChevronDown aria-hidden />
                  </Button>
                </div>
              }
            />
          </div>
        );
      })}
      {writing.map((w) => (
        <InFlightStep key={w.draft.call} draft={w.draft} n={w.n} />
      ))}
    </div>
  );
}
