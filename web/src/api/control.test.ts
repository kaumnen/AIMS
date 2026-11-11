import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { send } from "./control";

const RUN = "01a18a50-bee5-7263-ad19-24385640cee1";
const fetch = vi.fn<(request: Request) => Promise<Response>>();

let polls = 0;
let sent: { path: string; body: unknown }[];

// as the server's command_state reads an answer
const stateOf = (outcome: string) => /^(failed|dropped)/.exec(outcome)?.[1] ?? "done";
// as the server keeps a drop by the run's end open to the runner's answer, which never comes here
const ENDED = "dropped: the run ended";

const commands = (acked: boolean, outcome: string) => [
  {
    id: "k1",
    run_id: RUN,
    runner: "local",
    action: "resume",
    created: 0,
    acked: acked ? 1 : null,
    outcome: acked ? outcome : null,
    state: acked ? stateOf(outcome) : "waiting",
    final: acked && outcome !== ENDED,
  },
];

function serve(reply: Record<string, unknown>, outcome = "done", ackOn = 2) {
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === "POST") {
      sent.push({ path, body: await request.json() });
      return Response.json(reply, { status: 202 });
    }
    polls += 1;
    return Response.json(commands(polls >= ackOn, outcome));
  });
}

beforeEach(() => {
  polls = 0;
  sent = [];
  vi.stubGlobal("fetch", fetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetch.mockReset();
});

const SENT = { command_id: "k1", delivered: true, state: "sent" };

test("a resume sends `then` and waits for the runner's answer", async () => {
  serve(SENT);
  const answer = await send(RUN, { action: "resume", agent: "a", next: "deny" }, { every: 1 });
  expect(answer).toEqual({ state: "done", text: "done" });
  expect(polls).toBe(2);
  expect(sent).toEqual([
    {
      path: `/api/runs/${RUN}/resume`,
      // oxlint-disable-next-line unicorn/no-thenable
      body: { agent: "a", reason: null, then: "deny", text: null },
    },
  ]);
});

test("a refusal comes back with the runner's reason", async () => {
  serve(SENT, "failed: no paused agent has a tool call waiting to deny", 1);
  const answer = await send(RUN, { action: "pause" }, { every: 1 });
  expect(answer).toEqual({
    state: "failed",
    text: "no paused agent has a tool call waiting to deny",
  });
});

// the two ways the control plane settles a command the runner has not answered
test("a command dropped as another token took the runner name is not done", async () => {
  serve(SENT, "dropped: another token took the runner name", 1);
  const answer = await send(RUN, { action: "kill", reason: null }, { every: 1 });
  expect(answer).toEqual({ state: "failed", text: "another token took the runner name" });
  expect(polls).toBe(1);
});

test("a command dropped as the run ended is not done when no answer comes in time", async () => {
  serve(SENT, ENDED, 1);
  const answer = await send(RUN, { action: "kill", reason: null }, { wait: 50, every: 1 });
  expect(answer).toEqual({ state: "failed", text: "the run ended" });
  expect(polls).toBeGreaterThan(1);
});

test("a kill dropped as the run ended is done once the runner's late answer lands", async () => {
  fetch.mockImplementation(async (request) => {
    if (request.method === "POST") return Response.json(SENT, { status: 202 });
    polls += 1;
    return Response.json(commands(true, polls < 3 ? ENDED : "done"));
  });
  const answer = await send(RUN, { action: "kill", reason: null }, { every: 1 });
  expect(answer).toEqual({ state: "done", text: "done" });
  expect(polls).toBe(3);
});

test("no answer in time says whether the runner got it", async () => {
  serve({ ...SENT, delivered: false }, "done", 1000);
  const answer = await send(RUN, { action: "kill", reason: "enough" }, { wait: 5, every: 1 });
  expect(answer.state).toBe("waiting");
  expect(answer.text).toContain("offline");
  expect(sent[0]?.body).toEqual({ reason: "enough" });
});

test("a run cancelled in the queue needs no answer", async () => {
  serve({ command_id: null, delivered: false, state: "cancelled" });
  expect(await send(RUN, { action: "cancel" })).toEqual({
    state: "done",
    text: "Cancelled before it started.",
  });
  expect(polls).toBe(0);
});
