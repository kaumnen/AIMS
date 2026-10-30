import { Link } from "@tanstack/react-router";

import type { Schemas } from "@/api/client";

import { Health } from "./health";
import { UserMenu } from "./user-menu";

const nav = [
  { to: "/runs", label: "Runs" },
  { to: "/scenarios", label: "Scenarios" },
  { to: "/settings", label: "Settings" },
] as const;

export function Header({ me }: { me: Schemas["Whoami"] }) {
  return (
    <header className="flex h-10 shrink-0 items-stretch gap-6 border-b px-4 whitespace-nowrap max-sm:gap-4">
      <Link to="/runs" className="flex items-center font-semibold tracking-tight">
        AIMS
      </Link>
      <nav className="flex items-stretch gap-5 max-sm:gap-3">
        {nav.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="text-muted-foreground hover:text-foreground data-[status=active]:border-foreground data-[status=active]:text-foreground -mb-px flex items-center border-b-2 border-transparent"
          >
            {item.label}
          </Link>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-3">
        <Health />
        <UserMenu me={me} />
      </div>
    </header>
  );
}
