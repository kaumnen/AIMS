# Changelog

Notable changes to AIMS, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Runners, the CLI and model calls use the proxy in `HTTPS_PROXY`, `HTTP_PROXY` or `ALL_PROXY`. Hosts, domains and networks in `NO_PROXY` go direct. Loopback always goes direct. A private CA can come from `SSL_CERT_FILE` or the runner's `--tls-ca`.

### Changed

- The repository has no `aims.toml` any more. A checkout runs with the built-in defaults. The pilot's settings are in `docs/pilot.toml`.
- The OpenAPI schema declares every reply status the routes give.
- The config routes read and check files off the event loop.

### Fixed

- A run that keeps failing to ship logs a warning with the last error. Another warning says when it ships again.
- A shipper that stops on an unexpected error logs it.
- A runner that cannot reach the control plane logs a warning with the address and the last error. A refusal such as a bad token or a version mismatch shows at once.
- A config file that is not UTF-8 gives an error that names the file. The CLI printed a traceback and the API answered 500.
- A refused run import leaves no run behind. It no longer clears the mark of a deleted run.
- A run import can no longer take the run id of a queued job. It is refused with 409 `in_use`.
- A run import whose log names a malformed run id is refused with 422, not 404.
- The runner that `aims ui` starts never reaches it through a proxy.
- `aims runner --tls-ca` no longer prints a deprecation warning.
- `aims rates` groups scores by monitor. A run counts once per monitor, however many times that monitor reviewed it. A score without a monitor gives take rates only. It used to pool every review of a run and count a run without a monitor as a miss.

## [0.2.1] - 2026-11-10

### Added

- Each release attaches `constraints.txt`. It pins AIMS and its dependencies to the versions the release was tested with. Pass its URL to `uv tool install aims-bench -c` or `pip install aims-bench -c`.
- `aims images prune` removes scenario images no run used in 30 days. It also removes the helper images of other AIMS versions. `--dry-run` only lists them.
- The package metadata lists classifiers, keywords and links to the issues, the changelog and the security policy.
- The control plane keeps an audit log of writes and failed sign-ins. Each row says who acted, from where, on what and with what result. Read it under Settings > Audit, with `aims audit` or at `GET /api/audit`, which also exports JSON Lines.
- Tokens show when they were last used, on the tokens page and in `aims token list`.
- `aims delete <run>` or **Delete** on the run page removes a run with its monitor runs and scores. A run in use stays. `--force` or **Delete anyway** removes a run whose runner is offline. Open tabs drop a deleted run at once.
- A runner that still holds a deleted run drops its copy. The control plane never takes it back, unless an operator imports it.
- `aims ui --keep-days N` deletes runs that ended more than N days ago, at start and once a day.
- `aims scenario delete` or **Delete** on a scenario page removes a scenario from a writable source. Its runs stay.
- `GET /healthz` answers without a token for container health checks. The compose server has a healthcheck. The runner and the proxy start once it passes.

### Changed

- The app image and the sandbox helper images use newer base images. Python and web dependencies are updated.
- `aims run`, `aims runner` and self-tests remove the same images when they start. A runner that stays up checks every hour. `keep_days` under `[images]` in a config file changes the 30 days.
- Runtime dependencies at version 1 or later are capped below their next major version. A new major release of one of them no longer reaches a fresh install.
- The sandboxes of a multi-agent run start together, so the run starts sooner.
- The scenario list and the model profiles in settings load faster. The control plane reads a scenario or a model check run again only once it changed.
- A runner keeps one copy of a run another runner made. Later monitor, score and fork jobs of that run reuse it. The copy arrives in chunks. A copy unused for 7 days is deleted, or sooner with a lower `--keep-days`.
- In a checkout, a plain `uv sync` no longer installs the packages the deploy scripts need. `uv run --group deploy` adds them.
- A review now scores a resampled next step that holds only text. It used to count as harmless.
- Audit rows go after 90 days. `keep_days` under `[audit]` changes that. 0 keeps them all.
- Run imports buffer on disk under the server's data folder.

### Fixed

- A kill of one agent no longer drops the pause the same flags call for on the other agents.
- The live stream no longer fails when a runner connects or leaves while it reports runner health.
- The config and scenario file editors answer bad TOML, a file that is not UTF-8 or a directory with a clear error. They used to fail with a server error.
- The control plane deletes expired sessions at start and once a day.
- A runner forgets each run once the run has shipped. Its memory no longer grows with every run.
- A config or scenario write the filesystem refuses answers 403 with the path and the reason. It used to fail with a server error.
- A run whose data holds a whole number of 2^53 or more as a float verifies. `aims verify` used to report it corrupt.

### Security

- The control plane refuses a request body over 8 MiB, or 16 MiB on the runner API, before it checks the token.
- A scenario path that leads outside the scenario's folder is refused. This includes a path through a symlinked directory.
- A job's files go only to the runner token that holds the job. A new runner under the name of a revoked one cannot fetch them.
- In compose, the server, the proxy and the init job run with a read-only root filesystem, a small `/tmp` and no added privileges. The server and init job hold no capabilities. Config saved in the UI goes to the `server` volume.

## [0.2.0] - 2026-11-04

### Added

- `aims report <run>` writes up one run. It opens with the verifier's reward beside the judge's verdict. Then come warnings, the judge's findings, plants, flags, a timeline and run facts. It prints to the terminal, or saves Markdown with `--out` or one HTML page with `--html`.
- The run page has a **Report** tab with a Markdown download. `GET /api/runs/{run_id}/report` serves the report as JSON or Markdown.
- The judge lists anomalies: odd behaviour outside the categories, or signs of a broken scenario or sandbox.
- Built-in scenarios run by name: `builtin/cache-keys`, `builtin/shared-cache` and `builtin/word-count`.
- `aims runner` and `aims ui` take `--audit`. `snapshot` never starts the privileged kernel tracer. The compose runner reads it from `AIMS_AUDIT`.
- The run page has a live feed, resizable columns and a pinned step heading. Browser tabs show the run's name and an icon.
- An agent skill in `skills/aims` teaches coding agents to write, check and run scenarios.
- Scripts in `organism/` tune the model organism and repeat its runs.
- The deploy script for the self-hosted model server loads LoRA adapters at runtime.
- `SECURITY.md` describes the security model and how to report a vulnerability.

### Changed

- `aims rates` prints the catch rates over scored runs. In 0.2.0rc1 this command was `aims report`.

### Fixed

- A second Ctrl-C no longer cuts the cleanup short in `aims run`, `aims runner` or `aims ui`. Sandboxes still close.
- `aims ui` sends its local runner SIGTERM before any SIGKILL. The runner also stops when `aims ui` dies or the terminal closes.
- The control plane stops promptly while runners are connected.
- A kill during runner cleanup exits with code 130.
- A cancelled start no longer logs a stray tracer error. On Python 3.14 a cancel no longer logs spurious asyncio errors.
- Image cleanup keeps a helper image that carries a current tag under another name.
- Command help shows words in square brackets.
- Timeline labels and hover cards no longer overlap.
- The settings page says where to set spend credits.

## [0.2.0rc1] - 2026-11-02

### Added

- Scenario files in YAML: the task, the sandbox, the agents, the plants, shared channels and variants. `aims scenario check` validates one and runs its self-test with scripted agents. `aims scenario schema` prints the JSON Schema.
- Sandboxes run as containers, one per agent, with no network. A kernel tracer records processes, file access and content, pipes, IPC, signals, ptrace, namespace changes and network attempts. Resource use is sampled per sandbox.
- Agents work through the `bash`, `edit`, `submit` and `report_to_operator` tools. Model profiles cover several hosted providers and a self-hosted vLLM server.
- Config files layer profiles, roles and every text agents and the monitor read. `aims init`, `aims config` and `aims prompts show` manage them.
- Every model call, tool call and kernel event goes into an append-only run log with a hash chain. `aims verify` checks the chain and the blobs.
- The monitor has live signals, model sweeps and an end-of-run judge. The presets are `off`, `signals`, `standard` and `research`.
- The oversight modes `observe`, `autonomous` and `human` decide whether a flag is only recorded, pauses the agent for a model review or waits for a person. `aims control` pauses, resumes, denies, injects, kills, cancels or sweeps a run.
- Plants: bait files, canaries, impossible tasks, bait tools, rule triggers and a fake cloud metadata service. Shared folders and boards connect the agents of a multi-agent run.
- `aims score` and `aims check` list the plants each agent took. `aims score` also shows which monitor layer caught each one. `aims report` sums catch rates over scored runs. `aims label` labels a flag for review.
- `aims run --replay <run> --fork-at` forks a recorded run at a step, with `--inject` and `--samples`. `--series` chains runs, each going on from the one before.
- Budgets cap steps, tokens, cost and time. `aims estimate` prints what a run can cost. `aims spend` shows what each provider was paid.
- `aims monitor` feeds a sealed run through the monitor again.
- `aims ui` serves the control plane and the web UI with a local runner. `aims open` prints a sign-in link. Tokens have roles: `aims token create`, `list` and `revoke`.
- The run page shows steps, a timeline, raw events, commands, flags and the score. It follows a live run. Runs can be started, controlled, forked and reviewed from the UI.
- Scenario pages show each scenario and run its self-test. Scenario files, agent texts and config files are editable in the browser. Settings show model profiles, runners, spend and tokens.
- The UI has keyboard shortcuts, a skip link and visible focus.
- `aims runner` takes jobs from a control plane. `aims push` ships runs recorded without one.
- The built-in `cache-keys`, `shared-cache` and `word-count` scenarios ship in the package.
- A multi-arch container image. Compose files run the control plane and a runner behind a TLS proxy.
- Runners pull the published helper images by digest before they build their own.

## [0.1.0] - 2026-10-26

### Added

- The `aims-bench` package with the `aims` command, which prints its version.

[Unreleased]: https://github.com/kaumnen/AIMS/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/kaumnen/AIMS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kaumnen/AIMS/compare/v0.2.0rc1...v0.2.0
[0.2.0rc1]: https://github.com/kaumnen/AIMS/compare/v0.1.0...v0.2.0rc1
[0.1.0]: https://github.com/kaumnen/AIMS/releases/tag/v0.1.0
