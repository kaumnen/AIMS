import { createFileRoute, Outlet } from "@tanstack/react-router";

export const Route = createFileRoute("/runs")({
  staticData: { crumb: "Runs" },
  component: Outlet,
});
