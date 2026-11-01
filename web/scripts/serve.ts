// A control plane on seeded runs, for scripts/bench.ts and scripts/pass.ts: seeds with
// scripts/seed.py, starts `aims ui` without a runner and ships the sealed runs with `aims push`.

import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export const ROOT = resolve(import.meta.dir, "../..");

export interface Seeded {
  run: string;
  events: number;
  steps: number;
  runs: number;
  needle: string;
  score: string;
  review: string;
  fork: string;
  selftest: string;
  cases: string[];
  check: string;
  live: string;
}

export interface Plane {
  url: string;
  // where seed.py wrote the runs, and the logs
  dir: string;
  env: Record<string, string>;
  seeded: Seeded;
  // the local operator's token; none without auth
  operator: string | null;
  stop(): Promise<void>;
}

export interface PlaneOptions {
  // reuse this directory's seeded runs, or seed into it; it is kept
  dir?: string | undefined;
  // the control plane's state directory; by default <dir>/cp
  serverDir?: string | undefined;
  auth?: boolean;
  // where `aims ui` runs: its project's scenarios and config are the ones served
  cwd?: string;
  env?: Record<string, string>;
  prefix: string;
}

export function spawn(cmd: string[], cwd: string, env: Record<string, string>, log: string) {
  return Bun.spawn(cmd, {
    cwd,
    env: { ...process.env, ...env },
    stdout: Bun.file(log),
    stderr: Bun.file(`${log}.err`),
  });
}

export async function run(cmd: string[], cwd: string, env: Record<string, string>, log: string) {
  const proc = spawn(cmd, cwd, env, log);
  if ((await proc.exited) !== 0)
    throw new Error(`${cmd.join(" ")} failed: ${await Bun.file(`${log}.err`).text()}`);
  return Bun.file(log).text();
}

export async function stop(proc: Bun.Subprocess): Promise<void> {
  proc.kill("SIGTERM");
  const ended = await Promise.race([proc.exited.then(() => true), Bun.sleep(10_000)]);
  if (!ended) proc.kill("SIGKILL");
}

interface ServerJson {
  url: string;
  operator?: string;
}

async function up(info: string, log: string): Promise<ServerJson> {
  const file = Bun.file(info);
  for (let i = 0; i < 600; i++) {
    // server.json can be half written
    // oxlint-disable-next-line no-await-in-loop
    const json = await file.json().then(
      (j: ServerJson) => j,
      () => null,
    );
    // with auth on, health answers a caller without a token 401: up all the same
    const health =
      json &&
      fetch(`${json.url}/api/health`).then(
        (r) => r.ok || r.status === 401,
        () => false,
      );
    // oxlint-disable-next-line no-await-in-loop
    if (json && (await health)) return json;
    // oxlint-disable-next-line no-await-in-loop
    await Bun.sleep(100);
  }
  throw new Error(`aims ui did not start: ${await Bun.file(`${log}.err`).text()}`);
}

export async function plane(o: PlaneOptions): Promise<Plane> {
  const dir = o.dir ? resolve(o.dir) : await mkdtemp(join(tmpdir(), o.prefix));
  const env = {
    ...o.env,
    AIMS_SERVER_DIR: o.serverDir ? resolve(o.serverDir) : join(dir, "cp"),
  };
  const runs = join(dir, "runs");
  // each written once its step succeeds; a failed step runs again next time
  const seededMark = join(dir, "seeded");
  const pushedMark = join(dir, "pushed");
  let server: Bun.Subprocess | undefined;
  const close = async () => {
    if (server) await stop(server);
    if (!o.dir) await rm(dir, { recursive: true, force: true });
  };
  try {
    if (!(await Bun.file(seededMark).exists())) {
      await rm(runs, { recursive: true, force: true });
      await mkdir(dir, { recursive: true });
      const seed = ["uv", "run", "--project", ROOT, "python", join(ROOT, "scripts/seed.py"), runs];
      await run(seed, dir, {}, join(dir, "seed.log"));
      await Bun.write(seededMark, "");
    }
    const seeded = JSON.parse(await Bun.file(join(dir, "seed.log")).text()) as Seeded;
    const info = join(env.AIMS_SERVER_DIR, "server.json");
    await rm(info, { force: true });
    const ui = ["uv", "run", "--project", ROOT, "aims", "ui", "--no-runner", "--no-open"];
    if (!o.auth) ui.push("--no-auth");
    server = spawn([...ui, "--port", "0"], o.cwd ?? dir, env, join(dir, "ui.log"));
    const json = await up(info, join(dir, "ui.log"));
    if (!(await Bun.file(pushedMark).exists())) {
      const push = ["uv", "run", "--project", ROOT, "aims", "push", runs];
      await run(push, dir, env, join(dir, "push.log"));
      await Bun.write(pushedMark, "");
    }
    return {
      url: json.url,
      dir,
      env,
      seeded,
      operator: o.auth ? (json.operator ?? null) : null,
      stop: close,
    };
  } catch (e) {
    await close();
    throw e;
  }
}
