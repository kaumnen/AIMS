import { type Channel, type Member, sharedChannel } from "./channel";
import {
  type LiveState,
  parseMessage,
  type StreamKind,
  streamKinds,
  type StreamMessage,
  streamUrl,
} from "./stream";
import type { Subscriptions } from "./stream";

export type { LiveState } from "./stream";
export type MessageOf<K extends StreamKind> = Extract<StreamMessage, { kind: K }>;
export type RunnerInfo = MessageOf<"runner">["runner"];
type Listener = (message: StreamMessage) => void;

// One subscription of the page. Tabs share one connection through a shared worker; where
// that fails, the subscription opens its own EventSource. Parts of the page register by
// message kind.
export class Live {
  #source: EventSource | null = null;
  #channel: Channel | null = null;
  #id: number | null = null;
  // the last seq held of each run, where a new connection resumes
  #cursors = new Map<string, number>();
  #listeners = new Map<StreamKind, Set<Listener>>();
  #watchers = new Set<() => void>();
  #state: LiveState = "connecting";
  // tabs on the shared connection, null while this one holds its own
  #sharing: number | null = null;
  // the server sends every runner once a stream opens, then changes; parts mounted later read
  // them here
  #runners: readonly RunnerInfo[] = [];

  constructor(
    readonly subscriptions: Subscriptions,
    // the browser gave up, which a refused session also causes
    readonly onClosed: () => void = () => undefined,
    readonly channel: () => Channel | null = sharedChannel,
  ) {}

  get state(): LiveState {
    return this.#state;
  }

  get runners(): readonly RunnerInfo[] {
    return this.#runners;
  }

  get sharing(): number | null {
    return this.#sharing;
  }

  start(): void {
    this.stop();
    this.#cursors = new Map((this.subscriptions.run ?? []).map((r) => [r.id, r.after]));
    const channel = this.channel();
    const id = channel?.add(this.#member) ?? null;
    if (channel && id !== null) {
      this.#channel = channel;
      this.#id = id;
      this.#update(channel.state, channel.tabs);
    } else this.#own();
  }

  stop(): void {
    this.#source?.close();
    this.#source = null;
    if (this.#channel && this.#id !== null) this.#channel.remove(this.#id);
    this.#channel = null;
    this.#id = null;
    this.#update("connecting", null);
  }

  on<K extends StreamKind>(kind: K, fn: (message: MessageOf<K>) => void): () => void {
    let set = this.#listeners.get(kind);
    if (!set) this.#listeners.set(kind, (set = new Set()));
    const listener = fn as Listener;
    set.add(listener);
    return () => set.delete(listener);
  }

  // for useSyncExternalStore over `state` and `sharing`
  watch = (fn: () => void): (() => void) => {
    this.#watchers.add(fn);
    return () => this.#watchers.delete(fn);
  };

  #member: Member = {
    wanted: () => ({
      runs: this.subscriptions.runs ?? false,
      run: [...this.#cursors].map(([id, after]) => ({ id, after })),
      deltas: this.subscriptions.deltas ?? [],
    }),
    deliver: (m) => this.#deliver(m),
    shared: (state, tabs) => this.#update(state, tabs),
    closed: () => this.onClosed(),
    fallback: () => {
      this.#channel = null;
      this.#id = null;
      this.#own();
    },
  };

  #own(): void {
    const run = [...this.#cursors].map(([id, after]) => ({ id, after }));
    const source = new EventSource(streamUrl({ ...this.subscriptions, run }));
    for (const kind of streamKinds) {
      source.addEventListener(kind, (e) =>
        this.#deliver(parseMessage((e as MessageEvent<string>).data)),
      );
    }
    source.addEventListener("open", () => this.#update("open", null));
    source.addEventListener("error", () => {
      if (this.#state === "open") this.#update("retrying", null);
      if (source.readyState === EventSource.CLOSED) this.onClosed();
    });
    this.#source = source;
    // a worker that died while open leaves this tab reconnecting on its own
    this.#update(this.#state === "open" ? "retrying" : this.#state, null);
  }

  #deliver(message: StreamMessage): void {
    let m = message;
    if (m.kind === "events") {
      const last = m.events.at(-1);
      const held = this.#cursors.get(m.run_id);
      if (last && held !== undefined && last.seq > held) this.#cursors.set(m.run_id, last.seq);
      // the shared stream carries every type; the server filters a connection of our own
      const types: readonly string[] | undefined = this.subscriptions.types;
      if (this.#channel && types) {
        const events = m.events.filter((e) => types.includes(e.type));
        if (events.length === 0) return;
        m = { ...m, events };
      }
    }
    if (m.kind === "runner") {
      const { runner } = m;
      this.#runners = [...this.#runners.filter((r) => r.name !== runner.name), runner].toSorted(
        (a, b) => a.name.localeCompare(b.name),
      );
      for (const fn of this.#watchers) fn();
    }
    for (const fn of this.#listeners.get(m.kind) ?? []) fn(m);
  }

  #update(state: LiveState, sharing: number | null): void {
    if (state === this.#state && sharing === this.#sharing) return;
    this.#state = state;
    this.#sharing = sharing;
    for (const fn of this.#watchers) fn();
  }
}
