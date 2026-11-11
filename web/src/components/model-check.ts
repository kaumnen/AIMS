import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

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

// One provider check at a time: the profiles it covers from the click until its run ends, then
// the models query again for the result.
export function useModelCheck() {
  const qc = useQueryClient();
  const live = useLive();
  // `run` is null while the check is being started
  const [checking, setChecking] = useState<{ run: string | null; profiles: string[] } | null>(null);
  // A check is paid for, so a second click before the page shows the first starts nothing.
  const starting = useRef(false);
  const [problem, setProblem] = useState<string | null>(null);
  const run = checking?.run ?? null;
  useEffect(() => {
    if (run === null) return;
    return live.on("run", (m) => {
      if (m.run.run_id !== run || m.run.state !== "done") return;
      setChecking(null);
      void qc.invalidateQueries({ queryKey: modelsQuery.queryKey });
    });
  }, [live, qc, run]);
  const models = useQuery({
    ...modelsQuery,
    enabled: run !== null,
    refetchInterval: (q) => (run === null || ended(q.state.data, run) ? false : POLL_MS),
  });
  const current = checking !== null && (run === null || !ended(models.data, run)) ? checking : null;
  const check = async (profiles: string[]) => {
    if (starting.current || current !== null) return;
    starting.current = true;
    setChecking({ run: null, profiles });
    setProblem(null);
    try {
      setChecking({ run: await checkModels(profiles), profiles });
    } catch (e) {
      setChecking(null);
      setProblem(isProblem(e) ? (e.detail ?? e.title) : "The check did not start.");
    } finally {
      starting.current = false;
    }
  };
  return { checking: current?.profiles ?? null, problem, check };
}
