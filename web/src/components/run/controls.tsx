import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { useState } from "react";

import { type Answer, type Command, send } from "@/api/control";
import type { RunDetail } from "@/api/run";
import { isProblem, whoami } from "@/api/session";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const DONE: Record<Command["action"], string> = {
  pause: "Paused.",
  resume: "Resumed.",
  inject: "The agent reads it before its next model call.",
  kill: "Killing the run.",
  cancel: "Cancelling the run.",
};

const problemText = (e: unknown) =>
  isProblem(e) ? (e.detail ?? e.title) : "The control plane did not answer.";

// Sends commands to a run and keeps the runner's last answer to show.
export function useCommand(runId: string) {
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const run = async (c: Command): Promise<Answer> => {
    setBusy(true);
    setAnswer(null);
    let got: Answer;
    try {
      got = await send(runId, c);
      if (got.state === "done" && got.text === "done") got = { ...got, text: DONE[c.action] };
    } catch (error) {
      got = { state: "failed", text: problemText(error) };
    }
    setBusy(false);
    setAnswer(got);
    return got;
  };
  return { run, busy, answer };
}

export function CommandAnswer({ answer }: { answer: Answer | null }) {
  if (answer === null) return null;
  return (
    <p
      role="status"
      className={cn(answer.state === "failed" ? "text-flag" : "text-muted-foreground")}
    >
      {answer.state === "failed" ? `Not done: ${answer.text}` : answer.text}
    </p>
  );
}

export function useOperator(): boolean {
  const me = useQuery(whoami);
  return me.data?.role === "operator";
}

export const agentsOf = (detail: RunDetail): string[] => {
  const agents = detail.started?.data["agents"];
  return Array.isArray(agents) ? agents.filter((a): a is string => typeof a === "string") : [];
};

const WAITING = new Set(["queued", "offered", "claimed"]);

// Pause, resume, inject, cancel and kill, for an operator while the run is open.
export function RunControls({ detail }: { detail: RunDetail }) {
  const operator = useOperator();
  const { run: command, busy, answer } = useCommand(detail.run.run_id);
  const [dialog, setDialog] = useState<"inject" | "kill" | "cancel" | null>(null);
  const run = detail.run;
  const live = run.status === "running" && run.state === "running";
  const waiting = WAITING.has(run.state);
  if (!operator || (!live && !waiting)) return null;
  const agents = agentsOf(detail);
  const held = [...new Set(run.held.map((h) => h.agent))];
  const free = agents.filter((a) => !held.includes(a));
  // a self-test's agents follow scripts: there is nothing to pause or tell them
  const steered = live && run.kind !== "selftest" && run.kind !== "selftest.case";
  const pausable = agents.length === 0 ? held.length === 0 : free.length > 0;
  const close = () => setDialog(null);
  const act = (c: Command) => {
    void command(c).then((a) => {
      if (a.state !== "failed") close();
    });
  };
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap items-center gap-1.5">
        {steered && pausable && (
          <Each
            label="Pause"
            agents={agents.length > 1 ? free : []}
            disabled={busy}
            onPick={(agent) =>
              void command({ action: "pause", ...(agent ? { agent } : {}), reason: null })
            }
          />
        )}
        {steered && held.length > 0 && (
          <Each
            label="Resume"
            agents={agents.length > 1 ? held : []}
            disabled={busy}
            onPick={(agent) =>
              void command({ action: "resume", next: "continue", ...(agent ? { agent } : {}) })
            }
          />
        )}
        {steered && (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => setDialog("inject")}>
            Inject
          </Button>
        )}
        <Button variant="outline" size="sm" disabled={busy} onClick={() => setDialog("cancel")}>
          Cancel
        </Button>
        {live && (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => setDialog("kill")}>
            Kill
          </Button>
        )}
      </div>
      {dialog === null && <CommandAnswer answer={answer} />}
      <Dialog open={dialog !== null} onOpenChange={(open) => !open && close()}>
        <DialogContent className="sm:max-w-md">
          {dialog === "inject" && (
            <InjectForm
              agents={agents}
              busy={busy}
              answer={answer}
              onSend={(agent, text) => act({ action: "inject", text, ...(agent ? { agent } : {}) })}
              onClose={close}
            />
          )}
          {(dialog === "kill" || dialog === "cancel") && (
            <StopForm
              action={dialog}
              waiting={waiting}
              busy={busy}
              answer={answer}
              onStop={(reason) => act({ action: dialog, reason: reason || null })}
              onClose={close}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

// One button for one agent; a menu of every agent and each of `agents` for several.
function Each({
  label,
  agents,
  disabled,
  onPick,
}: {
  label: string;
  agents: string[];
  disabled: boolean;
  onPick: (agent: string | null) => void;
}) {
  if (agents.length === 0) {
    return (
      <Button variant="outline" size="sm" disabled={disabled} onClick={() => onPick(null)}>
        {label}
      </Button>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" size="sm" disabled={disabled} />}>
        {label}
        <ChevronDown aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => onPick(null)}>Every agent</DropdownMenuItem>
        <DropdownMenuSeparator />
        {agents.map((a) => (
          <DropdownMenuItem key={a} onClick={() => onPick(a)}>
            {a}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const EVERY = "";

function InjectForm({
  agents,
  busy,
  answer,
  onSend,
  onClose,
}: {
  agents: string[];
  busy: boolean;
  answer: Answer | null;
  onSend: (agent: string | null, text: string) => void;
  onClose: () => void;
}) {
  const [agent, setAgent] = useState(EVERY);
  const [text, setText] = useState("");
  const items = [
    { value: EVERY, label: agents.length > 1 ? "Every agent" : (agents[0] ?? "The agent") },
    ...(agents.length > 1 ? agents.map((a) => ({ value: a, label: a })) : []),
  ];
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) onSend(agent || null, text.trim());
      }}
    >
      <DialogHeader>
        <DialogTitle>Inject a message</DialogTitle>
        <DialogDescription>
          The agent reads it as a user message before its next model call.
        </DialogDescription>
      </DialogHeader>
      {agents.length > 1 && (
        <Select items={items} value={agent} onValueChange={(v) => setAgent(String(v ?? EVERY))}>
          <SelectTrigger size="sm" aria-label="Agent" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} align="start">
            {items.map((i) => (
              <SelectItem key={i.value || "every"} value={i.value}>
                {i.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Textarea
        aria-label="Message"
        value={text}
        rows={4}
        onChange={(e) => setText(e.currentTarget.value)}
        autoFocus
      />
      <CommandAnswer answer={answer} />
      <DialogFooter>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
        <Button type="submit" size="sm" disabled={busy || !text.trim()}>
          Send
        </Button>
      </DialogFooter>
    </form>
  );
}

const STOP = {
  kill: {
    title: "Kill the run",
    text: "Freezes, archives and kills every sandbox. The verifier does not run.",
    button: "Kill run",
  },
  cancel: {
    title: "Cancel the run",
    text: "Stops the agents, skips the verifier, then archives and seals the run as cancelled.",
    button: "Cancel run",
  },
};

export function StopForm({
  action,
  waiting,
  busy,
  answer,
  onStop,
  onClose,
}: {
  action: "kill" | "cancel";
  waiting: boolean;
  busy: boolean;
  answer: Answer | null;
  onStop: (reason: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const copy = STOP[action];
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        onStop(reason.trim());
      }}
    >
      <DialogHeader>
        <DialogTitle>{copy.title}</DialogTitle>
        <DialogDescription>
          {waiting ? "The run has not started; it leaves the queue." : copy.text}
        </DialogDescription>
      </DialogHeader>
      <Input
        aria-label="Reason"
        placeholder="Reason, kept in the log"
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
        autoFocus
      />
      <CommandAnswer answer={answer} />
      <DialogFooter>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Keep running
        </Button>
        <Button type="submit" size="sm" disabled={busy}>
          {copy.button}
        </Button>
      </DialogFooter>
    </form>
  );
}
