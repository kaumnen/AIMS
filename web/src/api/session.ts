import { client } from "./client";

// `aims ui` opens /#token=...; the token leaves the URL first, then becomes the session cookie.
export async function handoff(): Promise<void> {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (!token) return;
  history.replaceState(history.state, "", location.pathname + location.search);
  // a failed trade leaves the app signed out, which it shows
  await client.POST("/api/session", { body: { token } }).catch(() => undefined);
}
