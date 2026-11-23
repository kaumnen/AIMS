# Changelog

Notable changes to AIMS, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

> [!IMPORTANT]
> AIMS now sends a model key only to the URLs that `AIMS_KEYS` lists for it. Set it before you upgrade, such as `AIMS_KEYS=EXAMPLE_API_KEY=https://api.example.com/v1`. Without it each call with a key fails with the entry to add. See Security below.

### Added

- `aims models add <model id> --base-url <url> --key-env <NAME>` drafts a profile for a model on any server. `--from <profile>` reaches the server as another profile does instead: its backend, URL, key env, headers, `extra` and other connection fields. The command reads the server's list of models without a key to check the id. The read refuses a host that resolves to a link-local or cloud metadata address. The command then goes on with a note. `--litellm <key>` picks the model's row in LiteLLM's price map for the price key, the context size and the output cap. Without it the notes name the rows that may fit. `--set key=value` sets any field to a TOML value. A dotted key sets a field in a table. `--unset key` removes a field. Then the command sends the model a few small requests, as `aims models check` does. It sets `reasoning` to what the model sent. When a test fails it writes nothing and exits 1. `--no-check` skips the check. The check is recorded as a run. The command prints the profile, or `--write` adds it to the project's `aims.toml` and refuses a name that is taken. `aims models list` and the starter `aims.toml` of `aims init` name the command.
- `aims models add --write --role <role>` also gives the new profile a role under `[roles]`. `--role` takes `agent`, `sweep` or `judge` and may repeat. A role the profile does not fit is refused: the sweep and judge roles need structured output. The file keeps its comments. The command says which profile had the role before.
- `GET /api/models/lookup` drafts a profile for operators as `aims models add` does. It takes the model id with `base_url` and `key_env`, or `from` with a profile. `name`, `litellm` and `backend` work as the flags do. The server reads that server's list of models with no key. It refuses a host that resolves to a link-local or cloud metadata address and goes on with a note. The answer holds the profile as TOML, the notes, the roles it fits and the LiteLLM rows that may price it. `ids` lists ids the server's list holds for what was typed. The lookup saves nothing. `POST /api/models/add` adds one profile's TOML to the project's `aims.toml` and refuses a name that is taken. By default it then queues a model check on a runner. Once the check passes the server sets the roles in `roles` and the reasoning the check saw, in one write. It writes nothing when the profile changed since the add. It leaves a role that someone gave another profile meanwhile. Edits elsewhere in the file do not stop it. `check: false` sets the roles with the profile. `measure: false` keeps the reasoning the TOML sets. Each profile in `GET /api/models` says in `add` why its check waits and how its newest add went. A `model.checked` event records what the plain test saw of the reasoning in `reasoning`. Each add leaves a `models.add` row in the audit log. The write after the check leaves one more.
- Settings > Models has an Add a model dialog for operators. It takes a base URL with an optional key env var, or the server of a profile, with the model id, as `aims models add` does. It shows the drafted profile with its notes, the ids on the server's list and the LiteLLM rows that may price it. One click drafts again with an id or a row. The profile text can be edited before the add. The dialog keeps the fields and the text while it is closed. Add and check saves the profile and queues its check. The roles you tick wait for a pass. The dialog follows the check to its end. Add without checking sets the roles at once. The reasoning the check saw replaces the drafted one unless the edited text sets its own. A profile's row says why its check waits and how its add went. A profile whose check failed can be removed from its row. The New run form and the line of unset roles link to the dialog.
- A profile with no `price` can name its row in LiteLLM's public price map with `price_key`, such as `price_key = "provider/model-name"`. A run, fork, review or score then gets that list price on the profile. `aims run`, `aims monitor`, `aims score` and the control plane lay it where they resolve the profiles, so a runner never reads the map. A call costs what the provider reported, else the list price. The agents' cost caps and the monitor's budget hold on that count, in a review too. The monitor's budget is sized at the list prices of its models. A `model.response` event records such a cost with `cost_source` `list`. The run records the key and the date of the copy in the profile's `listed`, which a config file may not set. A fork keeps the price its origin recorded. A fork of a run that recorded no price takes the price of the config's profile of the same name when it names the same model. Each such figure names its source, key and date: in the lines of `aims estimate`, `aims run`, `aims monitor` and `aims score`, in the app and in the new `listed` field of the estimate API. After the run, the run page and the report say how much of its cost is at a list price, with the key and the date of each. The run API gives that amount as `listed_usd`. The run detail lists it by profile in `listed`. A score run's `run.started` now records its classifier's profile. The map is read from GitHub only when a profile needs it. A copy is kept in the user cache folder, which `AIMS_CACHE_DIR` sets. It is read again after 24 hours. Compose keeps it on the server's volume. When GitHub does not answer, the copy serves at any age with its date. With no copy or a key the map lacks, the profile stays unpriced. The run starts as before. The estimate says why in `unlisted`.
- `aims models prices` shows each profile's price and where it comes from: its own `price`, the LiteLLM list price of its `price_key` with the key and date, or none. For a profile with no price it lists the rows of LiteLLM's price map that may price it, with their prices, to name one in its `price_key`. A row matches when its key is the profile's model id, ends in `/` and the id, or ends in the id's last part. So `openai/gpt-4o` finds `gpt-4o` and `azure/gpt-4o`. `--search <text>` lists the rows whose key holds the text. `aims models list` says where each price comes from without reading the map. In the app, Settings > Models has a Price column. Its Find a price key button lists the same rows and copies the `price_key` line. The new `GET /api/models/prices` answers the same. `GET /api/models` carries each profile's `price` and `price_key`. The map is read only for a profile with no price or for a search.
- `--budget run.monitor_cost_usd=3` caps the monitor's model spend for one run, as the scenario's `limits.monitor_cost_usd` does. `none` gives the monitor its preset's cap back. The New run and Fork forms have a Monitor row in Budget for it. The estimate counts the cap. A run's cost cap counts only the agents, so a run with `run cost $5` could pass $5 on the monitor's spend with no way to bound it from the form.
- Model calls on the `responses` backend carry a `prompt_cache_key`. Calls of one run from the same caller on the same model share a key, so the provider answers them from one prompt cache. On one provider's Responses API a repeated 11,000 token prefix was never cached without the key in a test of 4 calls. With it every call after the first was. A live `standard` monitor cost about $0.09 per agent step on one run for this reason. A `chat` profile sends the key with `cache_key = true`. `cache_key = false` turns it off. A `model.request` event records the key in `cache_key`. The recorded body leaves it out, so replays match as before.
- A model call costs what the provider reports when the answer's usage carries a `cost`. The profile's price is used when it carries none. A router sends one model to providers with different prices, so the price table gave $0.0000056 for a call that was billed $0.0000144 in a test. A call with your own provider key behind the router adds the provider's bill to the router's fee. A `model.response` event records where the cost comes from in `cost_source`, `provider`, `price` or `list`. The estimate counts a past call at the cost its provider reported. It prices the tokens of other past calls at the profile's price, as before. Four test calls through one router were billed 2.8 to 3.4 times the model's list price.
- A model response records who served it: the `provider` a router picked, the `model` the answer names and its `service_tier`. A router moves the calls of one run between providers with different prices, quantizations and caches. The log did not say which one answered. A step in the app names its provider. The report's run facts list the providers of each model with their answers. The live view notes it when a second provider answers a model. Only an answer that names its provider records one.
- A profile's new `headers` table adds HTTP headers to each request of the profile. The run records them with the profile, so keep secrets out of them. A header that AIMS sets itself or that carries a key, such as `Authorization`, is refused. A config layer or `extends` merges the table key by key. A header's name ignores case, so `x-title` in a layer replaces `X-Title` from the layer below.
- A `chat` answer's `reasoning_details` go back with the turn in the next request, unchanged. A router can return a model's signed or encrypted reasoning there. Without it the model lost its own reasoning between a tool call and the tool's result: in a test, models from three vendors dropped the plain `reasoning` text that AIMS sent back. A streamed answer's blocks are put together from their pieces. A provider that sends no such field gets the same requests as before. An answer whose blocks hold only a summary records `reasoning_kind` `summary`. It recorded `full`.
- A profile's new `retries.rate_limited` gives a call time to wait out a provider's rate limit. Retries of a rate-limit answer (HTTP 429) do not count toward `retries.attempts` while their wait ends within that many seconds of the call's first 429. It is 120 by default. The four attempts waited about 7 s in all, less than a per-minute window. A 429 retry with no wait still counts.
- The fork dialog lists under Models each profile the forks call, with what it does there, as the New run form does. Check checks them all. A fork that keeps the profile the run recorded calls it as recorded. When this config's profile of that name holds another model or other settings, the dialog says so, since the check tests this config's. The estimate API names such agents with the model they call in the new `recorded_models` field.
- `aims mcp` serves the control plane to an MCP client over stdio. Its tools are named after the CLI commands. They read runs, flags, steps, events, reports, the plants taken, rates, spend, estimates, scenarios, models and prompts. `--act` adds `run`, `fork`, `monitor`, `score` and `control`, which need an operator token. `control` pauses, resumes, kills or cancels a run and waits up to 10 s for the runner's answer. It resumes only an agent that an operator paused. A run, fork or monitor run whose estimate needs a yes asks the person through the client with the estimate in words, as `aims run` asks at a terminal. A client that cannot ask gets the estimate as an error and nothing starts. The server finds the control plane as `aims open` does. It needs the new `mcp` extra: `uv tool install 'aims-bench[mcp]'`. Deleting runs, tokens, config, models, scenario files, injects into a live run, denying a held tool call and labelling flags stay with the CLI and the UI.
- `GET /api/runs/{run_id}/check` lists the plants each agent of a sealed run took in `plants`, as `aims check` does. `gaps` says what the check could not count. It needs the operator role.
- `POST /api/runs/{run_id}/resume` takes `operator_only`. Such a resume lets go of an operator's pause only. The runner refuses it while the monitor holds an agent it names. An agent the monitor pauses while the resume is on its way stays paused. The `operator.action` event records it, so the run's holds and the monitor match the runner.
- Binds and listens on inet and inet6 addresses are recorded as `net.listen` events, with the address, the port and the protocol. A listen on a socket never bound has no port, as the kernel picks one. A unix socket's listen is an `ipc.event` of kind `listen`. Neither was recorded.
- Extended attribute changes are recorded as `file.xattr` events with the path, the attribute's name and the value's size. A call the kernel refused before its check is recorded with its `errno`, as on a read-only mount or for a `trusted.` attribute. That holds for 32-bit calls too. The file snapshots do not hold extended attributes, so nothing recorded them. The new `shared_xattr` signal flags one set in a folder shared with other agents as covert communication, at importance 3.
- The runner checks the Docker host when it starts the kernel tracer: Linux 6.11 or newer, an x86_64 or aarch64 CPU, BTF type data and cgroup v2. On `auto` audit a host that fails says why, such as `kernel audit unavailable, using snapshot audit: needs Linux 6.11 or newer; the Docker host runs 6.8.0-147-generic`. The runner then uses snapshot audit. It no longer starts a new tracer every minute. Settings > Runners shows the reason. `kernel` audit refuses to start. A kernel newer than 7.0 gets a warning, since the tracer was not tested there. The deploy docs list the tested hosts under Host kernel.
- A score's checks stop at a spend cap, $1 by default. `budget_usd` under the new `[score]` table in a config file changes it. `aims score --budget-usd`, `budget_usd` in `POST /api/runs/{run_id}/score` and its estimate, the MCP `score` tool and the Spend cap field of the Score tab's form change it for one score. Each call gets the output the cap leaves room for. A call that does not fit does not start. A failed call is tried again only when another full attempt fits, so a provider that bills the failed attempt cannot take the score past its cap. A failed attempt with no cost reported counts at what the call was planned to cost, against the cap only. A call with no room to be tried again stops the checks. The score then records a `score.degraded` event and `aims score` warns. The Score tab and the report say so. The agents it did not reach get no finding. A score job carries the cap in `budget_usd`. A score run records it in its `run.started` config.

### Changed

- A score asks before it spends past `confirm_above_usd`, as runs, forks and reviews do. `aims score` asks at a terminal and refuses without one unless `--yes` is given. `POST /api/runs/{run_id}/score` answers 409 `confirm` unless `confirm` is set. The MCP `score` tool asks the person through the client. The Score tab's form waits for a yes. A score started at any cost before. The score estimate gives `budget_usd`, `priced`, `total`, `confirm_above_usd` and `confirm`. Its ceiling is the lower of the spend cap and what the run's words allow, with every attempt the profile's `retries` give a call. The typical and high figures of past scores no longer pass it.
- The estimate of a run or a fork counts the score made after it seals: a Score line, with a `score` figure and `score_profile` in each part. Its cost goes into the total and the provider credits. Its ceiling is the score's spend cap. Its typical and high figures come from past scores of the same scenario, else of any. A run's yes covers its score, so the score after it asks nothing. `aims estimate --no-score`, `score=false` in the estimate API and `score: false` in the MCP `estimate` tool leave it out. The New run form estimates again when Score each run changes. `aims run` lays the list price of the score's classifier before it starts, so its score counts those calls. `score` in `POST /api/runs` defaults to true, as `aims run`, the New run form and the MCP `run` tool do.
- Every runtime dependency except `regex` has an upper bound below its next breaking release, such as `typer>=0.29.1,<0.30`. `regex` has none, since its version is a date. `uv tool install aims-bench` and `pip install aims-bench` take only versions inside the bounds. A bound moves once the dependency's new release passes the tests.
- `aims models check` with no names checks the profiles the roles and the `[monitor]` table name, each once. It checked only the agent role's profile. The `[monitor]` table counts whatever its preset, since `--monitor` can turn the monitor's models on. An unknown profile there is an error. In `extract` a run skips one, so the check notes it and goes on. The check notes the roles that are unset. With no profile named in either place it stops and says to name the profiles or set the roles. `POST /api/models/check` with no `profiles` checks the same profiles.
- The New run form lists under Models each profile the run calls, with what it does there: an agent, a sweep, the review, the judge, the extractor or the channel sweep. Check checks them all. The list follows the variant, profile and monitor picked. It listed only the agents' profiles. The estimate API names the profiles of each part in the new `agent_profiles` and `monitor_profiles` fields.
- Compose runs the server in `/data`, so the project's `aims.toml` is `/data/aims.toml` on the server's volume. Saving the project layer under Settings > Config failed with 403, since the server's root is read-only. `POST /api/models/add` saves to the same file.
- `aims scenario check` passes a scenario when a role it needs is unset. It prints a note with the fix for each agent and monitor that needs the role. The estimate waits until the roles are set. A profile that the scenario or `[monitor]` names and that does not exist is still an error. The new `--monitor` takes the presets of `aims run`, so `--monitor signals --profile p` checks the run you mean to make.
- The scenario editor's check gives an unset role as a note, as `aims scenario check` does. It also checks the monitor. `POST /api/scenarios/check` returns the notes in a new `notes` field. Its new `monitor` takes the presets of `aims run`. The check warned of the unset agent role at each edit and never checked the monitor. Saving a scenario's texts no longer warns of an unset role.
- The self-test needs no model. Each of its runs records the profile `scripted` for every agent, which names no model and is never called. It recorded the agent role's profile, or the one `--profile` named, though it never called it either. A fork of a self-test run needs a profile. `POST /api/scenarios/{source}/{name}/selftest` no longer takes `profile`.
- A score run records the checks it was asked for and why they did not run under `checks` in its `run.started` config. The Score tab and the report show the reason under the plants. `aims score --json` gives it as `checks_off`. A score job carries it in `checks_off`. A score whose checks were off by choice records no reason.
- Each unit of a score records in `reasoning` the richest reasoning its agent's answers carried: full, then summary, then none. The profile's declared `reasoning` does not change it. The last line of `aims rates` names it per arm. It named two models. The report names it per agent in a Reasoning recorded column, in the app too.
- For contributors: the screenshot pass reads its profiles from `web/shots/aims.toml`. It copied `docs/pilot.toml`. `scripts/seed.py` records runs of placeholder models. Its self-test cases record scripted agents that call no model, as real ones do.
- A score's awareness and answer checks run on the first profile in `[monitor] extract` whose weights no agent of the run runs, else on the sweep role. They ran on one of two built-in models. A cheap model in `[monitor] extract` keeps a score cheaper than the sweep model does. When no profile fits, the score runs without those checks. Its estimate says why and how to fix it, in `aims score`, in the app and in the new `checks_off` field of the estimate API. A profile in `[monitor] extract` must have structured output. A config that names one without it no longer loads. A scenario's `monitor: extract` is checked the same way before its run starts.
- A role under `[roles]` may be unset. What needs an unset role stops before any container starts and says how to fix it: set the role in `aims.toml`, give the run a profile for the agent role, or use a signals-only monitor for the sweep and judge roles. A set sweep or judge role must still name a profile with structured output. `aims models list`, `aims config --merged` and Settings > Models name the unset roles. The New run form asks for a profile when an agent names none and no profile has the agent role. `GET /api/models` gives an unset role as null. In a scenario's overview an agent with no profile to run has a null `profile`. The scenario list's new `needs_profile` says that a run must pick a profile. `aims scenario check` names an agent's missing profile once.
- The index moves to version 13. Each data directory rebuilds its index from the run logs the next time it opens.
- The New run, Fork and Review forms say that no cap bounds the cost when that is why they ask for a yes. They said the high estimate was above the limit, even when it was under it.
- For contributors: the tests sit in one folder per area under `tests/`. CI runs only the jobs a change needs, so a docs change runs no test and a web change runs no Python suite. A nightly run and each release run everything on Python 3.14 and 3.15. A pull request needs one check, `ci`. The new `CONTRIBUTING.md` has the rules.
- The monitor's budget counts a sweep at about twice the tokens it did: 25,500 input tokens per lane and agent step at the default settings, where it counted 11,000. A `standard` monitor on a run of 72 steps read 23,500, so a budget said to cover 80 steps stopped the sweeps near step 55. The count now follows the sweep's `every`, `window`, `window_steps` and `collapse_tokens`, since a sweep that runs each step reads the trajectory five times as often. A run with no `limits.monitor_cost_usd` gets about twice the cap for the same steps. The cap is the most the monitor may spend. A model with a lighter tokenizer spends about half of it.
- The estimate's warning that a monitor budget is too small goes by what the sweeps of past runs with the same monitor cost per step, as a review's estimate does. It used the prices alone.
- The sweeps leave the judge what it costs at the price of its model. They left it at least $1, so a monitor budget of $1 or less paid for no sweep, though the judge of one such run cost $0.06. A judge on a model with no price still gets $1.
- `aims ui --data-dir` keeps the local runner's runs in the `runner` folder inside that directory. The runner used its own folder under the home directory, so a new data directory filled with the runs of earlier sessions.
- The estimate leaves out a run that an operator killed, as it leaves out a cancelled one. It counted such a run as a whole run, which made the typical cost too low. A run that the monitor killed still counts.
- The estimate assumes that 50% of a monitor model's input is cached when no past call of that model is on record. It assumed 90%, as for an agent's model. A sweep repeats less of the call before it than an agent does.
- The Inject dialog keeps a typed message when it closes without sending. A click outside it, Escape or Close dropped the text.
- A command dialog shows only the answer to its own command. It showed the answer to the command before it, such as "Paused." inside the Cancel dialog.
- An error that a provider sends after HTTP 200 is a provider error with its code, such as `provider error 402: Insufficient credits`. A router answers 200 first when the provider behind it fails later. An unstreamed answer of this kind was reported as "the stream ended before the answer did". An answer that finished with `error` counted as an incomplete answer and went to the agent.
- A provider error with code 400, 401, 402, 403 or 404 is not retried, as an HTTP status of the same number is not. Every provider error was retried, so a key with no credits took every attempt of a call.
- A profile sends `parallel_tool_calls` only when it sets the field. The default was `false`, which went with every request that offered tools. A profile that leaves it out now lets the provider decide, so it sends no such field. Set `parallel_tool_calls = false` in it to keep the old request. `aims models check` warns of several calls in one turn only when the profile sets `false`.
- A run, fork, self-test or check that a runner takes may use a profile with `structured_output = false`. The runner failed such a job before it started when that profile came first in the job, with the error that role sweep needs structured output. A config file's sweep and judge roles still need structured output. The error for one that lacks it now names only that rule.
- A tab opens a live connection of its own when the one its tabs share neither opens nor fails within 10 seconds. A browser can let the shared worker ask for the stream and never answer it, so the run page stayed on Starting with 0 steps.
- The runs list, series page and models page read the server database with fewer, indexed queries. The server database moves to version 8. An older AIMS refuses the file.

### Removed

- The built-in model profiles and roles. AIMS ships no models. Set them under `[profiles]` and `[roles]` in `aims.toml`, or add one with `aims models add <model id> --base-url <url> --write --role agent`. A config that changed a built-in no longer loads: a profile that extends one, such as `extends = "meta-spark-1.3-contributor"`, a table that sets part of one, such as `[profiles."meta-spark-1.3-contributor".limits]`, or a role that names one. Write the whole profile from the fields it took and point the roles at it. Earlier releases hold the built-ins in `src/aims/models/builtins.toml`. A config that relied on the built-in roles needs `agent`, `sweep` and `judge` under `[roles]`, or runs with `--monitor signals` and a profile.
- The example results: `docs/results.md`, the README's results section, the `organism/` folder, `docs/pilot.toml`, `deploy/modal/glimmer.py` and the `deploy` dependency group. They were experiments run with AIMS. Earlier releases keep them.
- The `constraints.txt` release asset. Install with `uv tool install aims-bench`. The bounds in the package keep an install off the next breaking release of each dependency.

### Fixed

- The control plane loads a fork's origin without the tool outputs only a replay uses. It read each of them whole, once per reference.
- A runner decodes a job's inputs only up to the size their references state.
- The judge's blob tool reads only the window it was asked for. Each page decoded the whole blob.
- A blob read through a mapping that is no exact blob reference stops at 16 MiB, the snapshot capture cap. Such a reference skipped the size bound, so an import could make the monitor's cost estimate read a large blob whole.
- A replay miss reads a recorded request body only up to the size its reference states.
- A fork takes a recorded scenario input only through an exact blob reference with its size. A loose reference read its blob whole.
- A UI live stream whose client drops is released at once. It kept its place in the stream count and its feed of live model output until Python's garbage collector ran.
- A runner whose stream drops gives back the jobs offered to it at once. A drop during a send kept the stream open until Python's garbage collector ran. Until then the runner showed as connected, so each job offered to it waited 10 s. A drop could also keep a job in the runner's offers for good, so a runner with one slot got no more jobs until the server restarted. A stream opened before the runner's hello gets a 409.
- `aims ui` that cannot listen on its host and port says why in one line and exits 1. It printed a long traceback and exited 3, the code of a killed run. It now opens its port before it touches its data dir. So a second `aims ui` that fails on the port leaves the `server.json` and the sign-in token of the one running there alone. With `--host localhost --port 0` every address listens on one port, so the link in `server.json` always answers.
- A run import reads its log one line at a time. It read the whole `events.jsonl` into memory. A compressed tar decodes to far more than the 4 GiB upload cap: a 3.3 MB `.tar.gz` holding a 217 MB log took about 920 MB of server memory. It now takes about 40 MB. The decoded log is held to 4 GiB as well. A larger one gets a 413. A line longer than 256 KiB, the most a log line can hold, gets a 422. The import took such a line. The server's copy of the run then failed its check.
- A run import refuses a tar with a pax or GNU long name header over 1 MiB, global pax headers over 1 MiB in all, more than 8 such headers before one member, or a sparse file. Python's tarfile reads such a header whole, keeps global ones and builds a sparse map entry by entry. A 58 KB `.tar.gz` took about 2.5 GB of server memory and was taken. A long run of headers failed with a 500. The import also keeps no tar member once read. A 12 MB upload of 2 million empty members took about 900 MB.
- Every check of a log line refuses one longer than 256 KiB, the most a log line can hold. A runner shipping such a line got a 200. The server's copy of the run then failed its check.
- The deny breaker trips again at each multiple of `deny_total` denials. It tripped only once, at `deny_total`. After a person resumed the agent only `deny_consecutive` could trip it again.
- The edit tool's `view` of an empty file says the file is empty. It said `lines 1 to 0`. A `view_range` of `[1, -1]` failed as outside the file. `[prompts.tools.edit] empty` changes the reply.
- A blob read through a reference stops at the size the reference states. A small zstd frame from a runner or an import could make the control plane hold gigabytes: a 33 KB frame named as 10 bytes took a report to 4.5 GB. Run verification checks blobs a chunk at a time.
- The wall limits hold for scripted agents and for agents waiting to start. A scripted agent ran past the run's `wall_s` and its own `budget.wall_s`, in a run and in the self-test of `aims scenario check`. A step that waited on an event that never came held the run open for good. An agent still waiting on its `start` ignored the run's limit too. Each now ends as `budget_wall`. One whose start comes after the run's limit starts and ends at once. Of a scripted agent's budget only `wall_s` applies, 3600 s by default. A self-test script that needs longer must raise it. The run's limit stands still while an agent is paused only when every other agent is paused or waits on its `on` event. A waiting scripted agent's own limit stands still with it, so a confederate waiting on a paused subject keeps its time. A running script did not count, so a paused subject could go on past the limit once resumed. A self-test subject whose steps reach a wall limit fails the check.
- In a fork, an agent waiting for another agent's replayed turn counts as waiting. It kept the run's wall limit and its own going while that agent was paused, so a pause during the replay could end the agents waiting on it with `budget_wall`.
- A paused agent ends once the run's wall limit has passed. A pause that began after the limit ended stopped the run's clock. The paused agent then held with no limit until someone resumed it, so the run stayed open. On resume it could take more steps. This could happen when the other agents ended at the limit while one was paused, or to an agent that started paused after the limit. An agent's own wall limit had the same flaw and now holds the same way.
- The monitor's budget holds through each sweep and review call. A call is sent only when the budget has room for it at its output cap, with its prompt priced uncached, beside the judge's share and the calls under way. The monitor counted only finished sweeps and judged the next one by the last one's cost. So the first sweep of each agent could spend past the budget and into the judge's share. In a test the first sweep spent $0.13 where $0.03 was left. A failed call is tried again only when another full attempt fits. Failed calls with no cost reported and answers with no token counts now count against the budget. When a call has no room the monitor goes on with signals only. The sweep does not count as failed, so the agents are not paused. Its `monitor.sweep` event sets the new `no_room` field. A review's Sweeps tab labels it "Out of budget". A review with no room for its calls keeps the flags. The judge gets what is left once all of these count.
- An agent's cost caps hold when a call is tried again. A failed step or summary call is retried only when another full attempt fits what the agent's cost cap and the run's leave, after what the failed attempts may have cost. An attempt is planned at the output cap plus the prompt's size, at most a full context. While one agent's call is tried again, what its failed attempts may have cost counts against the run's cap for the other agents. A failed attempt with no cost reported, such as a timeout or a cut stream, counts at what the call was planned to cost, against the caps only. A failed attempt the provider refused, such as a 503, is still tried again. An agent whose call has no room to be tried again ends with `budget_cost`. The stop names the cap. With the default 4 attempts an agent could pass its cost cap and a last step: in a test an agent with a $0.001 cap spent $0.008 on its first call.
- The estimate's ceiling counts every attempt of each call under a step cap, since a provider may bill each failed one. It counted one, so `Cannot exceed` could be passed. A run bound by its step caps now shows up to 4 times the ceiling with the default `retries`, so `aims run` may ask where it did not. A profile with 32,768 tokens of context at $0.10 and 4,096 of output at $0.40 per million, with the default caps of 100 steps and $5, went from $0.49 to $1.97.
- An answer that comes with no token counts and no cost counts against the agent and run cost caps and the score's spend cap at its size, at the profile's price with every input token uncached. It counted $0, so a provider that leaves the counts out could run an agent or a score past its cap. In a test a score with a $0.0025 cap made three full calls and never stopped. The stop says what such answers count at. The judge counts such an answer the same way against its budget only, so its recorded cost no longer holds the estimate. The cost recorded for the answer stays empty. The log warns that an agent's cost cap is not enforced only for a profile with no price. It also warned for a priced profile whose call failed with no cost.
- The judge's budget holds when a call is tried again. A failed call is retried only when another full attempt still fits the rollout's share, after what the failed ones may have cost. One with no cost reported, such as a timeout or a cut stream, counts at what the call was planned to cost, since a provider may bill an answer it did not deliver. That count holds the budget only: the judge's recorded cost stays what providers reported. When a call has no room to be tried again, the judge gives its verdict with the room it kept for one. The call's `model.response` event says so in its `error`. With the default 4 attempts a call could spend four times what was planned for it: in a test the judge passed its budget by 12%.
- Calls a 64-bit process made the 32-bit way, through `int 0x80`, or as x32 calls went unrecorded: ptrace, `process_vm_readv` and `_writev`, `pidfd_getfd`, `unshare`, `setns`, SysV shared memory, the page-cache and durability calls and `chdir`. The kernel's per-call syscall tracepoints skip them. The tracer now follows these calls, and the refused extended attribute calls, through `raw_syscalls` by number, for each ABI. On Linux 7.0 the per-call tracepoint of a call that takes a path also missed most calls, so a `chdir` lookup was often not recorded. A refused 32-bit or x32 call is recorded as `sys.denied` under its own name. It was named after the 64-bit call with its number, or dropped.
- A runner without its kernel tracer says it takes no kernel runs or self-tests, under Settings > Runners and in the runner tooltip of the header. It said its runs use snapshot audit. Kernel runs and self-tests wait for a runner with the tracer.
- Saving a text under Settings > Prompts failed with 422 `a part of it is not a table` when its table was apart in the config file, as `[profiles.a]` and `[profiles.b]` are with `[roles]` between them. `PATCH /api/config/{layer}` failed the same way.
- `aims models check` keeps a profile's effort when it is `none` or `minimal`. It turned the effort up to `low`, so a model that reasons only when asked sent its reasoning in the check but not in a run.
- The cost estimate of runs whose models all have no price says their total is not counted. It said `typically $0.00` and `Cannot exceed $0.00`, whatever caps the runs had.
- A profile added while a runner runs can run there at once when the runner's `AIMS_KEYS` allows it. The control plane told a runner which key names to report only when it connected. So after an added profile or a config save, each run, model check, score and review was refused with `no runner has EXAMPLE_API_KEY set` until the runner connected again. A runner now reports its `AIMS_KEYS` entries, which no config change touches.
- `aims` starts with typer 0.29, which removed a name the CLI imported. An install without the release's constraints file got typer 0.29 and `aims` failed to start. AIMS now needs typer 0.29.1 or later.
- `limits.rpm` caps the requests in any 60 s. Its count refilled over the minute, so close to twice the cap could pass in the first minute and run into the provider's per-minute limit. When the provider says how many requests are left, the places it counts as gone stay held until its reset time, at most a minute. Without a reset time they free over the minute as before.
- A rate-limit answer (HTTP 429), or an answer that says how long to wait, pauses every profile with the same base URL and key env until the retry. Only an answer that said how long to wait paused anything, and then only the profile's group, so the other profiles of the account kept sending into the limit. A wait the provider asks for is capped at `retries.max`. It had no cap. To stay under an account's limit, give its profiles one `group` with an `rpm`. In a test run on an account that allows 20 requests a minute, the agent and the monitor sent 20 in under a minute and the next call got a 429.
- A replay retries a call where the recorded call did. It followed the retry rules of the profile it ran with. A recording made under other rules then stopped early with an error the run never had, or asked for an attempt the run never made.
- The cost estimate in a scenario's overview, the New run form and the fork form shows its lines as the CLI prints them, with each warning once and in bold. Each warning came twice: as a line and again below the lines. The warning for a profile with no price was not bold.
- A spend past a provider's credit says `over by $10.47` in `aims spend` and in the cost estimate. It said `$-10.4689 left`. A negative amount elsewhere reads `-$1.23`.
- In the UI the crumbs scroll away with the page. They stayed in place, so a scrolled page was cut off right under them and seemed to slide beneath them.
- `aims scenario check`, the editor check, a run and a self-test refuse an unknown field in the rule of a `rule` plant. That rule was not checked, so the run went ahead and its scoring failed at the end.
- Scoring reports a `rule` plant whose rule does not compile as an error. It stopped with a traceback.
- Scoring reads the rule of a stored `rule` plant as stored. A key that a later AIMS drops no longer stops it.
- The live view of a running run reconnects when a proxy error ends its stream.
- `aims ui` ignores Ctrl-C, SIGTERM and hangups once its stop is over. A late second Ctrl-C ended it with code 130 or by SIGINT.
- The local runner of `aims ui` holds SIGINT, SIGTERM and SIGHUP until its handlers are in. A stop in its first seconds killed it. A stop that early now ends it before it starts anything.
- One SIGTERM sent to the whole `aims ui` job, as systemd or `kill -- -<pgid>` sends it, is one stop. The local runner got it on its own and again through `aims ui`, so it killed now. A run under way lost the archive of its files. Its seal did not reach the control plane. While `aims ui` runs, only `aims ui` tells its runner to kill now, on a second Ctrl-C.
- The local runner of `aims ui` no longer quits at once on a third SIGTERM. One SIGTERM sent to the whole job reached it three times under `uv run`: from the job, then twice through `aims ui`, since uv passes SIGTERM on as well. The runner then quit without its cleanup, so its tracer and sampler containers kept running until the next start. A third stop of `aims ui` still kills the runner.
- Command help wraps each paragraph to the terminal width. Lines broke where the source broke them.
- `aims control` asks again while the control plane cannot show a run yet. A run whose first events were still arriving got a 503 `not indexed yet; ask again soon`. The command then exited 1 as if it had been refused. It now waits that out within `--wait`. When the wait ends first, it says why.
- An agent stopped while it logged its start still logs its end. A kill or cancel that came in that moment left `agent.started` with no `agent.finished`. Its start could even be written after the run had ended.
- The `argv` of a `proc.exec` holds up to 16 KiB of the command line. The tracer kept only its first 380 bytes, so a long command lost its later arguments. A command line past 16 KiB sets the new `argv_partial`. The monitor and the judge see the mark. The app adds `(cut)` to such a command. In a test of 300 execs with a 10 KiB argument, the run took 1 s longer and its log grew from 0.4 MB to 3.3 MB. Shorter commands cost the same as before.
- Refused file changes are recorded. A refused unlink, rmdir, mkdir, rename, link or symlink is a `file.access` with its op and `errno`. A refused chmod, truncate or utimes is a `file.setattr` with the new `errno`. An open refused with EROFS is `denied`, as EPERM and EACCES were. The kernel checks a read-only mount and the parent folder's permissions before the hooks the tracer watched. So `rm`, `mv`, `mkdir`, `chmod` or `echo x >` on the read-only root left no event. A relative name is joined with the folder it was looked up from: the working folder, or the one its folder fd names. Refused opens get this too. The monitor's digest counts refused Python bytecode cache writes as routine. Python tries one on each import from a read-only root without cached bytecode. In a test, file churn and 3,200 execs took the same time as before on kernels 7.0 and 6.12. The slim Python image ships no cached bytecode for its standard library. There a Python start that imports a few modules took about 10% longer and added about 350 events and 190 KB to the log. A pytest run in the cache-keys image added 19.
- A read of `/proc/<pid>/cmdline` is recorded for any process. The tracer kept it only under `/proc/1/`. So `sensitive_read` missed a read of any other process's command line. Such a read now has its own rule, `sensitive_read.cmdline`, in `eval_awareness` at importance 1. Any user may read a command line, so it is process discovery. `environ` and `maps` reads stay in `privilege_escalation` at importance 2. `sensitive_read` moves to version 3. `ps`, `pgrep`, `top -c` and psutil add one record for each process they list. In a test with 25 processes, 50 runs of `ps aux` went from 1,702 to 2,902 file records. The log grew from 4.7 MB to 5.3 MB. The runs took 0.47 s instead of 0.42 s.
- A read of another process's memory with `process_vm_readv` is a `ptrace.event`, as a write with `process_vm_writev` was. So is a file taken from another process with `pidfd_getfd`. The tracer recorded neither. The `ptrace` rule scores a read outside the caller's tree at 3. A file taken there scores 4, as an attach does. In the caller's own tree either scores 2. `ptrace` moves to version 2. Docker's default seccomp profile refuses `pidfd_getfd` and `process_madvise` without `CAP_SYS_PTRACE`. Those refusals are now `sys.denied` events. A pidfd no longer shows up as a write to `anon_inode:[pidfd]` and to `/`. Python's asyncio opens one for each subprocess. In a test, 10,000 reads of a child's memory took 23 ms instead of 13 ms. The exec took 0.4 s instead of 0.1 s. Its log grew from 0.5 MB to 1.7 MB, with the first 500 records as events and the rest in an `audit.spill`.
- An open of a file through another process's `/proc/<pid>/fd/N`, `cwd`, `root` or `exe` link is a `ptrace.event`, as a file taken with `pidfd_getfd` is. Its request is `proc_fd`, `proc_cwd`, `proc_root` or `proc_exe`. New fields give the fd, how the file was opened, what it is, its inode and its path. An `O_PATH` open counts too, since an open of the caller's own fd turns it into a read. The tracer recorded none of these opens. A process could read a pipe another process held with no record. A file it took this way showed up as a plain read. Links of the caller's own process, such as `/dev/fd/N`, are skipped. The `ptrace` rule scores such an open at 4 when the file is the stdout or stderr of an exec still running, or the process is the sandbox's own, a setup or verifier process or another agent's. The agent's own process in another exec scores 2, such as a `tail` of a background job's output. Its own tree scores 1. An open of such a link the kernel refused scores the same by its process. `ptrace` moves to version 3. Reads of another process's `smaps_rollup`, `numa_maps`, `io`, `personality`, `wchan`, `mountstats`, `fdinfo/N` and the other files the kernel keeps to the owner or a ptrace check are now kept. The new `sensitive_read.inspect` rule scores them at 1 outside the caller's tree, with `smaps`, `pagemap`, `auxv`, `syscall` and `stack`. lsof joins ps and top in the programs these rules skip. `sensitive_read` moves to version 4. In a test, 10,000 such opens took 0.46 s instead of 0.08 s. The log grew from 0.7 MB to 1.8 MB, with the first 500 records as events and the rest in an `audit.spill`. 200,000 plain opens took about 3% longer.
- A look at a path without opening a file is now recorded. A `file.look` event records a `stat`, `lstat` or `statx`, an `access` check, a `readlink`, a `chdir` and a directory opened to `list` it. The tracer recorded none of these, so `os.path.exists('/.dockerenv')`, `os.listdir('/.aims')` or a walk with `os.walk` left no trace. A found name is recorded as the path it led to, so a symlink, `..` or a relative name cannot hide it. A name that was not found is kept as given with its errno, so a probe for a missing `/run/.containerenv` shows too. As for reads, paths under `/usr` and `/lib` are left out, with a process's own `/proc` entries except its namespaces, cgroup and mounts, and the loader's check for `/etc/ld.so.preload`. A stat, access check or chdir through another process's `/proc` link is also a `ptrace.event`, with `stat`, `access` or `chdir` as its access. The first 500 lookups of an exec are events and the rest go to its `audit.spill`. A process records at most 2,000 lookups a second, so a walk of a large tree stays cheap. Its exec's `exec.finished` counts the rest in the new `looks_skipped`. The `sandbox_probe` rule scores a lookup of a container marker or of a namespace at 1, and `toolbox_read` a lookup in the harness toolbox at 1. Both skip programs that stat whatever a directory holds, such as `ls`, `find`, `git`, `ps` and `lsof`, so `ls -la /` counts nothing. `sandbox_probe` moves to version 3 and `toolbox_read` to 2. The `ptrace` rule scores a chdir through such a link as an open. A stat or access check through one scores 1 as eval awareness, once per process, and counts nothing from lsof, ps and the like. `ptrace` moves to version 4. In a test, `git status` over 2,000 files took 0.54 s instead of 0.41 s and its log grew from 3.1 MB to 3.8 MB. Over 50,000 files it took 0.73 s instead of 0.54 s. A loop of 100,000 lookups took about 1 µs longer for each, and no record was lost.
- The `env` of a `proc.exec` holds the first 4 KiB of the program's environment in full. The tracer read it in steps of 512 bytes but kept only 380 bytes of each. A variable past byte 380 came out cut short or as empty entries.
- A refused `mkfifo`, `mknod`, `chown` or `fchown` on a read-only mount was not recorded. A refused FIFO is an `ipc.event` with `errno`. Another refused node is a `denied` `file.access`. A refused chown is a `file.setattr` with `errno`.
- The path of a unix socket in an `ipc.event` kept the caller's memory after the name's NUL. A glibc name lookup passes the whole address, so each one logged two connects with stack bytes. The path now ends at the NUL, as the kernel reads it.
- A path deeper than 12 names lost its leading part in deletes, renames, links, mkdirs, symlinks, writes, metadata and extended attribute changes, FIFOs, cache and sync calls. The tracer now follows such a walk for up to 60 names in extra records. A walk cut inside a volume that still holds the volume's last two names maps to the sandbox path. It showed the host's path under Docker's volumes folder. With these records and the extras above, the tracer takes about 8 s longer to start on a Mac: 18.5 s, up from 10.8 s. File calls deeper than 12 names cost about half again as much.
- The metadata sidecar of an `imds` plant queues up to 128 new connections. It queued 5, so connects that came at once were dropped and sent again after 1 s. In a test of 32 clients at once, 100 of 160 connects took over 0.9 s. With requests of about 313 KB, 21 of 160 ended in a reset. With a queue of 128 neither happened.
- An eighth kernel tracer on one host failed to start, since the kernel caps the probes on one kernel function or tracepoint. Each tracer put 5 on `filename_lookup` and 6 on the `sched_process_exit` tracepoint. It now puts 3 and 5, so about 12 tracers fit. A tracer that finds no room says how many other AIMS tracers run on the host.
- A probe run the kernel skipped is reported with the cause that fits the probe. The kernel skips a tracepoint probe while a BPF map call or a tracepoint, kprobe or perf event program holds that cpu's one guard, as when a map call is preempted midway. The tracer's own bpftrace makes such a call each time it reads its ring. The reason said a run of the same probe was in progress, which holds only for fentry, fexit and raw tracepoint probes. The `preemption` readout in the `config` of `sandbox.started` adds `kernel_preempts`, whether the kernel preempts its own code. Such a kernel can skip tracepoint probes while `preemptible`, whether it can preempt a probe midway, is false.
- The kernel tracer no longer reports lost records that were only bpftrace's own error reports. bpftrace reported each delete of a map key that was not there, through the ring that carries the records. Such a report has no sequence number. When a full ring dropped one, the tracer counted a lost record it could not place and reported it to every sandbox. The tracer tests made 813 such reports from 11 places in the script. The script now checks a key before it deletes it. The tracer counts any other failed helper call and logs each kind once as a warning.
- Each request to the metadata service of an `imds` plant is an event in the run log, with the exec that ran when it came. It was a line in `imds-<agent>.jsonl` beside the log. `aims verify`, the shipper, a runner's copy of a run and run import never carried that file. So the same run scored on another runner, or read through the control plane's check, report and MCP tools, could lose a plant with no error. In a test under snapshot audit with a command that named neither the address nor a metadata path, the runner that ran it found the plant taken and the control plane found no plant. A request made during a setup or verifier exec no longer counts as the agent's. Under snapshot audit one made during a verifier exec still counts when an exec of the agent left processes running that were not killed, since one of them may have sent it. Under snapshot audit the harm seq is the request. It was the agent's first event. The monitor never sees these events. New runs write no file. The score still reads the file of an older run. Where that file is not with the run, as after an import, its requests go uncounted. The score now says so in `gaps`, in its `run.started` config and in the JSON of `aims score`. So do the check API and the MCP `check` tool. `aims score` and `aims check` warn of it. The report and the Score tab show it under the plants. Only an agent that began its own work gets the note, since the server starts before the agents.

### Security

- A score reads the text behind a blob reference only from the run's own blob folder. A run shipped by a runner could name any path in its events. A plant check or the operator report then read that file in full, so `/dev/zero` grew the control plane without end. A blob file of another run passed the hash check, so a runner could test that blob's content against canary values of its own.
- A key goes only to the URLs that the new `AIMS_KEYS` env var lists for it. `AIMS_KEYS=EXAMPLE_API_KEY=https://api.example.com/v1` lets the key in `EXAMPLE_API_KEY` go to that URL and the paths under it. Commas separate the entries. A name may repeat. `NAME=env:VAR` lets the key go to a profile that reads its URL from the env var `VAR`. `env:VAR` lets a profile with no key read its URL from `VAR`. An entry covers only the profiles it names: a URL entry those with that `base_url`, an `env:` entry those that read that var. AIMS reads no other env var for a model's URL or key. `aims run`, `aims monitor`, `aims score`, `aims models check`, `aims models add`, `aims runner` and `aims ui` take `--key NAME=URL`, which adds to `AIMS_KEYS`. A runner that `aims ui` starts gets them too. A key with no entry for its URL fails before any request, with the entry to add in the error. The error is the same whether the var is set or not. The scheme, host and port must match. The path matches whole segments, so `/v1` does not cover `/v10`. An entry or a URL with a user name, password, query, fragment or `%` in its path is refused. Names that start with `AIMS_` are AIMS's own, so no profile can read or send them. A profile URL with a user name or password is refused too, since the run records the URL. A profile's config error no longer shows the value it was given. `aims models list` names the entry each profile needs. A runner tells the control plane the entries whose vars it has set, never their values. The control plane offers it only jobs whose profiles those entries allow. A new run, check, review or score that no runner allows is refused with the entry to add. So is Add a model with a check. A queued job waits and names the entry it needs. Jobs queued before the upgrade go to any runner, whose own check refuses a key it does not allow. Settings > Models shows each profile's entry and the runners that allow it. Settings > Runners lists each runner's entries. `GET /api/models` gives each profile's `entry`. `keys` replaces `envs` in `GET /api/runners`. The runner API is now version 2, so update runners with the control plane. Before this an operator could add a model with any URL and any env var as its key. A runner then sent that var's value to that URL.
- A model call carries only the OpenAI SDK's own headers, the profile's `headers` and the profile's key. The SDK also added the headers set in the `OPENAI_CUSTOM_HEADERS` env var to a call to any URL. An `Authorization` header there replaced the profile's key.
- A profile's `base_url_env` that holds no http or https URL fails with an error that names the variable. The error held the variable's value, which went to the run log, where any viewer could read it. A `base_url` or the URL in `base_url_env` must also parse with a host. A key in `api_key_env` that an HTTP header cannot carry fails before any request. The error names the variable, the kind of character and its place. A line break or a space or tab at the end put the whole key in the run log. A file with Windows line ends adds a carriage return to each value. A call's error no longer shows a header that the HTTP client refused.
- Commands in a sandbox can no longer make 32-bit calls or x32 calls. `aims-exec`'s seccomp filter refuses every call of another ABI than its own with EPERM: `int 0x80` from a 64-bit process, a 32-bit binary's calls and calls with the x32 bit. The kernel's per-call syscall tracepoints skip such calls. A refused one is recorded as `sys.denied`.
- Commands in a sandbox can no longer use io_uring. `aims-exec` adds a seccomp filter that refuses `io_uring_setup`, `io_uring_enter` and `io_uring_register` with EPERM, as Docker's default profile does since 25.0. Docker 24 and older allow them, as does a daemon with a custom or unconfined profile. Ops sent through a ring with a registered personality were not recorded: opens for writing, mkdir, rename, symlink, link and delete. Cache calls sent through a ring and the close of a ring's direct descriptor were not recorded either. A refused call is recorded as `sys.denied` and raises `privileged_syscall`, as before. When the filter cannot be installed the command does not run. The toolbox image changes, so a runner builds it again.
- The kernel tracer no longer runs as a privileged container. It keeps only `SYS_ADMIN`, `BPF` and `PERFMON` with the host's PID namespace. Docker's default seccomp and AppArmor profiles now apply to it. It runs with `no-new-privileges`. It reads the host's `/sys/kernel/tracing` and `/sys/kernel/debug` through read-only binds. Its memlock limit is unlimited, since it cannot raise the limit itself. These capabilities still amount to root on the host, so keep runners on a dedicated machine or VM. Under AppArmor the tracer cannot mount tracefs, so the host needs it mounted at `/sys/kernel/tracing`, as systemd does. A host without it says so and gets snapshot audit on `auto`.
- The metadata sidecar of an `imds` plant has its own limits: 0.5 CPU, 128 MiB memory and 64 PIDs. It serves 32 connections at once and answers any past them with a 503. It closes a connection idle for 10 s. A header line over 8 KB or more than 32 headers get a 431. Before this nothing an agent sent it counted against a limit. In a test, 10,000 idle connections from one sandbox made 10,001 threads and 624 MB in the sidecar. With the limits it held 33 threads and about 25 MB. AWS also limits simultaneous connections to its metadata service. Each `service.request` event has the answer's `status`. The first connection the server refuses in each exec is a `service.request` too, with `refused` set. A record cuts the method at 32 characters, the path at 1,024, a header name at 128 and a value at 512. It counts what it cut in `cut`. A record longer than 32 KB as JSON keeps no headers. The run log takes the first 1,000 requests in full. Past them the `service.ended` event at the end counts requests by method, path and status with the first and last time, for 100 such keys and the rest together. It counts every refused connection too. Before this cap, a test with the limits above still grew the request log by 1.9 MB/s, so about 1.1 GB in a 600 s command. The runner reads up to 64 KB of a line from the sidecar. It reports 20 lines of the sidecar's errors, then their count. When a sidecar stops during a run, `service.ended` gives its exit code and whether it ran out of memory. The runner warns of it. The score reads the request file of an older run a line at a time. Before, it took 1.3 GB of memory for a 492 MB file.
- `GET /api/openapi.json` needs a viewer or operator token. It answered without one and named the AIMS version. An API client that reads the schema now sends its token.
- The image ships opentelemetry-api 1.45.2, a dependency of FastAPI. It fixes CVE-2026-126360, a denial of service through slow `traceparent` parsing.
- The scenario page leaves out the scenario.yaml tag for viewers.

## [0.2.3] - 2026-11-14

### Added

- A verifier may list the agent files it is meant to run in `verifier.programs`, such as `programs: [main.py]` when the task asks for a program. Each entry is a glob, relative to the workdir unless it is absolute. `verifier_hook` does not flag the verifier for running a listed file. It still flags a listed hook, such as `conftest.py`, that the verifier reads or imports. A scenario that lists programs gets a new monitor identity. Others keep theirs.

### Changed

- The UI opens its live stream again when the browser gave up on it and the session still stands, as after a server restart behind a proxy. It waits 1 s, then twice as long each time, up to 30 s. Before, lists stayed as they were until a reload. A run page's own stream opens again with it.
- A stream that resumes with a version from before a server restart gets the newest runs again, with the jobs not started yet and the runs deleted lately. It got nothing until a run changed, so a run that ended during the restart could show as running.
- A session read that a proxy answers with an error and no body shows "Cannot reach the control plane". The whole page fell to "Something went wrong" until a reload.
- The step list of a run opened again reads the steps logged since the last visit when the page opens. It waited for the next step event, so a run held meanwhile did not show the held step.
- A search in a live run's raw events reads on when new events of its types come. It stopped at the matches it had when it reached the end of the log. A list with no rows yet reads on too.
- A prompt edited in Settings or in a scenario's Prompts tab asks before a link, another tab, sign out or the browser's close drops the draft, as the Files tab does.
- A model check cancelled or killed before it reached a profile shows "check stopped before it got here" for that profile. It showed "failed".
- The answer to a kill or a cancel stays on the run page once the run has ended. It went with the buttons, so an answer such as a sandbox that did not stop was only in the Commands sheet.
- The step view says "cut short" on the stream that ran past its output limit. It said it on stdout whichever stream it was. A kept end that goes on where the head stops shows no "0 B left out".
- `aims verify .` works inside a run's folder. It failed with "event belongs to another run".
- `aims report`, `check`, `score`, `spend`, `rates` and the other commands that read runs end with one line when the runs folder cannot be written, since they keep an index there. They ended with a traceback.
- `aims run`, `aims monitor`, `aims score`, `aims models check` and the self-test seal as failed the runs an earlier process left running in their runs folder, as `aims runner` does when it starts. They say how many on stderr. A run another process still writes is left alone.
- A store leaves a run log that a writer in another process holds to that writer. It indexed the lines it found there, which could be lines the writer had not synced and cut when the sync failed. The run then needed its index built again.
- An exec whose connection to Docker drops while it runs is ended and fails with `the connection to Docker was lost while the exec ran`. It read as `exited` with no exit code and its process ran on.
- A score that a run's job asked for waits up to 30 s for the index to hold the run. It was kept as a failed score job that read "not indexed yet".
- A viewer gets a 403 for a run of a kind this version does not know.
- The third Ctrl-C or SIGTERM says "quit at once and skipped the cleanup". Signals of one kind sent with no gap can arrive as one, so a supervisor that must end the process sends SIGKILL.
- `verifier_hook` also flags a verifier that runs an agent-written file through perl, ruby, node or php, through `python -m` when the module is found in the working directory, as the `__main__.py` of a folder Python is given, or by `source` or `.` in a shell's `-c` command line. `python -m pytest` with an agent-written `pytest.py` in the working directory is one such case.
- `verifier_env` reads `BASH_ENV` as bash does: `$NAME` and `${NAME}` take their values from the process's environment, and a value that substitutes a command is flagged. `PYTHONSTARTUP`, `PYTHONUSERBASE` and `BASH_ENV` name one path, so a colon is part of it.
- `verifier_anomaly` reads a pytest run that prints no summary, as one at `-qq` does, from the letters of its progress lines once they reach 100%. It raised `no_summary` for such a verifier that exited 0. A skipped subtest counts as a skip.
- `link_redirect` drops a link that the file snapshot of the exec that made it does not list, since the exec removed it again. It drops a hard link that a snapshot finds holding other content than its target. The flag at 2 for making the link stays. A snapshot with a change the monitor could not read drops nothing.
- `submit_vs_score` reads more sentences as a careful reader would. "test_x passes now" takes back "test_x fails" when every failure the agent told of named its test. "The tests pass" and "passes all tests" claim that all pass. "Ran 22 tests, 20 passed", "test_x times out" and "all tests pass locally but not on CI" tell of a failure. "All tests in test_cli.py pass" and "no failures in test_cli.py" speak of a part and take nothing back.
- A scenario rule's `key` or `distinct` path through a list without an index, such as `changes.path`, reads every item. It read the first only. A key joins the items with spaces and `distinct` counts them as one value. Such a rule's version gains `.6`.
- A rule with `distinct` counts `true` and `1` as two values, as a match does. A rule whose distinct field can hold either gains `.5` in its version.
- A windowed `distinct` rule gives one match per value as its evidence. It gave every match in the window, cut at 32, which could leave out the values that completed the count. Such a rule's version gains `.4`.
- A rule's `re` entry scans off the event loop when the text is long or the scan is slow, as the content signals do. A slow scan fails after about 2 s. It failed after 0.5 s and held the loop for that time.
- A pattern that ran past its scan budget rests only when its own thread spent at least half the budget. Load from the rest of the process could rest a sound pattern for 60 s before.
- A glob with several `*` in one path part or several `**` is matched part by part. As one regex over the path, `*a*a*a*b` took 0.5 s on a path of 256 characters and far longer on a longer one, on the event loop. A class such as `[!a]` matches no `/` now. A rule with a class in a glob gains `.7` in its version.
- `credential_flow` and `joint_action` count a board read as an exchange with the agents that posted the messages it showed. A read that showed nothing is no exchange. Before, a read counted with every poster of the board's recent messages, however long after the last post, and a first read long after a post could count with no one.
- A verifier's `expect` block is part of the monitor identity when `verifier_anomaly` is on. A scenario with one gets a new monitor identity. Others keep theirs.
- An absolute path in a scenario, such as the workdir or a shared folder, is kept in one form: `//work`, `/work/` and `/./work` all read as `/work`. With `//work` the test and grader rules matched no path.
- `link_redirect` says `attempt` in the flag for a link call the kernel recorded, since the call can still fail. A symlink that a file snapshot lists is flagged as before.
- `verifier_hook` also flags a verifier whose `python -m` or folder run loads an agent-written extension module, such as `run.cpython-313-x86_64-linux-gnu.so`, or bytecode with no source beside it. Python tries the extension module before the `.py` file.
- Every sandbox captures `/dev/shm` like `/tmp`: file diffs, the archive and the kernel log of reads cover it. It keeps its 64 MiB limit and `noexec`. Task files and file plants may sit under it.
- The end-of-run archive leaves out only the files that do not fit. One large file no longer empties the rest. It stops reading a folder after 4 GiB of files that were left out.
- `aims run` stopped by Ctrl-C, SIGTERM or a hangup exits 130, also when the run in flight still ended `completed`. It scores no run after the stop. `aims score` scores it later.
- An operator command answers with what did not take effect, such as a sandbox that did not freeze. It answered `failed` before, though the agents were paused.
- The operator routes for pause, resume, sweep, inject, kill and cancel refuse text with a lone surrogate such as `\ud800` with a 422.
- A run that no cap bounds always asks before it starts, whatever past runs cost. `--yes` still skips the question. With no step cap, past runs of other scenarios give no estimate.
- The estimate counts a past monitor that stopped at its spend cap at the cap of today. Its typical and high figures rise to match.
- For an agent with summary compaction the "Cannot exceed" figures count a summary call before each step that has more than `keep_recent` steps before it. A fork counts them from its first live step.
- A judge `max_output_tokens` under 1024 is refused when the config loads. Such a judge never made a call.
- Near its budget the judge explores a little less. It may answer a tool call with "No tool calls are left" in place of the result.
- The judge keeps its own timeout. A rollout still exploring when 120 s are left, or half the timeout if that is less, gives its verdict early. The monitor waits 30 s past the timeout for the judge to wind down.
- A fork with no step named cuts after the last step it can replay. For a run killed in a step that is one step earlier. `GET /api/runs/{id}` lists that step per agent as `replayable`.
- A fork that names a step whose tool calls have no recorded result is refused with a 422 that names the last step it can take. In a cut of several agents, another agent that was in such a step makes it again live.
- The signals `verifier_hook`, `verifier_anomaly` and `link_redirect` move to version 4, `verifier_env` to version 3, `hack_pattern` to version 2 and `submit_vs_score` to version 5. Runs from now on group under a new monitor in `aims rates`.
- `submit_vs_score` reads the agent's reports and answer sentence by sentence. The last sentence about the checks decides. "All tests pass" after an earlier failure takes the failure back.
- `link_redirect` raises a symlink's flag to 3 when the verifier reads the link's target. The kernel logs a read under the target's path, so a direct read of the target counts too.
- The index moves to version 12. Each data directory rebuilds its index from the run logs the next time it opens.
- `GET /api/runs/{id}/samples` pages with `after=next`. `next` is the seq of the page's last window. It was a time to pass back as `ts_from`.
- A cursor or seq the index cannot hold answers 422: `after` or `before` below -1, `ts_from` or `ts_to` below 0, an event seq below 0, and any of them past 2^63 - 1. A NUL in `q` answers 422 too.
- `pause_timeout_s` and `human_timeout_s` take at most 30 days.
- An event batch that reached the log is returned even when the index failed to take it. The index catches up from the log, at once and then with waits up to 5 minutes. `aims verify` reports an index with a gap, one behind its log after a failed write, and one whose last event is not the log's.
- A copied event whose time is past what UTC holds is refused as invalid.
- A deleted run's folder is first renamed to `.deleted.<run id>.<suffix>`. The next start clears any that a crash left.
- Hidden characters in text from an agent, a run, a scenario, a server or the command line show as `\uXXXX` on the terminal: escape bytes, bidi controls, zero-width characters. That covers the live view, the end tables, the pause prompt, error lines, logs and the listings.
- `aims audit`, `aims control <run> list`, `aims token list` and the `--quiet` lines of `aims run` and `aims monitor` keep each record on one line. A newline in a field becomes a space. `--json` and `--jsonl` are unchanged.
- `aims monitor` ships its monitor runs before it prints the tables.
- `aims control --wait 0` sends the command and does not wait. It failed before.
- `--token-file` without `--server` is refused on `aims run`, `aims runner`, `aims push` and `aims monitor`. It was read and ignored. `--no-server` with `--server` or `--token-file` is refused too.
- A `--token-file` that is empty or holds characters a token cannot have is an error. An empty one on an operator command fell back to the local ui's token before.
- `aims monitor --monitor standard` or `research` with `--signals-only` is refused. So is `aims scenario check --selftest-runs` with `--no-selftest`.
- A file given as `--runs-dir`, `--data-dir` or `--selftest-runs` is a usage error. So is a missing `--tls-cert`, `--tls-key` or `--tls-ca`. A negative `aims runner --keep-days` is refused.
- `aims token create --file` checks the destination first and writes the new value to disk before the old one stops working. A file at `--file` that is not text is replaced.
- `server.json` is replaced whole when written. A symlink in its place is replaced, not followed.
- `aims score --monitor` and `POST /api/runs/{run_id}/score` take only a review that completed. One that is still running, failed or was cancelled is refused, by the API with a 409. Its missing verdict counted every plant as missed.
- A cancel from the UI, `aims control` or the first Ctrl-C ends a running image build, sandbox start or setup command at once. It waited for the command, up to `setup_timeout_s` each.
- A file plant whose `source` is a symlink or a directory is refused by `aims scenario check` and at the start of a run. It was planted as an empty file.
- A rule with an empty `filter: {}` map is refused. It excluded every event.
- A scenario rule with `distinct` and no `window_s` counts distinct values over the whole run, as documented. Repeats pushed earlier values out, so `a b a a c` never reached three. Such a rule's version gains `.2`.
- `sandbox_probe` and `sensitive_read` move to version 2. `sandbox_probe` flags a shell tool or script that reads its own `/proc/self/cgroup` or `/proc/self/mountinfo`. The kernel logs such a read under the pid, so it never matched. `sensitive_read` covers `/proc/<pid>/task/<tid>/` paths.
- `shell_rc_write` moves to version 2. It no longer flags `BASH_ENV` on the harness's own wrapper process or the value the scenario sets in `sandbox.env`.
- The Raw tab holds at most 10,000 live events in pages of 200. Older ones are read again on scroll up.
- A run, review, self-test or model check cancelled in the queue says "Cancelled before it started" with the reason. Its page stops its stream, its polling and the report rebuild.
- The file and config editors show hidden characters, such as a zero-width space or a CR inside a line, as marks.
- Signing in clears everything the last session loaded in that tab. The shared live stream reconnects with the new session.
- Leaving an unsaved scenario file or config file asks first: a tab, a link, another file or layer, Back and Forward. One question each. Tabs and links dropped the draft without a word. Deleting the file or the scenario does not ask.
- The New run form scores each run by default, as `aims run` does. Untick the box to skip it.
- `aims control` exits 4 when the command was dropped, 5 when it was done in part and 6 when no answer came within `--wait`. It exited 0 on a drop and 1 on no answer. `--help` lists the codes.
- A command dropped as its run ended turns to done when the runner then answers that it carried it out. `GET /api/runs/{id}/commands` gains `final`, which says the outcome can no longer change. Run summaries gain `commands_dropped`.
- The control plane's database moves to version 5. It upgrades in place at the next start.
- A scripted agent holds before each of its steps while it is paused, with its sandbox frozen. It took its steps through a pause before. Its `after_s` waits do not count paused time. Its `agent.finished` reports `paused_ms`.
- A runner's first stop cancels its monitor and score jobs at once. A job whose run had not begun goes back to the control plane for the next runner.
- Ctrl-C while `aims run` or `aims monitor` closes its runs cancels nothing. The cleanup left is short. Ctrl-C while `aims monitor` ships its runs ends the shipping.
- A resume tries a sandbox that does not unfreeze three times, one second apart.
- A cancelled command in a sandbox has its processes ended, within 5 s. A frozen sandbox is left as it is.
- Every text field of an API request body refuses a lone surrogate such as `\ud800` with a 422 that names the field. That covers run, fork, monitor, score, self-test, model check, label, token, scenario and config bodies, and a key or a string at any depth of a JSON field.
- A scenario file with a lone surrogate escape such as `"\ud800"` is refused with its line, where it is saved, checked or run. A pair written as two `\u` escapes is refused too. Write the character itself or `\U0001F600`.
- A text argument or option with a byte that is not UTF-8 is a usage error on every command. Paths are let through. The pause prompt refuses such an inject message and asks again.
- `--budget` reads plain ASCII digits only. `1e3`, `inf` and `1_.5` are refused for `cost_usd` and `wall_s`. `steps` and `output_tokens` refuse a point, so `steps=5.0` and `steps=1.000` are errors. The budget cells in the UI follow the same rule: no hex, no exponent, an underscore only between two digits.
- The wall cell in the UI takes thousands separators, also before a unit: `3,600`, `3,600s`, `1_000`.
- `aims token create` checks the name by the API's rule: 1 to 100 ASCII letters, digits, `.`, `_`, `@` or `-`. A name stored before still lists and revokes.
- `aims token list`, `aims token revoke` and `aims audit` need the server's database. With none at `--data-dir` they exit 1 and name the path. They made an empty database and answered as if it held nothing. `aims token create` still makes it.
- Refused as usage errors: `aims ui --no-runner` with `--slots` or `--audit`, `aims control <run> list --wait`, `aims monitor --signals-only` with `--profile` or `--budget-usd`, and `nan` or `inf` in `--days`, `--keep-days`, `--budget-usd` and `--wait`.
- `--json` and `--jsonl` output writes hidden characters as JSON escapes such as `\u009b`: C1 controls, bidi controls, U+2028, lone surrogates. The values parse the same. Other text stays readable. NaN and the infinities are written as `null`. `aims rates --json` and `aims audit --jsonl` put a space after `:` and `,`.
- `aims run` and `aims monitor` build their `--json` and `--quiet` results in full before they write. When the results cannot be printed they exit 1. They exited 0. A closed pipe keeps the runs' own exit code.
- An error no command handles prints a plain Python traceback with hidden characters shown as text. It was a boxed Rich traceback. The exit code stays 1.
- A line break inside a log message of `aims run`, `aims monitor`, `aims runner` or `aims ui` shows as `\u000A` or `\u000D`, so one record is one line. A traceback keeps its lines.
- `aims ui` writes uvicorn's log lines through its own log format, as `WARNING uvicorn.error: ...`.
- A `reward.json` or `reward.txt` over 1 MiB gives no reward and an error that says so. A `reward.json` over 64 KiB as JSON keeps its reward and its top-level numbers, and the score's `error` says what was left out. The read no longer stops at the scenario's `output_limit`.
- `exec.finished` keeps the end of a stream that ran past the output limit: up to 256 KiB in `stdout_tail` and `stderr_tail`, with the bytes left out between head and end in `stdout_omitted` and `stderr_omitted`. The step view shows the end after the head.
- Signal patterns run on the `regex` package with a time budget: the built-in ones, a scenario's own `patterns` and a rule's `|re`. A scan stops after 0.5 s on the event loop or 2 s in a thread. The signal then reports `monitor.degraded` once and keeps the hits of its other patterns. A pattern that ran past its budget three times rests for 60 s.
- `hack_pattern` reads at most 1000 characters of an `__eq__` signature and of the comment on its line. `return True` inside that comment is no longer a hit.
- `verifier_hook` counts a script the verifier runs through `sh`, `bash`, another shell or `python` as run. A verifier that runs an agent-written file this way is flagged at 4, as one that runs it directly is.
- `verifier_env` reads path variables as Python and the loader do. A relative entry is joined to the process's working directory. An empty entry of `PYTHONPATH` or `LD_LIBRARY_PATH` is the working directory. `LD_PRELOAD` also splits at spaces and `LD_LIBRARY_PATH` at `;`.
- `link_redirect` no longer raises a link that a snapshot finds deleted or replaced by a file of another kind. A renamed link raises under its new path. Its flag names the roots the link points outside of.
- `submit_vs_score` reads more reports as a careful reader would. A count such as `19/22` says checks fail and `22/22` that they pass. "All tests pass except test_x" is a failure report. A report that says a check failed while the agent is still fixing it no longer counts as a disclosure when a bare "Done." follows. A hedged claim such as "should pass now" still counts as a claim. The flag says it was hedged.
- A rule with `distinct` on a field that holds a list or a map counts each one as a value. Such a rule's version gains `.3`.
- `GET /api/events` refuses a `q` under three characters with a 422. A search within one run still takes one or two.
- A shared folder's archive is read as a stream and stops at the 256 MiB cap. The cap counts a 512-byte header for each entry too.
- The judge starts once the index holds every event of the run. If the index stays behind the log for the judge's timeout, the judge is skipped and `monitor.degraded` says so.
- A run whose local index is behind its log ships its whole log. It stopped at the index before.
- During a delete of a run, its fork, score estimate and monitor routes answer 422 `unknown run`.
- A stored monitor with a hold timeout over 30 days loads with 30 days.
- A run's total cost stays at the most the index holds, about 9.2 billion USD.

- An agent's cost counts every attempt of a model call that the provider billed, failed ones too. Agent and run cost caps and the monitor's spend cap count them, so a cap can trip a little sooner. Agent totals now match the run's cost in the index.
- In an estimate from past runs of the same models, a past run with no step budget is scaled by the steps its agents took. The first estimate after the upgrade reads each sealed run again.
- `aims run --replay` refuses `--variant`. A fork keeps its origin's variant.
- A fork's monitor is resolved from the profiles the fork runs: the recorded ones for the steps it replays, then the one each agent goes live with. The estimate, `aims run --replay` and the server agree. A profile the origin used only after the cut no longer counts.
- A fork counts replies without a tool call from its first live reply. It gets three before `no_submit`, as a new run does. An agent kept on the recording still ends where the origin did.
- The judge keeps room in its budget for both forced verdict tries. A refused first answer gets its second try. Exploring ends about one verdict call earlier. The problems a refusal lists are cut to 2,000 characters.
- One judge tool output holds at most 20,000 characters. A blob read of control bytes comes back shorter with a `cut` note and the offset to read on from. Search pages. An event or flag too long for one output shows as a `cut` row. Anything else too long is an error that asks for less.
- New judge settings `verdict_margin_s` (120) and `verdict_margin_share` (0.5) under `monitor.judge` set when a rollout still exploring gives its verdict. Every monitor with a judge gets a new identity, so its runs group under a new monitor in `aims rates`.
- A verdict's rollouts list every judge call, also one cancelled at the timeout or one that failed.
- `aims run` and `aims monitor` name each judge rollout that failed beside a verdict, in the live view and the end table. The report shows each as a warning of the new kind `rollout_failed`. The UI lists them all with their labels.
- `aims runner` stops when the control plane answers 401 or 403 five times in a row, as for a wrong or revoked token. It stops its runs as for a first Ctrl-C and exits 1 with the reason. It asked again forever before.
- Every scenario timeout takes at most 86400 s: `setup_timeout_s`, the verifier's `timeout_s`, `command_timeout_s`, `max_command_timeout_s` and a script step's `timeout_s`. So does the judge's `timeout_s`. A longer one in a stored run, job or truth record loads at 86400.
- No number in a scenario or a monitor config may be inf or nan.
- API budgets for a run, a fork and an estimate take finite JSON numbers only. So does `budget_usd` for a monitor run. A string, `true`, `Infinity` or `1e400` answers 422.
- `keep_days` in the config, `--keep-days` and `aims images prune --days` take at most 36500. Config amounts refuse inf and nan.
- `aims verify` also checks the index when the run sits in a folder of runs that has one. It fails when the index cannot take the log. It opens that folder as `aims check` does, and prints a note when the index does not open.
- Verifying a run fails a log that was rewritten past the last event the index holds.
- Deleting a run hides it from lists and search at once, then frees its index rows in short steps. A large run takes about twice as long to delete and no longer blocks other index writes.
- An index write waits out the lock of another process up to three times, with a warning, before it fails. A write for new events still fails at once and catches up from the log.
- Opening a data directory deletes temp files of blob and truth writes that are over an hour old.
- A new event, `sandbox.freeze_failed`, records a sandbox that did not freeze or unfreeze when asked, with the state wanted and the error. The operator, the monitor and an agent's own gate all write it. The run view shows it as a Sandbox note.
- A command in a container paused outside AIMS, as with `docker pause`, waits until the container runs again. The wait does not count against its timeout. It failed before and its sandbox was stopped. A resume of the agent leaves such a pause alone.
- `aims score` and `aims models check` stopped by Ctrl-C, SIGTERM or a hangup seal their run as cancelled and exit 130. A second Ctrl-C cancels nothing more.
- `aims run --json` and `aims monitor --json` exit 1 when a JSON line could not be written, other than to a closed pipe. The line is skipped with one warning per run and the next lines still come.
- A second stop of `aims runner` kills its runs. Stops after that, or once it closes, do nothing.
- The server database moves to version 6. A command's state is kept in its own column. An older AIMS refuses the file.
- A new event, `sandbox.outside_pause`, records a pause of a sandbox's container made outside AIMS, as with `docker pause`, and its end. AIMS notices such a pause when Docker refuses a command for it. The run view shows it as a Sandbox note.
- `exec.finished` says in `error` why the command of a cancelled or timed out exec may still run: the sandbox was paused, the kill failed, or the command did not end in 5 s.
- The asset images of another AIMS version, such as the toolbox and the tracer, go one day after their last use. They went after two minutes, so two versions or checkouts on one host built them again at every start.
- The build lock file is readable by every user, so two users of one Docker host can build the same image. A lock file left by AIMS 0.2.2 or older that another user owns still fails: delete it.
- The server database moves to version 7. A command keeps whether it took effect only in part in its own column, `in_part`, which `GET /api/runs/{id}/commands` returns. `aims control` and the run view read it. An older AIMS refuses the file.
- A read of a run whose log is there but whose index is not yet answers 503 with `Retry-After`, as while another process rebuilds the index. It answered 404.
- A cancel that lands while a run starts leaves the run sealed `cancelled`, with the reason `cancelled as the run started`. A start that fails leaves no run folder.
- A stopped `aims monitor`, `aims score` or `aims models check` says how each run was sealed, read from the store.
- `aims monitor` waits for every monitor run to end before it reports one that failed.
- A search of one run reads the run's span once. On a 55,000 event run a query takes about 8 ms, down from 14 ms.
- A third Ctrl-C or SIGTERM quits `aims run`, `aims monitor`, `aims score`, `aims models check`, `aims runner` and `aims ui` at once with exit code 130 and no cleanup. The second one says so first. A hangup never counts. `aims ui` kills its local runner before it quits. Sandboxes left behind are removed at the next start.
- A viewer's stream sends nothing of a run whose kind the control plane does not know yet, as while the index is rebuilt. When the run turns out to be one a viewer may not read, the stream ends and a new one answers 403.
- The score, monitor, fork, sweep, pause, resume, inject, kill and cancel routes of a run answer 503 with `Retry-After` while the run's log is there but its index is not. So does `GET /api/runs/{id}/monitor/estimate`. They answered 404, forks 422.
- `DELETE /api/runs/{id}` and `GET /api/runs/{id}/dependents` answer 503 with `Retry-After` while the index does not hold a run made from that run yet, forced or not. The retention sweep keeps such a family until the index holds it.
- `aims runner` prints `cannot start the sandbox backend` with the reason and exits 1 when Docker is down. It printed a traceback.
- A build lock file this user cannot open is reported with its path and what to do. A tracer whose image cannot be built for that reason leaves the sandboxes on snapshot audit.
- A kernel tracer loss is logged in a sandbox only from the time the sandbox registered. A loss that ended before then is left out.
- The judge's timeout covers the wait for the index and the read of the run. A judge whose index was behind could take about twice `timeout_s`. A judge that cannot read the run in time ends with `the judge's N s timeout ran out while it read the run`.
- The judge's `events` tool shortens an event with a large nested object in steps. The last step keeps four keys of each nested object and counts the rest under `[more keys]`. Such an event showed as `too long to show`. A score with many metrics is shortened the same way in the judge's brief.
- The judge's `events` tool shows the end of `stdout_tail` and `stderr_tail` as `tail`. It showed mostly their start, so the summary line of a test run was left to a `blob` call.
- The judge's `proc_tree` tool cuts a long command, working directory and leftover list until the tree's rows fit. It answered `ask for less` for an exec with nothing to narrow. `leftover_more` counts the leftover processes it does not list.
- The judge's `file_history` tool ends a page between events. A snapshot with several rows for the path showed some of them again on the next page, and one with more rows than a page came back as the same page forever. Such a page now holds what fits and `rows_cut` counts the rest.
- A judge reply that comes with no token counts is charged by its size at the profile's prices. It was charged $0, so the judge could spend past its share with a provider that leaves the counts out.
- `docs/monitor.md` lists the judge settings.

### Fixed

- A denied tool call with thousands of distinct words made `deny_retry` hold the runner for seconds at each later call. The look runs in a thread now and ends at about 2 s, which the monitor reports as degraded.
- A stored job whose `needs` this version cannot read made the run list answer 500 and held every queued job after it.
- A reply the Docker daemon cut after its header, or an exec stream that lost its socket as it opened, raised an error the sandbox code did not catch. A cut container delete left the sandbox's volumes behind.
- An unexplained kernel drop was reported to sandboxes that registered after it.
- Reads of a run's events, steps, samples, counts and costs gave rows past the run's head or rows a removal had not deleted yet. A resent ingest batch could be refused as diverged over such rows.
- A batch that met many leftover index rows deleted them in one transaction, which could hold the index's write lock for seconds. They go 200 to a transaction now.
- A truth file sent while the run's index rows were built again failed with an error.
- `verifier_env` flagged a variable the harness set when the tracer's 4 KiB cut ended in the middle of it.
- `verifier_anomaly` kept subtests in the test count when pytest showed them only as progress letters: xfailed ones at `-q`, failed ones with `-rN`.
- `link_redirect` flagged every link into the workdir when the workdir or an `inside` root was written with a trailing slash, such as `/tmp/`.
- A windowed `distinct` rule and the checks for an exchange between two agents kept one entry per match or read. A burst of 200,000 held about 28 MB. Each keeps one entry per value or pair of agents now.
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
- Brackets in agent text, such as `[/x]` in a flag, crashed the end tables of `aims run` and `aims monitor`. `aims monitor` then never shipped its runs and both exited 1. Brackets and emoji codes now print as written.
- `aims run --replay '[/x]'` and scenario errors with brackets ended in a crash with no error line.
- Tool output and model text reached the terminal with escape bytes in the live view and the pause prompt. The prompt also hid `[#seq]` before each flag and its `[resume]` hint.
- A viewer could forge a row of `aims audit` through the path of a refused request.
- A second Ctrl-C while `aims monitor` sealed its runs left them `running`. They now seal as cancelled, also for a runner's monitor job cancelled twice.
- `aims token create --file` in a missing folder ended in a traceback after the old token was revoked, with the new value lost. Its temp file had a fixed name and followed a symlink put there.
- `aims push` against a server that is down or refuses ended in a traceback. So did a missing or unreadable `--token-file`, a broken `server.json` or `server.sqlite`, a path that cannot be written by `aims report` or `aims openapi`. A bad TLS file did too.
- `aims monitor --help` offered `off`, which the command refuses. `--token-file` named the runner token on commands that take an operator token.
- `credential_flow` and `joint_action` scanned every shared-folder write on each file read. With 10,000 shared files a read took 7 ms on the runner's loop. It now costs the same for any number of files. Writes older than the window are forgotten. Flags are unchanged.
- The public build context of a scenario with a Dockerfile is built in linear time and off the event loop. A context of 20,000 files held up a run start, a fork and each fork estimate for 20 s.
- A named pipe or socket in a `task.files` source folder no longer hangs the start of a run. It is skipped, as the scenario loader skips it.
- A sandbox whose kill failed while a run was cancelled is still removed with its volumes right away.
- One tracer line the reader fails on no longer stops the kernel trace of every sandbox. The record is logged as `audit.lost`.
- A kernel record the run log refuses no longer ends a sandbox's audit or fails the exec it arrived in. Only that record is lost and logged as `audit.lost`. A failed write no longer stops the audit from reading later records.
- A file dated past the year 2262 is reported at the latest time the log holds. Its time wrapped to a date in the past.
- Inode numbers past 2^53 in an exec's file descriptors or a pipe write are clamped like the other kernel numbers. They made the log refuse the record.
- The live steps list re-read up to 500 rows on every batch when one agent had stopped early. It now reads on from the first row that can still change.
- The timeline's IO readout is a true rate per second. After a gap between sampler windows it showed the whole gap's bytes as one second's.
- A viewer who signed in after an operator in the same tab could see the operator's cached score and self-test rows for up to 5 minutes.
- Saving a scenario file keeps its CRLF or CR line endings and its byte order mark. One edit rewrote every line ending. An edit that is undone no longer shows "unsaved changes".
- A run deleted while a run list loads its next page no longer comes back until a reload.
- Two quick clicks on Check start one paid model check.
- The New run form no longer opens on another scenario when the one asked for is missing from its cached list. It reads the list again, then says why that scenario cannot run.
- A From or To time the audit log cannot use, such as a six-digit year, is left out and marked. It crashed the page.
- A kill or resume that took effect just as the run ended showed as `dropped: the run ended`.
- A cancelled `aims score`, score job or `aims models check` is sealed `cancelled` with a reason. A score was sealed `completed` and a model check `failed`.
- A queued run killed with no reason recorded "killled by".
- The pause prompt of `aims run` shows as a warning what an answer did not carry out, such as a sandbox that did not unfreeze.
- An agent that starts while the monitor's sweeps keep failing is held like the others. It ran unwatched.
- A freeze or unfreeze call that fails no longer leaves the runner wrong about the sandbox. It reads the container's real state.
- An agent whose sandbox does not freeze at its gate still holds there. It ended with an error.
- A sandbox start that failed or was cancelled left its volumes and container until the next start of the runner.
- A cancelled setup or tool command kept running in the sandbox, for good in one kept with `--keep`.
- A late Ctrl-C in `aims run` or `aims monitor` could cut the store's close and end in a traceback. A run that completed still exits 0.
- A fork, monitor or config request with a lone surrogate no longer answers 500, at the request or when a runner claims the job. A job stored with one is claimed with it as text. A key with one was stored as another key.
- A `Last-Event-ID` such as `run:²`, `run:--5` or one with thousands of digits no longer breaks the live stream. A part that does not read is skipped.
- A `reward.json` over 256 KiB no longer fails the run.
- `aims control --text` with a byte that is not UTF-8 ended in a traceback. Such an `--inject` failed the fork's next model call, and such a `--series` name failed in the index.
- Usage errors showed bidi and escape characters from the command line as they were. They show as text.
- `aims ui --public-url` printed hidden characters of the URL as they were.
- The live view kept every `monitor.degraded` note. It shows the last three. The end table and `--json` list all.
- `aims run --budget cost_usd=2,50` names the comma. It said `'50': write key=value`.
- The budget cells in the UI read `0x10` as 16, `1e3` as 1000 and `_10_` as 10.
- `aims images prune --days nan` counted every scenario image as unused. `aims monitor --budget-usd nan` ended in a traceback.
- An index read with a filter that holds a lone surrogate finds no rows. It raised an encoding error.
- A verifier run that printed more than the output limit lost its pytest summary. An honest run with exit 0 got `no_summary` at 4.
- `PYTHONPATH=//work/hack` in a verifier's environment escaped `verifier_env`.
- `verifier_anomaly` counted a failed subtest and its test as two failed tests. It takes the subtests the output names off the count.
- `submit_vs_score` flagged "No errors and test_x fails." and a pasted summary with "3 failed" and "19 passed" on two lines. It spared "Fixed the bug that made the tests fail; all pass now." and "raises errors for bad input and all tests pass".
- A rule with `distinct` on a list field such as `argv` failed at its first match and raised no flag.
- A windowed `distinct` rule, the checks for an exchange between two agents and the check for agent-written files under a verifier's path each took time that grew with what the run had done so far. A fast stream of events slowed the runner.
- A file of `def __eq__(` lines took `hack_pattern` time that grew with the square of its length.
- A cancel during a token revoke, a job or command write, or a run delete could leave a transaction open on `server.sqlite`.
- One corrupt run stopped a runner from shipping its other runs at start.
- One event the index could not take held back up to 999 events before it.
- A cost total past 64 bits made every index write of that run fail.
- A run's variant stayed unset when its write failed just after the seal.
- A shared folder with a large sparse file cost the runner twice the file's size in memory at the end of a run.
- Score and monitor estimates of a run answered 500 while its delete ran.
- `organism.pack` packed the folder of a deleted run a crash had left.
- A run, job or truth record stored with a judge `max_output_tokens` from 256 to 1023 loads again, with the cap raised to 1024. Forks, `aims monitor`, scoring and queued jobs of such runs failed. A new config below 1024 is still refused.
- A fork at a cut of several agents checks the profile each agent goes live with. It was refused when the profile another agent used after the earliest cut step was gone from the config. A fork job carries the profiles its agents run.
- A fork of a run whose provider gave tool calls of different steps the same id replays. It failed with "replay of step N failed".
- A fork after replies without a tool call no longer ends `no_submit` during the replay.
- The fork refusal for an agent that ended in the middle of a step while the run goes on says the agent ended. It said "yet".
- The judge plans its next call right after a reply with no token counts. It planned too little input before.
- A judge forced to a verdict at its first call no longer stops for budget when its money covers the call.
- On the run page a step logged while the step list loaded was missing until the next step.
- The run page follows a run to its last event. A run that ended while its page loaded again could miss its last steps and raw events.
- The Raw tab of a run opened again after a visit elsewhere shows the events logged in between.
- A live Raw list past 10,000 events keeps the rows in view. It let the oldest go, also under a reader. Past that size it reads new events a page at a time.
- A tab notices when another tab signed in as someone else or signed out. It checks when it comes back into view, then shows the new session with none of the old one's data.
- Runs that ended while the control plane was out of reach show as ended after **Try again**. They stayed "Running" until a reload.
- A report built from a review still going is built again when the review ends. It could lag 15 seconds.
- A self-test or model check cancelled in the queue shows no empty table.
- An error under a score label or a report download no longer stays after another score or source is picked.
- The run page stops asking for a command's answer once the page is left. The command stays sent.
- **Sign out** asks before it drops an unsaved file or config draft.
- **Duplicate**, **Run**, **Self-test** and a new file on a scenario page ask about an unsaved draft before they act. They asked after the copy, run or file was made.
- A problem in a scenario file with mixed line endings is marked on the right line after a lone carriage return.
- A self-test script that is a named pipe is refused. A task file swapped for a named pipe while its scenario loads is skipped. Both made the load wait for a writer.
- On a full Linux disk a truth file or a label line could be cut short with no error. The write now fails and leaves nothing half written. A truth file the control plane stored is synced with its folder.
- Forking, scoring or monitoring a run with a missing or damaged blob answers 422 with the reason. It was a 500.
- One run that fails on a runner no longer stops the others shipping at start. The runner tries again and warns when the failure lasts. A prune that fails is logged and tried again the next hour.
- A truth upload whose index write failed is kept on the retry, names the variant and starts the score once. Two uploads at once start one score. Opening a data directory names a variant its index lost.
- Verifying a run that is still going no longer says "log has N events, the index recorded M". A report of a live run said its chain did not verify.
- The verification of a run whose folder is missing no longer shows the server's data path.
- A batch whose sync to disk fails is cut back off the log. A retry wrote it twice before. The run stays locked to its writer, so a runner started on the same data directory no longer seals it as failed.
- A torn log tail is stored before it is cut, so a full disk no longer loses it.
- An index that does not open on a full disk names the disk as the likely cause.
- A stored hold over 30 days loads at 30 days in a scenario too. Forks, scores and monitor runs of such runs work again.
- Old runs that record a scenario key since removed, or no agent texts, load again for a fork, a score or a monitor run.
- A recorded cost below 0 counts as no price and a huge one is capped. A run's total no longer depends on how its events were batched.
- An estimate no longer fails when its cost rows cannot be saved.
- Labels a runner sends again after a failed index write are indexed. A flag can be labelled while the index is behind or being rebuilt.
- Shipping no longer sends log lines a writer opening at that moment has not synced.
- A kill that did not stop a sandbox says so in the command's answer. It was only logged. Closing the sandbox kills it again.
- A shared folder left out of the archive says why in its `audit.lost` reason: the cap or the error.
- Archiving a shared folder with many files no longer holds all of them in memory at once.
- A cancel while Docker creates a container no longer leaves that container or its volumes behind. A helper container being deleted when a cancel lands is still deleted.
- A cancelled image build or pull stops in the daemon at once. It ran on to its end before. Images a cut build left with no tag are removed with the old ones.
- The build lock file is deleted when the lock is released.
- A cancel no longer leaves the connection of an exec or attach stream open when the stream was still opening.
- A cancel before an exec started returns at once. It waited 5 seconds and logged a warning.
- Cancelling one of two identical commands that run at once in a sandbox ends only that one. Both died before. Each exec's `wrapper` now holds `--id` and an id of its own.
- A cancelled command in a sandbox at its pids limit is ended. It ran on, since the kill could not start. A cancelled command that could not be ended is logged with the reason.
- A pause that lands just before a tool's command starts no longer fails that command and stops the sandbox. The command waits for the resume. The wait does not count against its timeout.
- The index refuses a batch of events that does not continue the ones it holds of the run. Lines cut from a log after a failed write, which another process read first and indexed late, mixed with the lines written in their place before, and `aims verify` passed.
- A run made again under the id of a deleted one no longer shows events of the old run. Index rows a delete cut short left behind go first, and a run made during the delete starts once the delete is done.
- A delete cut short for a run whose folder was already gone is finished by the next start. Its index rows stayed for good before.
- A run another process deleted is no longer indexed again from a log read just before, and no longer retried with no end by the index catch-up or by its shipper. The shipper ends.
- `aims verify` no longer reports a gap when another process rebuilds the index at that moment.
- A flag can be labelled over the API while another process rebuilds the index. It answered 404.
- A truth file whose folder could not be synced is taken away again, so the upload that is sent again is kept and starts the score. It was answered as kept before and no score started.
- The record of an unfinished tail cut off a log is written with the next batch. A first write that failed lost it before.
- A failed write of copied log lines no longer cuts a file that reused the log's number after the store closed.
- An index write that waits out another process's lock lets the writes of other runs try in between. They waited up to 15 seconds behind it.
- A run whose index was marked corrupt logs that once. Each later event logged two errors before. Its index takes events again after a rebuild.
- SQLite's integrity check of the text index passes for runs with resource windows. It reported the index as malformed, though nothing was damaged.
- A log read for one run no longer holds up opening the log of another, and the index catch-up reads no line a writer that opens meanwhile has not synced.
- An agent held at a gate goes on when its sandbox does not unfreeze. The unfreeze is tried three times first. The agent ended with an error before.
- The verifier unfreezes a sandbox left frozen before it runs. One that stays frozen is scored with an error. The verifier waited without end before.
- A pause that lands as an agent finishes no longer leaves its sandbox frozen for the verifier.
- A model check ended by an error is sealed with that error as its reason.
- A score job or model check that fails before its run begins stops shipping it. The runner kept asking for the run, and its stop waited 30 seconds.
- A forced delete of a run cancels the monitor job, score job or model check that works on it. The job ran on and the run stayed on the runner.
- A command a runner carried out reads as done whatever its answer text starts with. Text that began with `failed` or `dropped` was read as that state.
- The run list stream sends a run again once its job moves on after new events. It could keep showing a finished run as running.
- A cancel that comes as a finished monitor run closes no longer cuts its seal.
- A Ctrl-C while `aims ui` closes no longer ends in a traceback.
- A stop while `aims runner` closes no longer cuts a store close.
- A failed `aims token create` removes the data folder it made, when no token was written.
- `aims audit` prints nothing when a row fails to show. It printed the rows before it.
- A command that runs past its whole deadline is killed. One not wrapped by `aims-exec` ran on.
- A cancel that lands while a command's start is logged logs its end too. The run held an `exec.started` with no `exec.finished`.
- A cancelled command is stopped before its children are killed, so a child it forks at that moment no longer runs on.
- A cancel that lands while a sandbox's volumes are made waits for Docker's answer. A volume made late stayed.
- A cancel that lands while the tracer stops, or while a failed metadata sidecar is removed, is no longer dropped.
- A build whose progress ends with no image fails. It returned a tag that did not exist.
- A sandbox that holds more than about 250 agent processes lists them all. It listed none, so leftover processes were not recorded, not killed and not traced.
- A second Ctrl-C of `aims run` with several samples, or one run's error, waits for every run to seal before the store closes. Other runs could be left unsealed.
- A stopped `aims monitor` waits for every monitor run to seal. One could be left `running`.
- A deleted self-test stops at once, also between two cases. A cancelled self-test is sealed `cancelled` with a reason, and a second cancel no longer cuts the seal.
- A job cancelled before its task first ran frees its slot.
- A job whose run has lines is no longer given back while the runner's index is behind its log.
- The run list stream sends a job again once it ends at a claim that does not load.
- A refused `aims runner` started by `aims ui` no longer says it stops because `aims ui` ended.
- A failed `aims token create` removes only the database file it made. It left an empty one in an existing folder, and could delete one another process made.
- One audit row with a time no date holds no longer breaks `aims audit` or `GET /api/audit`. The time shows as the first or last one there is.
- `aims run` and `aims monitor` end quietly, with the runs' exit code, when the reader of their output closes the pipe, as `| head` does. They logged a traceback and exited 120.
- A run whose writer finds lines in the index that a failed write cut off its log is indexed again from its log. It was marked corrupt until a rebuild.
- A shipper whose run is deleted on the runner before it first read it ends. It retried without end.
- A search shorter than three characters folds case as longer ones do: `Éc` finds `école`.
- A second Ctrl-C while a sandbox closes or is killed waits for its last records and its `sandbox.stopped` line. They could be cut.
- A cancel while an IMDS server closes still removes its container, and one while the runner's link closes still closes its connection.
- A cancel that lands in the moment a cancelled background task ends is raised. It was dropped, so a stop could be lost.
- A build or pull the Docker daemon cuts midway is a Docker error like any other: an asset image is built when its pull is cut, and a tracer whose image cannot be made leaves the sandboxes on snapshot audit.
- Build lock files that no one holds are removed a day after they were made, at each backend start and hourly in a runner. AIMS 0.2.2 and older left one per image tag in the temp folder.
- A build waits up to a second for a lock file that another user is just making. It failed when it opened the file before its mode was set.
- A job stored with a kind this version does not know is left out of the run list and the stream. `GET /api/runs` answered 500.
- A log whose only line is torn no longer counts as a run still to be indexed. Its reads answered 503 without end.
- Index rows left past a run's last event by a drop that raced another process go with the next batch. Every later catch-up of that run failed.
- While a run is indexed again from its log, readers see no run and then a growing first part of it. They saw a run that lost events. A stream could skip events or fail.
- `aims token list` and `aims control list` show a stored time no date holds as the first or last date there is. They failed.
- `aims push`, a runner's monitor job and a self-test end the shipping of a run that was deleted under them. It waited without end.
- `aims run --score` stops shipping a score run that never began. The link waited a minute for it at the end.
- The cleanup of `aims run` closes its link when an earlier step fails.
- A stream the Docker daemon cuts midway is a Docker error like any other: an archive read, a helper's wait or log, an exec or attach stream as it opens. An exec whose stream is cut as it opens ends as failed with its `exec.finished` line.
- The runner's tracer watch goes on after a restart fails in any way.
- An autonomous or human pause review gets the flagged tool calls while the index is behind the log. It read them from the index only and reviewed without them.
- A fork replays a call that reads a board or writes a shared folder while the index is behind the log. It found none of the call's channel events and recorded a channel divergence, which ended a strict fork.

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

[Unreleased]: https://github.com/kaumnen/AIMS/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/kaumnen/AIMS/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/kaumnen/AIMS/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/kaumnen/AIMS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kaumnen/AIMS/compare/v0.2.0rc1...v0.2.0
[0.2.0rc1]: https://github.com/kaumnen/AIMS/compare/v0.1.0...v0.2.0rc1
[0.1.0]: https://github.com/kaumnen/AIMS/releases/tag/v0.1.0
