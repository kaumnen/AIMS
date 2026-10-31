import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { startSelftest } from "@/api/selftest";
import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { Button } from "@/components/ui/button";

// Queue the scenario's self-test and follow it; scripted agents, so no model is called.
export function SelfTestButton({ source, name }: { source: string; name: string }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const start = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const runId = await startSelftest(source, name);
      void navigate({ to: "/runs/$runId", params: { runId } });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The self-test did not start.");
      setBusy(false);
    }
  };
  return (
    <>
      {problem && <span className="max-w-md text-right">{problem}</span>}
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        title="Runs the reference and exploit scripts at no model cost"
        onClick={() => void start()}
      >
        Self-test
      </Button>
    </>
  );
}
