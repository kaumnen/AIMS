import { QueryClient } from "@tanstack/react-query";
import { createRouter, type RouterHistory } from "@tanstack/react-router";

import { isProblem } from "./api/session";
import { routeTree } from "./routeTree.gen";

// A 4xx answer stays the same on a retry.
const retry = (failures: number, error: unknown) =>
  !(isProblem(error) && error.status < 500) && failures < 3;

export function makeRouter(history?: RouterHistory) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry } } });
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    ...(history ? { history } : {}),
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
