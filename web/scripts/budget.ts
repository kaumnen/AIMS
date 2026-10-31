// Bundle size budget, run after `bun run build`: `bun scripts/budget.ts [dist]`.
//
// budget.json, sizes in kB (1000 bytes): "firstPaint" and "total" cap raw and gzip bytes of
// what index.html loads and of every js and css asset; "chunks" caps a lazy chunk's gzip size
// by its name without the hash, and "chunk" caps any lazy chunk not named there.
import { readdirSync, readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

interface Size {
  raw: number;
  gzip: number;
}

interface Budget {
  firstPaint: Size;
  total: Size;
  chunk: number;
  chunks: Record<string, number>;
}

const dist = process.argv[2] ?? new URL("../../src/aims/ui/dist", import.meta.url).pathname;
const budget: Budget = JSON.parse(readFileSync(new URL("../budget.json", import.meta.url), "utf8"));

const kb = (bytes: number) => bytes / 1000;
const nameOf = (file: string) => file.replace(/-[\w-]{8}\.(js|css)$/, ".$1");

const html = readFileSync(`${dist}/index.html`, "utf8");
const loaded = new Set(
  [...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="\/assets\/([^"]+)"/g)].map(
    (m) => m[1] as string,
  ),
);

const sizes = new Map<string, Size>();
for (const file of readdirSync(`${dist}/assets`)) {
  if (!/\.(js|css)$/.test(file)) continue;
  const bytes = readFileSync(`${dist}/assets/${file}`);
  sizes.set(file, { raw: kb(bytes.length), gzip: kb(gzipSync(bytes).length) });
}

const sum = (files: Iterable<string>) => {
  const out = { raw: 0, gzip: 0 };
  for (const f of files) {
    const s = sizes.get(f);
    if (!s) throw new Error(`index.html loads ${f}, which is not in ${dist}/assets`);
    out.raw += s.raw;
    out.gzip += s.gzip;
  }
  return out;
};

const rows: { what: string; size: number; cap: number }[] = [];
const first = sum(loaded);
const total = sum(sizes.keys());
rows.push(
  { what: `first paint raw (${loaded.size} files)`, size: first.raw, cap: budget.firstPaint.raw },
  { what: "first paint gzip", size: first.gzip, cap: budget.firstPaint.gzip },
  { what: `total raw (${sizes.size} files)`, size: total.raw, cap: budget.total.raw },
  { what: "total gzip", size: total.gzip, cap: budget.total.gzip },
);
for (const [file, s] of sizes) {
  if (loaded.has(file)) continue;
  const named = budget.chunks[nameOf(file)];
  if (named !== undefined) rows.push({ what: `${file} gzip`, size: s.gzip, cap: named });
  else if (s.gzip > budget.chunk)
    rows.push({ what: `${file} gzip`, size: s.gzip, cap: budget.chunk });
}
const largest = [...sizes]
  .filter(([f]) => !loaded.has(f) && budget.chunks[nameOf(f)] === undefined)
  .toSorted((a, b) => b[1].gzip - a[1].gzip)[0];
if (largest && largest[1].gzip <= budget.chunk) {
  rows.push({
    what: `largest other chunk ${largest[0]} gzip`,
    size: largest[1].gzip,
    cap: budget.chunk,
  });
}

const over = rows.filter((r) => r.size > r.cap);
const width = Math.max(...rows.map((r) => r.what.length));
for (const r of rows) {
  const mark = r.size > r.cap ? "OVER" : "ok";
  console.log(
    `${r.what.padEnd(width)}  ${r.size.toFixed(1).padStart(7)} kB  of ${String(r.cap).padStart(5)} kB  ${mark}`,
  );
}
if (over.length > 0) {
  console.error(`\n${over.length} over budget. Trim, or raise web/budget.json with a reason.`);
  process.exit(1);
}
