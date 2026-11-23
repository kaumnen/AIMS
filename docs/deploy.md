# Deploy

- [Docker Compose](#docker-compose)
- [Host kernel](#host-kernel)
- [Public address](#public-address)
- [More runners](#more-runners)
- [Proxies](#proxies)
- [Helper images and offline hosts](#helper-images-and-offline-hosts)
- [Audit log](#audit-log)

> [!WARNING]
> A runner has root on its host through the Docker socket. It also starts a kernel tracer whose capabilities amount to root on the host. Put runners on a dedicated machine or VM.

## Docker Compose

Compose runs the control plane, a runner and Caddy on one host:

```sh
curl -fsSLO https://github.com/kaumnen/AIMS/releases/latest/download/compose.yaml \
  -O https://github.com/kaumnen/AIMS/releases/latest/download/compose.public.yaml \
  -O https://github.com/kaumnen/AIMS/releases/latest/download/Caddyfile
echo "EXAMPLE_API_KEY=<your key>" > .env
echo "AIMS_KEYS=EXAMPLE_API_KEY=https://api.example.com/v1" >> .env
docker compose up -d
docker compose exec server aims open --no-open
```

The last command prints a sign-in link for http://localhost:8484. AIMS ships no models. Add them under **Settings** > **Models** > **Add a model**, or in the project config under **Settings** > **Config**. [Models](models.md) has the fields. The runner reads the keys and `AIMS_KEYS` from `.env`. After you edit `.env`, run `docker compose up -d` again. `docker compose restart` keeps the old values.

| Service | Role |
| --- | --- |
| `init` | Makes the runner's token once, then exits. |
| `server` | Control plane and UI. No Docker socket. |
| `runner` | Runs the jobs. Mounts the Docker socket. |
| `caddy` | Serves the UI on 127.0.0.1:8484, or with TLS on a public address. |

- `server` has a healthcheck on `GET /healthz`. That route needs no token and answers only `{"ok": true}`. `runner` and `caddy` start once it passes. `docker compose ps` shows its state.
- `init`, `server` and `caddy` run with a read-only root filesystem, no capabilities and `no-new-privileges`. Caddy keeps `NET_BIND_SERVICE`, which its binary needs. They write only to their volumes and a 64 MiB `/tmp`.
- The `server` volume holds the config the UI saves and the server's large temp files. The project's `aims.toml` is `/data/aims.toml` there.
- One `aims ui` uses a data dir at a time. A second one on the same data dir exits with an error that names the first.
- Only the control plane writes the `runs` folder of its data dir. A command that writes runs, such as `aims run` or `aims runner`, exits with an error there. A command that only reads runs, such as `aims report`, works there.

> [!NOTE]
> Compose gives the runner 3 minutes to stop. It seals its runs as cancelled, then removes its sandboxes, tracer and sampler. After 3 minutes Docker sends SIGKILL. Anything not yet removed keeps running until the next AIMS start on that host. `docker ps --filter label=aims.managed=1` lists it.

| `.env` variable | Default | Meaning |
| --- | --- | --- |
| The variables named in `api_key_env` | | The model API keys, for the runner |
| `AIMS_KEYS` | | The URLs each key may go to, such as `EXAMPLE_API_KEY=https://api.example.com/v1`. See [Keys](models.md#keys). |
| `AIMS_SITE` | `http://localhost:8484` | The address people open |
| `AIMS_AUDIT` | `auto` | The runner's audit: `auto`, `kernel` or `snapshot` |
| `AIMS_IMAGE` | The release's image, pinned by digest | The AIMS image |
| `AIMS_SUBNET` | `172.30.84.0/24` | The Compose network |

> [!NOTE]
> Keep the named volumes from `compose.yaml`. The server runs as uid 10001. A new named volume takes that owner from the image. A host directory does not.

## Host kernel

The kernel tracer runs on the kernel of the runner's Docker host. It needs:

- Linux 6.11 or newer
- An x86_64 or aarch64 CPU
- BTF type data in `/sys/kernel/btf/vmlinux`
- cgroup v2
- On a host with AppArmor, tracefs mounted at `/sys/kernel/tracing`, as systemd does. Without AppArmor the tracer mounts it itself.
- Rootful Docker, since the tracer needs `SYS_ADMIN`, `BPF` and `PERFMON` in the host's PID namespace
- No kernel lockdown in `confidentiality` mode

The runner checks the first five when it starts the tracer. To check a host yourself:

```sh
docker info --format '{{.KernelVersion}} {{.Architecture}} {{.CgroupVersion}}'
ls /sys/kernel/btf/vmlinux
sudo ls /sys/kernel/tracing/available_filter_functions
```

| Host | Kernel | CPU | Kernel audit |
| --- | --- | --- | --- |
| Ubuntu 26.04 | 7.0 | x86_64 | Yes |
| Ubuntu 24.04 with the HWE kernel | 6.11, 6.14, 6.17, 7.0 | x86_64 | Yes |
| Debian 13 | 6.12 | x86_64 | Yes |
| Docker Desktop on a Mac with Apple silicon | 7.0 | aarch64 | Yes |
| Ubuntu 24.04 | 6.8 | x86_64 | No, the kernel is too old |

On each host marked Yes the full test suite passed with the tracer.

Kernels from 6.11 to 7.0 that the table does not list are untested. aarch64 is tested on 7.0 only. On a kernel newer than 7.0 the runner warns that the tracer was not tested there.

> [!TIP]
> Ubuntu 24.04 ships Linux 6.8. Its HWE kernel is new enough:
>
> ```sh
> sudo apt install linux-generic-hwe-24.04
> sudo reboot
> ```

Each AIMS process that runs sandboxes starts its own tracer. A runner starts one for all its runs. Each `aims run` and each self-test of `aims scenario check` starts one of its own. About 12 tracers fit on one host with Linux 7.0, since the kernel caps the probes on one kernel function or tracepoint. The limit can differ with the kernel version and CPU. A tracer past that fails to start. Its error says how many other AIMS tracers run there.

On a host that cannot run the tracer, the audit setting decides:

| `--audit` or `AIMS_AUDIT` | What happens |
| --- | --- |
| `auto` | The runner logs why and uses snapshot audit. It takes no kernel runs or self-tests. |
| `kernel` | The runner refuses to start. |
| `snapshot` | The tracer never starts. |

For example:

```text
kernel audit unavailable, using snapshot audit: needs Linux 6.11 or newer; the Docker host runs 6.8.0-147-generic
```

## Public address

Set `AIMS_SITE` in `.env` to a domain name or a public IP. Then add `compose.public.yaml`:

```sh
docker compose -f compose.yaml -f compose.public.yaml up -d
```

It opens ports 80 and 443. Caddy gets a certificate from Let's Encrypt.

## More runners

Make a runner token on the control plane:

```sh
docker compose exec server aims token create box2 --role runner
```

Then start a runner on another Docker host, with the server's version:

```sh
uv tool install aims-bench==<version>
export AIMS_TOKEN=<runner token> EXAMPLE_API_KEY=<your key>
export AIMS_KEYS=EXAMPLE_API_KEY=https://api.example.com/v1
aims runner --server https://<your AIMS_SITE>
```

The runner tells the control plane its `AIMS_KEYS` entries, never the keys. It gets only the jobs those entries allow.

Or run it as a container:

```sh
docker run -d --name aims-runner --restart unless-stopped --user root --stop-timeout 180 \
  -v /var/run/docker.sock:/var/run/docker.sock -v aims-runner:/data \
  --env-file runner.env \
  ghcr.io/kaumnen/aims:<version> runner --server https://<your AIMS_SITE>
```

`runner.env` holds `AIMS_TOKEN`, the model keys and `AIMS_KEYS`, without quotes. `docker run --env-file` keeps quotes as part of a value. `--stop-timeout 180` gives the runner the same 3 minutes to stop as Compose. Docker's default is 10 seconds.

> [!IMPORTANT]
> A runner stops when the control plane answers 401 or 403 five times in a row, as for a wrong or revoked token. It stops its runs as for a first Ctrl-C and exits 1 with the reason. A container with a restart policy starts again and stops the same way. Give it a valid token or remove it.

## Proxies

Runners, the CLI and model calls go through the proxy in `HTTPS_PROXY`, `HTTP_PROXY` or `ALL_PROXY`. Hosts, domains and networks in `NO_PROXY` go direct. Loopback always goes direct.

A private CA can come from `SSL_CERT_FILE` or `SSL_CERT_DIR`. For the control plane, a runner's `--tls-ca` wins over them.

The control plane itself calls these hosts, with no key and through the same proxy settings:

| Host | What reads it | When the host cannot reach it |
| --- | --- | --- |
| The server of a model you add | **Add a model** under **Settings** > **Models**, for the server's list of models. It refuses a host on a link-local or cloud metadata address ([SECURITY.md](../SECURITY.md#network)). | The draft goes on with a note. [`aims models add`](models.md#add-a-model) does the same from the CLI. |
| `raw.githubusercontent.com` | A run, fork, review, score or estimate reads [LiteLLM's price map](models.md#list-prices) when it calls a profile that names a `price_key` and sets no `price`. **Settings** > **Models** reads it for any profile with no `price`. **Add a model** reads it for each draft. A runner never reads it. | A kept copy serves with its date. Without one such a profile stays unpriced. Give it a `price` instead. |

- Nothing else changes when a lookup fails.
- A read of the price map waits 5 seconds at most. After a failed one the next try comes 10 minutes later.
- Compose keeps the copy on the server's volume, in `/data/cache`.

## Helper images and offline hosts

A runner needs two helper images: `aims-toolbox` and `aims-tracer`. Its first start pulls them from GHCR by the digests the release pinned. That is about 125 MB to download on amd64 or arm64. On disk they take about 500 MB.

Print the tags and digests of the version you run:

```sh
uvx --from aims-bench==<version> python -c "from aims.sandbox import images; print(images.published())"
```

| Setting | Effect |
| --- | --- |
| No `AIMS_IMAGE_REGISTRY` | Pulls from `ghcr.io/kaumnen` |
| `AIMS_IMAGE_REGISTRY=registry.example.com/aims` | Pulls from your mirror |
| `AIMS_IMAGE_REGISTRY=` (empty) | Never pulls. Builds both images on the runner. |

A mirror must hold byte for byte copies, for example made with `crane copy`. A rebuilt image has another digest, so the runner would not use it.

### Offline hosts

A host without network access can load the images instead. On a machine with the same CPU architecture:

```sh
docker pull ghcr.io/kaumnen/aims-tracer@sha256:<digest>
docker tag ghcr.io/kaumnen/aims-tracer@sha256:<digest> aims-tracer:<tag>
docker save -o aims-tracer.tar aims-tracer:<tag>
```

Do the same for `aims-toolbox`. Then run `docker load -i aims-tracer.tar` on the offline host. A runner uses a local image with the right tag without pulling.

Scenario images build from their Dockerfiles, which often need the network. Run `aims scenario check` on a connected host first. Then move the `aims-env:<hash>` images it built with `docker save` and `docker load`.

### Cleanup

`aims run`, `aims runner` and a self-test remove old images when they start. A runner that stays up checks again every hour. Images a container uses always stay.

| Image | Removed once unused for |
| --- | --- |
| A scenario image | 30 days. `keep_days` under `[images]` in a config file changes this, up to 36500. |
| A helper image of another AIMS version | 1 day |

On an offline host, set `keep_days` high enough to keep the scenario images you loaded. `aims images prune` removes these images now. Add `--dry-run` to only list them.

## Audit log

The control plane keeps an audit log of each change sent to its API, refused ones too. It also logs each failed sign-in. Runner traffic is not logged.

Operators read it under **Settings** > **Audit** or with `aims audit`. Rows stay 90 days. `keep_days` under `[audit]` changes that, up to 36500. 0 keeps them all. [SECURITY.md](../SECURITY.md#audit-log) says what a row holds.
