import type { Schemas } from "@/api/client";

import { usd } from "./format";

export type ListedCost = Schemas["ListedCost"];
export type RecordedPrice = Schemas["RecordedPrice"];

// The price map list prices come from, as the server names it.
export const LIST_SOURCE = "LiteLLM";

// The day a copy of the map was confirmed current, in UTC as the Markdown report writes it.
export const day = (iso: string) => new Date(iso).toISOString().slice(0, 10);

// The run header's line under its cost: how much of it is at a list price, and from where.
export function listedNote(total: number, listed: readonly ListedCost[]): string {
  const head = `${usd(total)} of it at ${LIST_SOURCE} list price`;
  if (listed.length === 0) return `${head}.`;
  const parts = listed.map((c) => {
    if (c.prices.length === 0) return c.profile;
    const rows = c.prices.map((p) =>
      c.prices.length === 1
        ? `row ${p.key}, copy checked ${day(p.checked)}`
        : `row ${p.key} (copy checked ${day(p.checked)})`,
    );
    return `${c.profile}, ${rows.join(" or ")}`;
  });
  return `${head}: ${parts.join("; ")}.`;
}
