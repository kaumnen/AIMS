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

export interface TreeNode {
  name: string;
  // the file's path, null for a folder
  path: string | null;
  children: TreeNode[];
}

// Paths as folders and files: scenario.yaml first, then folders, then files, each by name.
export function fileTree(paths: readonly string[]): TreeNode[] {
  const root: TreeNode = { name: "", path: null, children: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let node = root;
    for (const [i, part] of parts.entries()) {
      const leaf = i === parts.length - 1;
      let next = node.children.find((c) => c.name === part && (c.path === null) === !leaf);
      if (!next) {
        next = { name: part, path: leaf ? path : null, children: [] };
        node.children.push(next);
      }
      node = next;
    }
  }
  const rank = (n: TreeNode) => (n.path === "scenario.yaml" ? 0 : n.path === null ? 1 : 2);
  const sort = (nodes: TreeNode[]): TreeNode[] =>
    nodes
      .map((n) => ({ ...n, children: sort(n.children) }))
      .toSorted((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  return sort(root.children);
}
