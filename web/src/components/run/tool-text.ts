import type { ToolInfo } from "@/api/run";
import { bytes } from "@/lib/event-text";

type Args = Record<string, unknown>;

const asArgs = (v: unknown): Args | null =>
  typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Args) : null;

// The part of a call worth a glance: the command, the file, the answer.
export function callText(name: string, args: unknown): string {
  const a = asArgs(args);
  if (!a) return "";
  const s = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : "");
  switch (name) {
    case "bash":
      return s("command");
    case "edit":
      return [s("command"), s("path")].filter(Boolean).join(" ");
    case "submit":
      return s("answer");
    default: {
      const first = Object.values(a).find((v) => typeof v === "string");
      return typeof first === "string" ? first : "";
    }
  }
}

export function toolLine(t: ToolInfo): string {
  if (t.args === null || t.args === undefined) {
    return t.args_bytes ? `${t.name} (${bytes(t.args_bytes)} of arguments)` : t.name;
  }
  const text = callText(t.name, t.args).replaceAll(/\s+/g, " ").trim();
  return text ? `${t.name} ${text}` : t.name;
}

export function failed(t: ToolInfo): boolean {
  return Boolean(t.denied) || Boolean(t.error) || (t.exit_code ?? 0) !== 0;
}

export function parseArgs(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
