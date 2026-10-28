import { type components, eventTypeValues } from "./schema.gen";

type Schemas = components["schemas"];
export type Event = Schemas["Event"];
export type EventType = Schemas["EventType"];
export type EventTypes = Schemas["EventTypes"];

export type KnownEvent = {
  [K in keyof EventTypes]: Omit<Event, "type" | "data"> & { type: K; data: EventTypes[K] };
}[keyof EventTypes];

const types: ReadonlySet<string> = new Set(eventTypeValues);

export function known(e: Event): e is KnownEvent {
  return types.has(e.type);
}
