import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";

import { isProblem, whoami } from "@/api/session";
import {
  createToken,
  revokeToken,
  type TokenInfo,
  type TokenRole,
  tokensQuery,
} from "@/api/settings";
import { Field, Pick, problemText } from "@/components/form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { fullTime, started } from "@/lib/format";
import { useOperator } from "@/lib/use-operator";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/settings/tokens")({
  staticData: { crumb: "Tokens" },
  component: TokensPage,
});

const NAME = /^[A-Za-z0-9._@-]{1,100}$/;

const ROLES: { value: TokenRole; label: string; detail: string }[] = [
  { value: "viewer", label: "Viewer", detail: "Reads runs, scenarios and settings." },
  {
    value: "operator",
    label: "Operator",
    detail: "Starts and steers runs, edits scenarios and settings, manages tokens.",
  },
  { value: "runner", label: "Runner", detail: "Connects a runner that takes this plane's jobs." },
];

const iso = (s: number) => new Date(s * 1000).toISOString();

function TokensPage() {
  const operator = useOperator();
  if (!operator) {
    return <p className="text-muted-foreground">Only operators see and manage tokens.</p>;
  }
  return <Tokens />;
}

function Tokens() {
  const me = useQuery(whoami);
  const tokens = useQuery(tokensQuery);
  // the token stays while the dialog closes, so its text does not blank mid fade
  const [revoking, setRevoking] = useState<TokenInfo | null>(null);
  const [asking, setAsking] = useState(false);
  if (tokens.isPending) return <p className="text-muted-foreground">Loading tokens</p>;
  if (tokens.isError) {
    return (
      <p className="text-muted-foreground flex items-center">
        Tokens did not load.
        <Button variant="link" size="sm" onClick={() => void tokens.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  const live = tokens.data.filter((t) => t.revoked == null);
  const revoked = tokens.data
    .filter((t) => t.revoked != null)
    .toSorted((a, b) => (b.revoked ?? 0) - (a.revoked ?? 0));
  const mine = me.data?.name;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="font-medium">Tokens</h2>
        <span className="text-muted-foreground">
          {live.length} live{revoked.length > 0 && ` · ${revoked.length} revoked`}
        </span>
        <div className="ml-auto self-center">
          <NewToken taken={new Set(tokens.data.map((t) => t.name))} />
        </div>
      </div>
      {me.data?.auth === false && (
        <p className="text-muted-foreground">
          Sign-in is off on this control plane, so it lets anyone in without a token.
        </p>
      )}
      <table className="w-full">
        <thead className="text-muted-foreground text-left">
          <tr className="border-b">
            <th className="py-1 pr-4 font-normal">Name</th>
            <th className="py-1 pr-4 font-normal">Role</th>
            <th className="py-1 pr-4 font-normal max-md:hidden">Created</th>
            <th className="py-1 pr-4 font-normal">State</th>
            <th className="py-1 font-normal">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {[...live, ...revoked].map((t) => (
            <Row
              key={t.name}
              t={t}
              mine={t.name === mine}
              onRevoke={() => {
                setRevoking(t);
                setAsking(true);
              }}
            />
          ))}
        </tbody>
      </table>
      <p className="text-muted-foreground">
        A token&apos;s value is shown once, when it is made. A revoked token&apos;s name stays
        taken.
      </p>
      <Revoke
        token={revoking}
        open={asking}
        mine={revoking !== null && revoking.name === mine}
        onClose={() => setAsking(false)}
      />
    </div>
  );
}

function Row({ t, mine, onRevoke }: { t: TokenInfo; mine: boolean; onRevoke: () => void }) {
  const gone = t.revoked != null;
  const role = ROLES.find((r) => r.value === t.role)?.label ?? t.role;
  return (
    <tr className={cn("border-b align-top last:border-0", gone && "text-muted-foreground")}>
      <td className="py-1.5 pr-4">
        <span className={cn(!gone && "font-medium")}>{t.name}</span>
        {mine && <span className="text-muted-foreground ml-2">you</span>}
        {t.ui && <span className="text-muted-foreground ml-2">aims ui</span>}
      </td>
      <td className="py-1.5 pr-4">{role}</td>
      <td className="py-1.5 pr-4 max-md:hidden" title={fullTime(iso(t.created))}>
        {started(iso(t.created))}
      </td>
      <td className="py-1.5 pr-4">
        {t.revoked == null ? (
          "Live"
        ) : (
          <span title={fullTime(iso(t.revoked))}>Revoked {started(iso(t.revoked))}</span>
        )}
      </td>
      <td className="py-1 text-right">
        {!gone && (
          <Button size="sm" variant="outline" aria-label={`Revoke ${t.name}`} onClick={onRevoke}>
            Revoke
          </Button>
        )}
      </td>
    </tr>
  );
}

function Revoke({
  token,
  open,
  mine,
  onClose,
}: {
  token: TokenInfo | null;
  open: boolean;
  mine: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const close = () => {
    setProblem(null);
    onClose();
  };
  const revoke = async () => {
    if (!token) return;
    setBusy(true);
    setProblem(null);
    try {
      await revokeToken(token.name);
      close();
      void queryClient.invalidateQueries({ queryKey: tokensQuery.queryKey });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Revoke {token?.name}?</DialogTitle>
          <DialogDescription>
            It stops working at once and every session made with it ends. Its name stays taken.
          </DialogDescription>
        </DialogHeader>
        {mine && (
          <p className="font-medium">You are signed in with this token, so this signs you out.</p>
        )}
        {token?.ui && (
          <p className="font-medium">
            The launch link from aims ui and local aims commands use this token. They stop working
            until aims ui starts again and makes a new one.
          </p>
        )}
        {problem && <p className="font-medium">{problem}</p>}
        <DialogFooter>
          <DialogClose render={<Button size="sm" variant="outline" />}>Cancel</DialogClose>
          <Button size="sm" disabled={busy} onClick={() => void revoke()}>
            Revoke
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function NewToken({ taken }: { taken: ReadonlySet<string> }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" />}>New token</DialogTrigger>
      <DialogContent className="sm:max-w-md">
        {open && <Create taken={taken} onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function Create({ taken, onDone }: { taken: ReadonlySet<string>; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [role, setRole] = useState<TokenRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // kept apart from the list so its refetch cannot drop the one sight of the value
  const [made, setMade] = useState<{ name: string; role: TokenRole; secret: string } | null>(null);
  const valid = NAME.test(name);
  const clash = taken.has(name);

  if (made) return <Made {...made} onDone={onDone} />;

  const submit = async () => {
    if (!valid || clash) return;
    setBusy(true);
    setProblem(null);
    try {
      const secret = await createToken(name, role);
      setMade({ name, role, secret });
      void queryClient.invalidateQueries({ queryKey: tokensQuery.queryKey });
    } catch (e) {
      setProblem(isProblem(e) ? problemText(e) : "The request failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <DialogHeader>
        <DialogTitle>New token</DialogTitle>
        <DialogDescription>Its value is shown once, right after it is made.</DialogDescription>
      </DialogHeader>
      <Field label="Name">
        <Input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-invalid={name !== "" && (!valid || clash)}
        />
      </Field>
      {name !== "" && !valid && (
        <p className="text-muted-foreground">
          Up to 100 letters, digits, dots, dashes, underscores and @.
        </p>
      )}
      {clash && (
        <p className="text-muted-foreground">
          A token named {name} exists or was revoked; names stay taken.
        </p>
      )}
      <Field label="Role">
        <Pick label="Role" value={role} items={ROLES} onChange={(v) => setRole(v as TokenRole)} />
      </Field>
      <p className="text-muted-foreground -mt-2">{ROLES.find((r) => r.value === role)?.detail}</p>
      {problem && <p className="font-medium">{problem}</p>}
      <DialogFooter>
        <Button type="submit" size="sm" disabled={!valid || clash || busy}>
          Create
        </Button>
      </DialogFooter>
    </form>
  );
}

function Made({
  name,
  role,
  secret,
  onDone,
}: {
  name: string;
  role: TokenRole;
  secret: string;
  onDone: () => void;
}) {
  const [copied, setCopied] = useState<boolean | null>(null);
  // missing outside a secure context, such as plain http on a LAN address
  const clipboard = (navigator as { clipboard?: Clipboard }).clipboard;
  const copy = async () => {
    try {
      await clipboard?.writeText(secret);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>Token {name}</DialogTitle>
        <DialogDescription>Copy it now. It is not shown again.</DialogDescription>
      </DialogHeader>
      <div className="flex items-start gap-2">
        <code
          aria-label="Token value"
          className="min-w-0 flex-1 rounded-sm border px-2 py-1 font-mono break-all select-all"
        >
          {secret}
        </code>
        {clipboard && (
          <Button size="sm" variant="outline" onClick={() => void copy()}>
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </div>
      {copied === false && <p className="text-muted-foreground">Copy failed; select the text.</p>}
      {role === "runner" ? (
        <p className="text-muted-foreground">
          Save it in a file on the runner&apos;s machine, then start{" "}
          <code className="text-foreground">
            aims runner --server {location.origin} --token-file &lt;file&gt;
          </code>
          .
        </p>
      ) : (
        <p className="text-muted-foreground">
          Sign in with it in a browser, or set <code className="text-foreground">AIMS_TOKEN</code>{" "}
          to it and give <code className="text-foreground">--server {location.origin}</code> to aims
          run, push, monitor or control.
        </p>
      )}
      <DialogFooter>
        <Button size="sm" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </div>
  );
}
