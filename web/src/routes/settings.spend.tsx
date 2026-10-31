import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useEffect } from "react";

import { type ProviderCredit, spendQuery } from "@/api/settings";
import { useLive } from "@/components/live";
import { Button } from "@/components/ui/button";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/settings/spend")({
  staticData: { crumb: "Spend" },
  component: SpendPage,
});

// Under a tenth of the credit left is worth a look before the next run.
const LOW = 0.1;

function SpendPage() {
  const qc = useQueryClient();
  const live = useLive();
  const spend = useQuery(spendQuery);
  useEffect(
    () =>
      live.on("run", ({ run }) => {
        if (run.state === "done") void qc.invalidateQueries({ queryKey: spendQuery.queryKey });
      }),
    [live, qc],
  );
  if (spend.isPending) return <p className="text-muted-foreground">Loading spend</p>;
  if (spend.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Spend did not load.
        <Button variant="link" size="sm" onClick={() => void spend.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const { providers } = spend.data;
  const total = providers.reduce((sum, p) => sum + p.spent_usd, 0);
  return (
    <div className="flex max-w-3xl flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="font-medium">Spend</h2>
        <span className="text-muted-foreground">
          {usd(total)} paid to providers in the runs this control plane holds
        </span>
      </div>
      {providers.length === 0 ? (
        <p className="text-muted-foreground rounded-sm border px-3 py-6">
          No model calls on record.
        </p>
      ) : (
        <table className="w-full">
          <thead className="text-muted-foreground text-left">
            <tr className="border-b">
              <th className="py-1 pr-4 font-normal">Provider</th>
              <th className="py-1 pr-4 text-right font-normal">Spent</th>
              <th className="py-1 pr-4 text-right font-normal">Credit</th>
              <th className="py-1 pr-4 text-right font-normal">Left</th>
              <th className="w-1/3 py-1 font-normal max-sm:hidden">
                <span className="sr-only">Used</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <Row key={p.provider} p={p} />
            ))}
          </tbody>
        </table>
      )}
      <p className="text-muted-foreground">
        Credits come from <code>[credits]</code> in the config files, such as{" "}
        <code>meta = 150</code>. <code>aims spend</code> prints the same.
      </p>
    </div>
  );
}

function Row({ p }: { p: ProviderCredit }) {
  const credit = p.credit_usd;
  const left = p.left_usd;
  const used = credit != null && credit > 0 ? Math.min(1, p.spent_usd / credit) : null;
  const low = credit != null && left != null && (left <= 0 || left < credit * LOW);
  return (
    <tr className="border-b last:border-0">
      <td className="py-1.5 pr-4 font-medium">{p.provider}</td>
      <td className="py-1.5 pr-4 text-right tabular-nums">{usd(p.spent_usd)}</td>
      <td className="py-1.5 pr-4 text-right tabular-nums">
        {credit == null ? <span className="text-muted-foreground">not set</span> : usd(credit)}
      </td>
      <td className={cn("py-1.5 pr-4 text-right tabular-nums", low && "text-flag font-medium")}>
        {left == null ? "" : left < 0 ? `over by ${usd(-left)}` : usd(left)}
      </td>
      <td className="py-1.5 max-sm:hidden">
        {used !== null && (
          <span
            role="meter"
            aria-label={`${p.provider} credit used`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(used * 100)}
            className="bg-muted block h-1.5 w-full overflow-hidden rounded-full"
          >
            <span
              className={cn("block h-full", low ? "bg-flag" : "bg-foreground")}
              style={{ width: `${used * 100}%` }}
            />
          </span>
        )}
      </td>
    </tr>
  );
}
