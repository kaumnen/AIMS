import { capText, KEYS } from "./budget";

const WORDS: Record<string, string> = {
  steps: "steps",
  output_tokens: "output tokens",
  cost_usd: "",
  wall_s: "wall",
};

// A budget or limits mapping as one line: "80 steps, $4, wall 30m, warns at 80%".
export function capsText(caps: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of KEYS) {
    const value = caps[key];
    if (typeof value !== "number") continue;
    const text = capText(key, value);
    const word = WORDS[key];
    parts.push(
      key === "steps" || key === "output_tokens"
        ? `${text} ${word}`
        : word
          ? `${word} ${text}`
          : text,
    );
  }
  if (typeof caps["monitor_cost_usd"] === "number") {
    parts.push(`monitor $${caps["monitor_cost_usd"]}`);
  }
  if (typeof caps["warn_at"] === "number") {
    parts.push(`warns at ${Math.round(caps["warn_at"] * 100)}%`);
  }
  return parts.join(", ");
}

// A YAML block keeps the line breaks it was typed with; for reading, lines run on unless a blank
// line, a list item or an indented line starts.
export function reflow(text: string): string {
  return text.replace(/([^\n])\n(?=[^\n\s\-*>#\d])/g, "$1 ").trimEnd();
}

// An agent's text overrides as the keys they set: "system, tools.bash.description".
export function overrideKeys(prompts: Record<string, unknown> | null | undefined): string[] {
  if (!prompts) return [];
  const out: string[] = [];
  for (const [key, value] of Object.entries(prompts)) {
    if (key === "tools" && value && typeof value === "object") {
      for (const [tool, texts] of Object.entries(value)) {
        for (const name of Object.keys(texts ?? {})) out.push(`tools.${tool}.${name}`);
      }
    } else {
      out.push(key);
    }
  }
  return out;
}
