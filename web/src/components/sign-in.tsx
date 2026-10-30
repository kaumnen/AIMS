import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { client } from "@/api/client";
import { whoami } from "@/api/session";

import { Button } from "./ui/button";
import { Input } from "./ui/input";

export function SignIn() {
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { error: problem } = await client.POST("/api/session", {
        body: { token: token.trim() },
      });
      if (problem) {
        setError(problem.status === 401 ? "That token is unknown or revoked." : problem.title);
        return;
      }
      await queryClient.resetQueries({ queryKey: whoami.queryKey });
    } catch {
      setError("Cannot reach the control plane.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center px-4">
      <form className="flex w-full max-w-sm flex-col gap-3" onSubmit={(e) => void submit(e)}>
        <p className="font-semibold">AIMS</p>
        <h1 className="text-lg font-medium">Sign in</h1>
        <p className="text-muted-foreground">
          The link printed by <code className="whitespace-nowrap">aims ui</code> signs you in once,
          within 2 minutes. After that, paste a token from{" "}
          <code className="whitespace-nowrap">aims token create &lt;name&gt; --role operator</code>.
        </p>
        <label htmlFor="token" className="font-medium">
          Token
        </label>
        <Input
          id="token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          required
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-describedby={error ? "token-error" : undefined}
        />
        {error && (
          <p id="token-error" role="alert">
            {error}
          </p>
        )}
        <Button type="submit" disabled={busy || !token.trim()} className="self-start">
          Sign in
        </Button>
      </form>
    </main>
  );
}

export function Unreachable({ retry }: { retry: () => void }) {
  return (
    <main className="grid min-h-dvh place-items-center px-4">
      <div className="flex max-w-sm flex-col gap-3">
        <p className="font-semibold">AIMS</p>
        <p>Cannot reach the control plane. Is aims ui still running?</p>
        <Button variant="outline" className="self-start" onClick={retry}>
          Try again
        </Button>
      </div>
    </main>
  );
}
