import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Event } from "@/api/events";
import type { FlagInfo, RunDetail, SampleWindow, StepInfo } from "@/api/run";
import { makeRouter } from "@/router";
import { FakeEventSource } from "@/test/event-source";
import { stubHeight } from "@/test/layout";
import { run } from "@/test/runs";

const BEE5 = "01a18a50-bee5-7263-ad19-24385640cee1";
const ARGS = "a".repeat(64);
const fetch = vi.fn<(request: Request) => Promise<Response>>();

stubHeight();

const ev = (seq: number, type: string, data: Record<string, unknown>): Event =>
  ({
    agent_id: "agent",
    data,
    hash: "",
    prev_hash: "",
    run_id: BEE5,
    seq,
    ts: `2026-10-30T10:00:${String(seq).padStart(2, "0")}Z`,
    type,
    v: 1,
  }) as Event;

const baseEvents = (): Event[] => [
  ev(1, "agent.input", { kind: "instruction", text: "Fix the failing tests." }),
  ev(2, "model.response", {
    call_id: "m1",
    text: "Running the tests first.",
    reasoning: "Check what fails.",
    finish_reason: "tool_calls",
    usage: { input_tokens: 1370, output_tokens: 108 },
    latency_ms: 1141,
  }),
  ev(3, "tool.call", {
    call_id: "c1",
    name: "bash",
    arguments: '{"command": "python -m pytest -q", "timeout": 60}',
  }),
  ev(4, "agent.step", { n: 1, call_id: "m1", status: "completed" }),
  ev(5, "exec.started", { exec_id: "x1", call_id: "c1", command: "python -m pytest -q" }),
  ev(6, "file.access", { exec_id: "x1", op: "read", path: "/work/kiln/canonical.py" }),
  ev(7, "exec.finished", {
    exec_id: "x1",
    exit_code: 1,
    end: "exited",
    stdout: "1 failed, 7 passed",
    stderr: "",
    procs: 3,
  }),
  ev(8, "tool.result", { call_id: "c1", output: "1 failed, 7 passed" }),
  ev(9, "model.response", { call_id: "m2", text: "", finish_reason: "tool_calls" }),
  ev(10, "tool.call", { call_id: "c2", name: "edit", arguments: { sha256: ARGS, size: 1200 } }),
  ev(11, "agent.step", { n: 2, call_id: "m2", status: "completed" }),
  ev(12, "tool.result", { call_id: "c2", output: "File written." }),
  ev(13, "agent.finished", { answer: "Fixed the encoder.", reason: "submitted" }),
  ev(14, "task.scored", { reward: 1, metrics: { passed: 8, total: 8 } }),
];

const step = (over: Partial<StepInfo> & Pick<StepInfo, "seq" | "n">): StepInfo => ({
  agent_id: "agent",
  call_id: `m${over.n}`,
  seq_from: over.seq - 2,
  seq_to: over.seq,
  status: "completed",
  ts_from: "2026-10-30T10:00:00Z",
  ts_to: "2026-10-30T10:00:01Z",
  tools: [],
  ...over,
});

const BOOT = 5_000_000_000;
const T0 = Date.parse("2026-10-30T10:00:00Z") * 1000;

const sample = (i: number): SampleWindow => ({
  agent_id: "agent",
  count: 50,
  cpu_full_usec: null,
  cpu_some_usec: null,
  cpu_system_usec: null,
  cpu_throttled: null,
  cpu_throttled_usec: null,
  cpu_usec: i * 200_000,
  cpu_user_usec: null,
  hz: 50,
  io_full_usec: null,
  io_rbytes: 0,
  io_rios: null,
  io_some_usec: null,
  io_wbytes: 0,
  io_wios: null,
  memory_bytes: 50 << 20,
  memory_full_usec: null,
  memory_some_usec: null,
  pids: 3,
  run_id: BEE5,
  sandbox_id: "s1",
  seq: 1000 + i,
  ts_from: BOOT + i * 1_000_000,
  ts_to: BOOT + i * 1_000_000 + 999_999,
});

const flag = (
  seq: number,
  evidence: number[],
  importance: number,
  status: FlagInfo["status"] = "open",
): FlagInfo => ({
  agent_id: "agent",
  category: "scorer_tampering",
  count: 1,
  evidence,
  final: false,
  first_seq: null,
  importance,
  justification: "",
  key: null,
  lane: null,
  last_seq: null,
  latest: seq,
  layer: "signal",
  phase: null,
  quote: null,
  quote_verified: null,
  reflags: 0,
  run_id: BEE5,
  seq,
  source: "fs",
  status,
  status_reason: null,
  status_seq: null,
  status_source: null,
  subcategory: null,
  suspicion: null,
  tainted: false,
  ts: "2026-10-30T10:06:31Z",
});

let events: Event[];
let steps: StepInfo[];
let detail: RunDetail;
let samples: SampleWindow[];
let flags: FlagInfo[];

beforeEach(() => {
  FakeEventSource.all = [];
  events = baseEvents();
  steps = [
    step({
      seq: 4,
      n: 1,
      seq_from: 2,
      seq_to: 8,
      cost_usd: 0.0008,
      tools: [
        {
          seq: 3,
          call_id: "c1",
          name: "bash",
          args: { command: "python -m pytest -q", timeout: 60 },
          execs: ["x1"],
          exit_code: 1,
          end: "exited",
          duration_ms: 812,
          done: true,
        },
      ],
    }),
    step({
      seq: 11,
      n: 2,
      seq_from: 9,
      seq_to: 12,
      ts_from: "2026-10-30T10:06:00Z",
      ts_to: "2026-10-30T10:06:30Z",
      tools: [{ seq: 10, call_id: "c2", name: "edit", args: null, args_bytes: 1200, done: true }],
    }),
  ];
  detail = {
    run: run(BEE5, {
      status: "completed",
      state: "done",
      started_at: "2026-10-30T10:00:00+00:00",
      ended_at: "2026-10-30T10:12:04+00:00",
      variant: "observed",
      steps: 72,
      cost_usd: 1.12123625,
      reward: 1,
    }),
    started: ev(0, "run.started", { agents: [{ id: "agent" }] }),
    finished: null,
    verdict: null,
    monitor_runs: [],
  };
  samples = [];
  flags = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetch);
  fetch.mockImplementation(async (request) => {
    const url = new URL(request.url);
    const q = url.searchParams;
    const path = url.pathname;
    if (path === "/api/session") return Response.json({ name: "op", role: "operator", auth: true });
    if (path === "/api/scenarios") return Response.json([]);
    if (path === "/api/runs") return Response.json({ items: [], next: null });
    if (path === `/api/runs/${BEE5}`) return Response.json(detail);
    if (path === `/api/runs/${BEE5}/steps`) return Response.json({ items: steps, next: null });
    if (path === `/api/runs/${BEE5}/activity`) {
      return Response.json(
        q.getAll("exec").map((id) => ({
          exec_id: id,
          events: 130,
          changes: 1,
          file_ops: { read: 120 },
          types: { "proc.exec": 3 },
        })),
      );
    }
    if (path === `/api/runs/${BEE5}/blobs/${ARGS}`) {
      return new Response('{"command": "create", "path": "/work/kiln/canonical.py"}');
    }
    if (path === `/api/runs/${BEE5}/samples`) {
      const limit = Number(q.get("limit") ?? 1000);
      const items = samples.filter((w) => w.ts_to >= Number(q.get("ts_from") ?? 0)).slice(0, limit);
      const last = items.at(-1);
      return Response.json({ items, next: items.length === limit && last ? last.ts_to + 1 : null });
    }
    if (path === `/api/runs/${BEE5}/flags`) return Response.json({ items: flags, next: null });
    if (path === `/api/runs/${BEE5}/events` && q.getAll("types").join() === "sandbox.started") {
      const started = ev(0, "sandbox.started", {
        sandbox_id: "s1",
        config: { kernel_clock: { boot_us: BOOT, tai_us: T0 } },
      });
      return Response.json({ items: [{ ...started, ts: "2026-10-30T10:00:00Z" }], next: null });
    }
    if (path === `/api/runs/${BEE5}/events`) {
      const types = q.getAll("types");
      const execs = q.getAll("exec");
      const text = q.get("q");
      const items = events.filter(
        (e) =>
          e.seq > Number(q.get("after") ?? -1) &&
          e.seq < Number(q.get("before") ?? Infinity) &&
          (types.length === 0 || types.includes(e.type)) &&
          (execs.length === 0 || execs.includes(String(e.data["exec_id"]))) &&
          (!text || JSON.stringify(e.data).includes(text)),
      );
      return Response.json({ items, next: null });
    }
    return Response.json(
      { type: "about:blank", title: "Not found", status: 404 },
      { status: 404, headers: { "content-type": "application/problem+json" } },
    );
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetch.mockReset();
});

function renderAt(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={router.options.context.queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

// The timeline's own reads (sandbox starts, sweeps) are left out.
const TIMELINE = new Set(["sandbox.started", "monitor.sweep"]);
const eventQueries = () =>
  fetch.mock.calls
    .map(([r]) => new URL(r.url))
    .filter((u) => u.pathname === `/api/runs/${BEE5}/events`)
    .map((u) => u.searchParams)
    .filter((q) => !q.getAll("types").some((t) => TIMELINE.has(t)));

const requests = (suffix: string) =>
  fetch.mock.calls
    .map(([r]) => new URL(r.url))
    .filter((u) => u.pathname === `/api/runs/${BEE5}/${suffix}`)
    .map((u) => u.searchParams);

test("a run opens on its first step", async () => {
  renderAt(`/runs/${BEE5}`);
  expect(await screen.findByRole("heading", { name: /cache-keys/ })).toBeTruthy();
  expect(screen.getByText("observed")).toBeTruthy();
  expect(screen.getByText("Completed")).toBeTruthy();
  expect(screen.getByText("12m 04s")).toBeTruthy();
  expect(screen.getByText("$1.12")).toBeTruthy();
  expect(screen.getByText("1.00")).toBeTruthy();

  const list = await screen.findByRole("listbox", { name: "Steps" });
  const first = within(list).getByRole("option", { selected: true });
  expect(first.textContent).toContain("bash python -m pytest -q");
  expect(first.textContent).toContain("exit 1");
  expect(within(list).getByRole("option", { selected: false }).textContent).toContain(
    "edit (1.2 KB of arguments)",
  );

  expect(await screen.findByText("Fix the failing tests.")).toBeTruthy();
  expect(screen.getByRole("heading", { name: /Step 1/ }).textContent).toContain("1370 in");
  expect(screen.getByText("Running the tests first.")).toBeTruthy();
  expect(screen.getByText("Check what fails.")).toBeTruthy();
  expect(screen.getByText("exit 1 · 812 ms")).toBeTruthy();
  expect(screen.getByText("python -m pytest -q", { selector: "dd" })).toBeTruthy();
  expect(screen.getByText("60", { selector: "dd" })).toBeTruthy();
  expect(screen.getByText("Ran 3 processes")).toBeTruthy();
  expect(screen.getAllByText("1 failed, 7 passed")).toHaveLength(2);
  expect(screen.queryByText("Fixed the encoder.")).toBeNull();

  const [query] = eventQueries();
  expect(query?.get("after")).toBe("-1");
  expect(query?.get("before")).toBe("9");
  expect(query?.get("agent")).toBe("agent");
  expect(query?.getAll("types")).toContain("tool.call");
  expect(query?.getAll("types")).not.toContain("file.access");
});

test("the timeline pages the samples and draws the agent's activity", async () => {
  samples = Array.from({ length: 1200 }, (_, i) => sample(i));
  renderAt(`/runs/${BEE5}`);
  const timeline = await screen.findByRole("slider", { name: "Timeline" });
  await waitFor(() => expect(requests("samples")).toHaveLength(2));
  expect(requests("samples")[1]?.get("ts_from")).toBe(String(BOOT + 999 * 1_000_000 + 999_999 + 1));
  const lane = timeline.querySelector('[data-lane="agent"]');
  await waitFor(() =>
    expect(lane?.querySelector('[data-trace="fill"]')?.getAttribute("d")).toMatch(/^M/),
  );
  expect(lane?.querySelector('[data-trace="edge"]')?.getAttribute("d")).toMatch(/^M/);
  expect(timeline.getAttribute("aria-valuetext")).toBe("Step 1 at 0:00");
});

test("a click, a drag or the arrow keys on the timeline pick a step", async () => {
  const router = renderAt(`/runs/${BEE5}`);
  const timeline = await screen.findByRole("slider", { name: "Timeline" });
  await screen.findByText("Fix the failing tests.");
  const svg = timeline.querySelector("svg")!;
  fireEvent.pointerDown(svg, { clientX: 600, clientY: 30, button: 0, pointerId: 1 });
  expect(router.state.location.search).toEqual({ step: 11 });
  fireEvent.pointerMove(svg, { clientX: 100, clientY: 30, pointerId: 1, buttons: 1 });
  expect(router.state.location.search).toEqual({ step: 4 });
  fireEvent.pointerUp(svg, { pointerId: 1 });
  fireEvent.keyDown(timeline, { key: "ArrowRight" });
  expect(router.state.location.search).toEqual({ step: 11 });
  await waitFor(() => expect(timeline.getAttribute("aria-valuenow")).toBe("2"));
  fireEvent.keyDown(timeline, { key: "Home" });
  expect(router.state.location.search).toEqual({ step: 4 });
});

test("flags mark the step they cite, red from importance 3 until set aside", async () => {
  flags = [flag(100, [10], 4), flag(101, [3], 2), flag(102, [6], 5, "cleared")];
  const router = renderAt(`/runs/${BEE5}`);
  const timeline = await screen.findByRole("slider", { name: "Timeline" });
  await waitFor(() => expect(timeline.querySelectorAll("[data-flag]")).toHaveLength(3));
  const tone = (seq: number) =>
    timeline.querySelector(`[data-flag="${seq}"]`)?.getAttribute("data-tone");
  expect(tone(100)).toBe("red");
  expect(tone(101)).toBe("ink");
  expect(tone(102)).toBe("ink");
  fireEvent.pointerDown(timeline.querySelector('[data-flag="100"]')!, { button: 0, pointerId: 1 });
  expect(router.state.location.search).toEqual({ step: 11 });
});

test("the kernel fold pages the exec's records", async () => {
  renderAt(`/runs/${BEE5}`);
  const fold = await screen.findByRole("button", {
    name: "Kernel: 120 reads, 3 procs, 1 file change",
  });
  fireEvent.click(fold);
  expect(await screen.findByText("read /work/kiln/canonical.py")).toBeTruthy();
  expect(eventQueries().at(-1)?.getAll("exec")).toEqual(["x1"]);
});

test("?step= picks the step and the last one carries the answer", async () => {
  renderAt(`/runs/${BEE5}?step=11`);
  expect(await screen.findByText("Fixed the encoder.")).toBeTruthy();
  expect(screen.getByRole("heading", { name: /Step 2/ })).toBeTruthy();
  expect(screen.getByText("No text, 1 call.")).toBeTruthy();
  expect(await screen.findByText(/"path": "\/work\/kiln\/canonical.py"/)).toBeTruthy();
  expect(screen.getByText("File written.")).toBeTruthy();
  expect(screen.getByText("Reward 1.00, 8 of 8 tests passed")).toBeTruthy();
  const query = eventQueries().at(-1);
  expect(query?.get("after")).toBe("8");
  expect(query?.has("before")).toBe(false);
});

test("a submitted answer shows once", async () => {
  events[9] = ev(10, "tool.call", {
    call_id: "c2",
    name: "submit",
    arguments: '{"answer": "Fixed the encoder."}',
  });
  steps[1] = { ...steps[1]!, tools: [{ seq: 10, call_id: "c2", name: "submit", args: {} }] };
  renderAt(`/runs/${BEE5}?step=11`);
  expect(await screen.findByText("The submit call above.")).toBeTruthy();
  expect(screen.getAllByText("Fixed the encoder.")).toHaveLength(1);
});

test("j and k move through the steps in the URL", async () => {
  const router = renderAt(`/runs/${BEE5}`);
  await screen.findByText("Fix the failing tests.");
  fireEvent.keyDown(window, { key: "j" });
  expect(router.state.location.search).toEqual({ step: 11 });
  expect(await screen.findByText("Fixed the encoder.")).toBeTruthy();
  fireEvent.keyDown(window, { key: "k" });
  expect(router.state.location.search).toEqual({ step: 4 });
  fireEvent.keyDown(window, { key: "k" });
  expect(router.state.location.search).toEqual({ step: 4 });
});

test("the raw tab asks for a type group and a search", async () => {
  const router = renderAt(`/runs/${BEE5}?tab=raw&group=Execs`);
  expect(await screen.findByText("2 events")).toBeTruthy();
  const types = eventQueries().at(-1)?.getAll("types") ?? [];
  expect(types).toContain("exec.started");
  expect(types).not.toContain("tool.call");

  const search = screen.getByRole("textbox", { name: "Search events" });
  fireEvent.change(search, { target: { value: "pytest" } });
  fireEvent.keyDown(search, { key: "Enter" });
  expect(router.state.location.search).toEqual({ tab: "raw", group: "Execs", q: "pytest" });
  expect(await screen.findByText("1 events")).toBeTruthy();
  expect(eventQueries().at(-1)?.get("q")).toBe("pytest");

  fireEvent.click(screen.getByRole("option", { name: /exec.started/ }));
  expect(router.state.location.search).toMatchObject({ seq: 5 });
  expect(await screen.findByText(/"exec_id": "x1"/)).toBeTruthy();
});

test("/ opens the raw tab with its search focused", async () => {
  const router = renderAt(`/runs/${BEE5}`);
  await screen.findByText("Fix the failing tests.");
  fireEvent.keyDown(window, { key: "/" });
  const search = await screen.findByRole("textbox", { name: "Search events" });
  expect(router.state.location.search).toEqual({ tab: "raw" });
  await waitFor(() => expect(document.activeElement).toBe(search));
});

test("a run without agent steps points at its raw events", async () => {
  steps = [];
  const router = renderAt(`/runs/${BEE5}`);
  expect(await screen.findByText(/This run has no agent steps/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Raw events" }));
  expect(router.state.location.search).toEqual({ tab: "raw" });
});

test("an unknown run says so", async () => {
  renderAt("/runs/01a18a50-0000-7000-8000-000000000000");
  expect(await screen.findByText(/No run 01a18a50-0000-7000-8000-000000000000\./)).toBeTruthy();
  expect(screen.getByRole("link", { name: "Back to runs" }).getAttribute("href")).toBe("/runs");
});

test("the crumb back to the list keeps its filters", async () => {
  const router = renderAt("/runs?kind=monitor&severity=3");
  await screen.findByText("No runs match these filters.");
  await router.navigate({ to: "/runs/$runId", params: { runId: BEE5 } });
  await screen.findByText("Fix the failing tests.");
  const crumbs = within(screen.getByRole("navigation", { name: "Breadcrumb" }));
  expect(crumbs.getByRole("link", { name: "Runs" }).getAttribute("href")).toBe(
    "/runs?kind=monitor&severity=3",
  );
});
