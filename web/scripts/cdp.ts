// A headless Chrome driven over the DevTools protocol, for scripts/shot.ts and scripts/bench.ts.

export interface Chrome {
  port: string;
  close(): Promise<void>;
}

export type Params = Record<string, unknown>;

export interface Cdp {
  // `session` targets a page attached with flatten; none talks to the socket's own target.
  send<T = Params>(method: string, params?: object, session?: string): Promise<T>;
  on(method: string, fn: (params: Params, session?: string) => void): () => void;
  once(method: string, session?: string): Promise<Params>;
  close(): void;
}

export function chromePath(): string {
  if (process.env["CHROME"]) return process.env["CHROME"];
  return process.platform === "darwin"
    ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    : "google-chrome";
}

export async function launch(path: string, profile: string, flags: string[] = []): Promise<Chrome> {
  const proc = Bun.spawn(
    [
      path,
      "--headless",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      `--user-data-dir=${profile}`,
      "--remote-debugging-port=0",
      ...flags,
      "about:blank",
    ],
    { stderr: "pipe", stdout: "ignore" },
  );
  try {
    return { port: await devtoolsPort(proc.stderr), close: () => stop(proc) };
  } catch (e) {
    await stop(proc);
    throw e;
  }
}

async function stop(proc: Bun.Subprocess): Promise<void> {
  proc.kill();
  const ended = await Promise.race([proc.exited.then(() => true), Bun.sleep(5000)]);
  if (!ended) proc.kill("SIGKILL");
}

async function devtoolsPort(stderr: ReadableStream<Uint8Array>): Promise<string> {
  const decoder = new TextDecoder();
  const reader = stderr.getReader();
  let seen = "";
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop
    const { done, value } = await reader.read();
    if (done) throw new Error(`chrome exited: ${seen}`);
    seen += decoder.decode(value);
    const port = /DevTools listening on ws:\/\/[^:]+:(\d+)/.exec(seen)?.[1];
    if (port) {
      void drain(reader);
      return port;
    }
  }
}

// Chrome blocks once its stderr pipe fills.
async function drain(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
  try {
    // oxlint-disable-next-line no-await-in-loop
    while (!(await reader.read()).done);
  } catch {
    // the pipe closed with Chrome
  }
}

export async function browserSocket(port: string): Promise<string> {
  const r = await fetch(`http://127.0.0.1:${port}/json/version`);
  return ((await r.json()) as { webSocketDebuggerUrl: string }).webSocketDebuggerUrl;
}

export async function pageSocket(port: string): Promise<string> {
  const targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as {
    type: string;
    webSocketDebuggerUrl: string;
  }[];
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("chrome has no page");
  return page.webSocketDebuggerUrl;
}

interface Message {
  id?: number;
  method?: string;
  params?: Params;
  sessionId?: string;
  result?: Params;
  error?: { message: string };
}

export async function connect(url: string): Promise<Cdp> {
  const ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let next = 0;
  const pending = new Map<number, { resolve: (r: Params) => void; reject: (e: Error) => void }>();
  const listeners = new Map<string, Set<(params: Params, session?: string) => void>>();
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(String(e.data)) as Message;
    if (m.id !== undefined) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) p?.reject(new Error(m.error.message));
      else p?.resolve(m.result ?? {});
    } else if (m.method) {
      for (const fn of listeners.get(m.method) ?? []) fn(m.params ?? {}, m.sessionId);
    }
  });
  const on = (method: string, fn: (params: Params, session?: string) => void) => {
    const set = listeners.get(method) ?? new Set();
    set.add(fn);
    listeners.set(method, set);
    return () => set.delete(fn);
  };
  return {
    send<T = Params>(method: string, params: object = {}, session?: string): Promise<T> {
      const id = ++next;
      ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
      return new Promise((resolve, reject) =>
        pending.set(id, { resolve: resolve as (r: Params) => void, reject }),
      );
    },
    on,
    once(method: string, session?: string): Promise<Params> {
      return new Promise((resolve) => {
        const off = on(method, (params, s) => {
          if (session !== undefined && s !== session) return;
          off();
          resolve(params);
        });
      });
    },
    close: () => ws.close(),
  };
}
