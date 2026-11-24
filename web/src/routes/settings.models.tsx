import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";

import {
  type AddInfo,
  type ModelPrices,
  modelPricesQuery,
  modelsQuery,
  type ProfileInfo,
} from "@/api/launch";
import { isProblem } from "@/api/session";
import { CONFIG_DEPENDENT, layerQuery, patchConfig } from "@/api/settings";
import { problemText } from "@/components/form";
import { CHECK_TEXT, free, useModelCheck } from "@/components/model-check";
import { AddLine, AddModel } from "@/components/settings/add-model";
import { figures, PriceKeys, rowNotes, Slashed } from "@/components/settings/price-keys";
import { Button } from "@/components/ui/button";
import { started } from "@/lib/format";
import { checkProblem, checkState } from "@/lib/launch";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

// `add` opens the dialog that adds a model.
interface ModelsSearch {
  add?: true;
}

export const Route = createFileRoute("/settings/models")({
  staticData: { crumb: "Models" },
  validateSearch: (raw: Record<string, unknown>): ModelsSearch =>
    raw["add"] === true ? { add: true } : {},
  component: Models,
});

// A check runs, or an add waits for its check to set what it asked for.
const running = (profiles: readonly ProfileInfo[]) =>
  profiles.some(
    (p) => p.check?.status === "running" || (p.add !== null && p.add?.outcome === null),
  );

// A profile that names a row of LiteLLM's price map and sets no price of its own.
const keyed = (p: ProfileInfo) => p.price === null && p.price_key !== null;

type Prices = UseQueryResult<ModelPrices>;

function Models() {
  const models = useQuery({
    ...modelsQuery,
    refetchInterval: (q) => (q.state.data && running(q.state.data.profiles) ? 3000 : false),
  });
  // the map is read only when a profile names a key in it
  const prices = useQuery({
    ...modelPricesQuery(),
    enabled: models.data?.profiles.some(keyed) ?? false,
  });
  const operator = useOperator();
  const { checking, problem, check } = useModelCheck();
  const adding = Route.useSearch().add === true;
  const navigate = Route.useNavigate();
  const setAdding = (open: boolean) =>
    void navigate({ search: open ? { add: true } : {}, replace: true });
  // a link that opens the dialog
  const addLink = (text: string) => (
    <button type="button" className="text-foreground underline" onClick={() => setAdding(true)}>
      {text}
    </button>
  );
  if (models.isPending) return <p className="text-muted-foreground">Loading profiles</p>;
  if (models.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Profiles did not load.
        <Button variant="link" size="sm" onClick={() => void models.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const { profiles } = models.data;
  const unset = Object.keys(models.data.roles).filter((r) => models.data.roles[r] === null);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="font-medium">Model profiles</h2>
        <span className="text-muted-foreground">
          {operator
            ? `Set in the config files; a check makes a few small ${profiles.every((p) => free(p, listedOf(p, prices))) ? "" : "paid "}calls per profile.`
            : "Set in the config files."}
        </span>
        {operator && (
          <div className="ml-auto flex gap-2">
            <AddModel open={adding} onOpenChange={setAdding} profiles={profiles} />
            {profiles.length > 0 && (
              <Button
                variant="outline"
                size="xs"
                disabled={checking !== null}
                onClick={() => void check(profiles.map((p) => p.name))}
              >
                Check all
              </Button>
            )}
          </div>
        )}
      </div>
      {unset.length > 0 && profiles.length > 0 && (
        <p className="text-muted-foreground">
          Unset roles: {unset.join(", ")}. Set {unset.length > 1 ? "them" : "it"} under [roles] in{" "}
          <ConfigLink />
          {operator ? (
            <>, or {addLink(unset.length > 1 ? "add a model with them" : "add a model with it")}.</>
          ) : (
            "."
          )}
        </p>
      )}
      {problem && <p className="text-flag">{problem}</p>}
      {profiles.length === 0 ? (
        <p className="text-muted-foreground">
          No profiles yet. Write one under [profiles] in <ConfigLink />
          {operator ? <>, or {addLink("add a model")}.</> : "."}
        </p>
      ) : (
        <table className="w-full">
          <thead className="text-muted-foreground text-left">
            <tr className="border-b">
              <th className="py-1 pr-4 font-normal">Profile</th>
              <th className="py-1 pr-4 font-normal max-sm:hidden">Model</th>
              <th className="py-1 pr-4 font-normal max-sm:hidden">Price per million tokens</th>
              <th className="py-1 pr-4 font-normal max-lg:hidden">Keys</th>
              <th className="py-1 pr-4 font-normal whitespace-nowrap">Last check</th>
              {operator && (
                <th className="py-1 font-normal">
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {profiles.map((p) => (
              <Row
                key={p.name}
                p={p}
                prices={prices}
                checking={checking?.includes(p.name) ?? false}
                busy={checking !== null}
                operator={operator}
                onCheck={() => void check([p.name])}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// How a profile's newest add stands, until it is done or something newer says more: a role
// given since, or a check after the add's own.
const addShown = (p: ProfileInfo, add: AddInfo) =>
  add.outcome === null ||
  (add.outcome !== "done" &&
    p.roles.length === 0 &&
    (p.check?.run_id ?? add.run_id) === add.run_id);

// Removes a profile whose add failed from the project's config file.
function Remove({ name }: { name: string }) {
  const qc = useQueryClient();
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const remove = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const file = await qc.fetchQuery(layerQuery("project"));
      await patchConfig("project", { unset: [["profiles", name]] }, file.exists ? file.etag : null);
      for (const queryKey of CONFIG_DEPENDENT) void qc.invalidateQueries({ queryKey });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="mt-0.5 flex flex-wrap items-center gap-2">
      {sure ? (
        <>
          <Button variant="outline" size="xs" disabled={busy} onClick={() => void remove()}>
            Remove {name} from aims.toml
          </Button>
          <Button variant="ghost" size="xs" disabled={busy} onClick={() => setSure(false)}>
            Keep
          </Button>
        </>
      ) : (
        <Button
          variant="link"
          size="xs"
          className="h-auto p-0"
          aria-label={`Remove ${name}`}
          onClick={() => setSure(true)}
        >
          Remove
        </Button>
      )}
      {problem && <span className="text-foreground">{problem}</span>}
    </span>
  );
}

function ConfigLink() {
  return (
    <Link to="/settings/config" search={{ view: "project" }} className="text-foreground underline">
      aims.toml
    </Link>
  );
}

function Row({
  p,
  prices,
  checking,
  busy,
  operator,
  onCheck,
}: {
  p: ProfileInfo;
  prices: Prices;
  checking: boolean;
  busy: boolean;
  operator: boolean;
  onCheck: () => void;
}) {
  const state = checking ? "running" : checkState(p.check);
  const bad = checking ? null : checkProblem(p.check);
  return (
    <tr className="border-b align-top last:border-0">
      <td className="py-1.5 pr-4">
        <span className="font-medium">{p.name}</span>
        {p.roles.length > 0 && (
          <span className="text-muted-foreground ml-2">{p.roles.join(", ")}</span>
        )}
        <span className="text-muted-foreground block sm:hidden">{p.model}</span>
        {p.add && addShown(p, p.add) && (
          <span className="text-muted-foreground block max-w-md">
            <AddLine info={p.add} />
            {operator && p.add.outcome === "failed" && <Remove name={p.name} />}
          </span>
        )}
        {/* the Price column is hidden on a narrow screen */}
        <div className="mt-1 sm:hidden">
          <Price p={p} prices={prices} />
        </div>
      </td>
      <td className="py-1.5 pr-4 max-sm:hidden">
        <span className="font-mono">{p.model}</span>
        <span className="text-muted-foreground block">
          {p.backend} · weights {p.weights} · reasoning {p.reasoning}
        </span>
      </td>
      <td className="py-1.5 pr-4 max-sm:hidden">
        <Price p={p} prices={prices} />
      </td>
      <td className="py-1.5 pr-4 max-lg:hidden">
        {p.entry === null ? (
          <span className="text-muted-foreground">none needed</span>
        ) : (
          <>
            <span className="font-mono break-all">{p.entry}</span>
            <span className={cn("block", p.runners.length > 0 && "text-muted-foreground")}>
              {p.runners.length > 0
                ? `allowed on ${p.runners.join(", ")}`
                : "no connected runner allows it"}
            </span>
          </>
        )}
      </td>
      <td className="py-1.5 pr-4">
        <span className={cn("sm:whitespace-nowrap", state === "fail" && "font-medium")}>
          {CHECK_TEXT[state]}
        </span>
        {p.check && !checking && (
          <Link
            to="/runs/$runId"
            params={{ runId: p.check.run_id }}
            className="text-muted-foreground hover:text-foreground block sm:whitespace-nowrap"
          >
            {started(p.check.started_at)}
          </Link>
        )}
        {bad && (
          // it wraps, or the table pushes the page wider than the screen
          <span className="text-muted-foreground block max-w-md">{bad}</span>
        )}
      </td>
      {operator && (
        <td className="py-1.5 text-right">
          <Button
            variant="ghost"
            size="xs"
            aria-label={`Check ${p.name}`}
            disabled={busy}
            onClick={onCheck}
          >
            Check
          </Button>
        </td>
      )}
    </tr>
  );
}

const listedOf = (p: ProfileInfo, prices: Prices) =>
  prices.data?.profiles.find((x) => x.name === p.name)?.listed ?? null;

// The price a profile has and where it comes from: its own, LiteLLM's list price for the key it
// names, or none, with rows of the map to name in its price_key.
function Price({ p, prices }: { p: ProfileInfo; prices: Prices }) {
  if (p.price) {
    return (
      <>
        <span className="lg:whitespace-nowrap">{figures(p.price)}</span>
        <span className="text-muted-foreground block">own price</span>
      </>
    );
  }
  if (p.price_key === null) {
    return (
      <>
        <span>none</span>
        <span className="text-muted-foreground block">
          estimates leave out its calls; caps count only reported costs
        </span>
        <PriceKeys profile={p} />
      </>
    );
  }
  const key = (
    <span className="text-muted-foreground block wrap-anywhere">
      LiteLLM{" "}
      <span className="font-mono">
        <Slashed text={p.price_key} />
      </span>
    </span>
  );
  const l = listedOf(p, prices);
  if (l) {
    const notes = [`fetched ${l.checked.slice(0, 10)}`, ...rowNotes(l)];
    if (l.problem) notes.push("could not be read again");
    return (
      <>
        <span className="lg:whitespace-nowrap">
          {figures({ input: l.input_usd, cached: l.cached_usd, output: l.output_usd })}
        </span>
        {key}
        <span className="text-muted-foreground block">{notes.join(" · ")}</span>
      </>
    );
  }
  if (prices.isPending) {
    return (
      <>
        {key}
        <span className="text-muted-foreground block">Reading the price map</span>
      </>
    );
  }
  const why = prices.isError
    ? "The price map did not load."
    : (prices.data?.profiles.find((x) => x.name === p.name)?.unlisted ?? "No list price.");
  return (
    <>
      <span>none</span>
      {key}
      <span className="text-muted-foreground block">{why}</span>
      <PriceKeys profile={p} />
    </>
  );
}
