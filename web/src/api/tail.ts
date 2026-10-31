import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Event } from "./events";
import { Live } from "./live";
import type { RawFilters, StepInfo } from "./run";

type StepPage = Schemas["StepPage"];
type EventPage = Schemas["EventPage"];
type WireDelta = Schemas["WireDelta"];

// Events that add a step row or change one: the step itself, its tool calls and their execs.
const STEP_EVENTS = new Set([
  "agent.step",
  "tool.call",
  "tool.result",
  "exec.started",
  "exec.finished",
]);
const TAIL_PAGE = 500;

export interface ToolDraft {
  name: string;
  args: string;
}

// A model answer as it streams, until its model.response is logged.
export interface InFlight {
  agent: string | null;
  call: string;
  attempt: number;
  text: string;
  reasoning: string;
  tools: readonly ToolDraft[];
  ended: boolean;
  // the server dropped deltas for a slow reader, so the text has gaps
  partial: boolean;
}

interface Draft {
  agent: string | null;
  call: string;
  attempt: number;
  text: string;
  reasoning: string;
  tools: Map<number, ToolDraft>;
  ended: boolean;
  partial: boolean;
}

const NONE: readonly InFlight[] = [];

// Keeps an open run's cached views current from one stream: the step list's tail, the raw
// events tab and the model text in flight. Batches apply once per animation frame.
export class RunTail {
  #live: Live | null = null;
  #drafts = new Map<string, Draft>();
  #snapshot: readonly InFlight[] = NONE;
  #watchers = new Set<() => void>();
  #pending: Event[] = [];
  // deltas and drop notices, in the order they came
  #deltas: (WireDelta | "dropped")[] = [];
  #scheduled = false;
  #refreshing = false;
  #again = false;
  #stopped = false;

  constructor(
    readonly runId: string,
    readonly qc: QueryClient,
    readonly frame: (fn: () => void) => void = (fn) => requestAnimationFrame(fn),
  ) {}

  start(after: number): void {
    this.#stopped = false;
    const live = new Live({ run: [{ id: this.runId, after }], deltas: [this.runId] }, () =>
      this.#closed(),
    );
    live.on("events", (m) => {
      if (m.run_id !== this.runId) return;
      this.#pending.push(...m.events);
      this.#schedule();
    });
    live.on("delta", (m) => {
      this.#deltas.push(...m.deltas.filter((d) => d.run_id === this.runId));
      this.#schedule();
    });
    live.on("deltas_dropped", (m) => {
      if (m.run_id !== null && m.run_id !== this.runId) return;
      this.#deltas.push("dropped");
      this.#schedule();
    });
    live.on("end", (m) => {
      if (m.run_id !== this.runId) return;
      // the server keeps the connection open after the end; nothing more will come
      live.stop();
      this.#drafts.clear();
      this.#publish();
      this.refreshSteps();
    });
    this.#live = live;
    live.start();
  }

  stop(): void {
    this.#stopped = true;
    this.#live?.stop();
    this.#live = null;
  }

  get inflight(): readonly InFlight[] {
    return this.#snapshot;
  }

  watch = (fn: () => void): (() => void) => {
    this.#watchers.add(fn);
    return () => this.#watchers.delete(fn);
  };

  // A closed stream is not a lost session here: the page's own stream says that.
  #closed(): void {
    this.#live?.stop();
  }

  #schedule(): void {
    if (this.#scheduled) return;
    this.#scheduled = true;
    this.frame(() => this.#flush());
  }

  #flush(): void {
    this.#scheduled = false;
    if (this.#stopped) return;
    const events = this.#pending;
    const deltas = this.#deltas;
    this.#pending = [];
    this.#deltas = [];
    let changed = false;
    for (const d of deltas) {
      if (d !== "dropped") changed = this.#delta(d) || changed;
      else {
        for (const draft of this.#drafts.values()) draft.partial = true;
        changed = changed || this.#drafts.size > 0;
      }
    }
    for (const e of events) {
      const data = e.data as Record<string, unknown>;
      // a failed attempt that is retried leaves its call in flight
      if (e.type !== "model.response" || data["retry"] === true) continue;
      const call = data["call_id"];
      for (const [key, d] of this.#drafts) {
        if (d.call === call) changed = this.#drafts.delete(key) || changed;
      }
    }
    if (changed) this.#publish();
    if (events.length === 0) return;
    this.#appendRaw(events);
    if (events.some((e) => STEP_EVENTS.has(e.type))) this.refreshSteps();
  }

  #delta(d: WireDelta): boolean {
    const key = `${d.agent_id ?? ""}|${d.call_id}`;
    let draft = this.#drafts.get(key);
    if (draft !== undefined && d.attempt < draft.attempt) return false;
    if (draft === undefined || d.kind === "start" || d.attempt > draft.attempt) {
      draft = {
        agent: d.agent_id ?? null,
        call: d.call_id,
        attempt: d.attempt,
        text: "",
        reasoning: "",
        tools: new Map(),
        ended: false,
        partial: false,
      };
      this.#drafts.set(key, draft);
    }
    const text = d.text ?? "";
    const index = d.index ?? 0;
    if (d.kind === "text") draft.text += text;
    else if (d.kind === "reasoning") draft.reasoning += text;
    else if (d.kind === "tool_call") draft.tools.set(index, { name: text, args: "" });
    else if (d.kind === "tool_args") {
      const tool = draft.tools.get(index) ?? { name: "", args: "" };
      draft.tools.set(index, { ...tool, args: tool.args + text });
    } else if (d.kind === "end") draft.ended = true;
    return true;
  }

  #publish(): void {
    this.#snapshot = [...this.#drafts.values()].map((d) => ({
      agent: d.agent,
      call: d.call,
      attempt: d.attempt,
      text: d.text,
      reasoning: d.reasoning,
      tools: [...d.tools.entries()].toSorted(([a], [b]) => a - b).map(([, t]) => t),
      ended: d.ended,
      partial: d.partial,
    }));
    for (const fn of this.#watchers) fn();
  }

  // Raw tabs that reach the end of the log take the new events; a tab with a search or more
  // pages to load gets them by paging.
  #appendRaw(events: readonly Event[]): void {
    const queries = this.qc.getQueriesData<InfiniteData<EventPage, number | null>>({
      queryKey: ["runs", "events", this.runId],
    });
    for (const [key, data] of queries) {
      const filters = (key[3] ?? {}) as RawFilters;
      const last = data?.pages.at(-1);
      if (!data || !last || last.next !== null || filters.q) continue;
      const types = filters.types ? new Set(filters.types) : null;
      const held = last.items.at(-1)?.seq ?? -1;
      const fresh = events.filter((e) => e.seq > held && (types === null || types.has(e.type)));
      if (fresh.length === 0) continue;
      this.qc.setQueryData<InfiniteData<EventPage, number | null>>(key, {
        ...data,
        pages: [...data.pages.slice(0, -1), { ...last, items: [...last.items, ...fresh] }],
      });
    }
  }

  // Reads the step rows that can still change, each agent's latest and any after it, and
  // merges them in; a burst during the read reads once more after it.
  refreshSteps(): void {
    if (this.#refreshing) {
      this.#again = true;
      return;
    }
    const key = ["runs", "steps", this.runId] as const;
    const state = this.qc.getQueryState(key);
    const data = this.qc.getQueryData<InfiniteData<StepPage, number | null>>(key);
    if (!data || state?.fetchStatus === "fetching" || (data.pages.at(-1)?.next ?? null) !== null)
      return;
    const rows = data.pages.flatMap((p) => p.items);
    const after = tailStart(rows);
    this.#refreshing = true;
    void client
      .GET("/api/runs/{run_id}/steps", {
        params: {
          path: { run_id: this.runId },
          query: { limit: TAIL_PAGE, ...(after === null ? {} : { after }) },
        },
      })
      .then(({ data: page }) => {
        if (!page || this.#stopped) return;
        const now = this.qc.getQueryData<InfiniteData<StepPage, number | null>>(key);
        if (!now) return;
        this.qc.setQueryData(key, mergeTail(now, after, page));
      })
      .finally(() => {
        this.#refreshing = false;
        if (this.#again && !this.#stopped) {
          this.#again = false;
          this.refreshSteps();
        }
      });
  }
}

// The seq to read on from: just before the earliest row that is some agent's latest.
export function tailStart(rows: readonly StepInfo[]): number | null {
  const latest = new Map<string, number>();
  rows.forEach((r, i) => latest.set(r.agent_id ?? "", i));
  const first = Math.min(...latest.values());
  if (!Number.isFinite(first) || first <= 0) return null;
  return rows[first - 1]?.seq ?? null;
}

export function mergeTail(
  data: InfiniteData<StepPage, number | null>,
  after: number | null,
  page: StepPage,
): InfiniteData<StepPage, number | null> {
  const keep = (s: StepInfo) => after !== null && s.seq <= after;
  const pages = data.pages.map((p) => ({ ...p, items: p.items.filter(keep) }));
  const last = pages.at(-1);
  if (!last) return data;
  pages[pages.length - 1] = { ...last, items: [...last.items, ...page.items], next: page.next };
  return { ...data, pages };
}
