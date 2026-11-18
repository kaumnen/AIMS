import { expect, test } from "vitest";

import type { Event } from "@/api/events";

import { summary } from "./event-text";

const exec = (data: Record<string, unknown>) => ({ type: "proc.exec", data }) as unknown as Event;
const setattr = (data: Record<string, unknown>) =>
  ({ type: "file.setattr", data }) as unknown as Event;
const ptrace = (data: Record<string, unknown>) =>
  ({ type: "ptrace.event", data }) as unknown as Event;
const look = (data: Record<string, unknown>) => ({ type: "file.look", data }) as unknown as Event;
const of = (type: string, data: Record<string, unknown>) => ({ type, data }) as unknown as Event;

test("an extended attribute change names the attribute and the file", () => {
  expect(summary(of("file.xattr", { op: "set", name: "user.k", path: "/work/f" }))).toBe(
    "xattr set user.k on /work/f",
  );
  expect(
    summary(of("file.xattr", { op: "remove", name: "trusted.k", path: "/etc/a", errno: 30 })),
  ).toBe("xattr remove trusted.k on /etc/a errno 30");
});

test("a bind or listen names its address and port", () => {
  expect(
    summary(of("net.listen", { kind: "listen", protocol: "tcp", address: "127.0.0.1", port: 80 })),
  ).toBe("listen tcp 127.0.0.1 80");
  expect(summary(of("net.listen", { kind: "listen", address: "0.0.0.0" }))).toBe(
    "listen 0.0.0.0 any port",
  );
});

test("a refused FIFO shows its errno", () => {
  expect(summary(of("ipc.event", { kind: "fifo", path: "/etc/p", errno: 30 }))).toBe(
    "fifo /etc/p errno 30",
  );
});

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

test("an open through another process's /proc link says what it opened", () => {
  expect(summary(ptrace({ request: "attach", target: 9, errno: 1 }))).toBe("attach of 9 errno 1");
  expect(
    summary(ptrace({ request: "proc_fd", target: 9, fd: 3, access: "read", path: "pipe:[7]" })),
  ).toBe("proc_fd of 9 fd 3 for read pipe:[7]");
});

test("a lookup says what was looked at and a miss shows its errno", () => {
  expect(summary(look({ op: "list", path: "/.aims" }))).toBe("list /.aims");
  expect(summary(look({ op: "stat", path: "/run/.containerenv", errno: 2 }))).toBe(
    "stat /run/.containerenv errno 2",
  );
  expect(summary(ptrace({ request: "proc_cwd", target: 9, access: "chdir", path: "work" }))).toBe(
    "proc_cwd of 9 for chdir work",
  );
});
