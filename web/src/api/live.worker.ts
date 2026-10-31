import { type FromHub, type FromTab, Hub, type Open, PING_MS } from "./hub";
import { streamKinds } from "./stream";

const open: Open = (url, on) => {
  const source = new EventSource(url);
  for (const kind of streamKinds) {
    source.addEventListener(kind, (e) => {
      const m = e as MessageEvent<string>;
      on.message(m.data, m.lastEventId);
    });
  }
  source.addEventListener("open", () => on.open());
  source.addEventListener("error", () => on.error(source.readyState === EventSource.CLOSED));
  return source;
};

const hub = typeof EventSource === "undefined" ? null : new Hub(open);
if (hub) setInterval(() => hub.tick(), PING_MS);

// connect is the shared worker's event, which the DOM lib has no type for
addEventListener("connect", (e) => {
  const port = (e as MessageEvent).ports[0];
  if (!port) return;
  const peer = { send: (m: FromHub) => port.postMessage(m) };
  port.addEventListener("message", (m: MessageEvent<FromTab>) => {
    if (hub) hub.receive(peer, m.data);
    else peer.send({ t: "unsupported" });
  });
  port.start();
});
