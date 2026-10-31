import { createFileRoute, Link, Outlet } from "@tanstack/react-router";

import { useOperator } from "@/lib/use-operator";

export const Route = createFileRoute("/settings")({
  staticData: { crumb: "Settings" },
  component: Settings,
});

const pages = [
  { to: "/settings/config", label: "Config", operator: false },
  { to: "/settings/prompts", label: "Prompts", operator: false },
  { to: "/settings/models", label: "Models", operator: false },
  { to: "/settings/runners", label: "Runners", operator: false },
  { to: "/settings/tokens", label: "Tokens", operator: true },
  { to: "/settings/spend", label: "Spend", operator: false },
] as const;

function Settings() {
  const operator = useOperator();
  return (
    <div className="flex max-w-6xl gap-8 max-md:flex-col max-md:gap-4">
      <h1 className="sr-only">Settings</h1>
      <nav
        aria-label="Settings"
        className="flex w-36 shrink-0 flex-col gap-0.5 max-md:w-auto max-md:flex-row max-md:gap-4 max-md:border-b"
      >
        {pages
          .filter((p) => operator || !p.operator)
          .map((p) => (
            <Link
              key={p.to}
              to={p.to}
              className="text-muted-foreground hover:text-foreground data-[status=active]:text-foreground data-[status=active]:border-foreground border-l-2 border-transparent py-0.5 pl-2 max-md:-mb-px max-md:border-b-2 max-md:border-l-0 max-md:pb-1.5 max-md:pl-0"
            >
              {p.label}
            </Link>
          ))}
      </nav>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
