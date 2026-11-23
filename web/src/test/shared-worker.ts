import { vi } from "vitest";

import { type FromHub, type FromTab, Hub, type Open } from "@/api/hub";
import { streamKinds } from "@/api/stream";

import { FakeEventSource } from "./event-source";

// The worker's side, as live.worker.ts wires it, over FakeEventSource.
const open: Open = (url, on) => {
  const source = new FakeEventSource(url);
  for (const kind of streamKinds) {
    source.addEventListener(kind, (e) => {
      const m = e as MessageEvent<string>;
      on.message(m.data, m.lastEventId);
    });
  }
  source.addEventListener("open", () => on.open());
  source.addEventListener("error", () => on.error(source.readyState === FakeEventSource.CLOSED));
  return source;
};

// A shared worker in the page itself: each tab's port talks to one real Hub, a message at a
// time, as a MessagePort would. The tab's channel is made once per test file, so stub it before
// anything opens a stream.
export function shareStreams(): Hub {
  const hub = new Hub(open);
  class Port extends EventTarget {
    readonly peer = {
      send: (m: FromHub) =>
        queueMicrotask(() => this.dispatchEvent(new MessageEvent("message", { data: m }))),
    };
    postMessage(m: FromTab): void {
      queueMicrotask(() => hub.receive(this.peer, m));
    }
    start(): void {}
    close(): void {}
  }
  vi.stubGlobal(
    "SharedWorker",
    class extends EventTarget {
      port = new Port();
    },
  );
  return hub;
}
