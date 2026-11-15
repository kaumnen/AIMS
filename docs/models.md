# Models

- [Profiles](#profiles)
- [Profile fields](#profile-fields)
- [List prices](#list-prices)
- [OpenRouter](#openrouter)
- [Routing and headers](#routing-and-headers)
- [What a run records](#what-a-run-records)
- [Check a profile](#check-a-profile)

## Profiles

A model profile says how to reach one model. The roles `agent`, `sweep` and `judge` each name a profile. A scenario can name another profile for one of its agents.

A config file adds a profile and sets the roles:

```toml
# aims.toml
[profiles.my-agent]
backend = "chat"
base_url = "https://api.example.com/v1"
model = "vendor/model-name"
weights = "vendor/model-name"
api_key_env = "MY_API_KEY"
reasoning = "none"
price = { input = 1.00, cached = 0.10, output = 4.00 }

[roles]
agent = "my-agent"
```

- `extends = "<profile>"` starts a profile from another one.
- A table with the name of a profile from a lower config layer merges into it key by key.
- Keys come from the environment only. A profile names the variable, never the key.
- `aims models list` shows every profile, the roles, where each price comes from and which keys are set.

## Profile fields

| Field | Meaning |
| --- | --- |
| `backend` | `chat` for the Chat Completions API, `responses` for the Responses API |
| `base_url` or `base_url_env` | The API's address, or the variable that holds it. Set exactly one. |
| `model` | The model's id at the provider |
| `weights` | The trained model behind it. Two profiles that serve one model share it. The monitor refuses to watch an agent on its own weights. |
| `api_key_env` | The variable that holds the key. Leave it out for a server that takes none. |
| `reasoning` | What the model sends of its reasoning: `none`, `summary` or `full` |
| `effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max` |
| `effort_kwarg` | `chat` only. A chat template argument that carries the effort instead of `reasoning_effort`. |
| `summary` | `responses` only. `auto`, `concise` or `detailed`. |
| `max_output_tokens` | The cap on one answer |
| `context_tokens` | The model's context size. Compaction and the estimate's ceiling need it. |
| `stream` | `true` by default |
| `parallel_tool_calls` | Sent only when set. Unset leaves it to the provider. |
| `structured_output` | `false` when the server cannot hold an answer to a schema. Such a model cannot be the sweep or judge model. |
| `cache_key` | Sends `prompt_cache_key`, so a run's calls share one prompt cache. On by default for `responses`, off for `chat`. |
| `sampling` | `temperature`, `top_p`, `top_k`, `seed` |
| `routing` | Where a router sends the request. See [Routing and headers](#routing-and-headers). |
| `headers` | HTTP headers for every request of the profile |
| `extra` | Any other fields of the request body |
| `price` | USD per million tokens: `input`, `cached`, `output`. Wins over `price_key`. |
| `price_key` | A row of LiteLLM's price map. Runs and estimates count the profile's calls at its list price. See [List prices](#list-prices). |
| `limits` | `rpm`, `tpm`, `concurrency` and a `group` that profiles can share |
| `timeouts` | Seconds: `connect`, `stall`, `wall` |
| `retries` | `attempts`, `backoff`, `max` |

A scenario may change `effort`, `max_output_tokens` and `sampling` on the profile it names for an agent.

## List prices

A run counts the calls of a profile with no price only when the provider reports their cost. No cost cap holds on the others. An estimate leaves out the profile's calls. Such a profile can name a row of [LiteLLM's price map](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) in `price_key` instead. Runs, forks, reviews, scores and estimates then count its calls at that row's list price.

```toml
[profiles.my-agent]
price_key = "provider/model-name"
```

`aims models prices` finds the key:

```sh
aims models prices my-agent
aims models prices my-agent --search model-name
```

For a profile with no price, it lists the rows that may price it, with their prices. A row fits when its key is the profile's model id, ends in `/` and the id, or ends in the id's last part. So `openai/gpt-4o` finds `gpt-4o` and `azure/gpt-4o`. `--search` lists the rows whose key holds the text. One model's rows differ by provider and region. Pick the row of the provider the profile calls.

In the app, **Settings** > **Models** shows each profile's price and where it comes from. **Find a price key** lists the same rows and copies the `price_key` line.

What a row gives:

- The input, cached input and output price. Only rows of chat models give a price.
- A row with no cached input price counts cached input at the input price.
- A row with a higher price past some prompt size gives its base price. The estimate says from which size.
- Batch, flex and priority prices are left out.

When AIMS reads the map:

- Only when it needs a row: for a run, fork, review, score or estimate that calls a profile with a `price_key` and no `price`, for `aims models prices` and for the Models page.
- Where the profiles are resolved: in `aims run`, `aims monitor`, `aims score` and `aims estimate`, or in the control plane. A runner never reads the map. Its job carries the price.
- From `raw.githubusercontent.com` with no key, through the [proxy settings](deploy.md#proxies). It waits 5 seconds at most.
- It keeps a copy, `litellm-prices.json.gz`, in the cache folder. That is `$AIMS_CACHE_DIR` when set. Otherwise it is `%LOCALAPPDATA%\aims\cache` on Windows, else `$XDG_CACHE_HOME/aims` or `~/.cache/aims`.
- A copy older than 24 hours is checked with GitHub again.
- When GitHub does not answer, the copy serves at any age, with its date. The next try comes 10 minutes later.
- With no copy, or a key the map lacks, the profile stays unpriced. Its runs still start. The estimate says why.

Each list price names its source, key and date. The date is when GitHub last confirmed the copy current. `aims estimate`, `aims run`, `aims monitor` and `aims score` print it. Here with placeholder figures:

```text
Profile my-agent: LiteLLM list price for provider/model-name, fetched 2026-11-15: $1.00 input, $0.10 cached, $4.00 output per million tokens.
```

What a run does with it:

- A call costs what its provider reported. When the provider reported nothing, the call costs the list price times the tokens. Its `cost_source` is then `list`.
- The agents' cost caps and the monitor's budget count that cost. The monitor's budget is sized at the list prices of its models.
- The run records the profile with the list price in its `price`. Its `listed` holds the date of the copy. A config file may not set `listed`.
- A fork keeps the list price its origin recorded. When the origin recorded no price, the fork takes the price of the config's profile of the same name, if that names the same model. A profile from `--profile` and the fork's monitor get the current list price.

> [!IMPORTANT]
> A cap on a list-priced call holds on its cost at the list price. The provider's bill can be higher.

## OpenRouter

AIMS ships no profile for OpenRouter. One command makes a profile for any model there:

```sh
aims models add openrouter author/model-name
```

It reads OpenRouter's public list of models, which needs no key. It prints the profile to stdout and its notes to stderr. The values here are placeholders:

```toml
[profiles."openrouter-model-name"]
backend = "chat"
base_url = "https://openrouter.ai/api/v1"
model = "author/model-name"
weights = "author/model-name"
api_key_env = "OPENROUTER_API_KEY"
reasoning = "full"
effort = "high"
# OpenRouter lists: high, medium, low, none
max_output_tokens = 16384
context_tokens = 123456
cache_key = true
routing = {sort = "price", require_parameters = true}
headers = {X-OpenRouter-Metadata = "enabled"}
price = {input = 1.23, cached = 0.123, output = 4.56}
```

| Option | Effect |
| --- | --- |
| `--write` | Adds the profile to the project's `aims.toml`. Makes the file when there is none. Refuses a name that is taken. |
| `--name <name>` | The profile's name instead of `openrouter-<model>` |
| `--alias` | Takes an alias such as `~author/model-latest` |

Then set the key, test the profile and run with it:

```sh
export OPENROUTER_API_KEY=<your key>
aims models check openrouter-model-name
aims run cache-keys --profile openrouter-model-name
```

In the app, **Settings** > **Models** > **Add from OpenRouter** does the same lookup for operators. **Add to project config** saves the profile as `--write` does.

What the profile sets:

| Setting | Why |
| --- | --- |
| `routing.sort = "price"` | The cheapest provider that can serve the request comes first. |
| `routing.require_parameters = true` | Only providers that support every parameter of a request serve it. Without it a provider may drop tools, structured output or the effort without an error. |
| `cache_key = true` | A run's calls share one prompt cache. |
| `extra.cache_control` | Only when OpenRouter lists a price for a cache write. Some such models cache a prompt only when the request asks. The others take the field without effect. |
| `X-OpenRouter-Metadata` | The answer names the provider that served it. |
| No `parallel_tool_calls` | Few providers support the field. With `require_parameters` it would cut the route to those few. |
| `effort` | The model's default. The comment below it lists the levels OpenRouter names. |
| `price` | The list price, which is the lowest among the model's providers. Estimates and the monitor's budget use it. A run records what OpenRouter reports each call cost, or the cost at this price when it reports none. |

> [!NOTE]
> The notes say what the list cannot tell. `reasoning = "full"` is a guess, for one. `aims models check` warns when the model sends a summary instead.

> [!WARNING]
> An alias moves to newer models over time, so a run would not say which model it used. The command refuses one without `--alias`. It also refuses a model that OpenRouter does not serve on chat completions.

## Routing and headers

`routing` goes out as the request's `provider` object. It works on any router that reads that object. Providers go by their slugs.

```toml
[profiles."openrouter-model-name"]
routing = { only = ["provider-a", "provider-b"], allow_fallbacks = false, quantizations = ["fp8"] }
```

| Key | Value |
| --- | --- |
| `order`, `only`, `ignore` | Lists of provider slugs |
| `allow_fallbacks`, `require_parameters`, `zdr`, `enforce_distillable_text` | `true` or `false` |
| `data_collection` | `allow` or `deny` |
| `quantizations` | A list such as `["fp8", "bf16"]` |
| `sort` | `price`, `throughput` or `latency`, or `{ by = "price", partition = "model" }` |
| `preferred_min_throughput`, `preferred_max_latency` | A number, or `{ p50 = 50, p90 = 20 }` |
| `max_price` | `{ prompt = 1, completion = 2 }`, also `request` and `image` |

A profile may not set both `routing` and `provider` in `extra`.

> [!TIP]
> One provider for a whole run keeps the price, the quantization and the prompt cache the same. Set `only` to one slug and `allow_fallbacks = false` for that.

`headers` adds HTTP headers to each request of the profile:

```toml
headers = { X-OpenRouter-Metadata = "enabled", X-Title = "my-bench" }
```

> [!WARNING]
> A run records its profiles with their headers. Keep secrets out of them. AIMS refuses a header that it sets itself or that carries a key, such as `Authorization` or `Cookie`.

A config layer or `extends` merges both tables key by key. A header's name ignores case, so `x-title` in a layer replaces `X-Title` from the layer below.

## What a run records

| Field of a `model.response` event | Meaning |
| --- | --- |
| `cost_usd` | The cost the provider reported in the answer's usage. The profile's price or list price times the tokens when it reported none. |
| `cost_source` | `provider`, `price` or `list`, after where the cost came from. `list` is a list price AIMS laid on the profile. |
| `provider` | The provider a router picked |
| `model` | The model the answer names |
| `service_tier` | The tier the answer names |

- Budgets and `aims spend` count the recorded cost.
- The run page and the report say how much of a run's cost is at a list price, with the key and the date of each. The run API gives the amount in `listed_usd`. The run detail lists it by profile in `listed`.
- The estimate counts a past call at the cost its provider reported. It prices other past calls and the calls to come at the profile's price or list price. A router can bill above the list price. Four test calls through OpenRouter cost 2.8 to 3.4 times the list price.
- A step in the app names its provider. The report lists the providers of each model with their answers. The live view notes it when a second provider answers a model.
- A `chat` answer's `reasoning_details` go back unchanged with the turn in the next request. OpenRouter returns a model's signed or encrypted reasoning there. The model keeps its reasoning between a tool call and the tool's result that way.

Errors:

- An error that a provider sends after HTTP 200 is a provider error with its code, such as `provider error 402: Insufficient credits`.
- Codes 400, 401, 402, 403 and 404 are not retried. A key with no credits fails a call on its first attempt.

## Check a profile

```sh
aims models check <profile> [<profile> ...]
```

It sends four small requests to each profile and records them as a run. Without a name it checks the agent's profile.

| Check | What it asks for |
| --- | --- |
| `plain` | A short answer with full usage counts. It warns when the reasoning is not of the kind the profile names. |
| `unstreamed` | The same without streaming, or with it when the profile turns it off |
| `tools` | A tool call and the answer after its result. It warns of several calls in one turn when the profile sets `parallel_tool_calls = false`. |
| `structured` | An answer that matches a JSON schema |
