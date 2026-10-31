import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { mergeRunners, type RunnerInfo, runnersQuery } from "@/api/settings";
import { up, useRunners } from "@/components/health";
import { Button } from "@/components/ui/button";
import { duration } from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/settings/runners")({
  staticData: { crumb: "Runners" },
  component: Runners,
});

function Runners() {
  const listed = useQuery(runnersQuery);
  const live = useRunners();
  const now = useNow(true, 30_000);
  if (listed.isPending) return <p className="text-muted-foreground">Loading runners</p>;
  if (listed.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Runners did not load.
        <Button variant="link" size="sm" onClick={() => void listed.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const liveNames = new Set(live.map((r) => r.name));
  const runners = mergeRunners(listed.data, live);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="font-medium">Runners</h2>
        <span className="text-muted-foreground">
          {runners.filter(up).length} of {runners.length} online
        </span>
      </div>
      {runners.length === 0 ? (
        <p className="text-muted-foreground rounded-sm border px-3 py-6">
          No runner has connected. Start one on this machine with <code>aims runner</code>.
        </p>
      ) : (
        <table className="w-full">
          <thead className="text-muted-foreground text-left">
            <tr className="border-b">
              <th className="py-1 pr-4 font-normal">Runner</th>
              <th className="py-1 pr-4 font-normal">Status</th>
              <th className="py-1 pr-4 font-normal">Tracer</th>
              <th className="py-1 pr-4 font-normal max-md:hidden">Takes</th>
              <th className="py-1 pr-4 font-normal max-lg:hidden">Keys</th>
            </tr>
          </thead>
          <tbody>
            {runners.map((r) => (
              <Row
                key={r.name}
                r={r}
                seenAgo={
                  liveNames.has(r.name)
                    ? null
                    : Math.max(0, now - listed.dataUpdatedAt) + (r.seen_s ?? 0) * 1000
                }
              />
            ))}
          </tbody>
        </table>
      )}
      <p className="text-muted-foreground">
        Another machine connects with{" "}
        <code className="text-foreground">
          aims runner --server {location.origin} --token-file &lt;file&gt;
        </code>
        , the file holding a token with the runner role.
      </p>
    </div>
  );
}

// `seenAgo` is known only for a runner the list holds and the live stream never sent.
function status(r: RunnerInfo, seenAgo: number | null): string {
  if (up(r)) {
    const busy = r.running.length;
    return `Online · ${r.free} of ${r.slots} slots free${busy ? ` · ${busy} running` : ""}`;
  }
  if (r.connected) return "Not reporting";
  if (seenAgo !== null && r.seen_s != null) return `Offline · seen ${duration(seenAgo)} ago`;
  return "Offline";
}

function Row({ r, seenAgo }: { r: RunnerInfo; seenAgo: number | null }) {
  const online = up(r);
  return (
    <tr className="border-b align-top last:border-0">
      <td className="py-1.5 pr-4">
        <span className="font-medium">{r.name}</span>
        {r.local && <span className="text-muted-foreground ml-2">this machine</span>}
        {(r.version || r.arch) && (
          <span className="text-muted-foreground block">
            {[r.version, r.arch].filter(Boolean).join(" · ")}
          </span>
        )}
      </td>
      <td className="py-1.5 pr-4">
        <span className="flex items-center gap-1.5">
          <span
            aria-hidden
            className={cn(
              "border-foreground size-2 shrink-0 rounded-full border",
              online && "bg-foreground",
            )}
          />
          <span className={cn(!online && "text-muted-foreground")}>{status(r, seenAgo)}</span>
        </span>
      </td>
      <td className="py-1.5 pr-4">
        {r.tracer == null ? (
          <span className="text-muted-foreground">not reported</span>
        ) : (
          <>
            <span className={cn(!r.tracer.alive && online && "font-medium")}>
              {r.tracer.alive ? "on" : "off"}
            </span>
            {r.tracer.restarts > 0 && (
              <span className="text-muted-foreground ml-2">
                {r.tracer.restarts} {r.tracer.restarts === 1 ? "restart" : "restarts"}
              </span>
            )}
            {r.tracer.detail && (
              <span
                className="text-muted-foreground block max-w-xs truncate"
                title={r.tracer.detail}
              >
                {r.tracer.detail}
              </span>
            )}
          </>
        )}
      </td>
      <td className="py-1.5 pr-4 max-md:hidden">{r.kinds.join(", ")}</td>
      <td className="py-1.5 pr-4 max-lg:hidden">
        {r.envs.length > 0 ? (
          <span className="font-mono">{r.envs.join(", ")}</span>
        ) : (
          <span className="text-muted-foreground">none</span>
        )}
      </td>
    </tr>
  );
}
