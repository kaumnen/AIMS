import {
  parseMessage,
  type StreamKind,
  streamKinds,
  type StreamMessage,
  streamUrl,
} from "./stream";
import type { Subscriptions } from "./stream";

export type MessageOf<K extends StreamKind> = Extract<StreamMessage, { kind: K }>;
type Listener = (message: StreamMessage) => void;
export type LiveState = "connecting" | "open" | "retrying";

// The one EventSource a tab holds. Parts of the page register by message kind instead of
// opening their own connection.
export class Live {
  #source: EventSource | null = null;
  #listeners = new Map<StreamKind, Set<Listener>>();
  #watchers = new Set<() => void>();
  #state: LiveState = "connecting";

  constructor(
    readonly subscriptions: Subscriptions,
    // the browser gave up, which a refused session also causes
    readonly onClosed: () => void = () => undefined,
  ) {}

  get state(): LiveState {
    return this.#state;
  }

  start(): void {
    this.stop();
    const source = new EventSource(streamUrl(this.subscriptions));
    for (const kind of streamKinds) {
      source.addEventListener(kind, (e) =>
        this.#emit(parseMessage((e as MessageEvent<string>).data)),
      );
    }
    source.addEventListener("open", () => this.#set("open"));
    source.addEventListener("error", () => {
      if (this.#state === "open") this.#set("retrying");
      if (source.readyState === EventSource.CLOSED) this.onClosed();
    });
    this.#source = source;
  }

  stop(): void {
    this.#source?.close();
    this.#source = null;
    this.#set("connecting");
  }

  on<K extends StreamKind>(kind: K, fn: (message: MessageOf<K>) => void): () => void {
    let set = this.#listeners.get(kind);
    if (!set) this.#listeners.set(kind, (set = new Set()));
    const listener = fn as Listener;
    set.add(listener);
    return () => set.delete(listener);
  }

  // for useSyncExternalStore over `state`
  watch = (fn: () => void): (() => void) => {
    this.#watchers.add(fn);
    return () => this.#watchers.delete(fn);
  };

  #emit(message: StreamMessage): void {
    for (const fn of this.#listeners.get(message.kind) ?? []) fn(message);
  }

  #set(state: LiveState): void {
    if (state === this.#state) return;
    this.#state = state;
    for (const fn of this.#watchers) fn();
  }
}
