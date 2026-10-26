import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/runs")({
  component: Runs,
});

function Runs() {
  return <h1 className="text-lg font-medium">Runs</h1>;
}
