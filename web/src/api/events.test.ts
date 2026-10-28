import { expect, expectTypeOf, test } from "vitest";

import { type Event, type EventType, type EventTypes, known } from "./events";
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

function event(type: string, data: Event["data"]): Event {
  const hash = "0".repeat(64);
  return { v: 1, run_id: "r", seq: 0, ts: "t", agent_id: null, type, data, prev_hash: hash, hash };
}

test("EventTypes has a payload for every EventType", () => {
  expectTypeOf<keyof EventTypes>().toEqualTypeOf<EventType>();
});

test("known narrows data by type", () => {
  const events = [
    event("run.started", { scenario: "s", oversight: "observe" }),
    event("operator.action", { action: "pause" }),
    event("tool.call", { call_id: "c", name: "bash", arguments: "{}" }),
  ];
  const seen: string[] = [];
  for (const e of events) {
    if (!known(e)) throw new Error(e.type);
    switch (e.type) {
      case "run.started":
        expectTypeOf(e.data).toEqualTypeOf<Schemas["RunStarted"]>();
        seen.push(e.data.scenario);
        break;
      case "operator.action":
        expectTypeOf(e.data).toEqualTypeOf<Schemas["OperatorAction"]>();
        seen.push(e.data.action);
        break;
      case "tool.call":
        expectTypeOf(e.data).toEqualTypeOf<Schemas["ToolCall"]>();
        expectTypeOf(e.data).not.toHaveProperty("scenario");
        seen.push(e.data.name);
        break;
    }
  }
  expect(seen).toEqual(["s", "pause", "bash"]);
});

test("known rejects types the client does not know", () => {
  expect(known(event("future.event", {}))).toBe(false);
});
