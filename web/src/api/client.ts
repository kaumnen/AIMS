import createFetchClient from "openapi-fetch";
import createClient from "openapi-react-query";

import type { components, paths } from "./schema.gen";

export type Schemas = components["schemas"];
export type Problem = Schemas["Problem"];

export const client = createFetchClient<paths>({
  baseUrl: "",
  credentials: "same-origin",
  // late bound so tests can stub the global
  fetch: (request) => globalThis.fetch(request),
});

const unauthorized = new EventTarget();

// A 401 anywhere but the session endpoint means the session ended under the page.
client.use({
  onResponse({ request, response }) {
    if (response.status === 401 && new URL(request.url).pathname !== "/api/session") {
      unauthorized.dispatchEvent(new Event("401"));
    }
    return undefined;
  },
});

// Etags are kept as the server's JSON gives them; a header names one in quotes.
export const quoted = (etag: string) => `"${etag}"`;

// What a write replaces: the version `etag` names, or no file yet for null.
export function precondition(etag: string | null): {
  "If-Match"?: string;
  "If-None-Match"?: string;
} {
  return etag === null ? { "If-None-Match": "*" } : { "If-Match": quoted(etag) };
}

export function onUnauthorized(fn: () => void): () => void {
  unauthorized.addEventListener("401", fn);
  return () => unauthorized.removeEventListener("401", fn);
}

export const $api = createClient(client);
