import { cn } from "@/lib/utils";

// A cost estimate in words, as the CLI prints it: its warnings are among the lines, so they
// show once. The total and the warnings stand out.
export function EstimateLines({ lines, stale = false }: { lines: string[]; stale?: boolean }) {
  return (
    <ul className={cn("flex flex-col gap-0.5", stale && "opacity-60")}>
      {lines.map((line) => (
        <li
          key={line}
          className={cn((line.startsWith("Total") || line.startsWith("warning:")) && "font-medium")}
        >
          {line}
        </li>
      ))}
    </ul>
  );
}
