// Screenshots of the running UI through the DevTools protocol: `bun scripts/shot.ts shots.json`.
// Chrome's own --screenshot waits for the network to settle, which never happens with the live
// stream open. Restart `aims ui` after `bun run build`: it reads asset names once at start.
//
// shots.json: { "chrome": path, "profile": dir, "shots": [{ "url", "out", "scheme"?: "light" |
// "dark", "width"?, "height"?, "wait"? ms, "eval"? JS run before the shot, "hover"? selector }] }

interface Shot {
  url: string;
  out: string;
  scheme?: "light" | "dark";
  width?: number;
  height?: number;
  wait?: number;
  eval?: string;
  hover?: string;
}

interface Config {
  chrome: string;
  profile: string;
  shots: Shot[];
}

interface Reply {
  id?: number;
  method?: string;
  result?: {
    data?: string;
    result?: { value?: unknown };
    exceptionDetails?: { text: string; exception?: { description?: string } };
  };
}

const path = process.argv[2];
if (!path) throw new Error("usage: bun scripts/shot.ts shots.json");
const config = (await Bun.file(path).json()) as Config;

const chrome = Bun.spawn(
  [
    config.chrome,
    "--headless",
    "--disable-gpu",
    "--hide-scrollbars",
    `--user-data-dir=${config.profile}`,
    "--remote-debugging-port=0",
    "about:blank",
  ],
  { stderr: "pipe", stdout: "ignore" },
);

try {
  const port = await devtoolsPort(chrome.stderr);
  const targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as {
    type: string;
    webSocketDebuggerUrl: string;
  }[];
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("chrome has no page");
  const cdp = await connect(page.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  // one page, one shot at a time
  // oxlint-disable-next-line no-await-in-loop
  for (const shot of config.shots) await take(cdp, shot);
} finally {
  chrome.kill();
}

async function devtoolsPort(stderr: ReadableStream<Uint8Array>): Promise<string> {
  const decoder = new TextDecoder();
  let seen = "";
  for await (const chunk of stderr) {
    seen += decoder.decode(chunk);
    const port = /DevTools listening on ws:\/\/[^:]+:(\d+)/.exec(seen)?.[1];
    if (port) return port;
  }
  throw new Error(`chrome exited: ${seen}`);
}

async function connect(url: string) {
  const ws = new WebSocket(url);
  await new Promise((resolve) => ws.addEventListener("open", resolve, { once: true }));
  let next = 0;
  const pending = new Map<number, (reply: Reply) => void>();
  const waiting = new Map<string, (() => void)[]>();
  ws.addEventListener("message", (e) => {
    const reply = JSON.parse(String(e.data)) as Reply;
    if (reply.id !== undefined) pending.get(reply.id)?.(reply);
    if (reply.method) {
      for (const fn of waiting.get(reply.method) ?? []) fn();
      waiting.delete(reply.method);
    }
  });
  return {
    send(method: string, params: object = {}): Promise<Reply> {
      const id = ++next;
      ws.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve) => pending.set(id, resolve));
    },
    once(method: string): Promise<void> {
      return new Promise((resolve) =>
        waiting.set(method, [...(waiting.get(method) ?? []), resolve]),
      );
    },
  };
}

async function take(cdp: Awaited<ReturnType<typeof connect>>, shot: Shot): Promise<void> {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: shot.width ?? 1280,
    height: shot.height ?? 720,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await cdp.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: shot.scheme ?? "light" }],
  });
  const loaded = cdp.once("Page.loadEventFired");
  await cdp.send("Page.navigate", { url: shot.url });
  await loaded;
  await Bun.sleep(shot.wait ?? 2000);
  if (shot.eval) {
    const r = await cdp.send("Runtime.evaluate", { expression: shot.eval, awaitPromise: true });
    const error = r.result?.exceptionDetails;
    if (error) throw new Error(`${shot.out}: ${error.exception?.description ?? error.text}`);
    await Bun.sleep(600);
  }
  if (shot.hover) {
    const expression = `JSON.stringify(document.querySelector(${JSON.stringify(shot.hover)})?.getBoundingClientRect() ?? null)`;
    const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
    const box = JSON.parse(String(r.result?.result?.value)) as DOMRect | null;
    if (!box) throw new Error(`${shot.out}: nothing matches ${shot.hover}`);
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await Bun.sleep(800);
  }
  const image = await cdp.send("Page.captureScreenshot", { format: "png" });
  if (!image.result?.data) throw new Error(`${shot.out}: no screenshot`);
  await Bun.write(shot.out, Buffer.from(image.result.data, "base64"));
  console.log(`wrote ${shot.out}`);
}
