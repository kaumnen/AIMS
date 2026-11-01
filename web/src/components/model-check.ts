import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { checkModels, modelsQuery, type ProfileInfo } from "@/api/launch";
import { isProblem } from "@/api/session";

import { useLive } from "./live";

export const CHECK_TEXT = {
  ok: "ok",
  warn: "works with warnings",
  fail: "failed",
  running: "checking",
  none: "never checked",
} as const;

// how often the models are read while a check runs, in case the stream misses its end
const POLL_MS = 3000;

// Whether the models read name check run `run` as ended.
const ended = (data: { profiles: ProfileInfo[] } | undefined, run: string) =>
  (data?.profiles ?? []).some((p) => p.check?.run_id === run && p.check.status !== "running");

// One provider check at a time: the profiles it covers until its run ends, then the models
// query again for the result.
export function useModelCheck() {
  const qc = useQueryClient();
  const live = useLive();
  const [checking, setChecking] = useState<{ run: string; profiles: string[] } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (checking === null) return;
    return live.on("run", ({ run }) => {
      if (run.run_id !== checking.run || run.state !== "done") return;
      setChecking(null);
      void qc.invalidateQueries({ queryKey: modelsQuery.queryKey });
    });
  }, [live, qc, checking]);
  const models = useQuery({
    ...modelsQuery,
    enabled: checking !== null,
    refetchInterval: (q) =>
      checking === null || ended(q.state.data, checking.run) ? false : POLL_MS,
  });
  const current = checking !== null && !ended(models.data, checking.run) ? checking : null;
  const check = async (profiles: string[]) => {
    setProblem(null);
    try {
      setChecking({ run: await checkModels(profiles), profiles });
    } catch (e) {
      setProblem(isProblem(e) ? (e.detail ?? e.title) : "The check did not start.");
    }
  };
  return { checking: current?.profiles ?? null, problem, check };
}
