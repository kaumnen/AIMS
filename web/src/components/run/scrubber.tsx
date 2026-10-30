import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { Event } from "@/api/events";
import {
  type FlagInfo,
  type RunDetail,
  samplesQuery,
  sandboxesQuery,
  type StepInfo,
} from "@/api/run";
import { categoryText, flagStep, flagTone } from "@/lib/monitor";
import {
  clocks,
  type Columns,
  columns,
  ms,
  offset,
  type Span,
  stepAt,
  stepOf,
  ticks,
  usage,
  type UsageWindow,
} from "@/lib/timeline";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

import { stepLine } from "./step-list";

const AXIS = 16;
const LANE = 40;
const LANE_MULTI = 28;
const PAD = 5;
const FALLBACK_WIDTH = 800;
const LIVE_MS = 3000;
const FILLS = ["fill-pen-1/20", "fill-pen-2/20", "fill-pen-3/20"];
const STROKES = ["stroke-pen-1", "stroke-pen-2", "stroke-pen-3"];
const TEXTS = ["fill-pen-1", "fill-pen-2", "fill-pen-3"];

// A review range a monitor covered, in one agent's step numbers.
export interface Band {
  agent: string;
  from: number;
  to: number;
  error: boolean;
}

export function bandsOf(sweeps: readonly Event[]): Band[] {
  return sweeps.flatMap((e) => {
    const from = e.data["step_from"];
    const to = e.data["step_to"];
    if (e.agent_id === null || typeof from !== "number" || typeof to !== "number" || to < 1) {
      return [];
    }
    return [{ agent: e.agent_id, from, to, error: typeof e.data["error"] === "string" }];
  });
}

function agentsOf(detail: RunDetail): string[] {
  const listed = detail.started?.data["agents"];
  if (!Array.isArray(listed)) return [];
  return listed.flatMap((a: unknown) => {
    if (typeof a === "string") return [a];
    if (typeof a === "object" && a !== null && typeof (a as { id?: unknown }).id === "string") {
      return [(a as { id: string }).id];
    }
    return [];
  });
}

function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

// The run on one time axis: a lane per agent with its CPU trace and step ticks, flags in red
// pen, review coverage along the lane top. Click or drag picks the step under way there.
// `flags` and `sweeps` come from the monitor chosen in the panel.
export function Scrubber({
  runId,
  detail,
  steps,
  selected,
  onSelect,
  flags,
  sweeps,
  flag,
  onFlag,
  harm,
}: {
  runId: string;
  detail: RunDetail;
  steps: readonly StepInfo[];
  selected: StepInfo;
  onSelect: (seq: number) => void;
  flags: readonly FlagInfo[];
  sweeps: readonly Event[];
  flag: number | undefined;
  onFlag: (f: FlagInfo) => void;
  harm?: number;
}) {
  const live = detail.run.status === "running";
  const every = live ? LIVE_MS : false;
  const samples = useQuery({ ...samplesQuery(runId), refetchInterval: every });
  const sandboxes = useQuery({ ...sandboxesQuery(runId), refetchInterval: every });
  const now = useNow(live, LIVE_MS);

  const windows = samples.data ?? [];
  const lanesUsage = usage(windows, clocks(sandboxes.data ?? [], windows));
  const lanes = [...agentsOf(detail)];
  for (const s of steps)
    if (s.agent_id !== null && !lanes.includes(s.agent_id)) lanes.push(s.agent_id);
  for (const a of lanesUsage.keys()) if (!lanes.includes(a)) lanes.push(a);
  if (lanes.length === 0) lanes.push("");

  const starts = [detail.run.started_at, steps[0]?.ts_from].flatMap((t) => (t ? [ms(t)] : []));
  const ends = [
    detail.run.ended_at ?? (live ? new Date(now).toISOString() : null),
    steps.at(-1)?.ts_to,
  ].flatMap((t) => (t ? [ms(t)] : []));
  for (const lane of lanesUsage.values()) {
    const first = lane[0];
    const last = lane.at(-1);
    if (first) starts.push(first.from);
    if (last) ends.push(last.to);
  }
  const t0 = Math.min(...starts);
  const span: Span = { t0, t1: Math.max(t0 + 1000, ...ends) };

  return (
    <Strip
      span={span}
      lanes={lanes.map((agent) => ({ agent, windows: lanesUsage.get(agent) ?? [] }))}
      steps={steps}
      selected={selected}
      onSelect={onSelect}
      flags={flags}
      bands={bandsOf(sweeps)}
      flag={flag}
      onFlag={onFlag}
      {...(harm === undefined ? {} : { harm })}
    />
  );
}

interface LaneInput {
  agent: string;
  windows: readonly UsageWindow[];
}

// The trace's fill and its top edge. Square root scale, so a short command still shows
// beside the busiest second; the readout gives exact values.
function trace(cols: Columns, top: number, h: number, max: number): { fill: string; edge: string } {
  let fill = "";
  let edge = "";
  let open = false;
  const base = top + h;
  cols.forEach((c, x) => {
    if (c.cpu === null) {
      if (open) fill += `L${x} ${base}Z`;
      open = false;
      return;
    }
    const y = (base - Math.sqrt(Math.min(c.cpu, max) / max) * h).toFixed(1);
    fill += open ? `L${x} ${y}` : `M${x} ${base}L${x} ${y}`;
    edge += open ? `L${x} ${y}` : `M${x} ${y}`;
    fill += `L${x + 1} ${y}`;
    edge += `L${x + 1} ${y}`;
    open = true;
  });
  if (open) fill += `L${cols.length} ${base}Z`;
  return { fill, edge };
}

const bytes = (n: number) =>
  n >= 1 << 30
    ? `${(n / (1 << 30)).toFixed(1)} GB`
    : n >= 1 << 20
      ? `${(n / (1 << 20)).toFixed(1)} MB`
      : n >= 1 << 10
        ? `${Math.round(n / (1 << 10))} KB`
        : `${n} B`;

function Trace({ d, pen }: { d: { fill: string; edge: string }; pen: number }) {
  return (
    <>
      <path data-trace="fill" d={d.fill} className={FILLS[pen]} />
      <path data-trace="edge" d={d.edge} className={STROKES[pen]} fill="none" strokeWidth={1} />
    </>
  );
}

function point(e: React.PointerEvent) {
  const r = e.currentTarget.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top] as const;
}

export function Strip({
  span,
  lanes,
  steps,
  selected,
  onSelect,
  flags,
  bands,
  flag,
  onFlag,
  harm,
}: {
  span: Span;
  lanes: readonly LaneInput[];
  steps: readonly StepInfo[];
  selected: StepInfo;
  onSelect: (seq: number) => void;
  flags: readonly FlagInfo[];
  bands: readonly Band[];
  flag?: number | undefined;
  onFlag?: (f: FlagInfo) => void;
  harm?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box);
  const picked = useRef<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  const multi = lanes.length > 1;
  const laneH = multi ? LANE_MULTI : LANE;
  const height = AXIS + lanes.length * laneH;
  const dt = span.t1 - span.t0;
  const x = (t: number) => Math.min(width, Math.max(0, ((t - span.t0) / dt) * width));
  const tAt = (px: number) => span.t0 + (px / width) * dt;
  const top = (i: number) => AXIS + i * laneH;

  const cols = lanes.map((l) => columns(l.windows, span, width));
  const max = Math.max(0.05, ...cols.flatMap((c) => c.map((v) => v.cpu ?? 0)));
  const stepsOf = (agent: string) => {
    const own = steps.filter((s) => s.agent_id === agent);
    return own.length > 0 || multi ? own : steps;
  };
  const laneOf = (agent: string | null) => {
    const i = lanes.findIndex((l) => l.agent === agent);
    return i < 0 ? 0 : i;
  };
  const at = steps.findIndex((s) => s.seq === selected.seq);

  const pick = (px: number, py: number) => {
    const lane = lanes[Math.min(lanes.length - 1, Math.max(0, Math.floor((py - AXIS) / laneH)))];
    const pool = lane && multi ? stepsOf(lane.agent) : steps;
    const step = stepAt(pool.length > 0 ? pool : steps, tAt(px));
    if (!step || step.seq === picked.current) return;
    picked.current = step.seq;
    onSelect(step.seq);
  };
  const move = (by: number) => {
    const next = steps[Math.min(steps.length - 1, Math.max(0, at + by))];
    if (next && next.seq !== selected.seq) onSelect(next.seq);
  };

  const sx0 = x(ms(selected.ts_from));
  const sx1 = Math.max(sx0 + 2, x(ms(selected.ts_to)));
  const hoverStep = hover === null ? undefined : stepAt(steps, tAt(hover));
  const hoverCol = hover === null ? -1 : Math.min(width - 1, Math.floor(hover));
  const harmStep = harm === undefined ? undefined : stepOf(steps, harm);

  return (
    <div
      ref={box}
      role="slider"
      tabIndex={0}
      aria-label="Timeline"
      aria-valuemin={1}
      aria-valuemax={Math.max(1, steps.length)}
      aria-valuenow={at + 1}
      aria-valuetext={`Step ${selected.n} at ${offset(ms(selected.ts_from) - span.t0)}`}
      onKeyDown={(e) => {
        const by =
          e.key === "ArrowRight"
            ? 1
            : e.key === "ArrowLeft"
              ? -1
              : e.key === "Home"
                ? -steps.length
                : e.key === "End"
                  ? steps.length
                  : 0;
        if (by === 0) return;
        e.preventDefault();
        move(by);
      }}
      className="focus-visible:ring-ring/50 relative mb-2 shrink-0 rounded-sm border select-none focus-visible:ring-2 focus-visible:outline-none"
    >
      <svg
        width={width}
        height={height}
        className="block touch-none"
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          picked.current = selected.seq;
          pick(...point(e));
        }}
        onPointerMove={(e) => {
          const [px, py] = point(e);
          setHover(px);
          if (e.currentTarget.hasPointerCapture(e.pointerId)) pick(px, py);
        }}
        onPointerLeave={() => setHover(null)}
      >
        {ticks(span, width).map((t) => {
          const gx = Math.round(x(span.t0 + t)) + 0.5;
          return (
            <g key={t}>
              <line x1={gx} x2={gx} y1={0} y2={height} className="stroke-grid" />
              {gx < width - 32 && (
                <text x={gx + 3} y={11} className="fill-muted-foreground text-[10px] tabular-nums">
                  {offset(t)}
                </text>
              )}
            </g>
          );
        })}
        <line x1={0} x2={width} y1={AXIS - 0.5} y2={AXIS - 0.5} className="stroke-grid" />
        <rect
          x={sx0}
          y={AXIS}
          width={sx1 - sx0}
          height={height - AXIS}
          className="fill-foreground/8"
        />
        {lanes.map((lane, i) => {
          const y = top(i);
          const pen = i % 3;
          const own = stepsOf(lane.agent);
          const byN = new Map(own.map((s) => [s.n, s]));
          return (
            <g key={lane.agent} data-lane={lane.agent}>
              {i > 0 && (
                <line x1={0} x2={width} y1={y + 0.5} y2={y + 0.5} className="stroke-grid" />
              )}
              <Trace d={trace(cols[i] ?? [], y + PAD, laneH - PAD - 1, max)} pen={pen} />
              {bands
                .filter((b) => b.agent === lane.agent)
                .map((b) => {
                  const a = byN.get(b.from);
                  const z = byN.get(b.to);
                  if (!a || !z) return null;
                  const bx = x(ms(a.ts_from));
                  return (
                    <rect
                      key={`${b.from}-${b.to}`}
                      data-band={`${b.from}-${b.to}`}
                      x={bx}
                      y={y + 1}
                      width={Math.max(1, x(ms(z.ts_to)) - bx)}
                      height={2}
                      className={b.error ? "fill-muted-foreground/40" : TEXTS[pen]}
                      opacity={b.error ? 1 : 0.6}
                    />
                  );
                })}
              {own.map((s) => {
                const tx = Math.round(x(ms(s.ts_from))) + 0.5;
                return (
                  <line
                    key={s.seq}
                    x1={tx}
                    x2={tx}
                    y1={y + laneH - 5}
                    y2={y + laneH}
                    className="stroke-muted-foreground/70"
                  />
                );
              })}
              {multi && (
                <text x={4} y={y + 11} className={cn("text-[10px]", TEXTS[pen])}>
                  {lane.agent}
                </text>
              )}
            </g>
          );
        })}
        <line
          x1={sx0}
          x2={sx0}
          y1={AXIS}
          y2={height}
          className="stroke-foreground"
          strokeWidth={1.5}
        />
        {flags.map((f) => {
          const i = laneOf(f.agent_id);
          const step = flagStep(f, steps);
          const fx = Math.round(x(step ? ms(step.ts_to) : ms(f.ts))) + 0.5;
          const y = top(i);
          const tone = flagTone(f);
          const on = f.seq === flag;
          return (
            <g
              key={f.seq}
              data-flag={f.seq}
              data-tone={tone.red ? "red" : "ink"}
              aria-current={on || undefined}
              className={cn("cursor-pointer", tone.red ? "text-flag" : "text-foreground")}
              onPointerDown={(e) => {
                e.stopPropagation();
                if (step) picked.current = step.seq;
                if (onFlag) onFlag(f);
                else if (step) onSelect(step.seq);
              }}
            >
              <title>{`${categoryText(f.category)}, importance ${f.importance}${tone.hollow ? `, ${f.status}` : ""}`}</title>
              <line
                x1={fx}
                x2={fx}
                y1={y}
                y2={y + laneH}
                stroke="currentColor"
                strokeWidth={on ? 2.5 : 1}
                strokeDasharray={tone.hollow ? "2 2" : undefined}
              />
              <path
                d={`M${fx - 4} ${y}L${fx + 4} ${y}L${fx} ${y + 6}Z`}
                fill={tone.hollow ? "var(--background)" : "currentColor"}
                stroke="currentColor"
              />
            </g>
          );
        })}
        {harmStep && (
          <g data-harm={harm} className="text-flag">
            <line
              x1={x(ms(harmStep.ts_to))}
              x2={x(ms(harmStep.ts_to))}
              y1={0}
              y2={height}
              stroke="currentColor"
              strokeWidth={2}
            />
          </g>
        )}
        {hover !== null && (
          <line
            x1={hover}
            x2={hover}
            y1={AXIS}
            y2={height}
            className="stroke-muted-foreground pointer-events-none"
            strokeDasharray="2 2"
          />
        )}
      </svg>
      {hover !== null && (
        <Readout
          left={hover}
          width={width}
          time={offset(tAt(hover) - span.t0)}
          step={hoverStep}
          readings={lanes.map((l, i) => ({ agent: l.agent, at: cols[i]?.[hoverCol] }))}
          multi={multi}
        />
      )}
    </div>
  );
}

function Readout({
  left,
  width,
  time,
  step,
  readings,
  multi,
}: {
  left: number;
  width: number;
  time: string;
  step: StepInfo | undefined;
  readings: { agent: string; at: Columns[number] | undefined }[];
  multi: boolean;
}) {
  const right = left > width / 2;
  return (
    <div
      className="bg-popover text-popover-foreground pointer-events-none absolute top-full z-10 mt-1 max-w-80 rounded-sm border px-2 py-1 text-xs"
      style={right ? { right: width - left } : { left }}
    >
      <div className="flex gap-2 tabular-nums">
        <span>{time}</span>
        {step && (
          <span className="text-muted-foreground truncate">
            step {step.n}
            {multi && step.agent_id ? ` of ${step.agent_id}` : ""}, {stepLine(step)}
          </span>
        )}
      </div>
      {readings.map(({ agent, at }) =>
        at && at.cpu !== null ? (
          <div key={agent} className="text-muted-foreground tabular-nums">
            {multi ? `${agent}: ` : ""}cpu {Math.round(at.cpu * 100)}%
            {at.mem !== null ? `, mem ${bytes(at.mem)}` : ""}
            {at.io ? `, io ${bytes(at.io)}/s` : ""}
            {at.pids !== null ? `, ${at.pids} procs` : ""}
          </div>
        ) : null,
      )}
    </div>
  );
}
