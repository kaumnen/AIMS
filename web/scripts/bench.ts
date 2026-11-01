// Interaction benchmark on a seeded control plane: `bun run bench [--out file] [--cpu 4]`.
// Seeds a 20k-event run and 1000 small runs with scripts/seed.py, serves them with `aims ui`,
// times each interaction in headless Chrome and checks the ceilings in bench.json.

import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";

import { browserSocket, type Cdp, chromePath, connect, launch } from "./cdp";

interface Ceiling {
  ms?: number;
  stat: "median" | "p95" | "max";
  what: string;
}

interface Seeded {
  run: string;
  events: number;
  steps: number;
  runs: number;
  needle: string;
}

const { values: opts } = parseArgs({
  options: {
    out: { type: "string", default: "bench-results.json" },
    samples: { type: "string", default: "5" },
    cpu: { type: "string", default: "1" },
    // reuse this directory's seeded runs, or seed into it; it is kept
    dir: { type: "string" },
  },
});
const SAMPLES = Number(opts.samples);
const CPU = Number(opts.cpu);
const ROOT = resolve(import.meta.dir, "../..");
const ceilings = (await Bun.file(join(import.meta.dir, "../bench.json")).json()) as Record<
  string,
  Ceiling
>;
const samples = new Map<string, number[]>();
const record = (name: string, ms: number) => samples.set(name, [...(samples.get(name) ?? []), ms]);

// Installed in every document before the app's own scripts.
const PROBE = `(() => {
  const b = (window.__bench = { tasks: [], input: 0 });
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) b.tasks.push([e.startTime, e.duration]);
  }).observe({ type: "longtask", buffered: true });
  for (const type of ["keydown", "pointerdown"])
    addEventListener(type, (e) => { b.input = e.timeStamp; }, { capture: true });
  // The time 'test' first holds, taken after the frame that shows it.
  b.until = (test, timeout = 60000) => new Promise((resolve, reject) => {
    let done = false;
    const check = () => {
      if (done) return;
      let ok = false;
      try { ok = Boolean(test()); } catch {}
      if (!ok) return;
      done = true;
      mo.disconnect();
      clearTimeout(timer);
      requestAnimationFrame(() => setTimeout(() => resolve(performance.now())));
    };
    const mo = new MutationObserver(check);
    mo.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    const timer = setTimeout(() => { done = true; mo.disconnect(); reject(new Error("timed out")); }, timeout);
    check();
  });
  b.arm = (test) => { b.pending = b.until(test); };
  b.longest = (from, to) =>
    Math.max(0, ...b.tasks.filter(([s, d]) => s + d >= from && s <= to).map(([, d]) => d));
  b.q = (s) => document.querySelector(s);
  b.selected = () => b.q('[role=option][aria-selected=true][id^="step-"]')?.id;
  // The chosen step's transcript, not the last one's, is on screen.
  b.stepShown = () => {
    const n = b.q('[role=option][aria-selected=true][id^="step-"] span')?.textContent;
    const h2 = b.q("article h2")?.textContent ?? "";
    const head = "Step " + n;
    const next = h2.charAt(head.length);
    return n !== undefined && h2.startsWith(head) && !(next >= "0" && next <= "9");
  };
  b.scroller = (s) => b.q(s)?.parentElement;
  // Scrolls 'el' to its end a frame at a time, until it stops growing for a second.
  b.scroll = (el, px = 800) => new Promise((resolve) => {
    const from = performance.now();
    let still = 0, height = -1;
    const frame = () => {
      const end = el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
      still = end && el.scrollHeight === height ? still + 1 : 0;
      height = el.scrollHeight;
      if (still >= 60) return resolve([from, performance.now()]);
      el.scrollTop += px;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
})();`;

const STEP_READY = `__bench.stepShown()`;
const RUNS_READY = `document.querySelectorAll('[aria-label=Runs] [role=rowgroup]:nth-child(2) [role=row]').length >= 2`;
// One panel shows once the switch is done; the old one stays for a moment beside the new.
const SCORE_READY = `(() => {
  const shown = [...document.querySelectorAll("[role=tabpanel]")].filter((p) => !p.hidden);
  const text = shown.length === 1 ? shown[0].innerText.trim() : "";
  return __bench.q("[role=tab][aria-selected=true]")?.textContent === "Score" && text && !text.includes("Loading");
})()`;
const tab = (name: string) =>
  `[...document.querySelectorAll("[role=tab]")].find((t) => t.textContent === "${name}")`;
const RAW_READY = `__bench.q('[aria-label=Events] [role=option]')`;

class Page {
  private constructor(
    private cdp: Cdp,
    private session: string,
    private target: string,
    private context: string,
  ) {}

  static async open(cdp: Cdp): Promise<Page> {
    const { browserContextId } = await cdp.send<{ browserContextId: string }>(
      "Target.createBrowserContext",
    );
    const { targetId } = await cdp.send<{ targetId: string }>("Target.createTarget", {
      url: "about:blank",
      browserContextId,
    });
    const { sessionId } = await cdp.send<{ sessionId: string }>("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    const page = new Page(cdp, sessionId, targetId, browserContextId);
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    await page.send("Runtime.addBinding", { name: "__benchReady" });
    await page.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await page.send("Emulation.setFocusEmulationEnabled", { enabled: true });
    if (CPU > 1) await page.send("Emulation.setCPUThrottlingRate", { rate: CPU });
    await page.send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE });
    return page;
  }

  send<T = Record<string, unknown>>(method: string, params: object = {}): Promise<T> {
    return this.cdp.send<T>(method, params, this.session);
  }

  // Opens `url` and resolves with the ms from navigation start until `ready` holds.
  async goto(url: string, ready: string): Promise<number> {
    const { identifier } = await this.send<{ identifier: string }>(
      "Page.addScriptToEvaluateOnNewDocument",
      {
        source: `__bench.until(() => (${ready})).then((t) => __benchReady(String(t)), (e) => __benchReady("error " + e.message))`,
      },
    );
    const called = new Promise<string>((done) => {
      const off = this.cdp.on("Runtime.bindingCalled", (p, s) => {
        if (s !== this.session || p["name"] !== "__benchReady") return;
        off();
        done(String(p["payload"]));
      });
    });
    await this.send("Page.navigate", { url });
    const payload = await called;
    await this.send("Page.removeScriptToEvaluateOnNewDocument", { identifier });
    if (payload.startsWith("error")) throw new Error(`${url}: ${payload}`);
    return Number(payload);
  }

  async eval<T>(expression: string): Promise<T> {
    const r = await this.send<{
      result: { value?: T };
      exceptionDetails?: { text: string; exception?: { description?: string } };
    }>("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      const why = r.exceptionDetails.exception?.description ?? r.exceptionDetails.text;
      throw new Error(`${why}\n  in ${expression.slice(0, 300)}`);
    }
    return r.result.value as T;
  }

  // Runs `act` and resolves with the ms from its input event until `test` holds.
  async timed(test: string, act: () => Promise<unknown>): Promise<number> {
    await this.eval(`__bench.arm(${test}); __bench.input = 0`);
    await act();
    return this.eval<number>(`__bench.pending.then((t) => t - __bench.input)`);
  }

  async key(key: string, code: string, keyCode: number): Promise<void> {
    const base = { key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode };
    await this.send("Input.dispatchKeyEvent", {
      type: "keyDown",
      ...base,
      ...(key.length === 1 ? { text: key } : {}),
    });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }

  // `el` is an expression for the element.
  async box(el: string): Promise<{ x: number; y: number; width: number; height: number }> {
    const box = await this.eval<DOMRect | null>(
      `(() => { const r = (${el})?.getBoundingClientRect(); return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null; })()`,
    );
    if (!box) throw new Error(`nothing matches ${el}`);
    return box;
  }

  async click(el: string): Promise<void> {
    const b = await this.box(el);
    const at = { x: b.x + b.width / 2, y: b.y + b.height / 2, button: "left", clickCount: 1 };
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", ...at });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", ...at });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...at });
  }

  // The longest task while `el` (an expression) scrolls to its end.
  scroll(el: string): Promise<number> {
    return this.eval<number>(
      `__bench.scroll(${el}).then(([from, to]) => __bench.longest(from, to))`,
    );
  }

  async close(): Promise<void> {
    await this.cdp.send("Target.closeTarget", { targetId: this.target });
    await this.cdp.send("Target.disposeBrowserContext", { browserContextId: this.context });
  }
}

function spawn(cmd: string[], cwd: string, env: Record<string, string>, log: string) {
  return Bun.spawn(cmd, {
    cwd,
    env: { ...process.env, ...env },
    stdout: Bun.file(log),
    stderr: Bun.file(`${log}.err`),
  });
}

async function run(cmd: string[], cwd: string, env: Record<string, string>, log: string) {
  const proc = spawn(cmd, cwd, env, log);
  if ((await proc.exited) !== 0)
    throw new Error(`${cmd.join(" ")} failed: ${await Bun.file(`${log}.err`).text()}`);
  return Bun.file(log).text();
}

async function stop(proc: Bun.Subprocess): Promise<void> {
  proc.kill("SIGTERM");
  const ended = await Promise.race([proc.exited.then(() => true), Bun.sleep(10_000)]);
  if (!ended) proc.kill("SIGKILL");
}

async function serve(tmp: string, env: Record<string, string>): Promise<string> {
  const info = Bun.file(join(env["AIMS_SERVER_DIR"] ?? "", "server.json"));
  const up = async () => {
    // server.json can be half written
    const url = await info.json().then(
      (j: { url: string }) => j.url,
      () => null,
    );
    const ok =
      url !== null &&
      (await fetch(`${url}/api/health`).then(
        (r) => r.ok,
        () => false,
      ));
    return ok ? url : null;
  };
  for (let i = 0; i < 600; i++) {
    // oxlint-disable-next-line no-await-in-loop
    const url = await up();
    if (url) return url;
    // oxlint-disable-next-line no-await-in-loop
    await Bun.sleep(100);
  }
  throw new Error(`aims ui did not start: ${await Bun.file(join(tmp, "ui.log.err")).text()}`);
}

function stat(values: number[], how: Ceiling["stat"]): number {
  const sorted = values.toSorted((a, b) => a - b);
  if (how === "max") return sorted.at(-1) ?? 0;
  const at = how === "median" ? 0.5 : 0.95;
  return sorted[Math.min(sorted.length - 1, Math.ceil(at * sorted.length) - 1)] ?? 0;
}

async function runsList(cdp: Cdp, url: string, last: boolean): Promise<void> {
  const page = await Page.open(cdp);
  try {
    record("runs.open", await page.goto(`${url}/runs`, RUNS_READY));
    if (!last) return;
    // A task planted here must show, or no long task below means anything. Tasks a DevTools
    // evaluate runs do not count as long tasks, so it runs from a timer.
    const planted = await page.eval<number>(
      `new Promise((r) => { const t = performance.now(); setTimeout(() => { const s = performance.now(); while (performance.now() - s < 120); }); setTimeout(() => r(__bench.longest(t, performance.now())), 300); })`,
    );
    if (planted < 100) throw new Error(`long tasks do not register: a 120 ms task read ${planted}`);
    record("runs.scroll", await page.scroll(`__bench.scroller("[aria-label=Runs]")`));
    const rows = await page.eval<number>(
      `Number(__bench.q("[aria-label=Runs]").getAttribute("aria-rowcount")) - 1`,
    );
    if (rows < seeded.runs) throw new Error(`runs list scrolled to ${rows} of ${seeded.runs} runs`);
  } finally {
    await page.close();
  }
}

async function runPage(cdp: Cdp, url: string, first: number, last: boolean): Promise<void> {
  const page = await Page.open(cdp);
  try {
    record("run.open", await page.goto(`${url}/runs/${seeded.run}?step=${first}`, STEP_READY));
    record("score.open", await page.timed(`() => ${SCORE_READY}`, () => page.click(tab("Score"))));
    if (!last) return;
    await page.click(tab("Steps"));
    await page.eval(`__bench.until(() => ${STEP_READY})`);
    for (let i = 0; i < 50; i++) {
      const test = `(() => { const prev = __bench.selected(); return () => __bench.selected() !== prev && __bench.stepShown(); })()`;
      // oxlint-disable-next-line no-await-in-loop
      record("steps.j", await page.timed(test, () => page.key("j", "KeyJ", 74)));
    }
    record("steps.scroll", await page.scroll(`__bench.scroller("[aria-label=Steps]")`));
    const height = await page.eval<number>(`__bench.scroller("[aria-label=Steps]").scrollHeight`);
    if (height < seeded.steps * 28) throw new Error(`step list ends at ${height} px`);
    const b = await page.box(`__bench.q("[aria-label=Timeline] svg")`);
    const y = b.y + b.height / 2;
    const from = await page.eval<number>("performance.now()");
    const at = (x: number) => ({ x, y, button: "left", buttons: 1, clickCount: 1 });
    await page.send("Input.dispatchMouseEvent", { type: "mousePressed", ...at(b.x + 2) });
    for (let i = 1; i <= 120; i++) {
      // oxlint-disable-next-line no-await-in-loop
      await page.send("Input.dispatchMouseEvent", {
        type: "mouseMoved",
        ...at(b.x + 2 + ((b.width - 4) * i) / 120),
      });
      // oxlint-disable-next-line no-await-in-loop
      await Bun.sleep(16);
    }
    await page.send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      ...at(b.x + b.width - 2),
    });
    await Bun.sleep(500);
    const reached = await page.eval<number>(
      `Number(__bench.q("[aria-label=Timeline]").getAttribute("aria-valuenow"))`,
    );
    if (reached < seeded.steps * 0.9)
      throw new Error(`the drag ended at step ${reached} of ${seeded.steps}`);
    record("scrubber.drag", await page.eval<number>(`__bench.longest(${from}, performance.now())`));
  } finally {
    await page.close();
  }
}

async function rawEvents(cdp: Cdp, url: string, matches: number, last: boolean): Promise<void> {
  const page = await Page.open(cdp);
  const lastRow = `[...document.querySelectorAll("[aria-label=Events] [role=option]")].some((r) => r.firstElementChild?.textContent === "${seeded.events - 1}")`;
  const list = `__bench.scroller("[aria-label=Events]")`;
  try {
    await page.goto(`${url}/runs/${seeded.run}?tab=raw`, RAW_READY);
    const t0 = await page.eval<number>("performance.now()");
    await page.eval(`__bench.scroll(${list}, 1e9)`);
    await page.eval(`__bench.until(() => ${lastRow})`);
    record("raw.end.cold", (await page.eval<number>("performance.now()")) - t0);
    if (last) {
      await page.eval(`${list}.scrollTop = 0`);
      await Bun.sleep(300);
      record("raw.scroll", await page.scroll(list));
      for (let i = 0; i < SAMPLES; i++) {
        // oxlint-disable-next-line no-await-in-loop
        await page.eval(`${list}.scrollTop = 0`);
        // oxlint-disable-next-line no-await-in-loop
        await Bun.sleep(300);
        // oxlint-disable-next-line no-await-in-loop
        const ms = await page.eval<number>(
          `(() => { const t = performance.now(); const p = __bench.until(() => ${lastRow}); const el = ${list}; el.scrollTop = el.scrollHeight; return p.then((e) => e - t); })()`,
        );
        record("raw.end", ms);
      }
    }
    await page.click(`__bench.q("#raw-search")`);
    await page.send("Input.insertText", { text: seeded.needle });
    const done = `() => __bench.q("[aria-label=Events]") && [...document.querySelectorAll("span")].some((s) => s.textContent === "${matches} events")`;
    record("raw.search", await page.timed(done, () => page.key("Enter", "Enter", 13)));
  } finally {
    await page.close();
  }
}

const tmp = opts.dir ? resolve(opts.dir) : await mkdtemp(join(tmpdir(), "aims-bench-"));
const env = { AIMS_SERVER_DIR: join(tmp, "cp") };
let seeded: Seeded;
let server: Bun.Subprocess | undefined;
let chrome: Awaited<ReturnType<typeof launch>> | undefined;
let failed = 0;
try {
  const runs = join(tmp, "runs");
  const pushed = join(tmp, "push.log");
  const fresh = !(await Bun.file(pushed).exists());
  if (fresh) {
    await mkdir(tmp, { recursive: true });
    const seed = ["uv", "run", "--project", ROOT, "python", join(ROOT, "scripts/seed.py"), runs];
    await run(seed, tmp, {}, join(tmp, "seed.log"));
  }
  seeded = JSON.parse(await Bun.file(join(tmp, "seed.log")).text()) as Seeded;
  await rm(join(env.AIMS_SERVER_DIR, "server.json"), { force: true });
  server = spawn(
    [
      "uv",
      "run",
      "--project",
      ROOT,
      "aims",
      "ui",
      "--no-auth",
      "--no-runner",
      "--no-open",
      "--port",
      "0",
    ],
    tmp,
    env,
    join(tmp, "ui.log"),
  );
  const url = await serve(tmp, env);
  if (fresh) await run(["uv", "run", "--project", ROOT, "aims", "push", runs], tmp, env, pushed);
  const api = (path: string) => fetch(`${url}/api/runs/${seeded.run}${path}`).then((r) => r.json());
  const steps = (await api("/steps?limit=1")) as { items: { seq: number }[] };
  const first = steps.items[0]?.seq ?? 0;
  const found = (await api(`/events?limit=200&q=${seeded.needle}`)) as { items: unknown[] };

  chrome = await launch(chromePath(), join(tmp, "chrome"));
  const cdp = await connect(await browserSocket(chrome.port));
  const version = await cdp.send<{ product: string }>("Browser.getVersion");
  // The first round warms the server and the disk cache and is not counted.
  for (let i = 0; i <= SAMPLES; i++) {
    const last = i === SAMPLES;
    // oxlint-disable-next-line no-await-in-loop
    await runsList(cdp, url, last);
    // oxlint-disable-next-line no-await-in-loop
    await runPage(cdp, url, first, last);
    // oxlint-disable-next-line no-await-in-loop
    await rawEvents(cdp, url, found.items.length, last);
    if (i === 0) samples.clear();
  }
  cdp.close();

  const results: Record<string, unknown> = {};
  for (const [name, c] of Object.entries(ceilings)) {
    const values = samples.get(name) ?? [];
    const value = Math.round(stat(values, c.stat) * 10) / 10;
    const ok = values.length > 0 && (c.ms === undefined || value < c.ms);
    if (!ok) failed++;
    results[name] = { ...c, value, ok, samples: values.map((v) => Math.round(v * 10) / 10) };
    const limit = c.ms === undefined ? "" : ` < ${c.ms}`;
    const shown = c.stat === "max" && value === 0 ? "none over 50" : value;
    console.log(
      `${ok ? "ok  " : "FAIL"} ${name.padEnd(14)} ${c.stat.padEnd(6)} ${shown} ms${limit}`,
    );
  }
  await Bun.write(
    opts.out,
    `${JSON.stringify({ chrome: version.product, cpu: CPU, seeded, results }, null, 2)}\n`,
  );
  console.log(`wrote ${opts.out}`);
} finally {
  await chrome?.close();
  if (server) await stop(server);
  if (!opts.dir) await rm(tmp, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
