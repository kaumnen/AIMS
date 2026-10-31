import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  use,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import { Live, type LiveState } from "@/api/live";
import { syncRuns } from "@/api/runs";

const LiveContext = createContext<Live | null>(null);

export function LiveProvider({
  onClosed,
  children,
}: {
  onClosed: () => void;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [live] = useState(() => new Live({ runs: true }, onClosed));
  useEffect(() => {
    const unsync = syncRuns(live, queryClient);
    live.start();
    return () => {
      live.stop();
      unsync();
    };
  }, [live, queryClient]);
  return <LiveContext value={live}>{children}</LiveContext>;
}

export function useLive(): Live {
  const live = use(LiveContext);
  if (!live) throw new Error("useLive outside LiveProvider");
  return live;
}

export function useLiveState(): LiveState {
  const live = useLive();
  return useSyncExternalStore(live.watch, () => live.state);
}

// tabs on the shared connection, null when this tab holds its own
export function useLiveSharing(): number | null {
  const live = useLive();
  return useSyncExternalStore(live.watch, () => live.sharing);
}
