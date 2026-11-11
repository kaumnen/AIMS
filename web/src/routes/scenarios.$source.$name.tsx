import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useBlocker } from "@tanstack/react-router";
import { lazy, Suspense, useRef } from "react";

import { SCENARIO_FILE } from "@/api/scenario-files";
import { scenarioQuery, sourceLabel, sourcesQuery } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { NewRun } from "@/components/runs/new-run";
import { DeleteScenario } from "@/components/scenarios/delete-scenario";
import { NewScenario } from "@/components/scenarios/new-scenario";
import { Overview } from "@/components/scenarios/overview";
import { PromptsTab } from "@/components/scenarios/prompts";
import { SelfTestButton } from "@/components/scenarios/selftest-button";
import { buttonVariants } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useOperator } from "@/lib/use-operator";

// The editor is the heaviest part of the app; it loads when the Files tab opens.
const FilesTab = lazy(() => import("@/components/scenarios/files"));

type Tab = "files" | "prompts";

interface ScenarioSearch {
  tab?: Tab;
  file?: string;
}

function without(search: ScenarioSearch, key: keyof ScenarioSearch): ScenarioSearch {
  const out = { ...search };
  delete out[key];
  return out;
}

const readSearch = (raw: Record<string, unknown>): ScenarioSearch => ({
  ...(raw["tab"] === "files" || raw["tab"] === "prompts" ? { tab: raw["tab"] } : {}),
  ...(typeof raw["file"] === "string" && raw["file"] ? { file: raw["file"] } : {}),
});

interface Place {
  routeId: string;
  pathname: string;
  search: unknown;
}

const searchAt = (at: Place) => readSearch(at.search as Record<string, unknown>);
const fileIn = (at: Place) => searchAt(at).file ?? SCENARIO_FILE;

// The file the Files tab shows at a place in the app, with its page; null where it shows none.
function fileAt(at: Place): string | null {
  if (at.routeId !== Route.id || searchAt(at).tab !== "files") return null;
  return `${at.pathname}#${fileIn(at)}`;
}

export const Route = createFileRoute("/scenarios/$source/$name")({
  staticData: { crumb: (p) => p["name"] ?? "" },
  validateSearch: readSearch,
  // A new scenario mounts a new page, so no draft or dialog of the last one carries over.
  remountDeps: ({ params }) => params,
  loader: ({ context, params }) => {
    void context.queryClient.prefetchQuery(scenarioQuery(params.source, params.name));
  },
  component: ScenarioPage,
});

function ScenarioPage() {
  const { source, name } = Route.useParams();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const operator = useOperator();
  const detail = useQuery(scenarioQuery(source, name));
  const sources = useQuery(sourcesQuery);
  // whether the open file has changes not saved
  const dirty = useRef(false);
  // Anything that leaves the file, such as a tab, a link, another file or Back, drops its draft,
  // so it asks first. Closing the browser tab asks too.
  useBlocker({
    shouldBlockFn: ({ current, next }) =>
      dirty.current &&
      fileAt(next) !== fileAt(current) &&
      !window.confirm(`Discard your changes to ${fileIn(current)}?`),
    enableBeforeUnload: () => dirty.current,
  });
  if (detail.isPending) return <p className="text-muted-foreground">Loading scenario</p>;
  if (detail.isError) {
    const missing = isProblem(detail.error) && detail.error.status === 404;
    return (
      <p className="text-muted-foreground flex items-center">
        {missing ? `No scenario ${name} here.` : "The scenario did not load."}
        <Link to="/scenarios" className={buttonVariants({ variant: "link", size: "sm" })}>
          Back to scenarios
        </Link>
      </p>
    );
  }
  const d = detail.data;
  const tab = operator && search.tab ? search.tab : "overview";
  return (
    <div className="flex max-w-7xl flex-col gap-3">
      <header className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div className="min-w-0">
          <h1 className="flex items-baseline gap-2 text-lg font-medium">
            <span className="truncate">{d.name}</span>
            {d.title && d.title !== d.name && (
              <span className="text-muted-foreground text-sm">{d.title}</span>
            )}
          </h1>
          <p className="text-muted-foreground">
            {sourceLabel(sources.data, d.source)}
            {!d.writable && ", read only: duplicate it to edit"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <DeleteScenario source={d.source} name={d.name} writable={d.writable} />
          <Link
            to="/runs"
            search={{ scenario: d.title ?? d.name }}
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            Runs
          </Link>
          {operator && d.selftest && <SelfTestButton source={d.source} name={d.name} />}
          <NewScenario from={{ source: d.source, name: d.name }} />
          {d.errors.length === 0 && (
            <NewRun scenario={{ source: d.source, name: d.name }} label="Run" />
          )}
        </div>
      </header>
      <Tabs
        value={tab}
        onValueChange={(v) =>
          void navigate({
            search: (prev) =>
              v === "files" || v === "prompts" ? { ...prev, tab: v } : without(prev, "tab"),
            replace: true,
          })
        }
      >
        {operator && (
          <TabsList variant="line" className="border-b">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="files">Files</TabsTrigger>
            <TabsTrigger value="prompts">Prompts</TabsTrigger>
          </TabsList>
        )}
        <TabsContent value="overview" className="pt-3">
          <Overview detail={d} />
        </TabsContent>
        {operator && (
          <TabsContent value="prompts" className="pt-3">
            {d.errors.length > 0 ? (
              <p className="text-muted-foreground">Fix scenario.yaml first: it does not load.</p>
            ) : (
              <PromptsTab detail={d} />
            )}
          </TabsContent>
        )}
        {operator && (
          <TabsContent value="files" className="pt-3">
            <Suspense fallback={<p className="text-muted-foreground">Loading the editor</p>}>
              <FilesTab
                detail={d}
                file={search.file ?? SCENARIO_FILE}
                onFile={(file, how) =>
                  void navigate({
                    search: (prev) =>
                      file === SCENARIO_FILE ? without(prev, "file") : { ...prev, file },
                    ignoreBlocker: how?.discard ?? false,
                  })
                }
                onDirty={(has) => {
                  dirty.current = has;
                }}
              />
            </Suspense>
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}
