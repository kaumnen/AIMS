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
// how many times the stream was opened again after the browser gave up on it
const EpochContext = createContext(0);

// the wait before the stream is opened again, doubled each time up to the last
export const REOPEN_MS = 1000;
export const REOPEN_MAX_MS = 30_000;

// Opens the page's stream again once the browser gave up on it, when the session still stands:
// the server or a proxy before it was away, and the browser tries no more on its own.
class Kept {
  readonly live = new Live({ runs: true }, () => this.#closed());
  #stands: () => Promise<boolean> = async () => false;
  #reopened: () => void = () => undefined;
  #tries = 0;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #asking = false;
  #on = false;
  #unwatch: (() => void) | null = null;

  // `stands` says whether the session still does; `reopened` runs after each new start
  use(stands: () => Promise<boolean>, reopened: () => void): void {
    this.#stands = stands;
    this.#reopened = reopened;
  }

  start(): void {
    this.#on = true;
    this.#unwatch = this.live.watch(() => {
      if (this.live.state === "open") this.#tries = 0;
    });
    this.live.start();
  }

  stop(): void {
    this.#on = false;
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    this.#unwatch?.();
    this.live.stop();
  }

  #closed(): void {
    if (this.#asking || this.#timer !== null) return;
    this.#asking = true;
    void this.#stands()
      .catch(() => false)
      .then((stands) => {
        this.#asking = false;
        if (!stands || !this.#on) return;
        const wait = Math.min(REOPEN_MAX_MS, REOPEN_MS * 2 ** this.#tries++);
        this.#timer = setTimeout(() => {
          this.#timer = null;
          this.live.start();
          this.#reopened();
        }, wait);
      });
  }
}

// `onClosed` runs when the browser gave up on the stream and says whether the session still
// stands.
export function LiveProvider({
  onClosed,
  children,
}: {
  onClosed: () => Promise<boolean>;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [kept] = useState(() => new Kept());
  const { live } = kept;
  // a run page's own stream closed with the page's: it opens again when this changes
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    kept.use(onClosed, () => {
      // as after an outage the page showed: no stream kept the runs current meanwhile, so each
      // read of them goes again when it next shows
      void queryClient.invalidateQueries({ queryKey: ["runs"], refetchType: "none" });
      setEpoch((e) => e + 1);
    });
  });
  useEffect(() => {
    const unsync = syncRuns(live, queryClient);
    kept.start();
    return () => {
      kept.stop();
      unsync();
    };
  }, [kept, live, queryClient]);
  return (
    <LiveContext value={live}>
      <EpochContext value={epoch}>{children}</EpochContext>
    </LiveContext>
  );
}

export function useLive(): Live {
  const live = use(LiveContext);
  if (!live) throw new Error("useLive outside LiveProvider");
  return live;
}

// Changes each time the page's stream was opened again after a close; a part with a stream of
// its own opens that one again then.
export function useLiveEpoch(): number {
  return use(EpochContext);
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
