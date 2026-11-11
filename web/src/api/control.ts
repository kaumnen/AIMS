import { client, type Schemas } from "./client";

export type Hold = Schemas["HoldInfo"];
type CommandSent = Schemas["CommandSent"];
type CommandInfo = Schemas["CommandInfo"];

type ResumeBody = Schemas["ResumeBody"];

// A resume's `next` is the API's `then`, renamed so no object here looks like a promise.
export type Command =
  | ({ action: "pause" } & Schemas["PauseBody"])
  | ({ action: "resume"; next?: ResumeBody["then"] } & Omit<ResumeBody, "then">)
  | ({ action: "inject" } & Schemas["InjectBody"])
  | ({ action: "kill" | "cancel" } & Schemas["StopBody"]);

// done: the runner carried it out; failed: the runner refused it or the control plane dropped
// it, saying why; waiting: no answer yet.
export interface Answer {
  state: "done" | "failed" | "waiting";
  text: string;
}

const ACK_MS = 10_000;
const POLL_MS = 200;

async function post(runId: string, c: Command): Promise<CommandSent> {
  const params = { path: { run_id: runId } };
  const agent = "agent" in c ? (c.agent ?? null) : null;
  const reason = "reason" in c ? (c.reason ?? null) : null;
  let r;
  switch (c.action) {
    case "pause":
      r = await client.POST("/api/runs/{run_id}/pause", { params, body: { agent, reason } });
      break;
    case "resume": {
      // oxlint-disable-next-line unicorn/no-thenable
      const body = { agent, reason, then: c.next ?? "continue", text: c.text ?? null };
      r = await client.POST("/api/runs/{run_id}/resume", { params, body });
      break;
    }
    case "inject":
      r = await client.POST("/api/runs/{run_id}/inject", { params, body: { agent, text: c.text } });
      break;
    case "kill":
      r = await client.POST("/api/runs/{run_id}/kill", { params, body: { reason } });
      break;
    case "cancel":
      r = await client.POST("/api/runs/{run_id}/cancel", { params, body: { reason } });
      break;
  }
  if (r.error) throw r.error;
  return r.data;
}

const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms));

const answerOf = (c: CommandInfo): Answer => {
  const outcome = c.outcome ?? "done";
  return c.state === "done"
    ? { state: "done", text: outcome }
    : { state: "failed", text: outcome.replace(/^(failed|dropped): /, "") };
};

// Sends `c` and waits for the runner's answer, as `aims control` does. A drop as the run
// ended is not final: the runner may have carried the command out, and its answer may still
// come and say done.
export async function send(
  runId: string,
  c: Command,
  { wait = ACK_MS, every = POLL_MS } = {},
): Promise<Answer> {
  const sent = await post(runId, c);
  if (sent.state === "cancelled") return { state: "done", text: "Cancelled before it started." };
  const id = sent.command_id;
  const deadline = Date.now() + (id ? wait : 0);
  let found: CommandInfo | undefined;
  while (Date.now() < deadline) {
    // one poll at a time, like `aims control`
    // oxlint-disable-next-line no-await-in-loop
    const { data, error } = await client.GET("/api/runs/{run_id}/commands", {
      params: { path: { run_id: runId } },
    });
    if (error) throw error;
    found = data.find((x) => x.id === id);
    if (found?.final) return answerOf(found);
    // oxlint-disable-next-line no-await-in-loop
    await sleep(every);
  }
  if (found?.state === "dropped") return answerOf(found);
  return {
    state: "waiting",
    text: sent.delivered
      ? "Sent; the runner has not answered yet."
      : "Sent; the runner is offline and gets it when it is back.",
  };
}
