import { hashKey, type InfiniteData, type QueryClient, type QueryKey } from "@tanstack/react-query";

import { client, type Schemas } from "./client";
import type { Event } from "./events";
import { Live } from "./live";
import {
  RAW_PAGE,
  type RawFilters,
  type RawPage,
  readFinal,
  refreshLastSteps,
  type StepInfo,
  stepTypes,
} from "./run";

type StepPage = Schemas["StepPage"];
type WireDelta = Schemas["WireDelta"];

// Events that add a step row or change one: the step itself, its tool calls and their execs.
// what a step's transcript shows, which a step that runs on to the end reads again
const SHOWN: ReadonlySet<string> = new Set(stepTypes);
const STEP_EVENTS = new Set([
  "agent.step",
  "tool.call",
  "tool.result",
  "exec.started",
  "exec.finished",
]);
const TAIL_PAGE = 500;
// What a raw list holds back while its page is read: at most the largest page the server sends.
const MAX_HELD = 1000;
// What a raw list that takes the stream holds at most.
const MAX_RAW = 50 * RAW_PAGE;
const NO_AGENTS: ReadonlySet<string> = new Set();

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
  // calls whose answer is logged; deltas of theirs that come late are dropped
  #done = new Set<string>();
  #snapshot: readonly InFlight[] = NONE;
  #watchers = new Set<() => void>();
  #pending: Event[] = [];
  // deltas and drop notices, in the order they came
  #deltas: (WireDelta | "dropped")[] = [];
  #scheduled = false;
  #refreshing = false;
  #again = false;
  // waiting for a read of the steps already under way to end
  #waiting = false;
  #stopped = false;
  // Raw lists whose page read is under way, by query hash, with the events that came meanwhile:
  // the read writes the pages as they were when it began, so these go on once it is in. "over"
  // when more came than a list holds back; it then reads on by paging.
  #held = new Map<string, Event[] | "over">();
  #unheld: (() => void) | null = null;
  #replaying = false;
  // Agents known to have ended, and those of them whose rows were read after that: the last
  // row of a settled agent is final, even with a tool call that never got its result.
  #ended = new Set<string>();
  #settled = new Set<string>();
  #endsRead = false;

  constructor(
    readonly runId: string,
    readonly qc: QueryClient,
    readonly frame: (fn: () => void) => void = (fn) => requestAnimationFrame(fn),
  ) {}

  start(after: number): void {
    this.#stopped = false;
    this.#readOnAll(after);
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
      readFinal(this.qc, this.runId);
    });
    this.#live = live;
    live.start();
  }

  stop(): void {
    this.#stopped = true;
    this.#live?.stop();
    this.#live = null;
    this.#held.clear();
    this.#unheld?.();
    this.#unheld = null;
    this.#readOnAll(Number.POSITIVE_INFINITY);
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
    for (const e of events) {
      const data = e.data as Record<string, unknown>;
      if (e.type === "model.response" && data["retry"] !== true)
        this.#done.add(String(data["call_id"]));
      else if (e.type === "agent.finished") this.#ended.add(e.agent_id ?? "");
    }
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
    if (events.some((e) => SHOWN.has(e.type))) refreshLastSteps(this.qc, this.runId);
  }

  #delta(d: WireDelta): boolean {
    if (this.#done.has(d.call_id)) return false;
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

  // Raw tabs that reach the end of the log take the new events of their types; a tab with a
  // search or more pages to load gets them by paging. A tab whose page is being read takes them
  // once the page is in.
  #appendRaw(events: readonly Event[]): void {
    for (const query of this.#rawLists()) {
      const filters = (query.queryKey[3] ?? {}) as RawFilters;
      const types = filters.types ? new Set(filters.types) : null;
      const mine = types === null ? events : events.filter((e) => types.has(e.type));
      if (mine.length === 0) continue;
      if (query.state.fetchStatus !== "idle" || this.#held.has(query.queryHash)) {
        this.#hold(query.queryHash, mine);
      } else this.#append(query.queryKey, mine);
    }
  }

  // The last page takes new events until it holds a server page; then they start new pages, as
  // reading on would. So a batch copies at most a page of events and the list of pages.
  #append(key: QueryKey, events: readonly Event[]): void {
    const data = this.qc.getQueryData<InfiniteData<RawPage>>(key);
    const last = data?.pages.at(-1);
    if (!data || !last || last.newer !== null) return;
    const held = lastSeq(data.pages);
    const fresh = events.filter((e) => e.seq > held);
    if (fresh.length === 0) return;
    const pages = data.pages.slice(0, -1);
    const params = data.pageParams.slice(0, -1);
    const room = Math.max(0, RAW_PAGE - last.items.length);
    let page = room > 0 ? { ...last, items: [...last.items, ...fresh.slice(0, room)] } : last;
    let param = data.pageParams.at(-1);
    for (let at = room; at < fresh.length; at += RAW_PAGE) {
      const end = page.items.at(-1)?.seq ?? held;
      pages.push({ ...page, newer: end });
      params.push(param);
      page = { items: fresh.slice(at, at + RAW_PAGE), older: null, newer: null };
      param = { after: end };
    }
    pages.push(page);
    params.push(param);
    trim(pages, params);
    this.qc.setQueryData<InfiniteData<RawPage>>(key, { pages, pageParams: params });
  }

  // The raw lists that take the stream; a search gets new events by paging.
  #rawLists() {
    return this.qc
      .getQueryCache()
      .findAll({ queryKey: ["runs", "events", this.runId] })
      .filter((q) => !(q.queryKey[3] as RawFilters | undefined)?.q);
  }

  // A list that stops before `after`, where the stream starts, reads on past its last event once
  // shown: one kept from an earlier visit, or one a stopped tail no longer keeps current. A list
  // whose page is read does so once the read is in, while the tail runs.
  #readOnAll(after: number): void {
    for (const query of this.#rawLists()) {
      const data = query.state.data as InfiniteData<RawPage> | undefined;
      if (data === undefined || lastSeq(data.pages) >= after) continue;
      if (query.state.fetchStatus === "idle") this.#readOn(query.queryKey);
      else if (!this.#stopped) this.#hold(query.queryHash, "over");
    }
  }

  #hold(hash: string, events: readonly Event[] | "over"): void {
    const held = this.#held.get(hash);
    if (held === "over") return;
    if (events === "over" || (held?.length ?? 0) + events.length > MAX_HELD)
      this.#held.set(hash, "over");
    else if (held) held.push(...events);
    else this.#held.set(hash, [...events]);
    this.#unheld ??= this.qc.getQueryCache().subscribe(({ query }) => {
      if (query.state.fetchStatus === "idle" && this.#held.has(query.queryHash)) this.#replay();
    });
  }

  // Once the read's own write is in, as syncRuns does: each list whose read ended, in success,
  // failure or cancel, takes what it held as if it had just come. A list gone takes nothing.
  #replay(): void {
    if (this.#replaying) return;
    this.#replaying = true;
    queueMicrotask(() => {
      this.#replaying = false;
      if (this.#stopped) return;
      const cache = this.qc.getQueryCache();
      for (const [hash, held] of this.#held) {
        const query = cache.get(hash);
        if (query !== undefined && query.state.fetchStatus !== "idle") continue;
        this.#held.delete(hash);
        if (query === undefined) continue;
        if (held === "over") this.#readOn(query.queryKey);
        else this.#append(query.queryKey, held);
      }
      if (this.#held.size > 0) return;
      this.#unheld?.();
      this.#unheld = null;
    });
  }

  // More came during the read than the list held back: it reads on past its last event.
  #readOn(key: QueryKey): void {
    const data = this.qc.getQueryData<InfiniteData<RawPage>>(key);
    const last = data?.pages.at(-1);
    if (!data || !last || last.newer !== null) return;
    this.qc.setQueryData<InfiniteData<RawPage>>(key, {
      ...data,
      pages: [...data.pages.slice(0, -1), { ...last, newer: lastSeq(data.pages) }],
    });
  }

  // Reads the step rows that can still change, each agent's latest while a tool call of it runs
  // and any after it, and merges them in; a burst during the read reads once more after it.
  refreshSteps(): void {
    if (this.#refreshing) {
      this.#again = true;
      return;
    }
    const key = ["runs", "steps", this.runId] as const;
    if (this.qc.getQueryState(key)?.fetchStatus === "fetching") {
      // that read, of the first page, the next or all, may have started before these events:
      // read on from it once it is in
      this.#afterRead(hashKey(key));
      return;
    }
    const data = this.qc.getQueryData<InfiniteData<StepPage, number | null>>(key);
    if (!data || (data.pages.at(-1)?.next ?? null) !== null) return;
    const rows = data.pages.flatMap((p) => p.items);
    const first = firstOpen(rows, this.#settled);
    // a row with steps after it may be the last of an agent that ended before the stream opened
    if (!this.#endsRead && first < rows.length - 1) this.#readEnds();
    const after = startAt(rows, first);
    // agents whose end came before this read: their rows in it are final
    const ended = this.#ended.size > this.#settled.size ? [...this.#ended] : [];
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
        for (const agent of ended) this.#settled.add(agent);
      })
      .finally(() => {
        this.#refreshing = false;
        if (this.#again && !this.#stopped) {
          this.#again = false;
          this.refreshSteps();
        }
      });
  }

  // Agents that ended before the stream opened, which only their logged ends tell. Read once.
  #readEnds(): void {
    this.#endsRead = true;
    void client
      .GET("/api/runs/{run_id}/events", {
        params: {
          path: { run_id: this.runId },
          query: { types: ["agent.finished"], limit: MAX_HELD },
        },
      })
      .then(({ data }) => {
        for (const e of data?.items ?? []) this.#ended.add(e.agent_id ?? "");
      })
      .catch(() => undefined);
  }

  #afterRead(hash: string): void {
    if (this.#waiting) return;
    this.#waiting = true;
    const off = this.qc.getQueryCache().subscribe(({ query }) => {
      if (query.queryHash !== hash || query.state.fetchStatus === "fetching") return;
      off();
      this.#waiting = false;
      if (!this.#stopped) this.refreshSteps();
    });
  }
}

// The seq of a raw list's last event, past pages that came back empty; -1 for none.
function lastSeq(pages: readonly RawPage[]): number {
  for (let i = pages.length - 1; i >= 0; i--) {
    const seq = pages[i]?.items.at(-1)?.seq;
    if (seq !== undefined) return seq;
  }
  return -1;
}

// Past MAX_RAW events a list lets its oldest pages go, down to half, so a batch never walks
// every event the list took. Its first page then reads them again on the way up.
function trim(pages: RawPage[], params: unknown[]): void {
  let total = 0;
  for (const p of pages) total += p.items.length;
  if (total <= MAX_RAW) return;
  let drop = 0;
  while (total > MAX_RAW / 2 && drop < pages.length - 1) total -= pages[drop++]?.items.length ?? 0;
  pages.splice(0, drop);
  params.splice(0, drop);
  const first = pages.find((p) => p.items.length > 0)?.items[0]?.seq;
  const top = pages[0];
  if (top && first !== undefined) pages[0] = { ...top, older: first };
}

// The index of the first row that may still change: some agent's latest, while a tool call of
// it has no result and the agent is not settled. A step logs its tool calls before itself, so
// a row whose calls all have results is final. Past the last row when none may change.
function firstOpen(rows: readonly StepInfo[], settled: ReadonlySet<string>): number {
  const latest = new Map<string, number>();
  rows.forEach((r, i) => latest.set(r.agent_id ?? "", i));
  let first = rows.length;
  for (const [agent, i] of latest) {
    if (i < first && !settled.has(agent) && rows[i]?.tools?.some((t) => !t.done)) first = i;
  }
  return first;
}

// The seq just before row `first`; null reads from the first step.
const startAt = (rows: readonly StepInfo[], first: number) =>
  first > 0 ? (rows[first - 1]?.seq ?? null) : null;

// The seq to read on from: just before the first row that may still change.
export function tailStart(
  rows: readonly StepInfo[],
  settled: ReadonlySet<string> = NO_AGENTS,
): number | null {
  return startAt(rows, firstOpen(rows, settled));
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
