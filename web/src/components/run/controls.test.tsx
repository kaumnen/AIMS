// What RunControls shows of a kill's answer when the run ends around it. The answer here is one
// done in part, which says more than the header's new status does. Also what a dialog keeps and
// shows: a typed message, and only the answer to its own command.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { RunDetail } from "@/api/run";
import { whoami } from "@/api/session";
import { run } from "@/test/runs";

import { RunControls } from "./controls";

const ID = "01a18a50-bee5-7263-ad19-24385640cee1";
const OUTCOME = "the sandbox of agent a did not stop: docker kill timed out";
const fetch = vi.fn<(request: Request) => Promise<Response>>();
// whether the runner's answer is in yet
let acked: boolean;

const detail = (over: Parameters<typeof run>[1]): RunDetail =>
  ({
    run: run(ID, over),
    started: null,
    finished: null,
    verdict: null,
    monitor_runs: [],
    score_runs: [],
    budget: [],
    series_previous: null,
    replayable: {},
  }) as unknown as RunDetail;

beforeEach(() => {
  acked = false;
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session") return Response.json({ name: "op", role: "operator", auth: true });
    if (path.endsWith("/pause")) {
      return Response.json({ command_id: "p1", delivered: true, state: "sent" }, { status: 202 });
    }
    if (path.endsWith("/kill")) {
      return Response.json({ command_id: "k1", delivered: true, state: "sent" }, { status: 202 });
    }
    if (path.endsWith("/commands")) {
      return Response.json([
        { id: "p1", run_id: ID, action: "pause", outcome: "done", state: "done", final: true },
        {
          id: "k1",
          run_id: ID,
          runner: "local",
          action: "kill",
          agent: null,
          // oxlint-disable-next-line unicorn/no-thenable
          then: null,
          text: null,
          reason: null,
          by: "op",
          created: 0,
          acked: acked ? 1 : null,
          outcome: acked ? OUTCOME : null,
          state: acked ? "done" : "waiting",
          in_part: acked,
          final: acked,
        },
      ]);
    }
    return Response.json({});
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function mount() {
  const qc = new QueryClient();
  qc.setQueryData(whoami.queryKey, { name: "op", role: "operator", auth: true });
  const live = detail({ status: "running", state: "running" });
  const view = render(
    <QueryClientProvider client={qc}>
      <RunControls detail={live} />
    </QueryClientProvider>,
  );
  const end = () =>
    view.rerender(
      <QueryClientProvider client={qc}>
        <RunControls detail={detail({ status: "killed", state: "done", commands: 1 })} />
      </QueryClientProvider>,
    );
  return end;
}

async function kill() {
  fireEvent.click(await screen.findByRole("button", { name: "Kill" }));
  const dialog = await screen.findByRole("dialog", { name: "Kill the run" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Kill run" }));
}

test("the answer to a kill stays once the run has ended", async () => {
  const end = mount();
  await kill();
  acked = true;
  expect(await screen.findByText(OUTCOME)).toBeTruthy();
  act(() => end());
  expect(screen.getByText(OUTCOME)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Kill" })).toBeNull();
});

test("an answer that comes after the run's end still shows", async () => {
  const end = mount();
  await kill();
  act(() => end());
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByText(OUTCOME)).toBeNull();
  acked = true;
  expect(await screen.findByText(OUTCOME)).toBeTruthy();
});

test("an ended run with no command sent shows no controls", () => {
  const end = mount();
  act(() => end());
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

test("a dialog does not show the answer of the command before it", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
  expect(await screen.findByText("Paused.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await screen.findByRole("dialog", { name: "Cancel the run" });
  expect(screen.queryByText("Paused.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Keep running" }));
  expect(await screen.findByText("Paused.")).toBeTruthy();
});

test("a typed message is still there when the inject dialog opens again", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Inject" }));
  const box = within(await screen.findByRole("dialog")).getByRole("textbox");
  fireEvent.change(box, { target: { value: "stop reading the tests" } });
  fireEvent.keyDown(box, { key: "Escape" });
  await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Inject" }));
  const again = within(await screen.findByRole("dialog")).getByRole("textbox");
  expect((again as HTMLTextAreaElement).value).toBe("stop reading the tests");
});
