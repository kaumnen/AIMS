# Deploy

- [Docker Compose](#docker-compose)
- [Public address](#public-address)
- [More runners](#more-runners)
- [Proxies](#proxies)
- [Helper images and offline hosts](#helper-images-and-offline-hosts)
- [Audit log](#audit-log)

> [!WARNING]
> A runner has root on its host through the Docker socket. It also starts a privileged kernel tracer. Put runners on a dedicated machine or VM.

## Docker Compose

Compose runs the control plane, a runner and Caddy on one host:

```sh
curl -fsSLO https://github.com/kaumnen/AIMS/releases/latest/download/compose.yaml \
  -O https://github.com/kaumnen/AIMS/releases/latest/download/compose.public.yaml \
  -O https://github.com/kaumnen/AIMS/releases/latest/download/Caddyfile
echo "MODEL_API_KEY=<your key>" > .env
docker compose up -d
docker compose exec server aims open --no-open
```

The last command prints a sign-in link for http://localhost:8484.

| Service | Role |
| --- | --- |
| `init` | Makes the runner's token once, then exits. |
| `server` | Control plane and UI. No Docker socket. |
| `runner` | Runs the jobs. Mounts the Docker socket. |
| `caddy` | Serves the UI on 127.0.0.1:8484, or with TLS on a public address. |

`server` has a healthcheck on `GET /healthz`. That route needs no token and answers only `{"ok": true}`. `runner` and `caddy` start once it passes. `docker compose ps` shows its state.

`init`, `server` and `caddy` run with a read-only root filesystem, no capabilities and `no-new-privileges`. Caddy keeps `NET_BIND_SERVICE`, which its binary needs. They write only to their volumes and a 64 MiB `/tmp`. The server keeps the config the UI saves and its large temp files in the `server` volume.

> [!NOTE]
> Compose gives the runner 3 minutes to stop. In that time it seals its runs as cancelled. Then it removes its sandboxes, tracer and sampler. After 3 minutes Docker sends SIGKILL. Anything not yet removed keeps running until the next AIMS start on that host. `docker ps --filter label=aims.managed=1` lists it.

| `.env` variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_API_KEY` and other keys | | Model API keys for the runner |
| `AIMS_SITE` | `http://localhost:8484` | The address people open |
| `AIMS_AUDIT` | `auto` | The runner's audit: `auto`, `kernel` or `snapshot` |
| `AIMS_IMAGE` | The release's image, pinned by digest | The AIMS image |
| `AIMS_SUBNET` | `172.30.84.0/24` | The Compose network |

> [!NOTE]
> Keep the named volumes from `compose.yaml`. The server runs as uid 10001. A new named volume takes that owner from the image. A host directory does not.

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
uv tool install aims-bench -c https://github.com/kaumnen/AIMS/releases/download/v<version>/constraints.txt
export AIMS_TOKEN=<runner token> MODEL_API_KEY=<your key>
aims runner --server https://<your AIMS_SITE>
```

The same runner as a container:

```sh
docker run -d --name aims-runner --restart unless-stopped --user root --stop-timeout 180 \
  -v /var/run/docker.sock:/var/run/docker.sock -v aims-runner:/data \
  --env-file runner.env \
  ghcr.io/kaumnen/aims:<version> runner --server https://<your AIMS_SITE>
```

`runner.env` holds `AIMS_TOKEN` and the model keys. `--stop-timeout 180` gives the runner the same 3 minutes to stop as Compose. Docker's default is 10 seconds.

## Proxies

Runners, the CLI and model calls go through the proxy in `HTTPS_PROXY`, `HTTP_PROXY` or `ALL_PROXY`, except to hosts, domains and networks in `NO_PROXY`. Loopback always goes direct. A private CA can come from `SSL_CERT_FILE`, or from the runner's `--tls-ca`, which wins over it.

## Helper images and offline hosts

A runner needs two helper images: `aims-toolbox` and `aims-tracer`. It pulls them from GHCR by the digests the release pinned. Print the tags and digests of the version you run:

```sh
uvx --from aims-bench==<version> python -c "from aims.sandbox import images; print(images.published())"
```

| Setting | Effect |
| --- | --- |
| No `AIMS_IMAGE_REGISTRY` | Pulls from `ghcr.io/kaumnen` |
| `AIMS_IMAGE_REGISTRY=registry.example.com/aims` | Pulls from your mirror |
| `AIMS_IMAGE_REGISTRY=` (empty) | Never pulls. Builds both images on the runner. |

A mirror must hold byte for byte copies, for example made with `crane copy`. A rebuilt image has another digest, so the runner would not use it.

A host without network access can load the images instead. On a machine with the same CPU architecture:

```sh
docker pull ghcr.io/kaumnen/aims-tracer@sha256:<digest>
docker tag ghcr.io/kaumnen/aims-tracer@sha256:<digest> aims-tracer:<tag>
docker save -o aims-tracer.tar aims-tracer:<tag>
```

Do the same for `aims-toolbox`. Then run `docker load -i aims-tracer.tar` on the offline host. A runner uses a local image with the right tag without pulling.

Scenario images build from their Dockerfiles, which often need the network. Run `aims scenario check` on a connected host first. Then move the `aims-env:<hash>` images it built with `docker save` and `docker load`.

A scenario image that no run used in 30 days is removed when `aims run`, `aims runner` or a self-test starts. So are the helper images of other AIMS versions. A runner that stays up checks again every hour. Images a container uses always stay. `keep_days` under `[images]` in a config file changes the 30 days. On an offline host, set it high enough to keep the images you loaded. `aims images prune` removes these images now. Add `--dry-run` to only list them.

## Audit log

The control plane keeps an audit log of every change sent to it and every failed sign-in. Operators read it under **Settings** > **Audit** or with `aims audit`. Rows stay 90 days. `keep_days` under `[audit]` changes that. 0 keeps them all. [SECURITY.md](../SECURITY.md#audit-log) says what a row holds.
