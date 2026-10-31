import { type FromHub, type FromTab, PING_MS, PROTOCOL, SILENT_MS, type Wanted } from "./hub";
import type { LiveState, StreamMessage } from "./stream";

export const ACK_MS = 3000;

// One subscription of this tab, as the channel sees it.
export interface Member {
  // asked again whenever the worker lost this tab, so it holds the cursors reached by then
  wanted(): Wanted;
  deliver(m: StreamMessage): void;
  shared(state: LiveState, tabs: number): void;
  closed(): void;
  // the worker is unusable: open a connection of its own
  fallback(): void;
}

interface Port extends EventTarget {
  postMessage(m: FromTab): void;
  start(): void;
  close(): void;
}

export interface WorkerLike extends EventTarget {
  port: Port;
}

// This tab's side of the shared worker: subscriptions go up, messages come back to each.
export class Channel {
  #members = new Map<number, Member>();
  #next = 1;
  #failed = false;
  #acked = false;
  #heard = Date.now();
  #probe: ReturnType<typeof setTimeout> | null = null;
  #watch: ReturnType<typeof setInterval> | null = null;
  state: LiveState = "connecting";
  tabs = 1;

  constructor(readonly worker: WorkerLike) {
    worker.addEventListener("error", () => this.#fail());
    worker.port.addEventListener("message", (e) =>
      this.#receive((e as MessageEvent<FromHub>).data),
    );
    worker.port.start();
    this.#hello();
  }

  get failed(): boolean {
    return this.#failed;
  }

  // null once the worker failed: the member opens its own connection
  add(member: Member): number | null {
    if (this.#failed) return null;
    const id = this.#next++;
    this.#members.set(id, member);
    this.#post({ t: "sub", id, wanted: member.wanted() });
    return id;
  }

  remove(id: number): void {
    if (this.#members.delete(id)) this.#post({ t: "unsub", id });
  }

  // after a sign-out, so every tab learns its session is gone
  reconnect(): void {
    this.#post({ t: "reconnect" });
  }

  // pagehide
  hide(): void {
    this.#post({ t: "bye" });
  }

  // pageshow from the back-forward cache
  show(): void {
    this.#hello();
  }

  #post(m: FromTab): void {
    // a MessagePort takes no target origin
    // oxlint-disable-next-line unicorn/require-post-message-target-origin
    if (!this.#failed) this.worker.port.postMessage(m);
  }

  #hello(): void {
    this.#post({ t: "hello", v: PROTOCOL });
    this.#probe ??= setTimeout(() => this.#fail(), ACK_MS);
  }

  #receive(m: FromHub): void {
    if (this.#failed) return;
    this.#heard = Date.now();
    switch (m.t) {
      case "ack": {
        if (this.#probe !== null) clearTimeout(this.#probe);
        this.#probe = null;
        if (m.v !== PROTOCOL) {
          this.#fail();
          return;
        }
        if (!this.#acked) {
          this.#acked = true;
          this.#watch = setInterval(() => this.#check(), PING_MS);
        } else if (!m.known) {
          for (const [id, member] of this.#members) {
            this.#post({ t: "sub", id, wanted: member.wanted() });
          }
        }
        this.#shared(m.state, m.tabs);
        return;
      }
      case "state":
        this.#shared(m.state, m.tabs);
        return;
      case "msg":
        this.#members.get(m.id)?.deliver(m.message);
        return;
      case "closed":
        for (const member of this.#members.values()) member.closed();
        return;
      case "ping":
        this.#post({ t: "pong" });
        return;
      case "gone":
        this.#hello();
        return;
      case "unsupported":
        this.#fail();
        return;
    }
  }

  // timers of a hidden tab run late, so silence only leads to a probe, and the probe's answer
  // decides
  #check(): void {
    if (Date.now() - this.#heard > SILENT_MS && this.#probe === null) this.#hello();
  }

  #shared(state: LiveState, tabs: number): void {
    this.state = state;
    this.tabs = tabs;
    for (const member of this.#members.values()) member.shared(state, tabs);
  }

  #fail(): void {
    if (this.#failed) return;
    this.#failed = true;
    if (this.#probe !== null) clearTimeout(this.#probe);
    if (this.#watch !== null) clearInterval(this.#watch);
    this.worker.port.close();
    const members = [...this.#members.values()];
    this.#members.clear();
    for (const member of members) member.fallback();
  }
}

let made: Channel | null | undefined;

// The tab's channel to the shared worker, or null where there is none.
export function sharedChannel(): Channel | null {
  if (made === undefined) made = open();
  return made === null || made.failed ? null : made;
}

export function reconnectLive(): void {
  if (made && !made.failed) made.reconnect();
}

function open(): Channel | null {
  if (typeof SharedWorker === "undefined") return null;
  try {
    const worker = new SharedWorker(new URL("./live.worker.ts", import.meta.url), {
      type: "module",
      name: "aims-live",
    });
    const channel = new Channel(worker);
    addEventListener("pagehide", () => channel.hide());
    addEventListener("pageshow", (e) => {
      if (e.persisted) channel.show();
    });
    return channel;
  } catch {
    return null;
  }
}
