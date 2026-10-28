import { expect, test } from "vitest";

import { parseMessage, streamKinds, streamUrl } from "./stream";

test("streamUrl repeats keys and pairs each run with its after", () => {
  expect(streamUrl({})).toBe("/api/stream");
  expect(
    streamUrl({
      runs: true,
      run: [
        { id: "a", after: -1 },
        { id: "b", after: 7 },
      ],
      types: ["tool.call", "run.started"],
      deltas: ["a", "b"],
    }),
  ).toBe(
    "/api/stream?runs=true&run=a&after=-1&run=b&after=7&types=tool.call&types=run.started&deltas=a&deltas=b",
  );
});

test("parseMessage reads a message discriminated by kind", () => {
  const m = parseMessage('{"kind":"deltas_dropped","count":3,"run_id":null}');
  expect(m.kind === "deltas_dropped" && m.count).toBe(3);
});

test("streamKinds lists every kind", () => {
  expect(streamKinds).toEqual(["events", "run", "runner", "delta", "deltas_dropped", "end"]);
});
