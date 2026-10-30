import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/settings")({
  staticData: { crumb: "Settings" },
  component: Settings,
});

function Settings() {
  return <h1 className="text-lg font-medium">Settings</h1>;
}
