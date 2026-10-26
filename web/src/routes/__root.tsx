import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Link, Outlet } from "@tanstack/react-router";

const nav = [
  { to: "/runs", label: "Runs" },
  { to: "/scenarios", label: "Scenarios" },
] as const;

const activeLink = { className: "text-neutral-100" };

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: Root,
});

function Root() {
  return (
    <div className="min-h-dvh bg-neutral-950 text-neutral-100">
      <header className="flex items-center gap-6 border-b border-neutral-800 px-6 py-3">
        <span className="font-semibold tracking-wide">AIMS</span>
        <nav className="flex gap-4 text-sm">
          {nav.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="text-neutral-400 hover:text-neutral-100"
              activeProps={activeLink}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="p-6">
        <Outlet />
      </main>
    </div>
  );
}
