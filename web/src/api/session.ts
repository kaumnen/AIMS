import { hashKey, type QueryClient } from "@tanstack/react-query";

import { $api, client, type Problem } from "./client";

export const whoami = $api.queryOptions("get", "/api/session");

// Drops all a session loaded but who is signed in, so none of it shows for the next one. The
// server lists some runs to operators only.
export function forgetSession(qc: QueryClient): void {
  const keep = hashKey(whoami.queryKey);
  qc.removeQueries({ predicate: (q) => q.queryHash !== keep });
}

// `aims ui` opens /#code=...; the one-shot code leaves the URL first, then becomes the session
// cookie.
export async function handoff(): Promise<void> {
  const code = new URLSearchParams(location.hash.slice(1)).get("code");
  if (!code) return;
  history.replaceState(history.state, "", location.pathname + location.search);
  // a failed trade leaves the app signed out, which it shows
  await client.POST("/api/session", { body: { code } }).catch(() => undefined);
}

export function isProblem(e: unknown): e is Problem {
  return typeof e === "object" && e !== null && "status" in e && "title" in e;
}

export function signedOut(e: unknown): boolean {
  return isProblem(e) && e.status === 401;
}
