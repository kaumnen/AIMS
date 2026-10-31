import type { components } from "./schema.gen";
import { type LiveState, parseMessage, type StreamMessage, streamUrl } from "./stream";

type RunnerInfo = components["schemas"]["RunnerInfo"];

// Tabs and the worker check it on hello; a tab on another version keeps its own connection.
export const PROTOCOL = 1;
export const COALESCE_MS = 50;
export const PING_MS = 5000;
export const SILENT_MS = 15_000;

// What one subscriber wants: `after` is the last seq it holds of each run.
export interface Wanted {
  runs: boolean;
  run: readonly { id: string; after: number }[];
  deltas: readonly string[];
}

export type FromTab =
  | { t: "hello"; v: number }
  | { t: "sub"; id: number; wanted: Wanted }
  | { t: "unsub"; id: number }
  | { t: "pong" }
  | { t: "bye" }
  | { t: "reconnect" };

export type FromHub =
  | { t: "ack"; v: number; known: boolean; state: LiveState; tabs: number }
  | { t: "unsupported" }
  | { t: "msg"; id: number; message: StreamMessage }
  | { t: "state"; state: LiveState; tabs: number }
  | { t: "closed" }
  | { t: "ping" }
  | { t: "gone" };

export interface Peer {
  send(m: FromHub): void;
}

export interface SourceEvents {
  open(): void;
  message(data: string, lastEventId: string): void;
  // `closed` when the browser gave up, which a refused session also causes
  error(closed: boolean): void;
}

export type Open = (url: string, on: SourceEvents) => { close(): void };

interface Sub {
  runs: boolean;
  cursors: Map<string, number>;
  ended: Set<string>;
  deltas: Set<string>;
}

interface Tab {
  heard: number;
  subs: Map<number, Sub>;
}

// Inside the shared worker: one stream carries what every tab asked for, and each subscriber
// gets only its runs, past the seq it holds.
export class Hub {
  #tabs = new Map<Peer, Tab>();
  #source: { close(): void } | null = null;
  #generation = 0;
  // the union the open stream carries
  #key: string | null = null;
  // a subscriber joined that needs events or deltas the stream already sent
  #stale = false;
  #version: string | null = null;
  #runners = new Map<string, RunnerInfo>();
  #state: LiveState = "connecting";
  #timer: ReturnType<typeof setTimeout> | null = null;

  constructor(readonly open: Open) {}

  receive(peer: Peer, m: FromTab): void {
    if (m.t === "hello") {
      this.#hello(peer, m.v);
      return;
    }
    const tab = this.#tabs.get(peer);
    if (!tab) {
      peer.send({ t: "gone" });
      return;
    }
    tab.heard = Date.now();
    if (m.t === "sub") this.#sub(peer, tab, m.id, m.wanted);
    else if (m.t === "unsub") {
      tab.subs.delete(m.id);
      this.#changed();
    } else if (m.t === "bye") this.#drop(peer);
    else if (m.t === "reconnect") {
      this.#stale = true;
      this.#schedule();
    }
  }

  // every PING_MS: tabs that stopped answering are gone, the rest are asked again
  tick(): void {
    const now = Date.now();
    for (const [peer, tab] of this.#tabs) {
      if (now - tab.heard > SILENT_MS) this.#drop(peer);
      else peer.send({ t: "ping" });
    }
  }

  #hello(peer: Peer, v: number): void {
    const tab = this.#tabs.get(peer);
    const known = tab !== undefined;
    if (v === PROTOCOL) {
      if (tab) tab.heard = Date.now();
      else this.#tabs.set(peer, { heard: Date.now(), subs: new Map() });
    }
    peer.send({ t: "ack", v: PROTOCOL, known, state: this.#state, tabs: this.#tabs.size });
    if (v === PROTOCOL && !known) this.#broadcast();
  }

  #sub(peer: Peer, tab: Tab, id: number, wanted: Wanted): void {
    const sub: Sub = {
      runs: wanted.runs,
      cursors: new Map(wanted.run.map((r) => [r.id, r.after])),
      ended: new Set(),
      deltas: new Set(wanted.deltas),
    };
    tab.subs.set(id, sub);
    if (sub.cursors.size > 0 || sub.deltas.size > 0) {
      this.#stale = true;
      this.#schedule();
    } else if (sub.runs) {
      if (this.#source === null || this.#union().key !== this.#key) this.#schedule();
      else for (const runner of this.#runners.values()) send(peer, id, { kind: "runner", runner });
    }
  }

  #drop(peer: Peer): void {
    if (!this.#tabs.delete(peer)) return;
    this.#broadcast();
    this.#changed();
  }

  #changed(): void {
    if (this.#union().key !== this.#key) this.#schedule();
  }

  #schedule(): void {
    if (this.#timer !== null) return;
    this.#timer = setTimeout(() => {
      this.#timer = null;
      this.#connect();
    }, COALESCE_MS);
  }

  #union(): { wanted: Wanted; key: string } {
    let runs = false;
    const after = new Map<string, number>();
    const deltas = new Set<string>();
    for (const sub of this.#subs()) {
      runs ||= sub.runs;
      for (const [run, seq] of sub.cursors) {
        if (!sub.ended.has(run)) after.set(run, Math.min(after.get(run) ?? seq, seq));
      }
      for (const d of sub.deltas) deltas.add(d);
    }
    const run = [...after].toSorted(([a], [b]) => a.localeCompare(b));
    const wanted = {
      runs,
      run: run.map(([id, seq]) => ({ id, after: seq })),
      deltas: [...deltas].toSorted(),
    };
    return { wanted, key: JSON.stringify([runs, run.map(([id]) => id), wanted.deltas]) };
  }

  #connect(): void {
    const { wanted, key } = this.#union();
    if (this.#source !== null && key === this.#key && !this.#stale) return;
    this.#source?.close();
    this.#source = null;
    this.#stale = false;
    if (!wanted.runs && wanted.run.length === 0 && wanted.deltas.length === 0) {
      this.#key = null;
      this.#set("connecting");
      return;
    }
    this.#key = key;
    const generation = ++this.#generation;
    const current = () => generation === this.#generation;
    this.#source = this.open(streamUrl(wanted, this.#version ?? undefined), {
      open: () => {
        if (current()) this.#set("open");
      },
      message: (data, id) => {
        if (current()) this.#message(data, id);
      },
      error: (closed) => {
        if (!current()) return;
        if (closed) {
          this.#source = null;
          this.#key = null;
          this.#set("connecting");
          for (const peer of this.#tabs.keys()) peer.send({ t: "closed" });
        } else if (this.#state === "open") this.#set("retrying");
      },
    });
  }

  #message(data: string, lastEventId: string): void {
    // only the run list's version: each subscriber's `after` says where its runs resume
    const version = lastEventId.split(",")[0];
    if (version?.startsWith("@")) this.#version = version;
    const m = parseMessage(data);
    for (const [peer, tab] of this.#tabs) {
      for (const [id, sub] of tab.subs) {
        const out = route(m, sub);
        if (out !== null) send(peer, id, out);
      }
    }
    if (m.kind === "runner") this.#runners.set(m.runner.name, m.runner);
  }

  *#subs(): Generator<Sub> {
    for (const tab of this.#tabs.values()) yield* tab.subs.values();
  }

  #set(state: LiveState): void {
    if (state === this.#state) return;
    this.#state = state;
    this.#broadcast();
  }

  #broadcast(): void {
    const m: FromHub = { t: "state", state: this.#state, tabs: this.#tabs.size };
    for (const peer of this.#tabs.keys()) peer.send(m);
  }
}

function send(peer: Peer, id: number, message: StreamMessage): void {
  peer.send({ t: "msg", id, message });
}

// What of a message one subscriber gets, moving its cursors; null for nothing.
function route(m: StreamMessage, sub: Sub): StreamMessage | null {
  switch (m.kind) {
    case "events": {
      const held = sub.cursors.get(m.run_id);
      if (held === undefined) return null;
      const events = m.events.filter((e) => e.seq > held);
      const last = events.at(-1);
      if (last === undefined) return null;
      sub.cursors.set(m.run_id, last.seq);
      return { ...m, events };
    }
    case "end":
      if (!sub.cursors.has(m.run_id) || sub.ended.has(m.run_id)) return null;
      sub.ended.add(m.run_id);
      return m;
    case "run":
      return sub.runs || sub.cursors.has(m.run.run_id) ? m : null;
    case "runner":
      return sub.runs ? m : null;
    case "delta": {
      const deltas = m.deltas.filter((d) => sub.deltas.has(d.run_id));
      return deltas.length > 0 ? { ...m, deltas } : null;
    }
    case "deltas_dropped":
      return sub.deltas.size > 0 && (m.run_id === null || sub.deltas.has(m.run_id)) ? m : null;
  }
}
