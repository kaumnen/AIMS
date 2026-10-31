import { useQuery } from "@tanstack/react-query";

import { scoreQuery, scoresQuery } from "@/api/score";
import { whoami } from "@/api/session";
import { readScore } from "@/lib/score";

// Where the newest finished score says each agent first did harm, for the timeline and the
// step list; none for a viewer or before a score.
export function useHarm(runId: string): { seq: number; agent: string }[] {
  const me = useQuery(whoami);
  const operator = me.data?.role === "operator";
  const scores = useQuery({ ...scoresQuery(runId), enabled: operator });
  const newest = (scores.data?.items ?? []).find((s) => s.status === "completed");
  const events = useQuery({
    ...scoreQuery(newest?.run_id ?? "", newest?.status ?? ""),
    enabled: operator && newest !== undefined,
  });
  if (!events.data) return [];
  return readScore(events.data)
    .units.flatMap((u) =>
      u.harm_seq === null || u.harm_seq === undefined ? [] : [{ seq: u.harm_seq, agent: u.agent }],
    )
    .toSorted((a, b) => a.seq - b.seq);
}
