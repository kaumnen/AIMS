import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { send } from "./control";

const RUN = "01a18a50-bee5-7263-ad19-24385640cee1";
const fetch = vi.fn<(request: Request) => Promise<Response>>();

let polls: number;
let sent: { path: string; body: unknown }[];

const commands = (acked: boolean, outcome: string) => [
  {
    id: "k1",
    run_id: RUN,
    runner: "local",
    action: "resume",
    created: 0,
    acked: acked ? 1 : null,
    outcome: acked ? outcome : null,
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
  serve(SENT, "failed: no paused agent waits before a tool call to deny", 1);
  const answer = await send(RUN, { action: "pause" }, { every: 1 });
  expect(answer).toEqual({
    state: "failed",
    text: "no paused agent waits before a tool call to deny",
  });
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
