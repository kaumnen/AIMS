import { useQueryClient } from "@tanstack/react-query";

import { reconnectLive } from "@/api/channel";
import type { Schemas } from "@/api/client";
import { client } from "@/api/client";
import { forgetSession, whoami } from "@/api/session";
import { setTheme, type Theme, themes, useTheme } from "@/lib/theme";
import { mayDiscard } from "@/lib/unsaved";

import { openShortcuts } from "./shortcuts";
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
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

const names: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

export function UserMenu({ me }: { me: Schemas["Whoami"] }) {
  const queryClient = useQueryClient();
  const theme = useTheme();

  async function signOut() {
    if (!mayDiscard()) return;
    await client.DELETE("/api/session").catch(() => undefined);
    // the tabs' shared stream still holds the old session until it connects again
    reconnectLive();
    // drop what this session loaded, then let the shell find out it is signed out
    forgetSession(queryClient);
    await queryClient.resetQueries({ queryKey: whoami.queryKey });
  }

  return (
    <DropdownMenu>
      {/* a long name shortens before the header outgrows a phone's screen */}
      <DropdownMenuTrigger render={<Button variant="ghost" size="sm" className="min-w-0 shrink" />}>
        <span className="truncate">{me.auth ? me.name : "No sign-in"}</span>
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
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={openShortcuts}>
          Shortcuts
          <DropdownMenuShortcut>?</DropdownMenuShortcut>
        </DropdownMenuItem>
        {me.auth && <DropdownMenuItem onClick={() => void signOut()}>Sign out</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
