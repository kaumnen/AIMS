import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { blobQuery, isBlob } from "@/api/run";
import { Button } from "@/components/ui/button";
import { bytes } from "@/lib/event-text";
import { cn } from "@/lib/utils";

const AUTO = 256 * 1024;

// Text kept inline in an event, or in a blob fetched once it is on screen; big blobs wait for a
// click.
export function TextOf({
  runId,
  value,
  mono = true,
  className,
}: {
  runId: string;
  value: unknown;
  mono?: boolean;
  className?: string;
}) {
  const blob = isBlob(value) ? value : null;
  const [asked, setAsked] = useState(false);
  const q = useQuery({
    ...blobQuery(runId, blob ?? { sha256: "", size: 0 }),
    enabled: blob !== null && (blob.size <= AUTO || asked),
  });
  const text = blob ? q.data : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const style = cn(
    "max-h-96 overflow-auto rounded-sm border px-2 py-1.5 break-words whitespace-pre-wrap",
    mono ? "font-mono text-xs" : "font-sans",
    className,
  );
  if (blob && text === undefined) {
    if (q.isError)
      return <p className="text-muted-foreground">Could not load {bytes(blob.size)}.</p>;
    if (blob.size > AUTO && !asked) {
      return (
        <Button variant="outline" size="sm" onClick={() => setAsked(true)}>
          Show {bytes(blob.size)}
        </Button>
      );
    }
    return <p className="text-muted-foreground">Loading {bytes(blob.size)}</p>;
  }
  if (!text) return null;
  return <pre className={style}>{text}</pre>;
}
