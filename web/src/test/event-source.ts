import type { StreamMessage } from "@/api/stream";

// EventSource for happy-dom, which has none; tests drive it by hand.
export class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static all: FakeEventSource[] = [];

  readyState = FakeEventSource.CONNECTING;

  constructor(readonly url: string) {
    super();
    FakeEventSource.all.push(this);
  }

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.dispatchEvent(new Event("open"));
  }

  send(message: StreamMessage): void {
    this.dispatchEvent(new MessageEvent(message.kind, { data: JSON.stringify(message) }));
  }

  fail(closed: boolean): void {
    this.readyState = closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.dispatchEvent(new Event("error"));
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }
}

type RunnerInfo = Extract<StreamMessage, { kind: "runner" }>["runner"];

export function runnerInfo(over: Partial<RunnerInfo> = {}): RunnerInfo {
  return {
    name: "local",
    version: null,
    arch: null,
    tracer: { alive: true, detail: null, restarts: 0 },
    envs: [],
    slots: 2,
    free: 2,
    running: [],
    connected: true,
    stale: false,
    seen_s: 0,
    local: true,
    kinds: ["scenario", "models.check"],
    ...over,
  };
}
