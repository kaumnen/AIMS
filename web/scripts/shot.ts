// Screenshots of the running UI through the DevTools protocol: `bun scripts/shot.ts shots.json`.
// Chrome's own --screenshot waits for the network to settle, which never happens with the live
// stream open. Restart `aims ui` after `bun run build`: it reads asset names once at start.
//
// shots.json: { "chrome"?: path, "profile": dir, "shots": [{ "url", "out", "scheme"?: "light" |
// "dark", "width"?, "height"?, "wait"? ms, "eval"? JS run before the shot, "hover"? selector }] }

import { type Cdp, chromePath, connect, launch, pageSocket } from "./cdp";

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
  chrome?: string;
  profile: string;
  shots: Shot[];
}

interface Evaluated {
  result?: { value?: unknown };
  exceptionDetails?: { text: string; exception?: { description?: string } };
}

const path = process.argv[2];
if (!path) throw new Error("usage: bun scripts/shot.ts shots.json");
const config = (await Bun.file(path).json()) as Config;

const chrome = await launch(config.chrome ?? chromePath(), config.profile);
try {
  const cdp = await connect(await pageSocket(chrome.port));
  await cdp.send("Page.enable");
  // one page, one shot at a time
  // oxlint-disable-next-line no-await-in-loop
  for (const shot of config.shots) await take(cdp, shot);
  cdp.close();
} finally {
  await chrome.close();
}

async function take(cdp: Cdp, shot: Shot): Promise<void> {
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
    const r = await cdp.send<Evaluated>("Runtime.evaluate", {
      expression: shot.eval,
      awaitPromise: true,
    });
    const error = r.exceptionDetails;
    if (error) throw new Error(`${shot.out}: ${error.exception?.description ?? error.text}`);
    await Bun.sleep(600);
  }
  if (shot.hover) {
    const expression = `JSON.stringify(document.querySelector(${JSON.stringify(shot.hover)})?.getBoundingClientRect() ?? null)`;
    const r = await cdp.send<Evaluated>("Runtime.evaluate", { expression, returnByValue: true });
    const box = JSON.parse(String(r.result?.value)) as DOMRect | null;
    if (!box) throw new Error(`${shot.out}: nothing matches ${shot.hover}`);
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await Bun.sleep(800);
  }
  const image = await cdp.send<{ data?: string }>("Page.captureScreenshot", { format: "png" });
  if (!image.data) throw new Error(`${shot.out}: no screenshot`);
  await Bun.write(shot.out, Buffer.from(image.data, "base64"));
  console.log(`wrote ${shot.out}`);
}
