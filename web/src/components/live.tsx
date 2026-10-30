import {
  createContext,
  type ReactNode,
  use,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import { Live, type LiveState } from "@/api/live";

const LiveContext = createContext<Live | null>(null);

export function LiveProvider({
  onClosed,
  children,
}: {
  onClosed: () => void;
  children: ReactNode;
}) {
  const [live] = useState(() => new Live({ runs: true }, onClosed));
  useEffect(() => {
    live.start();
    return () => live.stop();
  }, [live]);
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
