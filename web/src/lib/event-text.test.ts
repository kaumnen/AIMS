import { expect, test } from "vitest";

import type { Event } from "@/api/events";

import { summary } from "./event-text";

const exec = (data: Record<string, unknown>) => ({ type: "proc.exec", data }) as unknown as Event;
const setattr = (data: Record<string, unknown>) =>
  ({ type: "file.setattr", data }) as unknown as Event;

test("a command line longer than the tracer reads says it is cut", () => {
  expect(summary(exec({ argv: ["echo", "a"] }))).toBe("echo a");
  expect(summary(exec({ argv: ["echo", "aaa"], argv_partial: true }))).toBe("echo aaa (cut)");
});

test("a refused metadata change shows its errno", () => {
  expect(summary(setattr({ path: "/work/f" }))).toBe("setattr /work/f");
  expect(summary(setattr({ path: "/etc/profile", errno: 30 }))).toBe(
    "setattr /etc/profile errno 30",
  );
});
