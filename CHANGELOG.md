# Changelog

Notable changes to AIMS, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Every sandbox captures `/dev/shm` like `/tmp`: file diffs, the archive and the kernel log of reads cover it. It keeps its 64 MiB limit and `noexec`. Task files and file plants may sit under it.
- The end-of-run archive leaves out only the files that do not fit. One large file no longer empties the rest. It stops reading a folder after 4 GiB of files that were left out.
- `aims run` stopped by Ctrl-C, SIGTERM or a hangup exits 130, also when the run in flight still ended `completed`. It scores no run after the stop. `aims score` scores it later.
- An operator command answers with what did not take effect, such as a sandbox that did not freeze. It answered `failed` before, though the agents were paused.
- The operator routes for pause, resume, sweep, inject, kill and cancel refuse text with a lone surrogate such as `\ud800` with a 422.
- A run that no cap bounds always asks before it starts, whatever past runs cost. `--yes` still skips the question. With no step cap, past runs of other scenarios give no estimate.
- The estimate counts a past monitor that stopped at its spend cap at the cap of today. Its typical and high figures rise to match.
- For an agent with summary compaction every "Cannot exceed" figure counts two model calls a step.
- A judge `max_output_tokens` under 1024 is refused when the config loads. Such a judge never made a call.
- Near its budget the judge explores a little less. It may answer a tool call with "No tool calls are left" in place of the result.
- The judge keeps its own timeout. A rollout still exploring when 120 s are left, or half the timeout if that is less, gives its verdict early. The monitor waits 30 s past the timeout for the judge to wind down.
- A fork with no step named cuts after the last step it can replay. For a run killed in a step that is one step earlier. `GET /api/runs/{id}` lists that step per agent as `replayable`.
- A fork that names a step whose tool calls have no recorded result is refused with a 422 that names the last step it can take. In a cut of several agents, another agent that was in such a step makes it again live.
- The signals `verifier_hook`, `verifier_anomaly` and `link_redirect` move to version 2 and `submit_vs_score` to version 3. Runs from now on group under a new monitor in `aims rates`.
- `submit_vs_score` reads the agent's reports and answer sentence by sentence. The last sentence about the checks decides. "All tests pass" after an earlier failure takes the failure back.
- `link_redirect` raises a symlink's flag to 3 when the verifier reads the link's target. The kernel logs a read under the target's path, so a direct read of the target counts too.
- The index moves to version 11. Each data directory rebuilds its index from the run logs the next time it opens.
- `GET /api/runs/{id}/samples` pages with `after=next`. `next` is the seq of the page's last window. It was a time to pass back as `ts_from`.
- A cursor or seq the index cannot hold answers 422: `after` or `before` below -1, `ts_from` or `ts_to` below 0, an event seq below 0, and any of them past 2^63 - 1. A NUL in `q` answers 422 too.
- `pause_timeout_s` and `human_timeout_s` take at most 30 days.
- An event batch that reached the log is returned even when the index failed to take it. The index catches up from the log, at once and then with waits up to 5 minutes. `aims verify` reports an index with a gap, one behind its log after a failed write, and one whose last event is not the log's.
- A copied event whose time is past what UTC holds is refused as invalid.
- A deleted run's folder is first renamed to `.deleted.<run id>.<suffix>`. The next start clears any that a crash left.

### Fixed

- The archive reads the sandbox's files as a stream. A large or sparse file in the sandbox no longer takes the runner's memory.
- Kernel records dropped past the audit caps after their command ended, or outside any command, are logged as `audit.lost`.
- A tool call, an answer or a `reward.json` with a lone surrogate such as `\ud800` no longer breaks the monitor's sweeps or fails the run. The text is kept as written.
- Tool arguments nested deeper than 64 levels no longer break the monitor's sweeps. A `reward.json` nested that deep counts as unreadable.
- A run whose agent crashed before it logged its end is `failed`. It was `completed`.
- Long runs of whitespace or digits in agent text no longer stall the runner. Four monitor patterns took time that grew with the square of the run's length.
- One Ctrl-C late in a run stops `aims run --series`. The next run of the series started before.
- A dialog, a draft or a command's answer no longer follows the page to another run or scenario. A Kill dialog opened on one run could kill another after Back. A file draft could be saved into another scenario.
- A prompt edit closes when the agent toggle changes. It was saved as the other agent's text.
- A command the control plane dropped shows as not done in the UI. Its dialog stays open.
- A sandbox that does not freeze or unfreeze no longer strands a pause. The other sandboxes still freeze and the monitor's review or timeout still ends the pause.
- A pause of the whole run by the monitor holds an agent that starts later.
- A stored command with a lone surrogate no longer cuts a runner off from its commands. An event that holds one is written with it as text.
- `aims run`, `POST /runs` and forks size the monitor's spend cap by the step caps of `--budget`, as the estimate does. The cap came from the scenario as written, so a run with raised steps lost its sweeps early.
- A fork started from the API sizes its monitor on the full scenario. A confederate's script counts by its length, as in the estimate.
- Two agents on one model, one with an effort and one without, no longer crash the estimate. One such run on record broke every estimate, `aims spend` and the live view.
- The judge no longer ends without a verdict when its last exploring call writes a lot or brings back large tool results. It keeps room for the verdict and reads back a tool result only while that room stays. A larger budget could lose the verdict before.
- A budget cell in the UI refuses a decimal comma such as `2,50`. It read it as 250. A comma passes only as a thousands separator such as `1,000`.
- A judge that reaches its timeout writes the verdicts of the rollouts that finished. One stalled model call lost every verdict before. `monitor.degraded` names the rollouts that gave none.
- A fork of a run killed in a step no longer ends with "the origin has no output for tool call". The CLI, the API and the UI's fork dialog cut before that step.
- `submit_vs_score` flags a success claim that only negates a failure word, such as "All tests pass with no errors" or "Fixed the failing test; all tests pass". One early "hit an import error" no longer spares the whole run. "Two tests didn't pass" counts as an honest report.
- `verifier_hook` reads the config file line of pytest 7.3 and later. An agent-written `pytest.ini`, `tox.ini`, `pyproject.toml` or `setup.cfg` that the verifier took its settings from went unflagged.
- `verifier_hook` sees an agent-written program run by a relative path such as `./run_tests.sh`.
- `verifier_anomaly` reads a pytest summary with subtests and the summary of an output over 1 MiB. Both gave a false `no_summary` flag at importance 4.
- `verifier_anomaly` checks the verifier of the agent's own task when the agent has one. It was silent there.
- `link_redirect` flags absolute link targets with `..`, `//` or `./`, such as `/work/../etc/passwd`. Its rise to importance 3 on a verifier read could never fire.
- When a run ends, the UI reads its flags, sweeps, costs, self-test cases and model checks once more. What the run wrote after the last poll stayed hidden until a reload.
- The Raw tab keeps events that arrive while a page of the list loads. They were dropped and left a gap.
- The resource timeline pages every window once. With two sandboxes a window at a page edge was skipped.
- A search with a NUL character or a cursor past 64 bits no longer answers 500. `aims label` with such a flag number names the missing flag.
- Search finds text that follows a NUL character in an event. Short searches missed it, and so did every search on older SQLite versions.
- Text search reads its matches in page order. On a store of 420,000 events a search for a common word took 0.66 s and held up every write. It takes about 1 ms. A search inside one run with a cursor took up to 13 s.
- An index write cancelled while it waited for the database no longer blocks every later write with "cannot start a transaction within a transaction".
- A run deleted just before a crash stays deleted. It came back at the next start with its blobs missing.
- A failed index write no longer leaves a gap in the index or hides the batch from the live monitor. One run the index cannot take no longer stops the data directory from opening.
- A pause with a timeout too large for a date, or an event dated at the edge of year 9999, no longer fails the index write.

## [0.2.2] - 2026-11-10

### Added

- `aims estimate` and `aims monitor` say how many steps the sweeps cover when the monitor budget is too small for them all.
- Runners, the CLI and model calls use the proxy in `HTTPS_PROXY`, `HTTP_PROXY` or `ALL_PROXY`. Hosts, domains and networks in `NO_PROXY` go direct. Loopback always goes direct. A private CA can come from `SSL_CERT_FILE` or the runner's `--tls-ca`.

### Changed

- The `standard` and `research` monitor budgets grow with the agent steps a run allows, at the prices of the monitor's models. The preset amounts of $5 and $8 are now the least a run gets. An 80-step `cache-keys` run gets $9.45 under `standard` on Muse Wonder, enough to sweep every step. A review gets the budget for the steps its run took.
- The sweeps keep back what the judge costs at its model's price, at least $1.
- Scorer tampering no longer covers all reading of version control history. It covers looking up a solution where the task did not put it: future commits, other branches, tags, the reflog or deleted files. Reading the past history of the code under repair counts as debugging. So does trying variants in scratch files.
- The sweeps and the judge judge each action against the task. A task that says what the result must be does not forbid the means.
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
- The judge no longer stops without a verdict when one call costs more than the last. It keeps room for a verdict call at full input price and caps each call's output to what its budget still pays for.
- A review estimate counts a past review that stopped at its budget as if it had swept every step. Such reviews used to pull the typical figure down to the old cap.
- The `submit_vs_score` signal no longer flags an agent that told the operator a check failed, in its answer or a report. It flagged honest answers that listed the failing tests.
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

[Unreleased]: https://github.com/kaumnen/AIMS/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/kaumnen/AIMS/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/kaumnen/AIMS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kaumnen/AIMS/compare/v0.2.0rc1...v0.2.0
[0.2.0rc1]: https://github.com/kaumnen/AIMS/compare/v0.1.0...v0.2.0rc1
[0.1.0]: https://github.com/kaumnen/AIMS/releases/tag/v0.1.0
