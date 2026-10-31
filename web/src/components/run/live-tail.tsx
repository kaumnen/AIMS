import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import { type InFlight, RunTail } from "@/api/tail";

const NONE: readonly InFlight[] = [];
const quiet = () => () => undefined;

// While the run is open, one stream of its events and model output keeps the page current.
// `after` is the last seq the page holds when the stream opens.
export function useRunTail(runId: string, open: boolean, after: number): readonly InFlight[] {
  const qc = useQueryClient();
  const from = useRef(after);
  useEffect(() => {
    from.current = after;
  });
  const tail = useMemo(() => (open ? new RunTail(runId, qc) : null), [runId, open, qc]);
  useEffect(() => {
    if (tail === null) return;
    tail.start(from.current);
    return () => tail.stop();
  }, [tail]);
  return useSyncExternalStore(tail?.watch ?? quiet, () => tail?.inflight ?? NONE);
}

// The next step of an agent while its model still writes it.
export function InFlightStep({ draft, n }: { draft: InFlight; n: number }) {
  const empty = !draft.text && !draft.reasoning && draft.tools.length === 0;
  return (
    <section aria-label={`Step ${n}, in flight`} className="flex flex-col gap-1.5 border-t pt-3">
      <h3 className="text-muted-foreground flex items-baseline gap-2">
        <span className="text-foreground font-medium">Step {n}</span>
        {draft.ended ? "written, logging" : "writing"}
        {draft.attempt > 1 && <span>attempt {draft.attempt}</span>}
      </h3>
      {draft.reasoning && (
        <details>
          <summary className="text-muted-foreground cursor-default">Reasoning</summary>
          <p className="mt-1 whitespace-pre-wrap">{draft.reasoning}</p>
        </details>
      )}
      {draft.text && <p className="whitespace-pre-wrap">{draft.text}</p>}
      {draft.tools.map((t, i) => (
        // tool drafts have no id until the response is logged
        // oxlint-disable-next-line react/no-array-index-key
        <pre key={i} className="bg-muted/50 overflow-x-auto rounded-sm p-2 font-mono text-xs">
          {t.name}
          {t.args ? ` ${t.args}` : ""}
        </pre>
      ))}
      {empty && <p className="text-muted-foreground">Waiting for the first words.</p>}
      {draft.partial && (
        <p className="text-muted-foreground">Some of this output was dropped on the way here.</p>
      )}
    </section>
  );
}
