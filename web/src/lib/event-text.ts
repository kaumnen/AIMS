import type { Event } from "@/api/events";
import { eventTypeValues } from "@/api/schema.gen";

type Data = Record<string, unknown>;

const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined => (typeof v === "number" ? v : undefined);
const firstLine = (s: string) => s.split("\n", 1)[0] ?? "";

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// One line saying what an event is about, for tables of events.
export function summary(e: Event): string {
  const d = e.data as Data;
  switch (e.type) {
    case "file.access":
      return [str(d["op"]), str(d["path"]), d["errno"] ? `errno ${num(d["errno"])}` : ""]
        .filter(Boolean)
        .join(" ");
    case "file.write":
      return `write ${str(d["path"]) ?? ""} ${bytes(num(d["size"]) ?? 0)}`;
    case "file.setattr":
      return `setattr ${str(d["path"]) ?? ""}`;
    case "proc.exec":
      return Array.isArray(d["argv"]) ? d["argv"].join(" ") : (str(d["filename"]) ?? "");
    case "proc.exit": {
      const signal = num(d["signal"]);
      return `pid ${num(d["pid"])} ${signal ? `signal ${signal}` : `exit ${num(d["exit_code"])}`}`;
    }
    case "proc.fork":
      return `pid ${num(d["pid"])} forked from ${num(d["ppid"])}`;
    case "proc.output":
      return `${str(d["stream"])} ${bytes(num(d["bytes"]) ?? 0)}: ${firstLine(str(d["prefix"]) ?? "")}`;
    case "ipc.event":
      return [str(d["kind"]), str(d["family"]), str(d["path"])?.replace(/\0.*$/s, "")]
        .filter(Boolean)
        .join(" ");
    case "fs.snapshot":
      return `${num(d["total"]) ?? 0} changes, ${num(d["files"]) ?? 0} files`;
    case "exec.started":
      return firstLine(str(d["command"]) ?? "");
    case "exec.finished": {
      const code = num(d["exit_code"]);
      const end = str(d["end"]) === "exited" && code !== undefined ? `exit ${code}` : str(d["end"]);
      return `${end ?? ""}, ${num(d["duration_ms"]) ?? 0} ms`;
    }
    case "model.request":
      return `${str(d["model"]) ?? ""} attempt ${num(d["attempt"]) ?? 1}`;
    case "model.response": {
      const usage = d["usage"] as Data | undefined;
      return `${str(d["status"])} ${num(usage?.["output_tokens"]) ?? 0} tokens out ${firstLine(str(d["text"]) ?? "")}`;
    }
    case "tool.call":
      return `${str(d["name"])} ${typeof d["arguments"] === "string" ? d["arguments"] : ""}`;
    case "tool.result":
      return typeof d["output"] === "string" ? firstLine(d["output"]) : "";
    case "agent.step":
      return `step ${num(d["n"])} ${str(d["status"])}`;
    case "agent.input":
      return `${str(d["kind"])}: ${firstLine(str(d["text"]) ?? "")}`;
    case "monitor.flag":
      return `${str(d["category"])} importance ${num(d["importance"])}: ${str(d["justification"]) ?? ""}`;
    default:
      return Object.entries(d)
        .filter(([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean")
        .slice(0, 4)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(" ");
  }
}

const groupPrefixes: [string, string[]][] = [
  ["Model", ["model."]],
  ["Agent and tools", ["agent.", "tool."]],
  ["Execs", ["exec.", "proc.output"]],
  ["Files", ["file.", "fs.", "cache.", "mem."]],
  ["Processes", ["proc.", "ptrace.", "ns.", "sys."]],
  ["Network and IPC", ["net.", "ipc."]],
  ["Channels", ["channel."]],
  ["Monitor", ["monitor."]],
  ["Scoring", ["score.", "task.", "plant."]],
];

// Event types in groups a person can pick from; everything else is Run.
export const typeGroups: { name: string; types: string[] }[] = (() => {
  const left = new Set<string>(eventTypeValues);
  const out = groupPrefixes.map(([name, prefixes]) => {
    const types = [...left].filter((t) => prefixes.some((p) => t.startsWith(p)));
    for (const t of types) left.delete(t);
    return { name, types };
  });
  out.push({ name: "Run", types: [...left] });
  return out;
})();
