import { createFileRoute, Outlet } from "@tanstack/react-router";

export const Route = createFileRoute("/scenarios")({
  staticData: { crumb: "Scenarios" },
  component: Outlet,
});
