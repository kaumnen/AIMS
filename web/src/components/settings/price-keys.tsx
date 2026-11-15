import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";

import { modelPricesQuery, type PriceRow, type ProfileInfo } from "@/api/launch";
import { Field } from "@/components/form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { rate } from "@/lib/format";

interface Figures {
  input: number;
  cached: number;
  output: number;
}

// A price per million tokens, as one line.
export const figures = ({ input, cached, output }: Figures) =>
  `${rate(input)} in · ${rate(cached)} cached · ${rate(output)} out`;

// What a row of the map leaves out of its price, as short notes.
export function rowNotes(row: { cached_listed: boolean; tier_from: number | null }): string[] {
  const notes: string[] = [];
  if (!row.cached_listed) notes.push("no cached price listed, so cached input counts as input");
  if (row.tier_from !== null) notes.push(`more past ${row.tier_from.toLocaleString()} tokens`);
  return notes;
}

// A key or model id that a narrow column wraps after a slash, and within a part only when the
// part alone does not fit.
export function Slashed({ text }: { text: string }) {
  return text.split("/").map((part, i) => (
    // oxlint-disable-next-line no-array-index-key
    <Fragment key={i}>
      {i > 0 && (
        <>
          /<wbr />
        </>
      )}
      {part}
    </Fragment>
  ));
}

// The line a config file takes under the profile's table. JSON quotes printable ASCII as TOML
// does.
const line = (key: string) => `price_key = ${JSON.stringify(key)}`;

// Rows of LiteLLM's price map that may price a profile with no price, to name one in its
// price_key. The server reads the map; nothing is saved.
export function PriceKeys({ profile }: { profile: ProfileInfo }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={<Button variant="link" size="xs" className="h-auto p-0" />}
        aria-label={`Find a price key for ${profile.name}`}
      >
        Find a price key
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        {open && <Keys profile={profile} />}
      </DialogContent>
    </Dialog>
  );
}

function Keys({ profile }: { profile: ProfileInfo }) {
  const prices = useQuery(modelPricesQuery());
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const found = useQuery({ ...modelPricesQuery(search), enabled: search !== "" });
  const last = profile.model.split("/").pop() ?? profile.model;
  const data = found.data ?? prices.data;

  let rows: React.ReactNode = null;
  if (prices.isPending) rows = <p className="text-muted-foreground">Reading the price map</p>;
  else if (prices.isError) rows = <p className="font-medium">The price map did not load.</p>;
  else if (prices.data.checked !== null) {
    const mine = prices.data.profiles.find((p) => p.name === profile.name);
    rows = mine?.candidates.length ? (
      <Rows rows={mine.candidates} />
    ) : (
      <p className="text-muted-foreground">No row&apos;s key ends in {last}. Search instead.</p>
    );
  }

  let results: React.ReactNode = null;
  if (search === "") results = null;
  else if (found.isPending) results = <p className="text-muted-foreground">Searching</p>;
  else if (found.isError) results = <p className="font-medium">The search failed.</p>;
  else if (found.data.found.length > 0) results = <Rows rows={found.data.found} />;
  else if (found.data.checked !== null) {
    results = <p className="text-muted-foreground">No row&apos;s key holds {search}.</p>;
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <DialogHeader>
        <DialogTitle>Price key for {profile.name}</DialogTitle>
        <DialogDescription>
          Without a price, estimates leave out a profile&apos;s calls and caps count only the costs
          its provider reports. Name a row of LiteLLM&apos;s price map in its price_key, under
          [profiles.{profile.name}] in a config file. Rows of one model differ by provider and
          region, so pick the provider that serves it.
        </DialogDescription>
      </DialogHeader>
      <section aria-live="polite" className="flex flex-col gap-1">
        <h3 className="font-medium">
          Rows for{" "}
          <span className="font-mono wrap-anywhere">
            <Slashed text={profile.model} />
          </span>
        </h3>
        {rows}
      </section>
      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(text.trim());
        }}
      >
        <div className="flex items-end gap-2">
          <Field label="Search keys" className="min-w-0 flex-1">
            <Input
              value={text}
              maxLength={200}
              placeholder={last}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <Button type="submit" size="sm" variant="outline" disabled={text.trim() === ""}>
            Search
          </Button>
        </div>
        <div aria-live="polite">{results}</div>
      </form>
      {data && <Source checked={data.checked} problem={data.problem} />}
    </div>
  );
}

function Source({ checked, problem }: { checked: string | null; problem: string | null }) {
  if (checked === null) {
    return (
      <p className="font-medium">
        No copy of LiteLLM&apos;s price map is at hand{problem ? `: ${problem}` : "."}
      </p>
    );
  }
  return (
    <p className="text-muted-foreground">
      From LiteLLM&apos;s price map, fetched {checked.slice(0, 10)}, in USD per million tokens.
      {problem && ` It could not be read again: ${problem}`}
    </p>
  );
}

function Rows({ rows }: { rows: readonly PriceRow[] }) {
  return (
    <ul className="divide-y">
      {rows.map((r) => (
        <li key={r.key} className="flex items-start gap-2 py-1.5">
          <div className="min-w-0 flex-1">
            <span className="font-mono wrap-anywhere select-all">
              <Slashed text={r.key} />
            </span>
            <span className="text-muted-foreground block">
              {[
                figures({ input: r.input_usd, cached: r.cached_usd, output: r.output_usd }),
                ...rowNotes(r),
              ].join(" · ")}
            </span>
          </div>
          <Copy text={line(r.key)} label={`Copy the price_key line for ${r.key}`} />
        </li>
      ))}
    </ul>
  );
}

function Copy({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  // missing outside a secure context, such as plain http on a LAN address
  const clipboard = (navigator as { clipboard?: Clipboard }).clipboard;
  if (!clipboard) return null;
  const copy = async () => {
    try {
      await clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Button size="xs" variant="outline" aria-label={label} onClick={() => void copy()}>
      {copied === null ? "Copy" : copied ? "Copied" : "Copy failed"}
    </Button>
  );
}
