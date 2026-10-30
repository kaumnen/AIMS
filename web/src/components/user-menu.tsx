import { hashKey, useQueryClient } from "@tanstack/react-query";

import type { Schemas } from "@/api/client";
import { client } from "@/api/client";
import { whoami } from "@/api/session";
import { setTheme, type Theme, themes, useTheme } from "@/lib/theme";

import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

const names: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

export function UserMenu({ me }: { me: Schemas["Whoami"] }) {
  const queryClient = useQueryClient();
  const theme = useTheme();

  async function signOut() {
    await client.DELETE("/api/session").catch(() => undefined);
    // drop what this session loaded, then let the shell find out it is signed out
    const keep = hashKey(whoami.queryKey);
    queryClient.removeQueries({ predicate: (q) => q.queryHash !== keep });
    await queryClient.resetQueries({ queryKey: whoami.queryKey });
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="sm" />}>
        {me.auth ? me.name : "No sign-in"}
        <span className="text-muted-foreground max-sm:hidden">{me.role}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Theme</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={theme} onValueChange={(v: Theme) => setTheme(v)}>
            {themes.map((t) => (
              <DropdownMenuRadioItem key={t} value={t}>
                {names[t]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        {me.auth && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => void signOut()}>Sign out</DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
