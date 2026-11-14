# Models

- [Profiles](#profiles)
- [Profile fields](#profile-fields)
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
- `aims models list` shows every profile, the roles and which keys are set.

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
| `price` | USD per million tokens: `input`, `cached`, `output` |
| `limits` | `rpm`, `tpm`, `concurrency` and a `group` that profiles can share |
| `timeouts` | Seconds: `connect`, `stall`, `wall` |
| `retries` | `attempts`, `backoff`, `max` |

A scenario may change `effort`, `max_output_tokens` and `sampling` on the profile it names for an agent.

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
| `X-OpenRouter-Metadata` | The answer names the provider that served it. |
| No `parallel_tool_calls` | Few providers support the field. With `require_parameters` it would cut the route to those few. |
| `effort` | The model's default. The comment below it lists the levels OpenRouter names. |
| `price` | The list price, which is the lowest among the model's providers. Only estimates use it. |

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
| `cost_usd` | The cost the provider reported in the answer's usage. The profile's price times the tokens when it reported none. |
| `cost_source` | `provider` or `price`, after where the cost came from |
| `provider` | The provider a router picked |
| `model` | The model the answer names |
| `service_tier` | The tier the answer names |

- Budgets and `aims spend` count the recorded cost.
- The estimate counts a past call at the cost its provider reported. It prices other past calls and the calls to come at the profile's price. A router can bill above the list price. Four test calls through OpenRouter cost 2.8 to 3.4 times the list price.
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
