import { hashKey, type QueryClient } from "@tanstack/react-query";

import { reconnectLive, sharedChannel } from "./channel";
import { $api, client, type Problem, type Schemas } from "./client";

type Whoami = Schemas["Whoami"];

// An answer with no body, such as a proxy's 502 while the server restarts, is an error here
// too: read through `$api` it would come back as nobody at all, with no error.
export const whoami = {
  ...$api.queryOptions("get", "/api/session"),
  queryFn: async ({ signal }: { signal: AbortSignal }): Promise<Whoami> => {
    const got: { data?: Whoami; error?: unknown; response: Response } = await client.GET(
      "/api/session",
      { signal },
    );
    if (got.data) return got.data;
    throw got.error ?? { type: "about:blank", title: "No answer", status: got.response.status };
  },
};

// Drops all a session loaded but who is signed in, so none of it shows for the next one. The
// server lists some runs to operators only.
export function forgetSession(qc: QueryClient): void {
  const keep = hashKey(whoami.queryKey);
  qc.removeQueries({ predicate: (q) => q.queryHash !== keep });
}

const same = (a: Whoami, b: Whoami) => a.name === b.name && a.role === b.role && a.auth === b.auth;

// Another tab can sign in as someone else, or sign out, with the same cookie. A tab asks again
// who it is each time it comes back into view, also while it shows the sign-in form. Someone
// else, or nobody, drops all the last session loaded, as a sign-in does; the same one changes
// nothing.
export function watchSession(qc: QueryClient): () => void {
  let asking = false;
  const ask = async () => {
    const state = qc.getQueryState<Whoami>(whoami.queryKey);
    const before = state?.data;
    const out = state?.status === "error" && signedOut(state.error);
    if (asking || (!out && state?.status !== "success") || document.visibilityState !== "visible") {
      return;
    }
    asking = true;
    try {
      const { data, response } = await client.GET("/api/session");
      if (data ? !out && before && same(data, before) : out || response.status !== 401) return;
      // the tab moved on meanwhile, a sign-out say
      if (qc.getQueryData(whoami.queryKey) !== before) return;
      // the tabs' shared stream may still hold the old session
      reconnectLive();
      forgetSession(qc);
      await qc.resetQueries({ queryKey: whoami.queryKey });
    } catch {
      // out of reach: the stream says so
    } finally {
      asking = false;
    }
  };
  const onShow = () => void ask();
  addEventListener("focus", onShow);
  document.addEventListener("visibilitychange", onShow);
  return () => {
    removeEventListener("focus", onShow);
    document.removeEventListener("visibilitychange", onShow);
  };
}

// `aims ui` opens /#code=...; the one-shot code leaves the URL first, then becomes the session
// cookie. The tabs' shared stream may hold another session, so it connects again.
export async function handoff(): Promise<void> {
  const code = new URLSearchParams(location.hash.slice(1)).get("code");
  if (!code) return;
  history.replaceState(history.state, "", location.pathname + location.search);
  // a failed trade leaves the app signed out, which it shows
  const traded = await client.POST("/api/session", { body: { code } }).catch(() => undefined);
  if (traded?.response.ok) sharedChannel()?.reconnect();
}

export function isProblem(e: unknown): e is Problem {
  return typeof e === "object" && e !== null && "status" in e && "title" in e;
}

export function signedOut(e: unknown): boolean {
  return isProblem(e) && e.status === 401;
}
