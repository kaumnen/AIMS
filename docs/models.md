# Models

- [Profiles](#profiles)
- [Profile fields](#profile-fields)
- [Keys](#keys)
- [List prices](#list-prices)
- [Add a model](#add-a-model)
- [Headers and extra fields](#headers-and-extra-fields)
- [Rate limits](#rate-limits)
- [What a run records](#what-a-run-records)
- [Check a profile](#check-a-profile)

## Profiles

AIMS ships no models. A profile says how to reach one model. The roles `agent`, `sweep` and `judge` each name a profile. `agent` runs the agents. `sweep` and `judge` run the monitor's model layers. A scenario can give one of its agents another profile.

```toml
# aims.toml
[profiles.my-agent]
backend = "chat"
base_url = "https://api.example.com/v1"
model = "vendor/model-name"
weights = "vendor/model-name"
api_key_env = "EXAMPLE_API_KEY"
reasoning = "none"
price = { input = 1.00, cached = 0.10, output = 4.00 }

[profiles.my-monitor]
extends = "my-agent"
model = "vendor/monitor-model"
weights = "vendor/monitor-model"

[roles]
agent = "my-agent"
sweep = "my-monitor"
judge = "my-monitor"
```

- A role may be unset. A run that needs it stops before any container starts. It names the fix: set the role, give the agents a `--profile` or run with `--monitor signals`.
- `aims scenario check` passes with unset roles. It notes each one and skips the estimate.
- `extends = "<profile>"` starts a profile from another one.
- A table named after a profile of a lower config layer merges into it key by key.
- Keys come from the environment only. A profile names the variable, never the key. `AIMS_KEYS` lists the URLs each key may go to. See [Keys](#keys).
- `aims models list` shows the profiles, the roles and the source of each price. It says whether each profile can be called: `ready`, the `AIMS_KEYS` entry it needs or the variable that is not set.

## Profile fields

| Field | Meaning |
| --- | --- |
| `backend` | `chat` for the Chat Completions API, `responses` for the Responses API |
| `base_url` or `base_url_env` | The API's address, or the variable that holds it. Set exactly one. |
| `model` | The model's id at the provider |
| `weights` | The trained model behind it. Profiles that serve one model share it. The monitor refuses to watch an agent on its own weights. |
| `api_key_env` | The variable that holds the key. Leave it out for a server that takes none. [`AIMS_KEYS`](#keys) lists where the key may go. |
| `reasoning` | What the model sends of its reasoning: `none`, `summary` or `full` |
| `effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max` |
| `effort_kwarg` | `chat` only. A chat template argument that carries the effort instead of `reasoning_effort`. |
| `summary` | `responses` only. `auto`, `concise` or `detailed`. |
| `max_output_tokens` | The cap on one answer |
| `context_tokens` | The model's context size. Compaction and the estimate's ceiling need it. |
| `stream` | `true` by default |
| `parallel_tool_calls` | Sent only when set |
| `structured_output` | `false` when the server cannot answer to a JSON schema. Such a model cannot be the sweep or judge model. |
| `cache_key` | Sends `prompt_cache_key`, so a run's calls share one prompt cache. On by default for `responses`, off for `chat`. |
| `sampling` | `temperature`, `top_p`, `top_k`, `seed` |
| `headers` | HTTP headers for every request of the profile |
| `extra` | Other fields of the request body, such as a router's options. See [Headers and extra fields](#headers-and-extra-fields). |
| `price` | USD per million tokens: `input`, `cached`, `output`. Wins over `price_key`. |
| `price_key` | A row of LiteLLM's price map. Calls count at its list price. See [List prices](#list-prices). |
| `limits` | `rpm`, `tpm`, `concurrency` and a `group` that profiles can share. See [Rate limits](#rate-limits). |
| `timeouts` | Seconds: `connect`, `stall`, `wall` |
| `retries` | `attempts`, `backoff`, `max`, `rate_limited`. See [Rate limits](#rate-limits). |

A scenario can override `effort`, `max_output_tokens` and `sampling` for one of its agents.

## Keys

A profile's key goes only to the URLs that the `AIMS_KEYS` env var lists for it:

```sh
export EXAMPLE_API_KEY=<your key>
export AIMS_KEYS=EXAMPLE_API_KEY=https://api.example.com/v1
```

Commas separate the entries. A name may repeat for more URLs. Each profile needs the entry that fits how it reads its URL and key:

| The profile sets | Entry | Meaning |
| --- | --- | --- |
| `base_url` and `api_key_env` | `KEY=URL` | The key may go to that URL and the paths under it. |
| `base_url_env` and `api_key_env` | `KEY=env:VAR` | The key may go to the URL in `VAR`. |
| `base_url_env` only | `env:VAR` | AIMS may read the URL in `VAR`. |
| `base_url` only | None | AIMS calls the URL without a key. |

Here `KEY` and `VAR` stand for the variables the profile names.

- AIMS reads no other env var for a model's URL or key.
- A URL entry covers only profiles with a `base_url`. An `env:` entry covers only profiles that read that variable.
- The scheme, host and port must match. The path matches whole segments, so `/v1` does not cover `/v10`.
- An entry with a user name, password, query, fragment or `%` in its URL is refused.
- A profile URL with a user name or password is refused too, since the run records the URL.
- A variable is a key or a URL variable, never both.
- Names that start with `AIMS_` are AIMS's own. No profile can read or send them.
- In an env file, write the value without quotes. `docker run --env-file` keeps quotes as part of it.

`--key <entry>` adds an entry for one command. Repeat it for more. `aims run`, `aims monitor`, `aims score`, `aims models check`, `aims models add`, `aims runner` and `aims ui` take it. The runner that `aims ui` starts gets the entries too.

A call whose entry is missing fails before any request. The error names the entry, the same whether the variable is set or not:

```text
error: profile my-agent: AIMS_KEYS lacks EXAMPLE_API_KEY=https://api.example.com/v1; add it there or pass --key EXAMPLE_API_KEY=https://api.example.com/v1
```

Config files cannot set `AIMS_KEYS`. No config layer and no edit in the app can change it.

### Runners

- A runner reads `AIMS_KEYS` when it starts. Restart it after a change. With Compose, run `docker compose up -d` after you edit `.env`. `docker compose restart` keeps the old value.
- A runner tells the control plane its entries whose variables are set. It sends names and URLs, never values.
- The control plane offers a runner only the jobs whose profiles its entries allow. The runner checks again before each call.
- A new run, model check, review or score that no runner allows is refused with the entry to add. A queued job waits and names the entry it needs.
- **Settings** > **Models** shows each profile's entry and the runners that allow it. **Settings** > **Runners** lists each runner's entries.

## List prices

A profile with no `price` counts a call only when its provider reports the cost. No cap holds on the other calls. Estimates leave them out. `price_key` names a row of [LiteLLM's price map](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) instead. Runs, forks, reviews, scores and estimates then count every call at its list price.

```toml
[profiles.my-agent]
price_key = "provider/model-name"
```

`aims models prices` finds the key:

```sh
aims models prices my-agent
aims models prices my-agent --search model-name
```

It lists the rows that fit an unpriced profile, with their prices. A row fits when its key is the model id, ends in `/` and the id, or ends in the id's last part: `openai/gpt-4o` finds `gpt-4o` and `azure/gpt-4o`. `--search` lists the keys that hold the text. Prices differ by provider and region, so pick the row of the provider the profile calls.

In the app, **Settings** > **Models** shows each profile's price and its source. **Find a price key** lists the same rows and copies the `price_key` line.

What a row gives:

- Input, cached input and output prices. Only rows of chat models have them.
- A row with no cached price counts cached input at the input price.
- A row with a higher price past some prompt size gives its base price. The estimate says from which size.
- Batch, flex and priority prices are left out.

<details>
<summary>When AIMS reads the map</summary>

- Only when it needs a row: a run, fork, review, score or estimate that calls a profile with a `price_key` and no `price`, `aims models prices` and the Models page.
- Where profiles are resolved: `aims run`, `aims monitor`, `aims score`, `aims estimate` or the control plane. A runner never reads it. Its job carries the price.
- From `raw.githubusercontent.com` with no key, through the [proxy settings](deploy.md#proxies). It waits 5 seconds at most.
- It keeps a copy, `litellm-prices.json.gz`, in the cache folder: `$AIMS_CACHE_DIR` when set, else `%LOCALAPPDATA%\aims\cache` on Windows, else `$XDG_CACHE_HOME/aims` or `~/.cache/aims`.
- A copy older than 24 hours is checked with GitHub again.
- When GitHub does not answer, the copy serves at any age, with its date. The next try comes 10 minutes later.
- With no copy, or no row for the key, the profile stays unpriced. Its runs still start. The estimate says why.

</details>

Each list price names its source, key and date. The date is when GitHub last confirmed the copy. `aims estimate`, `aims run`, `aims monitor` and `aims score` print it, here with placeholder figures:

```text
Profile my-agent: LiteLLM list price for provider/model-name, fetched 2026-11-15: $1.00 input, $0.10 cached, $4.00 output per million tokens.
```

What a run does with it:

- A call costs what its provider reported. Without a report it costs the list price times the tokens. Its `cost_source` is then `list`.
- The agents' cost caps and the monitor's budget count that cost. The monitor's budget is sized at the list prices of its models.
- The run records the profile with the list price in `price` and the copy's date in `listed`. A config file may not set `listed`.
- A fork keeps the list price its origin recorded. When the origin recorded none, the fork takes the price of the config's profile of the same name, if that names the same model. A `--profile` profile and the fork's monitor get the current list price.

> [!IMPORTANT]
> A cap on a list-priced call holds on its cost at the list price. The provider's bill can be higher.

## Add a model

One command drafts a profile for a model on any server, checks it and adds it:

```sh
aims models add example/model-name --base-url https://api.example.com/v1 --key-env EXAMPLE_API_KEY --write --role agent
```

1. It checks the id against the server's list of models, read without a key, when the server has one. It skips a host on a link-local or cloud metadata address, with a note.
2. A LiteLLM row picked with `--litellm` gives the price key, the context size and the output cap.
3. It sends the model a few small requests, as `aims models check` does. It sets `reasoning` to what the model sent. These need the profile's entry in [`AIMS_KEYS`](#keys).
4. It prints the profile to stdout and its notes to stderr.

The values here are placeholders:

```toml
[profiles.model-name]
backend = "chat"
base_url = "https://api.example.com/v1"
model = "example/model-name"
weights = "example/model-name"
api_key_env = "EXAMPLE_API_KEY"
reasoning = "full"
price_key = "example/model-name"
context_tokens = 128000
max_output_tokens = 16384
```

`weights` is the model id. When the model also runs under another id, give both profiles the same `weights`.

| Option | Effect |
| --- | --- |
| `--base-url <url>` | Sets `base_url` |
| `--key-env <name>` | Sets `api_key_env` |
| `--from <profile>` | Reaches the server as that profile does: its backend, URL, key env, headers, `extra` and other connection fields. Instead of `--base-url` and `--key-env`. |
| `--name <name>` | The profile's name. The id's last part by default. |
| `--litellm <key>` | The model's row in LiteLLM's price map. Without it the notes name the rows that may fit. |
| `--backend <backend>` | `chat` or `responses`. By default `chat`, or the `--from` profile's. |
| `--set <key>=<value>` | Sets a field to a TOML value, such as `effort=low` or `extra.a.b=1`. Repeatable. |
| `--unset <key>` | Removes a field the draft sets. Repeatable. |
| `--write` | Adds the profile to the project's `aims.toml`. Makes the file when there is none. Refuses a name that is taken. |
| `--role <role>` | Also sets a role under `[roles]`: `agent`, `sweep` or `judge`. Repeatable. Needs `--write`. Sweep and judge need structured output. Names the profile that had the role. |
| `--key <entry>` | Adds an entry to `AIMS_KEYS` for the check. Repeatable. |
| `--no-check` | Skips the check. `reasoning` stays a guess. Needs no `AIMS_KEYS` entry. |
| `--config <path>` | An `aims.toml` to use instead of the project's |
| `--runs-dir <dir>` | Where the check is recorded as a run. `runs` by default. |

> [!NOTE]
> The server may bill the check's few small calls. When a test fails, the command writes nothing and exits 1. Fix the profile with `--set` or pass `--no-check`. A model without tool calls fails the `tools` test, so add it with `--no-check`.

Operators can do the same in the app under **Settings** > **Models** > **Add a model**. The profile text can be edited first. The check runs on a runner whose `AIMS_KEYS` allows the profile. When no runner does, Add and check saves nothing and names the entry. The app saves the profile, then sets the ticked roles once its check passes. A later check does not set them. A profile whose check failed can be removed from its row.

Then run with it:

```sh
aims run cache-keys --profile model-name --monitor signals
```

`--monitor signals` needs no monitor model. Give a second model the sweep and judge roles for the `standard` monitor.

## Headers and extra fields

`extra` adds fields to the request body as written, such as a router's own options. This one keeps a run on one provider of a router that reads a `provider` object, so the price, the quantization and the prompt cache stay the same:

```toml
[profiles.my-agent]
extra = { provider = { only = ["provider-a"], allow_fallbacks = false, require_parameters = true } }
```

`headers` adds HTTP headers to each request of the profile:

```toml
[profiles.my-agent]
headers = { X-Title = "my-bench" }
```

> [!WARNING]
> A run records its profiles with their headers. Keep secrets out of them. AIMS refuses a header that it sets itself or that carries a key, such as `Authorization` or `Cookie`.

A call carries only the client's own headers, the profile's `headers` and its key. AIMS ignores `OPENAI_CUSTOM_HEADERS`.

Config layers and `extends` merge `headers` key by key. Header names ignore case, so `x-title` in a layer replaces `X-Title` from below. A layer's `extra` replaces the whole `extra` below it. Give it every field the profile needs, such as `cache_control`.

## Rate limits

`limits` caps calls on the client side. Profiles with the same `group` share the caps. A profile with no group is a group of its own.

| Field | What it caps |
| --- | --- |
| `rpm` | Requests in any 60 s |
| `tpm` | Tokens a minute. The count refills over the minute. |
| `concurrency` | Calls in flight, 8 by default |

A provider's limit often covers every model of one account. Put the profiles of one account in one group, with an `rpm` at or under the account's limit:

```toml
[profiles.my-agent.limits]
group = "my-account"
rpm = 20

[profiles.my-monitor.limits]
group = "my-account"
rpm = 20
```

Give each profile of a group the same caps. The group takes them from the first profile that calls. When the provider's `x-ratelimit-remaining-requests` or `x-ratelimit-remaining-tokens` header says less is left, the caps drop to that.

A 429, or a failed call whose answer says how long to wait, pauses every profile with the same base URL and key env until the retry. The wait is the one the provider asks for, up to `retries.max`. Without one it is the backoff.

`retries` sets how a failed call is tried again:

| Field | Default | Meaning |
| --- | --- | --- |
| `attempts` | 4 | Tries of one call, the first one included |
| `backoff` | 1 | The first wait in seconds. Each wait doubles, with jitter. |
| `max` | 60 | The longest wait in seconds, also when the provider asks for more |
| `rate_limited` | 120 | Seconds from a call's first 429 in which its 429 retries do not count toward `attempts` |

A 429 retry with no wait counts toward `attempts`, so a profile with `backoff = 0` still stops.

Under a spend cap, the judge and the score retry a failed call only when another full attempt fits what is left. So do agents under their own cost cap and the run's. An agent plans an attempt at the output cap plus the prompt's size, at most a full context. What another agent's failed attempts may have cost counts against the run's cap while its call is tried again. A failed attempt the provider refused, such as a 503, costs nothing and is still tried again. A failed attempt with no cost, such as a timeout or a cut stream, counts against the cap at what the call was planned to cost, since a provider may bill an answer it did not deliver. The recorded cost stays what the provider reported. An agent whose call has no room to be tried again ends with `budget_cost`. So do the monitor's sweep and review calls, under its budget beside the judge's share. A sweep call with no room to be tried again leaves the monitor with signals only.

## What a run records

| Field of a `model.response` event | Meaning |
| --- | --- |
| `cost_usd` | The cost the provider reported. Without one, the price or list price times the tokens. |
| `cost_source` | `provider`, `price` or `list`. `list` is a list price AIMS applied. |
| `provider` | The provider a router picked |
| `model` | The model the answer names |
| `service_tier` | The tier the answer names |

- Budgets and `aims spend` count the recorded cost.
- An answer with no token counts and no cost has no recorded cost. The agent and run cost caps, the score's spend cap and the judge's budget still count it at its size, at the profile's price with every input token uncached. Its size is the request and the answer as the provider sent it, at most the output cap. A stop at a cap says what such answers count at. A profile with no price counts nothing.
- The run page and the report say how much of a run's cost is at a list price, with each key and date. The run API gives the amount in `listed_usd`. The run detail lists it by profile in `listed`.
- The estimate counts a past call at the cost its provider reported. Other past calls and the calls to come are priced at the profile's price or list price. A router can bill above the list price. Four test calls through one router cost 2.8 to 3.4 times the list price.
- A step in the app names its provider. The report lists the providers of each model with their answer counts. The live view notes it when a second provider answers a model.
- A `chat` answer's `reasoning_details` go back unchanged in the next request. A router can return signed or encrypted reasoning there. That way the model keeps its reasoning across a tool call.

Errors:

- An error a provider sends after HTTP 200 is a provider error with its code, such as `provider error 402: Insufficient credits`.
- Codes 400, 401, 402, 403 and 404 are not retried. A key with no credits fails on the first attempt.

## Check a profile

```sh
aims models check <profile> [<profile> ...]
```

It sends each profile four small requests and records them as a run. With no names it checks each of these once: the profiles the roles name and those the `[monitor]` table of the config files names. The table counts whatever its preset, since `aims run --monitor` can turn its models on. A scenario's `monitor:` section is not read. An unknown profile under `[monitor]` is an error. An unknown one in `extract` only gets a note, since a run skips it.

| Check | What it asks for |
| --- | --- |
| `plain` | A short answer with full usage counts. Warns when the reasoning is not of the kind the profile names, except in an add that takes `reasoning` from the check. |
| `unstreamed` | The same without streaming, or with it when the profile turns streaming off |
| `tools` | A tool call and the answer after its result. Warns of several calls in one turn when the profile sets `parallel_tool_calls = false`. |
| `structured` | An answer that matches a JSON schema |
