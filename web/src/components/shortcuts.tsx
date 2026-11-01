import { Fragment, useEffect, useState } from "react";

import { shortcut } from "@/lib/keys";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog";

const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);

interface Entry {
  // either key does it, or with `seq` they are pressed one after the other
  keys: string[];
  seq?: boolean;
  does: string;
}

const groups: { title: string; entries: Entry[] }[] = [
  { title: "Anywhere", entries: [{ keys: ["?"], does: "Show these shortcuts" }] },
  {
    title: "Runs",
    entries: [
      { keys: ["j", "k"], does: "Next or previous run" },
      { keys: ["Enter"], does: "Open the run" },
    ],
  },
  {
    title: "A run's steps",
    entries: [
      { keys: ["j", "k", "↓", "↑"], does: "Next or previous step" },
      { keys: ["f", "Shift F"], does: "Next or previous flag" },
      { keys: ["/"], does: "Search the raw events" },
    ],
  },
  {
    title: "Timeline, once focused",
    entries: [
      { keys: ["→", "←"], does: "Next or previous step" },
      { keys: ["Home", "End"], does: "First or last step" },
    ],
  },
  {
    title: "Raw events, once focused",
    entries: [
      { keys: ["j", "k", "↓", "↑"], does: "Next or previous event" },
      { keys: ["Home", "End"], does: "First or last event" },
    ],
  },
  {
    title: "Editors",
    entries: [
      { keys: [mac ? "⌘ S" : "Ctrl S"], does: "Save" },
      { keys: ["Esc", "Tab"], seq: true, does: "Leave the editor" },
    ],
  },
];

function Keys({ k, seq = false }: { k: string[]; seq?: boolean }) {
  return (
    <span className="flex flex-wrap items-center justify-end gap-1">
      {k.map((key, i) => (
        <Fragment key={key}>
          {i > 0 && <span className="text-muted-foreground">{seq ? "then" : "or"}</span>}
          <kbd className="bg-muted rounded-sm border px-1.5 text-xs">{key}</kbd>
        </Fragment>
      ))}
    </span>
  );
}

let show: (() => void) | null = null;

export function openShortcuts(): void {
  show?.();
}

// The keyboard shortcuts, opened with ? anywhere or from the user menu.
export function Shortcuts() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    show = () => setOpen(true);
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "?" || !shortcut(e)) return;
      e.preventDefault();
      setOpen(true);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      show = null;
      window.removeEventListener("keydown", onKey);
    };
  }, []);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
        </DialogHeader>
        {groups.map((g) => (
          <section key={g.title} aria-label={g.title}>
            <h3 className="text-muted-foreground mb-1">{g.title}</h3>
            <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
              {g.entries.map((e) => (
                <Fragment key={e.does}>
                  <dt>{e.does}</dt>
                  <dd>
                    <Keys k={e.keys} seq={e.seq ?? false} />
                  </dd>
                </Fragment>
              ))}
            </dl>
          </section>
        ))}
      </DialogContent>
    </Dialog>
  );
}
