import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";

import {
  type AuditEntry,
  auditExport,
  type AuditFilters,
  type AuditOutcome,
  auditQuery,
} from "@/api/settings";
import { DEFAULT, Field, Pick } from "@/components/form";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fullTime, started } from "@/lib/format";
import { useDebounced } from "@/lib/use-debounced";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/settings/audit")({
  staticData: { crumb: "Audit" },
  component: AuditPage,
});

const ACTIONS = [
  { value: DEFAULT, label: "All actions" },
  { value: "session", label: "Sign-ins" },
  { value: "token", label: "Tokens" },
  { value: "config", label: "Config" },
  { value: "scenario", label: "Scenarios" },
  { value: "run", label: "Runs" },
  { value: "models", label: "Model checks" },
  { value: "audit", label: "Audit log" },
];

const OUTCOMES: { value: AuditOutcome | typeof DEFAULT; label: string }[] = [
  { value: DEFAULT, label: "Any outcome" },
  { value: "ok", label: "OK" },
  { value: "refused", label: "Refused" },
  { value: "error", label: "Error" },
];

// as high as the small selects beside them, as on the runs list
const INPUT = "h-7 text-sm md:text-sm";

interface Picked {
  action: string;
  token: string;
  outcome: string;
  since: string;
  until: string;
}

// A datetime-local value has no zone; the browser's own turns it into the time the API takes.
const instant = (local: string) => new Date(local).toISOString();

function filtersOf(p: Picked): AuditFilters {
  const out: AuditFilters = {};
  if (p.action) out.action = p.action;
  if (p.token) out.token = p.token;
  if (p.outcome) out.outcome = p.outcome as AuditOutcome;
  if (p.since) out.since = instant(p.since);
  if (p.until) out.until = instant(p.until);
  return out;
}

function AuditPage() {
  const operator = useOperator();
  if (!operator) return <p className="text-muted-foreground">Only operators see the audit log.</p>;
  return <Audit />;
}

function Audit() {
  const [picked, setPicked] = useState<Picked>({
    action: DEFAULT,
    token: "",
    outcome: DEFAULT,
    since: "",
    until: "",
  });
  const token = useDebounced(picked.token.trim(), 300);
  const filters = filtersOf({ ...picked, token });
  const audit = useInfiniteQuery({ ...auditQuery(filters), placeholderData: keepPreviousData });
  const pick = (key: keyof Picked) => (value: string) => setPicked((p) => ({ ...p, [key]: value }));
  const rows = audit.data?.pages.flatMap((p) => p.items) ?? [];

  let body: React.ReactNode;
  if (audit.isPending) {
    body = <p className="text-muted-foreground">Loading the audit log</p>;
  } else if (audit.isError) {
    body = (
      <p className="text-muted-foreground flex items-center">
        The audit log did not load.
        <Button variant="link" size="sm" onClick={() => void audit.refetch()}>
          Retry
        </Button>
      </p>
    );
  } else if (rows.length === 0) {
    body = <p className="text-muted-foreground rounded-sm border px-3 py-6">No rows match.</p>;
  } else {
    body = (
      <>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="text-muted-foreground text-left">
              <tr className="border-b">
                <th className="py-1 pr-4 font-normal">Time</th>
                <th className="py-1 pr-4 font-normal">Token</th>
                <th className="py-1 pr-4 font-normal">Action</th>
                <th className="py-1 pr-4 font-normal">Target</th>
                <th className="py-1 pr-4 font-normal">Outcome</th>
                <th className="py-1 pr-4 font-normal max-md:hidden">Address</th>
                <th className="py-1 font-normal max-lg:hidden">Detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <Row key={r.id} r={r} />
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-muted-foreground">
            {rows.length} {rows.length === 1 ? "row" : "rows"}
          </span>
          {audit.hasNextPage && (
            <Button
              size="sm"
              variant="outline"
              disabled={audit.isFetchingNextPage}
              onClick={() => void audit.fetchNextPage()}
            >
              Show more
            </Button>
          )}
        </div>
      </>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="font-medium">Audit log</h2>
        <span className="text-muted-foreground">
          Every change sent to this control plane and every failed sign-in, newest first
        </span>
        <div className="ml-auto self-center">
          <a
            href={auditExport(filters)}
            download
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            Export JSON Lines
          </a>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Field label="Action">
          <Pick label="Action" value={picked.action} items={ACTIONS} onChange={pick("action")} />
        </Field>
        <Field label="Token">
          <Input
            className={INPUT}
            value={picked.token}
            placeholder="Any token"
            onChange={(e) => pick("token")(e.target.value)}
          />
        </Field>
        <Field label="Outcome">
          <Pick
            label="Outcome"
            value={picked.outcome}
            items={OUTCOMES}
            onChange={pick("outcome")}
          />
        </Field>
        <Field label="From">
          <Input
            className={INPUT}
            type="datetime-local"
            value={picked.since}
            onChange={(e) => pick("since")(e.target.value)}
          />
        </Field>
        <Field label="To">
          <Input
            className={INPUT}
            type="datetime-local"
            value={picked.until}
            onChange={(e) => pick("until")(e.target.value)}
          />
        </Field>
      </div>
      {body}
      <p className="text-muted-foreground">
        Rows never hold token values, codes or file contents. <code>aims audit</code> reads the same
        log. <code>[audit] keep_days</code> sets how long rows stay.
      </p>
    </div>
  );
}

const OUTCOME_LABEL: Record<AuditOutcome, string> = {
  ok: "OK",
  refused: "Refused",
  error: "Error",
};

function Row({ r }: { r: AuditEntry }) {
  return (
    <tr className="border-b align-top last:border-0">
      <td className="py-1.5 pr-4 whitespace-nowrap tabular-nums" title={fullTime(r.at)}>
        {started(r.at)}
      </td>
      <td className="py-1.5 pr-4">
        {r.token ?? <span className="text-muted-foreground">none</span>}
        {r.auth && <span className="text-muted-foreground ml-2">{r.auth}</span>}
      </td>
      <td className="py-1.5 pr-4 font-mono">{r.action}</td>
      <td className="py-1.5 pr-4 break-all">{r.target}</td>
      <td className={cn("py-1.5 pr-4 whitespace-nowrap", r.outcome !== "ok" && "font-medium")}>
        {OUTCOME_LABEL[r.outcome]}
        {r.status != null && ` ${r.status}`}
      </td>
      <td className="py-1.5 pr-4 tabular-nums max-md:hidden">{r.ip}</td>
      <td className="text-muted-foreground py-1.5 break-words max-lg:hidden">{r.detail}</td>
    </tr>
  );
}
