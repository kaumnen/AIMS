import type { Event } from "@/api/events";
import type { SampleWindow, StepInfo } from "@/api/run";

// The tracer's boot/TAI pair is trusted when it puts the first window this close to the
// sandbox's start. A host whose TAI clock carries the leap second offset is 37 s off.
const TRUST_US = 5_000_000;

export interface Span {
  t0: number;
  t1: number;
}

export interface Usage {
  cpu: number | null;
  io: number | null;
  mem: number | null;
  pids: number | null;
}

export interface UsageWindow extends Usage {
  from: number;
  to: number;
}

export type Columns = Usage[];

export const ms = (iso: string) => Date.parse(iso);

const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const reading = (v: number | null | undefined) =>
  v === null || v === undefined || v < 0 ? null : v;

// Per sandbox, what to add to its boot clock (µs) to get epoch µs.
export function clocks(
  starts: readonly Event[],
  windows: readonly SampleWindow[],
): Map<string, number> {
  const first = new Map<string, number>();
  for (const w of windows) {
    const at = first.get(w.sandbox_id);
    if (at === undefined || w.ts_from < at) first.set(w.sandbox_id, w.ts_from);
  }
  const out = new Map<string, number>();
  for (const e of starts) {
    const id = e.data["sandbox_id"];
    if (typeof id !== "string") continue;
    const wall = ms(e.ts) * 1000;
    const begin = first.get(id);
    const config = e.data["config"] as Record<string, unknown> | undefined;
    const pair = config?.["kernel_clock"] as Record<string, unknown> | undefined;
    const boot = num(pair?.["boot_us"]);
    const tai = num(pair?.["tai_us"]);
    let shift = boot !== null && tai !== null ? tai - boot : null;
    if (begin !== undefined && (shift === null || Math.abs(begin + shift - wall) > TRUST_US)) {
      shift = wall - begin;
    }
    if (shift !== null) out.set(id, shift);
  }
  return out;
}

// Each agent's windows on the epoch clock (ms), with the usage in each one: counters are
// running totals, so a window's usage is the change from the window before it.
export function usage(
  windows: readonly SampleWindow[],
  offsets: ReadonlyMap<string, number>,
): Map<string, UsageWindow[]> {
  const bySandbox = new Map<string, SampleWindow[]>();
  for (const w of windows) {
    if (w.agent_id === null || !offsets.has(w.sandbox_id)) continue;
    const list = bySandbox.get(w.sandbox_id) ?? [];
    list.push(w);
    bySandbox.set(w.sandbox_id, list);
  }
  const out = new Map<string, UsageWindow[]>();
  for (const [sandbox, list] of bySandbox) {
    list.sort((a, b) => a.ts_from - b.ts_from);
    const shift = offsets.get(sandbox) ?? 0;
    const agent = list[0]?.agent_id ?? "";
    const lane = out.get(agent) ?? [];
    let prev: SampleWindow | undefined;
    for (const w of list) {
      const span = prev ? w.ts_to - prev.ts_to : 0;
      const delta = (a: number | null, b: number | null | undefined) => {
        const now = reading(a);
        const then = reading(b);
        return now === null || then === null || now < then ? null : now - then;
      };
      const cpu = span > 0 ? delta(w.cpu_usec, prev?.cpu_usec) : null;
      const r = delta(w.io_rbytes, prev?.io_rbytes);
      const wb = delta(w.io_wbytes, prev?.io_wbytes);
      lane.push({
        from: (w.ts_from + shift) / 1000,
        to: (w.ts_to + shift) / 1000,
        cpu: cpu === null ? null : cpu / span,
        io: r === null && wb === null ? null : (r ?? 0) + (wb ?? 0),
        mem: reading(w.memory_bytes),
        pids: reading(w.pids),
      });
      prev = w;
    }
    lane.sort((a, b) => a.from - b.from);
    out.set(agent, lane);
  }
  return out;
}

const higher = (a: number | null, b: number | null) =>
  a === null ? b : b === null ? a : Math.max(a, b);

// One value per pixel column, the highest of the windows that touch it, so spikes survive.
export function columns(lane: readonly UsageWindow[], span: Span, width: number): Columns {
  const out: Columns = Array.from({ length: width }, () => ({
    cpu: null,
    io: null,
    mem: null,
    pids: null,
  }));
  const dt = span.t1 - span.t0;
  if (dt <= 0 || width <= 0) return out;
  const col = (t: number) => Math.floor(((t - span.t0) / dt) * width);
  for (const w of lane) {
    const a = Math.max(0, col(w.from));
    const b = Math.min(width - 1, col(w.to));
    for (let c = a; c <= b; c++) {
      const cell = out[c];
      if (!cell) continue;
      cell.cpu = higher(cell.cpu, w.cpu);
      cell.io = higher(cell.io, w.io);
      cell.mem = higher(cell.mem, w.mem);
      cell.pids = higher(cell.pids, w.pids);
    }
  }
  return out;
}

const INTERVALS_S = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10_800, 21_600, 43_200, 86_400,
];

// Offsets from the start at a round interval, no closer than `gap` px apart.
export function ticks(span: Span, width: number, gap = 72): number[] {
  const dt = span.t1 - span.t0;
  if (dt <= 0 || width <= 0) return [0];
  const most = Math.max(1, Math.floor(width / gap));
  const every = (INTERVALS_S.find((s) => dt / 1000 / s <= most) ?? INTERVALS_S.at(-1) ?? 1) * 1000;
  const out: number[] = [];
  for (let t = 0; t <= dt; t += every) out.push(t);
  return out;
}

export function offset(t: number): string {
  const s = Math.max(0, Math.floor(t / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// The step that was under way at `t`: the last one begun by then, else the first.
export function stepAt(steps: readonly StepInfo[], t: number): StepInfo | undefined {
  let found: StepInfo | undefined;
  for (const s of steps) {
    const at = ms(s.ts_from);
    if (at <= t && (found === undefined || at >= ms(found.ts_from))) found = s;
  }
  return found ?? steps[0];
}

// The step whose events hold `seq`, else the last one before it, else the first.
export function stepOf(steps: readonly StepInfo[], seq: number): StepInfo | undefined {
  let before: StepInfo | undefined;
  for (const s of steps) {
    if (s.seq_from <= seq && seq <= s.seq_to) return s;
    if (s.seq_to < seq && (before === undefined || s.seq_to > before.seq_to)) before = s;
  }
  return before ?? steps[0];
}
