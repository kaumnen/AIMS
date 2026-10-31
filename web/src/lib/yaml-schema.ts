// Completion and hover for block YAML from a JSON Schema, by indentation: where the cursor sits
// in the mapping tree, then what the schema allows there. Scenario files are block YAML, so lines
// and their indents are enough; flow collections and multi-line keys are not read.

export type Schema = { [key: string]: unknown };

export const ITEM = Symbol("item");
export type Segment = string | typeof ITEM;

interface Line {
  indent: number;
  dash: number | null;
  keyCol: number | null;
  key: string | null;
  value: string;
  // inside a block scalar (`|`, `>`) or blank or a comment: not structure
  skip: boolean;
}

const KEY = /^(\s*)(-\s+)?([A-Za-z_][\w.-]*)\s*:(?:\s+(.*)|$)/;
const DASH = /^(\s*)-(?:\s+(.*))?$/;

function readLines(text: string): Line[] {
  const out: Line[] = [];
  let block: number | null = null;
  for (const raw of text.split("\n")) {
    const indent = raw.length - raw.trimStart().length;
    const blank = raw.trim() === "" || raw.trimStart().startsWith("#");
    if (block !== null && (blank || indent > block)) {
      out.push({ indent, dash: null, keyCol: null, key: null, value: "", skip: true });
      continue;
    }
    block = null;
    if (blank) {
      out.push({ indent, dash: null, keyCol: null, key: null, value: "", skip: true });
      continue;
    }
    const m = KEY.exec(raw);
    if (m) {
      const lead = m[1]?.length ?? 0;
      const dash = m[2] === undefined ? null : lead;
      const keyCol = lead + (m[2]?.length ?? 0);
      const value = (m[4] ?? "").trim();
      if (/^[|>][-+]?\d*\s*(#.*)?$/.test(value)) block = keyCol;
      out.push({ indent, dash, keyCol, key: m[3] ?? null, value: unquote(value), skip: false });
      continue;
    }
    const d = DASH.exec(raw);
    if (d) {
      const lead = d[1]?.length ?? 0;
      out.push({
        indent,
        dash: lead,
        keyCol: null,
        key: null,
        value: (d[2] ?? "").trim(),
        skip: false,
      });
      continue;
    }
    out.push({ indent, dash: null, keyCol: null, key: null, value: "", skip: true });
  }
  return out;
}

function unquote(value: string): string {
  const v = value.replace(/\s+#.*$/, "");
  if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v.at(-1) === v[0]) return v.slice(1, -1);
  return v;
}

// One mapping on the way down to the cursor, with the keys it holds and their scalar values.
export interface Frame {
  siblings: Record<string, string>;
}

export interface Place {
  // from the root mapping down: keys and sequence items
  path: Segment[];
  // one per mapping the path passes through, the cursor's own last
  frames: Frame[];
  // the key whose value the cursor is in, or null when the cursor is on a key
  key: string | null;
  // the word before the cursor and where it starts
  prefix: string;
  from: number;
}

// Where `pos` falls in `text`: the mappings above it and what is being typed.
export function placeAt(text: string, pos: number): Place | null {
  const lines = readLines(text);
  const lineNo = text.slice(0, pos).split("\n").length - 1;
  const start = text.lastIndexOf("\n", pos - 1) + 1;
  const before = text.slice(start, pos);
  const keyed = /^(\s*)(-\s+)?([A-Za-z_][\w.-]*)\s*:\s*(\S*)$/.exec(before);
  const typing = /^(\s*)(-\s+)?([\w.-]*)$/.exec(before);
  let col: number;
  let dash: number | null;
  let key: string | null = null;
  let prefix: string;
  if (keyed) {
    const lead = keyed[1]?.length ?? 0;
    dash = keyed[2] === undefined ? null : lead;
    col = lead + (keyed[2]?.length ?? 0);
    key = keyed[3] ?? null;
    prefix = keyed[4] ?? "";
  } else if (typing) {
    const lead = typing[1]?.length ?? 0;
    dash = typing[2] === undefined ? null : lead;
    col = lead + (typing[2]?.length ?? 0);
    prefix = typing[3] ?? "";
  } else {
    return null;
  }
  const inner: Frame = { siblings: {} };
  const frames: Frame[] = [inner];
  const path: Segment[] = [];
  let frame = inner;
  // "mapping": collecting the keys of the mapping at column `c`, looking for its parent key;
  // "sequence": the mapping was an item of a sequence whose dashes sit at `d`, looking for the
  // key that holds the sequence
  let mode: "mapping" | "sequence" = "mapping";
  let c = col;
  let d = -1;
  let collect = true;
  const enter = (l: Line) => {
    path.unshift(l.key ?? "");
    frame = { siblings: { [l.key ?? ""]: l.value } };
    frames.unshift(frame);
    collect = true;
    c = l.keyCol ?? 0;
    mode = "mapping";
    if (l.dash !== null) item(l.dash);
  };
  const item = (at: number) => {
    path.unshift(ITEM);
    mode = "sequence";
    d = at;
    collect = false;
  };
  if (dash !== null) item(dash);
  for (let i = lineNo - 1; i >= 0; i--) {
    const l = lines[i];
    if (l === undefined || l.skip) continue;
    if (mode === "mapping") {
      if (l.key !== null && l.keyCol === c) {
        if (collect) frame.siblings[l.key] ??= l.value;
        if (l.dash !== null) item(l.dash);
      } else if (l.key !== null && l.keyCol !== null && l.keyCol < c) {
        enter(l);
      } else if (l.key === null && l.dash !== null && l.dash < c) {
        item(l.dash);
      }
      continue;
    }
    if (l.dash === d) continue;
    if (l.key !== null && l.keyCol !== null) {
      if (l.keyCol < d || (l.keyCol === d && l.dash === null)) enter(l);
      continue;
    }
    if (l.dash !== null && l.dash < d) item(l.dash);
  }
  // keys below the cursor in its own mapping, for a `kind` written after
  for (let j = lineNo + 1; j < lines.length; j++) {
    const l = lines[j];
    if (l === undefined || l.skip) continue;
    if ((l.dash !== null && l.dash < col) || (l.keyCol !== null && l.keyCol < col)) break;
    if (l.key !== null && l.keyCol === col && l.dash === null) inner.siblings[l.key] ??= l.value;
  }
  return { path, frames, key, prefix, from: pos - prefix.length };
}

const defs = (root: Schema) => (root["$defs"] ?? {}) as Record<string, Schema>;

function deref(root: Schema, s: Schema | undefined): Schema | undefined {
  let node = s;
  for (let i = 0; node && typeof node["$ref"] === "string" && i < 20; i++) {
    const name = String(node["$ref"]).replace("#/$defs/", "");
    node = defs(root)[name];
  }
  return node;
}

// The schemas `s` may be, `$ref`s followed and `null` left out.
export function variants(root: Schema, s: Schema | undefined): Schema[] {
  const node = deref(root, s);
  if (!node) return [];
  const many = (node["anyOf"] ?? node["oneOf"]) as Schema[] | undefined;
  if (many) return many.flatMap((v) => variants(root, v));
  if (node["type"] === "null") return [];
  return [node];
}

const props = (s: Schema) => (s["properties"] ?? {}) as Record<string, Schema>;

function kindOf(root: Schema, s: Schema): string[] | null {
  const k = deref(root, props(s)["kind"]);
  if (!k) return null;
  if (k["const"] !== undefined) return [String(k["const"])];
  if (Array.isArray(k["enum"])) return k["enum"].map(String);
  return [];
}

// The object variants of `s` that fit a mapping holding `siblings`: by its `kind` when the
// variants tell kinds apart.
export function narrow(root: Schema, s: Schema | undefined, siblings: Record<string, string>) {
  const objects = variants(root, s).filter((v) => v["properties"] !== undefined);
  const kind = siblings["kind"];
  if (objects.length < 2 || !kind) return objects;
  const exact = objects.filter((v) => kindOf(root, v)?.includes(kind));
  if (exact.length > 0) return exact;
  const open = objects.filter((v) => kindOf(root, v)?.length === 0);
  return open.length > 0 ? open : objects;
}

function child(root: Schema, s: Schema | undefined, seg: Segment, frame: Frame | undefined) {
  if (seg === ITEM) {
    const array = variants(root, s).find((v) => v["items"] !== undefined);
    return array ? (array["items"] as Schema) : undefined;
  }
  for (const v of narrow(root, s, frame?.siblings ?? {})) {
    const p = props(v)[seg];
    if (p) return p;
    const extra = v["additionalProperties"];
    if (extra && typeof extra === "object") return extra as Schema;
  }
  return undefined;
}

// The schema of the mapping `place` sits in, walked down the path.
export function containerAt(root: Schema, place: Place): Schema | undefined {
  let node: Schema | undefined = root;
  let f = 0;
  for (const seg of place.path) {
    if (!node) return undefined;
    node = child(root, node, seg, seg === ITEM ? undefined : place.frames[f]);
    if (seg !== ITEM) f++;
  }
  return node;
}

export interface Option {
  label: string;
  detail: string;
  info: string;
}

// A short type: "observe | autonomous | human", "list of string", "Task".
export function typeText(root: Schema, s: Schema | undefined): string {
  const node = deref(root, s);
  if (!node) return "";
  if (typeof s?.["$ref"] === "string") return String(s["$ref"]).replace("#/$defs/", "");
  if (node["const"] !== undefined) return JSON.stringify(node["const"]);
  if (Array.isArray(node["enum"])) return node["enum"].map(String).join(" | ");
  const many = (node["anyOf"] ?? node["oneOf"]) as Schema[] | undefined;
  if (many) {
    return many
      .filter((v) => deref(root, v)?.["type"] !== "null")
      .map((v) => typeText(root, v))
      .join(" | ");
  }
  if (node["type"] === "array")
    return `list of ${typeText(root, node["items"] as Schema) || "any"}`;
  if (typeof node["type"] === "string") return node["type"];
  return "";
}

function docOf(root: Schema, s: Schema | undefined): string {
  const own = s?.["description"];
  if (typeof own === "string") return own;
  for (const v of variants(root, s)) {
    if (typeof v["description"] === "string") return v["description"];
  }
  return "";
}

// Keys the mapping at `place` may still take.
export function keyOptions(root: Schema, place: Place): Option[] {
  const container = containerAt(root, place);
  const inner = place.frames.at(-1)?.siblings ?? {};
  const seen = new Map<string, Option>();
  for (const v of narrow(root, container, inner)) {
    const required = new Set((v["required"] ?? []) as string[]);
    for (const [name, p] of Object.entries(props(v))) {
      if (name in inner || seen.has(name)) continue;
      const type = typeText(root, p);
      seen.set(name, {
        label: name,
        detail: required.has(name) ? `${type}, required` : type,
        info: docOf(root, p),
      });
    }
  }
  return [...seen.values()];
}

// Values the key at `place` may take: enums, consts and booleans; for `kind`, every kind the
// mapping's schema tells apart.
export function valueOptions(root: Schema, place: Place): Option[] {
  if (place.key === null) return [];
  const container = containerAt(root, place);
  if (place.key === "kind") {
    const kinds = variants(root, container).flatMap((v) => {
      const doc = typeof v["description"] === "string" ? v["description"] : "";
      return (kindOf(root, v) ?? []).map((k) => ({ label: k, detail: "kind", info: doc }));
    });
    if (kinds.length > 0) return kinds;
  }
  const inner = place.frames.at(-1)?.siblings ?? {};
  const out: Option[] = [];
  for (const v of narrow(root, container, inner)) {
    const p = props(v)[place.key];
    for (const value of variants(root, p)) {
      if (value["const"] !== undefined) out.push(opt(value["const"]));
      if (Array.isArray(value["enum"])) out.push(...value["enum"].map(opt));
      if (value["type"] === "boolean") out.push(opt(true), opt(false));
    }
  }
  return out.filter((o, i) => out.findIndex((x) => x.label === o.label) === i);
}

const opt = (v: unknown): Option => ({ label: String(v), detail: "", info: "" });

export interface Hover {
  title: string;
  doc: string;
}

// What the key under `pos` is: its type and the docs of its field, or of the model holding it.
export function hoverAt(root: Schema, text: string, pos: number): Hover | null {
  const start = text.lastIndexOf("\n", pos - 1) + 1;
  const end = text.indexOf("\n", pos);
  const line = text.slice(start, end < 0 ? undefined : end);
  const m = KEY.exec(line);
  if (!m) return null;
  const keyFrom = start + (m[1]?.length ?? 0) + (m[2]?.length ?? 0);
  const name = m[3] ?? "";
  if (pos < keyFrom || pos > keyFrom + name.length) return null;
  const place = placeAt(text, keyFrom);
  if (!place) return null;
  const container = containerAt(root, place);
  const inner = place.frames.at(-1)?.siblings ?? {};
  const value = m[4]?.trim();
  if (value) inner[name] ??= unquote(value);
  for (const v of narrow(root, container, inner)) {
    const p = props(v)[name];
    if (!p) continue;
    const type = typeText(root, p);
    const doc = docOf(root, p) || (typeof v["description"] === "string" ? v["description"] : "");
    return { title: type ? `${name}: ${type}` : name, doc };
  }
  return null;
}
