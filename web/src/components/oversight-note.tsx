import { TriangleAlert } from "lucide-react";

import type { Note } from "@/lib/launch";
import { cn } from "@/lib/utils";

// What the monitor may do in the runs about to start; loud when nothing stops the agents or
// the monitor acts without asking.
export function OversightNote({ note, className }: { note: Note; className?: string }) {
  return (
    <p
      role={note.loud ? "alert" : undefined}
      className={cn(
        "flex items-start gap-2",
        note.loud
          ? "border-foreground rounded-sm border px-2 py-1.5 font-medium"
          : "text-muted-foreground",
        className,
      )}
    >
      {note.loud && <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />}
      {note.text}
    </p>
  );
}
