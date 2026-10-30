import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/scenarios")({
  staticData: { crumb: "Scenarios" },
  component: Scenarios,
});

function Scenarios() {
  return <h1 className="text-lg font-medium">Scenarios</h1>;
}
