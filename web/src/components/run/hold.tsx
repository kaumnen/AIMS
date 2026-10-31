import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import type { Hold } from "@/api/control";
import { flagsQuery, type RunDetail, stepsQuery } from "@/api/run";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { duration, started } from "@/lib/format";
import { categoryText } from "@/lib/monitor";
import { useNow } from "@/lib/use-now";
import { useOperator } from "@/lib/use-operator";

import { CommandAnswer, StopForm, useCommand } from "./controls";
import { toolLine } from "./tool-text";

const REASON = "answered in the UI";

// The monitor's pauses still open, one per decision, oldest first.
export function monitorPauses(detail: RunDetail): Hold[][] {
  const by = new Map<number, Hold[]>();
  for (const h of detail.run.held) {
    if (h.by === "monitor") by.set(h.seq, [...(by.get(h.seq) ?? []), h]);
  }
  return [...by.values()];
}

// A pause under human oversight waits for a person: what caused it, where each agent waits,
// and the answers `aims run` offers at the terminal.
export function HoldBanner({
  detail,
  onFlag,
}: {
  detail: RunDetail;
  onFlag: (seq: number) => void;
}) {
  const pauses = monitorPauses(detail);
  if (pauses.length === 0 || detail.run.status !== "running") return null;
  return (
    <div className="flex flex-col gap-2 pb-3">
      {pauses.map((holds) => (
        <Pause key={holds[0]?.seq} runId={detail.run.run_id} holds={holds} onFlag={onFlag} />
      ))}
    </div>
  );
}

function Pause({
  runId,
  holds,
  onFlag,
}: {
  runId: string;
  holds: Hold[];
  onFlag: (seq: number) => void;
}) {
  const first = holds[0];
  const now = useNow(true);
  const operator = useOperator();
  const { run, busy, answer } = useCommand(runId);
  const [writing, setWriting] = useState(false);
  const [killing, setKilling] = useState(false);
  const [text, setText] = useState("");
  const steps = useInfiniteQuery(stepsQuery(runId));
  const flags = useQuery(flagsQuery(runId));
  if (first === undefined) return null;
  const human = first.mode === "human";
  const names = holds.map((h) => h.agent).join(", ");
  const left = first.until ? Date.parse(first.until) - now : null;
  const tools = new Map(
    (steps.data?.pages ?? []).flatMap((p) =>
      p.items.flatMap((s) => (s.tools ?? []).map((t) => [t.call_id, t] as const)),
    ),
  );
  const cited = (first.flags ?? []).map((seq) => ({
    seq,
    flag: flags.data?.find((f) => f.seq === seq || f.latest === seq),
  }));
  const deniable = holds.some((h) => h.at === "before_tool");

  // Like the terminal prompt: each agent of the pause goes on by itself; only an agent
  // waiting before a tool call has it denied.
  const answerAll = async (choice: "continue" | "deny" | "inject", message?: string) => {
    for (const h of holds) {
      const next = choice === "deny" && h.at !== "before_tool" ? "continue" : choice;
      // one agent at a time, so a refusal stops the rest
      // oxlint-disable-next-line no-await-in-loop
      const got = await run({
        action: "resume",
        agent: h.agent,
        next,
        reason: REASON,
        ...(next === "inject" ? { text: message ?? "" } : {}),
      });
      if (got.state === "failed") return;
    }
  };

  return (
    <section
      aria-label={`Monitor pause of ${names}`}
      className="border-foreground/40 flex flex-col gap-1.5 rounded-sm border px-3 py-2"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-0.5">
        <h2 className="font-medium">
          {human
            ? `The monitor paused ${names} and waits for an answer.`
            : `The monitor paused ${names} and reviews why.`}
        </h2>
        {left !== null && first.until && (
          <p className="text-muted-foreground tabular-nums">
            {human ? "With no answer the run is killed" : "Unless the review clears it, killed"} at{" "}
            {started(first.until)}, in {duration(left)}
          </p>
        )}
      </div>
      {first.reason && <p>{first.reason}</p>}
      {cited.length > 0 && (
        <p className="flex flex-wrap items-baseline gap-x-3">
          <span className="text-muted-foreground">Flags</span>
          {cited.map(({ seq, flag }) => (
            <button
              key={seq}
              type="button"
              className="hover:text-foreground text-left underline underline-offset-2"
              onClick={() => onFlag(flag?.seq ?? seq)}
            >
              #{seq}
              {flag && ` ${categoryText(flag.category)}, importance ${flag.importance}`}
            </button>
          ))}
        </p>
      )}
      <ul className="flex flex-col">
        {holds.map((h) => {
          const tool = h.call ? tools.get(h.call) : undefined;
          return (
            <li key={h.agent}>
              {h.agent} {where(h)}
              {tool && <span className="font-mono text-xs"> {toolLine(tool)}</span>}
            </li>
          );
        })}
      </ul>
      {human && operator && (
        <>
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <Button size="sm" disabled={busy} onClick={() => void answerAll("continue")}>
              Allow
            </Button>
            {deniable && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => void answerAll("deny")}
              >
                Deny the call
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              aria-expanded={writing}
              onClick={() => setWriting((w) => !w)}
            >
              Inject
            </Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={() => setKilling(true)}>
              Kill run
            </Button>
          </div>
          <Dialog open={killing} onOpenChange={setKilling}>
            <DialogContent className="sm:max-w-md">
              <StopForm
                action="kill"
                waiting={false}
                busy={busy}
                answer={answer}
                onStop={(reason) =>
                  void run({ action: "kill", reason: reason || "killed in the UI" }).then(
                    (got) => got.state !== "failed" && setKilling(false),
                  )
                }
                onClose={() => setKilling(false)}
              />
            </DialogContent>
          </Dialog>
          {writing && (
            <form
              className="flex flex-col gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                if (text.trim()) void answerAll("inject", text.trim());
              }}
            >
              <Textarea
                aria-label="Message for the agent"
                value={text}
                rows={3}
                onChange={(e) => setText(e.currentTarget.value)}
                autoFocus
              />
              <div>
                <Button type="submit" size="sm" disabled={busy || !text.trim()}>
                  Send and allow
                </Button>
              </div>
            </form>
          )}
          <CommandAnswer answer={answer} />
        </>
      )}
      {human && !operator && <p className="text-muted-foreground">Only operators can answer.</p>}
    </section>
  );
}

function where(h: Hold): string {
  switch (h.at) {
    case "before_tool":
      return "waits before running";
    case "after_tool":
      return "waits after a tool call";
    case "after_reply":
      return "waits after its model's reply, before any tool runs";
    case "before_model":
      return "waits before its next model call";
    default:
      return "is frozen while a tool call runs";
  }
}
