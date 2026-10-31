import type { EventType } from "./events";
import type { components } from "./schema.gen";

export type StreamMessage = components["schemas"]["StreamMessage"];
export type StreamKind = StreamMessage["kind"];
export type LiveState = "connecting" | "open" | "retrying";

// Each SSE event is named by its kind, so EventSource needs a listener per kind.
const kindSet: Record<StreamKind, null> = {
  events: null,
  run: null,
  runner: null,
  delta: null,
  deltas_dropped: null,
  end: null,
};
export const streamKinds = Object.keys(kindSet) as StreamKind[];

export interface Subscriptions {
  runs?: boolean;
  // after is the last seq already held, -1 for the whole run
  run?: readonly { id: string; after: number }[];
  types?: readonly EventType[];
  deltas?: readonly string[];
}

// `last` resumes the run list from an id a stream sent before
export function streamUrl(s: Subscriptions, last?: string): string {
  const q = new URLSearchParams();
  if (s.runs) q.set("runs", "true");
  // the server pairs run and after by position
  for (const { id, after } of s.run ?? []) {
    q.append("run", id);
    q.append("after", String(after));
  }
  for (const t of s.types ?? []) q.append("types", t);
  for (const id of s.deltas ?? []) q.append("deltas", id);
  if (last) q.set("last_event_id", last);
  const query = q.toString();
  return query ? `/api/stream?${query}` : "/api/stream";
}

export function parseMessage(data: string): StreamMessage {
  return JSON.parse(data) as StreamMessage;
}
