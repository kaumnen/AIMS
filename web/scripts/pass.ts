// Screenshot and accessibility pass over every screen: `bun run pass [--out dir] [--dir d]
// [--server-dir d] [--only name,name] [--set key=run-id]`.
// Serves the seeded runs (scripts/serve.ts) with auth on, from a scratch copy of the project and
// a scratch user config dir so edits never touch the repo or this machine's config, and ships
// the live run with scripts/live.py. Then opens each state in shots/pass.json in headless Chrome
// as its role, shoots it light and dark at 1440 px and, for pages, light at 390 px, and runs
// axe-core on each. Writes the PNGs and report.json to --out. Fails on a serious or critical
// violation, a page wider than the screen, or a state that never gets ready. A state naming a
// run the data lacks is skipped and listed; --set adds runs, say bee5=<id> against a copy of
// real data with --server-dir.

import { appendFile, cp as copy, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";

import { browserSocket, type Cdp, chromePath, connect, launch } from "./cdp";
import { type Plane, plane, ROOT, spawn, stop } from "./serve";

type Role = "operator" | "viewer" | "nobody";

interface Step {
  // a button, link, tab or menu item by its text or aria-label, or `css` by selector
  click?: string;
  css?: string;
  press?: string;
  type?: string;
  wait?: string;
  // append text to a file on disk: [path under {project} or {config}, text]
  append?: [string, string];
}

interface State {
  name: string;
  path: string;
  as?: Role;
  ready: string;
  steps?: Step[];
  // a dialog or a moment within a page: no 390 px shot
  dialog?: boolean;
}

interface Violation {
  id: string;
  impact: string | null;
  help: string;
  nodes: { target: string; summary: string; html: string }[];
}

interface Evaluated<T> {
  result?: { value?: T };
  exceptionDetails?: { text: string; exception?: { description?: string } };
}

const VARIANTS = [
  { name: "light", width: 1440, height: 900, scheme: "light" },
  { name: "dark", width: 1440, height: 900, scheme: "dark" },
  { name: "mobile", width: 390, height: 844, scheme: "light" },
] as const;
const GATE = new Set(["serious", "critical"]);
const KEYS: Record<string, { code: string; vk: number; text?: string }> = {
  // a key event submits a form only with the text a real Enter types
  Enter: { code: "Enter", vk: 13, text: "\r" },
  Escape: { code: "Escape", vk: 27 },
  End: { code: "End", vk: 35 },
  Home: { code: "Home", vk: 36 },
  ArrowDown: { code: "ArrowDown", vk: 40 },
  ArrowUp: { code: "ArrowUp", vk: 38 },
  Tab: { code: "Tab", vk: 9 },
};
// Shots catch the end state, not a transition's middle frame.
const STILL = `addEventListener("DOMContentLoaded", () => {
  const s = document.createElement("style");
  s.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; caret-color: transparent !important; }";
  document.head.append(s);
});`;

const { values: opts } = parseArgs({
  options: {
    out: { type: "string", default: "pass-shots" },
    dir: { type: "string" },
    "server-dir": { type: "string" },
    only: { type: "string" },
    set: { type: "string", multiple: true, default: [] },
  },
});
const out = resolve(opts.out);
const states = (
  (await Bun.file(join(import.meta.dir, "../shots/pass.json")).json()) as { states: State[] }
).states;
const only = opts.only ? new Set(opts.only.split(",")) : null;
const axe = await Bun.file(join(import.meta.dir, "../node_modules/axe-core/axe.min.js")).text();

const dir = opts.dir ? resolve(opts.dir) : await mkdtemp(join(tmpdir(), "aims-pass-"));
const project = join(dir, "project");
const config = join(dir, "config");
let server: Plane | undefined;
let live: Bun.Subprocess | undefined;
let chrome: Awaited<ReturnType<typeof launch>> | undefined;
const report: {
  states: { name: string; path: string; as: Role; shots: Record<string, Violation[]> }[];
  skipped: { name: string; missing: string[] }[];
  errors: { name: string; error: string }[];
} = { states: [], skipped: [], errors: [] };
let gated = 0;
try {
  await rm(project, { recursive: true, force: true });
  await rm(config, { recursive: true, force: true });
  await mkdir(project, { recursive: true });
  await mkdir(config, { recursive: true });
  await copy(join(ROOT, "aims.toml"), join(project, "aims.toml"));
  await copy(join(ROOT, "src", "aims", "scenarios"), join(project, "scenarios"), {
    recursive: true,
  });
  await Bun.write(
    join(config, "config.toml"),
    "# Settings for every project on this machine.\n\n[credits]\nbaseten = 30\n",
  );
  await mkdir(out, { recursive: true });

  server = await plane({
    dir,
    serverDir: opts["server-dir"],
    auth: true,
    cwd: project,
    env: { AIMS_CONFIG_DIR: config },
    prefix: "aims-pass-",
  });
  const { url, seeded, env, operator } = server;
  if (!operator) throw new Error("server.json has no operator token");
  const ids: Record<string, string> = {
    run: seeded.run,
    score: seeded.score,
    review: seeded.review,
    fork: seeded.fork,
    selftest: seeded.selftest,
    case: seeded.cases[0] ?? "",
    check: seeded.check,
    live: seeded.live,
    project,
    config,
    stamp: Date.now().toString(36),
  };
  for (const kv of opts.set) {
    const [k, v] = kv.split("=");
    if (k && v) ids[k] = v;
  }

  const liveLog = join(dir, "live.log");
  live = spawn(
    [
      "uv",
      "run",
      "--project",
      ROOT,
      "python",
      join(ROOT, "scripts/live.py"),
      join(dir, "runs"),
      seeded.live,
    ],
    dir,
    env,
    liveLog,
  );
  await until(
    async () =>
      (
        await Bun.file(liveLog)
          .text()
          .catch(() => "")
      ).includes("live"),
    60_000,
    async () => `live.py did not ship the run: ${await Bun.file(`${liveLog}.err`).text()}`,
  );

  const bearer = { Authorization: `Bearer ${operator}`, "Content-Type": "application/json" };
  const viewer = await fetch(`${url}/api/tokens`, {
    method: "POST",
    headers: bearer,
    body: JSON.stringify({ name: `viewer-${Date.now().toString(36)}`, role: "viewer" }),
  }).then(async (r) => {
    if (!r.ok) throw new Error(`viewer token: ${r.status} ${await r.text()}`);
    return ((await r.json()) as { token: string }).token;
  });

  chrome = await launch(chromePath(), join(dir, "chrome-pass"));
  const browser = await connect(await browserSocket(chrome.port));
  const contexts: Record<Role, string> = {
    operator: await context(browser),
    viewer: await context(browser),
    nobody: await context(browser),
  };
  await signIn(browser, contexts.operator, url, async () => {
    const r = await fetch(`${url}/api/session/code`, { method: "POST", headers: bearer });
    return { code: ((await r.json()) as { code: string }).code };
  });
  await signIn(browser, contexts.viewer, url, async () => ({ token: viewer }));

  for (const state of states) {
    if (only && !only.has(state.name)) continue;
    const missing = [...`${state.path} ${JSON.stringify(state.steps ?? [])}`.matchAll(/\{(\w+)\}/g)]
      .map((m) => m[1] ?? "")
      .filter((k) => !ids[k]);
    if (missing.length) {
      report.skipped.push({ name: state.name, missing });
      console.log(`skip ${state.name}: no ${missing.join(", ")}`);
      continue;
    }
    // oxlint-disable-next-line no-await-in-loop
    await shoot(browser, contexts[state.as ?? "operator"], url, state, ids).catch((e: unknown) => {
      report.errors.push({ name: state.name, error: String(e) });
      console.log(`FAIL ${state.name}: ${String(e)}`);
    });
  }
  browser.close();
} finally {
  await chrome?.close();
  if (live) await stop(live);
  await server?.stop();
  if (!opts.dir) await rm(dir, { recursive: true, force: true });
}
await Bun.write(join(out, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
console.log(
  `${report.states.length} states, ${report.skipped.length} skipped, ${report.errors.length} errors, ${gated} serious or critical; wrote ${out}`,
);
process.exit(gated || report.errors.length ? 1 : 0);

async function until(test: () => Promise<boolean>, ms: number, why: () => Promise<string>) {
  const end = Date.now() + ms;
  // oxlint-disable-next-line no-await-in-loop
  while (!(await test())) {
    // oxlint-disable-next-line no-await-in-loop
    if (Date.now() > end) throw new Error(await why());
    // oxlint-disable-next-line no-await-in-loop
    await Bun.sleep(100);
  }
}

async function context(browser: Cdp): Promise<string> {
  const r = await browser.send<{ browserContextId: string }>("Target.createBrowserContext");
  return r.browserContextId;
}

class Tab {
  constructor(
    readonly cdp: Cdp,
    readonly session: string,
    readonly target: string,
  ) {}

  static async open(browser: Cdp, ctx: string): Promise<Tab> {
    const { targetId } = await browser.send<{ targetId: string }>("Target.createTarget", {
      url: "about:blank",
      browserContextId: ctx,
    });
    const { sessionId } = await browser.send<{ sessionId: string }>("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    const tab = new Tab(browser, sessionId, targetId);
    await tab.send("Page.enable");
    await tab.send("Page.addScriptToEvaluateOnNewDocument", { source: STILL });
    return tab;
  }

  send<T = Record<string, unknown>>(method: string, params: object = {}): Promise<T> {
    return this.cdp.send<T>(method, params, this.session);
  }

  async eval<T>(expression: string): Promise<T> {
    const r = await this.send<Evaluated<T>>("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    const e = r.exceptionDetails;
    if (e) throw new Error(e.exception?.description ?? e.text);
    return r.result?.value as T;
  }

  async goto(href: string): Promise<void> {
    const loaded = this.cdp.once("Page.loadEventFired", this.session);
    await this.send("Page.navigate", { url: href });
    await loaded;
  }

  async view(v: (typeof VARIANTS)[number]): Promise<void> {
    await this.send("Emulation.setDeviceMetricsOverride", {
      width: v.width,
      height: v.height,
      deviceScaleFactor: 1,
      mobile: v.width < 600,
    });
    await this.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: v.scheme }],
    });
  }

  async ready(selector: string, ms = 20_000): Promise<void> {
    await until(
      () => this.eval<boolean>(`Boolean(document.querySelector(${JSON.stringify(selector)}))`),
      ms,
      async () => `never ready: ${selector} (${await this.eval<string>("location.href")})`,
    );
    await this.settle();
  }

  // two frames, then a moment for data that follows the first paint
  settle(ms = 400): Promise<unknown> {
    return this.eval(
      `new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, ${ms}))))`,
    );
  }

  async click(text: string | undefined, css: string | undefined): Promise<void> {
    const find = css
      ? `document.querySelector(${JSON.stringify(css)})`
      : `[...document.querySelectorAll("button,a,[role=tab],[role=menuitem],[role=option],[role=button],[role=radio],label")].find((e) => { const r = e.getBoundingClientRect(); const t = ${JSON.stringify(text)}; return r.width > 0 && r.height > 0 && ((e.getAttribute("aria-label") ?? "").trim() === t || e.textContent.trim() === t); })`;
    const box = await this.eval<{ x: number; y: number } | null>(
      `(() => { const e = ${find}; if (!e) return null; e.scrollIntoView({ block: "nearest" }); const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`,
    );
    if (!box) throw new Error(`nothing to click: ${css ?? text}`);
    const at = { ...box, button: "left", clickCount: 1 };
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", ...at });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", ...at });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...at });
  }

  async press(key: string): Promise<void> {
    const named = KEYS[key];
    const base = named
      ? { key, code: named.code, windowsVirtualKeyCode: named.vk, text: named.text }
      : { key, text: key };
    await this.send("Input.dispatchKeyEvent", { type: "keyDown", ...base });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }

  async close(): Promise<void> {
    await this.cdp.send("Target.closeTarget", { targetId: this.target });
  }
}

async function signIn(
  browser: Cdp,
  ctx: string,
  url: string,
  body: () => Promise<{ code: string } | { token: string }>,
): Promise<void> {
  const tab = await Tab.open(browser, ctx);
  try {
    await tab.goto(`${url}/api/health`);
    const status = await tab.eval<number>(
      `fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: ${JSON.stringify(JSON.stringify(await body()))} }).then((r) => r.status)`,
    );
    if (status !== 204) throw new Error(`sign in: ${status}`);
  } finally {
    await tab.close();
  }
}

function fill(text: string, ids: Record<string, string>): string {
  return text.replaceAll(/\{(\w+)\}/g, (_, k: string) => ids[k] ?? "");
}

async function shoot(
  browser: Cdp,
  ctx: string,
  url: string,
  state: State,
  ids: Record<string, string>,
): Promise<void> {
  const tab = await Tab.open(browser, ctx);
  const shots: Record<string, Violation[]> = {};
  try {
    await tab.view(VARIANTS[0]);
    await tab.goto(url + fill(state.path, ids));
    const steps = state.steps ?? [];
    await tab.ready(steps.length ? "#main, main" : state.ready);
    for (const s of steps) {
      // oxlint-disable-next-line no-await-in-loop
      if (s.wait) await tab.ready(s.wait);
      // oxlint-disable-next-line no-await-in-loop
      if (s.click || s.css) await tab.click(s.click, s.css);
      // oxlint-disable-next-line no-await-in-loop
      if (s.press) await tab.press(s.press);
      // oxlint-disable-next-line no-await-in-loop
      if (s.type) await tab.send("Input.insertText", { text: fill(s.type, ids) });
      // oxlint-disable-next-line no-await-in-loop
      if (s.append) await appendFile(fill(s.append[0], ids), s.append[1]);
      // oxlint-disable-next-line no-await-in-loop
      await tab.settle(150);
    }
    if (steps.length) await tab.ready(state.ready);
    await tab.eval(axe);
    for (const v of VARIANTS) {
      if (v.name === "mobile" && state.dialog) continue;
      // oxlint-disable-next-line no-await-in-loop
      await tab.view(v);
      // oxlint-disable-next-line no-await-in-loop
      await tab.settle(300);
      // oxlint-disable-next-line no-await-in-loop
      const image = await tab.send<{ data: string }>("Page.captureScreenshot", { format: "png" });
      // oxlint-disable-next-line no-await-in-loop
      await Bun.write(join(out, `${state.name}.${v.name}.png`), Buffer.from(image.data, "base64"));
      // oxlint-disable-next-line no-await-in-loop
      const found = await tab.eval<Violation[]>(
        `axe.run(document, { resultTypes: ["violations"] }).then((r) => r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 20).map((n) => ({ target: n.target.join(" "), summary: n.failureSummary ?? "", html: n.html.slice(0, 240) })) })))`,
      );
      // A page wider than a phone's screen makes the phone zoom out until all of it is tiny;
      // within the app's frame, the main pane scrolls sideways and cuts the content off.
      // oxlint-disable-next-line no-await-in-loop
      const [page, main] = await tab.eval<[number, number]>(
        `(() => { const m = document.getElementById("main"); return [document.documentElement.scrollWidth, m ? m.scrollWidth - m.clientWidth : 0]; })()`,
      );
      if (page > v.width || main > 0) {
        found.push({
          id: "page-wider-than-screen",
          impact: "serious",
          help: `the page is ${page} px wide and its main pane ${main} px wider than shown, on a ${v.width} px screen`,
          nodes: [],
        });
      }
      shots[v.name] = found;
      const bad = found.filter((f) => GATE.has(f.impact ?? ""));
      gated += bad.length;
      const line = found.map((f) => `${f.id}(${f.impact})`).join(" ");
      console.log(`${bad.length ? "FAIL" : "ok  "} ${state.name}.${v.name} ${line}`);
    }
    report.states.push({ name: state.name, path: state.path, as: state.as ?? "operator", shots });
  } finally {
    await tab.close();
  }
}
