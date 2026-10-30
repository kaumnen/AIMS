import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";
import { useEffect } from "react";

import { onUnauthorized } from "@/api/client";
import { signedOut, whoami } from "@/api/session";
import { Crumbs } from "@/components/crumbs";
import { Header } from "@/components/header";
import { LiveProvider } from "@/components/live";
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

  if (me.isPending) return null;
  if (me.isError) {
    return signedOut(me.error) ? <SignIn /> : <Unreachable retry={() => void me.refetch()} />;
  }
  return (
    <TooltipProvider>
      <LiveProvider onClosed={() => void me.refetch()}>
        <div className="flex min-h-dvh flex-col">
          <Header me={me.data} />
          <Crumbs />
          <main className="flex-1 px-4 py-3">
            <Outlet />
          </main>
        </div>
      </LiveProvider>
    </TooltipProvider>
  );
}
