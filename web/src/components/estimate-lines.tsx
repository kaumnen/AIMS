import type { UseQueryResult } from "@tanstack/react-query";

import { isProblem } from "@/api/session";
import { problemText } from "@/components/form";
import { cn } from "@/lib/utils";

// A cost estimate in words, as the CLI prints it: its warnings are among the lines, so they
// show once. The total and the warnings stand out.
export function EstimateLines({ lines, stale = false }: { lines: string[]; stale?: boolean }) {
  return (
    <ul className={cn("flex flex-col gap-0.5", stale && "opacity-60")}>
      {lines.map((line) => (
        <li
          key={line}
          className={cn((line.startsWith("Total") || line.startsWith("warning:")) && "font-medium")}
        >
          {line}
        </li>
      ))}
    </ul>
  );
}

// What the work a form starts costs, its lines from `skip` on. Below them is the yes a cost
// past the limit needs before it starts, when `why` says why.
export function Cost({
  estimate,
  skip = 0,
  why,
  yes,
  onYes,
}: {
  estimate: UseQueryResult<{ lines: string[] }>;
  skip?: number;
  why: string | null;
  yes: boolean;
  onYes: (yes: boolean) => void;
}) {
  return (
    <section aria-label="Cost estimate" aria-live="polite" className="flex flex-col gap-1">
      <h3 className="font-medium">Cost</h3>
      {estimate.isPending ? (
        <p className="text-muted-foreground">Estimating</p>
      ) : estimate.isError ? (
        <p className="text-muted-foreground">
          No estimate: {isProblem(estimate.error) ? problemText(estimate.error) : "it failed."}
        </p>
      ) : (
        <EstimateLines lines={estimate.data.lines.slice(skip)} stale={estimate.isPlaceholderData} />
      )}
      {why && (
        <label className="flex items-start gap-2 font-medium">
          <input
            type="checkbox"
            checked={yes}
            onChange={(e) => onYes(e.target.checked)}
            className="accent-foreground mt-0.5"
          />
          {why} Start anyway.
        </label>
      )}
    </section>
  );
}
