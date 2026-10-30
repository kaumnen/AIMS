import { createFileRoute } from "@tanstack/react-router";

import { shortId } from "@/lib/format";

export const Route = createFileRoute("/runs/$runId")({
  staticData: { crumb: "Run" },
  component: Run,
});

function Run() {
  const { runId } = Route.useParams();
  return <h1 className="text-lg font-medium">Run {shortId(runId)}</h1>;
}
