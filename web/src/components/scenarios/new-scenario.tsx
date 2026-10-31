import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { createScenario, duplicateScenario } from "@/api/scenario-files";
import { sourceLabel, sourcesQuery } from "@/api/scenarios";
import { isProblem } from "@/api/session";
import { Field, Pick, problemText } from "@/components/form";
import { useOperator } from "@/components/run/controls";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;

// A new scenario from the starter file, or a copy of `from` (built-in ones included), in a
// source that takes edits; it opens on its files.
export function NewScenario({ from }: { from?: { source: string; name: string } }) {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  if (!operator) return null;
  const copy = from !== undefined;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" variant={copy ? "outline" : "default"} />}>
        {copy ? "Duplicate" : "New scenario"}
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        {open && <Form from={from} onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function Form({
  from,
  onDone,
}: {
  from?: { source: string; name: string } | undefined;
  onDone: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const sources = useQuery(sourcesQuery);
  const writable = (sources.data ?? []).filter((s) => s.writable);
  const [source, setSource] = useState("");
  const [name, setName] = useState(from ? `${from.name}-copy` : "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const chosen = writable.find((s) => s.id === source) ?? writable[0];
  const valid = NAME.test(name);

  const submit = async () => {
    if (!chosen || !valid) return;
    setBusy(true);
    setProblem(null);
    try {
      const to = { source: chosen.id, name };
      await (from ? duplicateScenario(from, to) : createScenario(to.source, to.name));
      void queryClient.invalidateQueries({ queryKey: ["scenarios"], exact: true });
      onDone();
      void navigate({
        to: "/scenarios/$source/$name",
        params: to,
        search: { tab: "files" },
      });
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
        <DialogTitle>{from ? `Duplicate ${from.name}` : "New scenario"}</DialogTitle>
        <DialogDescription>
          {from
            ? "Copies every file of the scenario into a source you can edit."
            : "Starts from a short scenario.yaml to fill in."}
        </DialogDescription>
      </DialogHeader>
      {writable.length === 0 ? (
        <p className="text-muted-foreground">No scenario source takes new scenarios here.</p>
      ) : (
        <>
          {writable.length > 1 && (
            <Pick
              label="Source"
              value={chosen?.id ?? ""}
              items={writable.map((s) => ({ value: s.id, label: sourceLabel(writable, s.id) }))}
              onChange={setSource}
            />
          )}
          <Field label="Name">
            <Input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={name !== "" && !valid}
            />
          </Field>
          {name !== "" && !valid && (
            <p className="text-muted-foreground">
              Letters, digits, dots, dashes and underscores; it starts with a letter or digit.
            </p>
          )}
        </>
      )}
      {problem && <p className="font-medium">{problem}</p>}
      <DialogFooter>
        <Button type="submit" size="sm" disabled={!chosen || !valid || busy}>
          {from ? "Duplicate" : "Create"}
        </Button>
      </DialogFooter>
    </form>
  );
}
