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

export const $api = createClient(client);
