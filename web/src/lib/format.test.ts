import { expect, test } from "vitest";

import { rate } from "./format";

test("a price per million tokens shows cents, or up to four places below ten cents", () => {
  expect([0, 0.1, 1.25, 20, 0.05, 0.002, 0.0015, 0.04].map(rate)).toEqual([
    "$0.00",
    "$0.10",
    "$1.25",
    "$20.00",
    "$0.05",
    "$0.002",
    "$0.0015",
    "$0.04",
  ]);
});
