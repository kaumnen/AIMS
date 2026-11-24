import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";

import type { Event } from "./events";
import { samplesQuery, type SampleWindow, sweepsQuery } from "./run";

afterEach(() => {
  vi.unstubAllGlobals();
});

const sample = (seq: number, start: number, cpu = 0): SampleWindow => ({
  agent_id: "a",
  count: 10,
  cpu_full_usec: null,
  cpu_some_usec: null,
  cpu_system_usec: null,
  cpu_throttled: null,
  cpu_throttled_usec: null,
  cpu_usec: cpu,
  cpu_user_usec: null,
  hz: 10,
  io_full_usec: null,
  io_rbytes: null,
  io_rios: null,
  io_some_usec: null,
  io_wbytes: null,
  io_wios: null,
  memory_bytes: null,
  memory_full_usec: null,
  memory_some_usec: null,
  pids: null,
  run_id: "r",
  sandbox_id: "s",
  seq,
  ts_from: start,
  ts_to: start + 900,
});

const query = (q: URLSearchParams) => Object.fromEntries(q);

// The server's samples: by start time then seq, or by seq with `order=seq`, `after` as each
// order reads it.
function serve(windows: () => SampleWindow[]): URLSearchParams[] {
  const asked: URLSearchParams[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const q = new URL(request.url).searchParams;
    asked.push(q);
    const limit = Number(q.get("limit") ?? 1000);
    const after = q.get("after");
    let items: SampleWindow[];
    if (q.get("order") === "seq") {
      items = windows()
        .toSorted((a, b) => a.seq - b.seq)
        .filter((w) => w.seq > Number(after ?? -1));
    } else {
      const sorted = windows().toSorted((a, b) => a.ts_from - b.ts_from || a.seq - b.seq);
      const at = sorted.find((w) => w.seq === Number(after));
      items =
        after === null
          ? sorted
          : sorted.filter(
              (w) => at && (w.ts_from > at.ts_from || (w.ts_from === at.ts_from && w.seq > at.seq)),
            );
    }
    const page = items.slice(0, limit);
    return Response.json({ items: page, next: page.length === limit ? page.at(-1)?.seq : null });
  });
  return asked;
}

test("a poll of the windows reads on from the last one held, by seq", async () => {
  let windows = [sample(10, 5000), sample(11, 1000)];
  const asked = serve(() => windows);
  const qc = new QueryClient();
  const read = async () => (await qc.fetchQuery({ ...samplesQuery("r"), staleTime: 0 })) ?? [];
  const seqs = async () => (await read()).map((w) => w.seq);

  expect(await seqs()).toEqual([10, 11]);
  // a window logged late, with the earliest start of all
  windows = [...windows, sample(12, 0)];
  expect(await seqs()).toEqual([10, 11, 12]);
  // nothing new: the list the cache holds stays as it is
  const held = await read();
  expect(await read()).toBe(held);
  expect(asked.map(query)).toEqual([
    { order: "seq", limit: "1000" },
    // the last window held is read again, to see it is still there as it was
    { order: "seq", limit: "1000", after: "10" },
    { order: "seq", limit: "1000", after: "11" },
    { order: "seq", limit: "1000", after: "11" },
  ]);

  // the server no longer holds the last window as it was: the whole list is read again
  windows = [sample(10, 5000), sample(11, 1000), sample(12, 0, 7), sample(13, 2000)];
  expect((await read()).map((w) => [w.seq, w.cpu_usec])).toEqual([
    [10, 0],
    [11, 0],
    [12, 7],
    [13, 0],
  ]);
  expect(asked.slice(4).map(query)).toEqual([
    { order: "seq", limit: "1000", after: "11" },
    { order: "seq", limit: "1000" },
  ]);
});

test("a first read of the windows pages through them all", async () => {
  const windows = Array.from({ length: 2500 }, (_, i) => sample(i, i * 1000));
  const asked = serve(() => windows);
  const qc = new QueryClient();
  const read = await qc.fetchQuery(samplesQuery("r"));
  expect(read.map((w) => w.seq)).toEqual(windows.map((w) => w.seq));
  expect(asked.map((q) => q.get("after"))).toEqual([null, "999", "1999"]);
});

const sweep = (seq: number, to: number, hash = `h${seq}`): Event =>
  ({
    agent_id: "a",
    data: { name: "standard", lane: "a", step_from: 1, step_to: to },
    hash,
    prev_hash: "",
    run_id: "m",
    seq,
    ts: "2026-11-24T10:00:00Z",
    type: "monitor.sweep",
    v: 1,
  }) as Event;

test("a poll of the sweeps reads on from the last one held", async () => {
  let sweeps = [sweep(3, 2), sweep(8, 4)];
  const asked: URLSearchParams[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const q = new URL(request.url).searchParams;
    asked.push(q);
    const items = sweeps.filter((e) => e.seq > Number(q.get("after") ?? -1));
    return Response.json({ items, next: null });
  });
  const qc = new QueryClient();
  const read = async () => (await qc.fetchQuery({ ...sweepsQuery("m"), staleTime: 0 })) ?? [];
  const seqs = async () => (await read()).map((e) => e.seq);

  expect(await seqs()).toEqual([3, 8]);
  sweeps = [...sweeps, sweep(12, 6)];
  expect(await seqs()).toEqual([3, 8, 12]);
  // the last sweep held is read again, to see it is still there as it was
  expect(asked.map(query)).toEqual([
    { types: "monitor.sweep", limit: "1000" },
    { types: "monitor.sweep", limit: "1000", after: "7" },
  ]);
  const held = await read();
  expect(await read()).toBe(held);
  // the log was cut past seq 8 and written again: the whole list is read again
  sweeps = [sweep(3, 2), sweep(8, 4), sweep(12, 5, "other")];
  expect((await read()).map((e) => e.hash)).toEqual(["h3", "h8", "other"]);
  expect(asked.slice(2).map(query)).toEqual([
    { types: "monitor.sweep", limit: "1000", after: "11" },
    { types: "monitor.sweep", limit: "1000", after: "11" },
    { types: "monitor.sweep", limit: "1000", after: "11" },
    { types: "monitor.sweep", limit: "1000" },
  ]);
});
