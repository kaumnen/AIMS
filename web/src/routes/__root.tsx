import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";
import { useEffect } from "react";

import { onUnauthorized } from "@/api/client";
import { signedOut, watchSession, whoami } from "@/api/session";
import { Crumbs } from "@/components/crumbs";
import { Header } from "@/components/header";
import { LiveProvider } from "@/components/live";
import { Shortcuts } from "@/components/shortcuts";
import { SignIn, Unreachable } from "@/components/sign-in";
import { TooltipProvider } from "@/components/ui/tooltip";

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: Root,
});

// Everything waits on who is signed in; a 401 anywhere brings back the sign-in form in place.
function Root() {
  const queryClient = useQueryClient();
  const me = useQuery({ ...whoami, retry: false, staleTime: Infinity });

  useEffect(() => {
    const recheck = () => {
      if (queryClient.getQueryData(whoami.queryKey) !== undefined) {
        void queryClient.resetQueries({ queryKey: whoami.queryKey });
      }
    };
    return onUnauthorized(recheck);
  }, [queryClient]);
  useEffect(() => watchSession(queryClient), [queryClient]);

  if (me.isPending) return null;
  if (me.isError) {
    return signedOut(me.error) ? <SignIn /> : <Unreachable retry={() => void me.refetch()} />;
  }
  return (
    <TooltipProvider>
      <LiveProvider onClosed={async () => (await me.refetch()).isSuccess}>
        <div data-shell className="flex h-dvh flex-col print:h-auto">
          <a
            href="#main"
            onClick={(e) => {
              e.preventDefault();
              document.getElementById("main")?.focus();
            }}
            className="bg-background focus-visible:ring-ring/50 fixed top-1.5 left-2 z-50 -translate-y-16 rounded-sm border px-2 py-1 outline-none focus:translate-y-0 focus-visible:ring-2"
          >
            Skip to content
          </a>
          <Header me={me.data} />
          {/* The crumbs scroll away with the page but stay before main, so the skip link skips them. */}
          <div
            data-scroller
            className="flex min-h-0 flex-1 flex-col overflow-auto print:overflow-visible"
          >
            <Crumbs />
            <main
              id="main"
              tabIndex={-1}
              className="flex min-h-0 flex-1 flex-col px-4 pt-3 outline-none after:h-3 after:shrink-0 print:p-0 print:after:hidden"
            >
              <Outlet />
            </main>
          </div>
          <Shortcuts />
        </div>
      </LiveProvider>
    </TooltipProvider>
  );
}
