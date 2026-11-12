import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useBlocker } from "@tanstack/react-router";
import { lazy, Suspense } from "react";

import { isProblem } from "@/api/session";
import { configQuery } from "@/api/settings";
import { problemText } from "@/components/form";
import type { View } from "@/components/settings/config-pane";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { hasUnsaved, mayDiscard, setUnsaved } from "@/lib/unsaved";
import { useOperator } from "@/lib/use-operator";

// The editor is heavy; the page around it shows first.
const ConfigPane = lazy(() => import("@/components/settings/config-pane"));

interface ConfigSearch {
  view?: View;
}

const VIEWS: Record<View, string> = {
  user: "This machine",
  project: "This project",
  merged: "In use",
};

const isView = (v: unknown): v is View => v === "user" || v === "project" || v === "merged";

// The layer this page shows at a place in the app, null elsewhere.
function viewAt(at: { routeId: string; search: unknown }, fallback: View): View | null {
  if (at.routeId !== Route.id) return null;
  const view = (at.search as Record<string, unknown>)["view"];
  return isView(view) ? view : fallback;
}

export const Route = createFileRoute("/settings/config")({
  staticData: { crumb: "Config" },
  validateSearch: (raw: Record<string, unknown>): ConfigSearch =>
    isView(raw["view"]) ? { view: raw["view"] } : {},
  component: ConfigPage,
});

function ConfigPage() {
  const operator = useOperator();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const config = useQuery(configQuery);
  const layers = config.data?.layers ?? [];
  const project = layers.find((l) => l.layer === "project");
  const fallback: View = project?.exists ? "project" : "user";
  const view = search.view ?? fallback;
  const file = view === "merged" ? undefined : layers.find((l) => l.layer === view);
  // Another layer, a link or Back drops the draft, so it asks first. Closing the browser tab
  // asks too.
  useBlocker({
    shouldBlockFn: ({ current, next }) =>
      viewAt(next, fallback) !== viewAt(current, fallback) && !mayDiscard(),
    enableBeforeUnload: hasUnsaved,
  });

  if (config.isPending) return <p className="text-muted-foreground">Loading config</p>;
  if (config.isError) {
    return (
      <p className="text-muted-foreground flex flex-wrap items-center">
        The config did not load
        {isProblem(config.error) ? `: ${problemText(config.error)}` : "."}
        <Button variant="link" size="sm" onClick={() => void config.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const { error } = config.data;

  const pick = (next: View) => {
    if (next !== view) void navigate({ search: { view: next }, replace: true });
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <ToggleGroup
            aria-label="Layer"
            value={[view]}
            onValueChange={(v: string[]) => isView(v[0]) && pick(v[0])}
            variant="outline"
            size="sm"
          >
            {(Object.keys(VIEWS) as View[]).map((v) => (
              <ToggleGroupItem key={v} value={v}>
                {VIEWS[v]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          {file && (
            <p className="text-muted-foreground min-w-0 break-all">
              {!operator
                ? file.exists
                  ? ""
                  : "No file yet at "
                : file.exists
                  ? "Saves to "
                  : "Saving creates "}
              <span className="font-mono">{file.path}</span>
            </p>
          )}
        </div>
        <p className="text-muted-foreground">
          {view === "merged" ? (
            <>
              Every setting in use, each with where it comes from. The texts are under{" "}
              <Link to="/settings/prompts" className="text-foreground underline">
                Prompts
              </Link>
              .
            </>
          ) : (
            <>
              This project's file wins over this machine's; a scenario can override both for its
              agents.{!operator && " Read only: operators edit these files."}
            </>
          )}
        </p>
      </div>
      {error && (
        <p role="alert" className="rounded-sm border px-3 py-2">
          <span className="font-medium">The config does not load: </span>
          {error}
        </p>
      )}
      <Suspense fallback={<p className="text-muted-foreground">Opening the editor</p>}>
        <ConfigPane
          key={view}
          view={view}
          file={file}
          editable={operator}
          onDirty={(d) => setUnsaved(d ? (file?.path ?? "the file") : null)}
        />
      </Suspense>
    </div>
  );
}
