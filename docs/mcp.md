# MCP server

- [Set up](#set-up)
- [Tools](#tools)
- [Costs](#costs)
- [Run text](#run-text)

`aims mcp` serves a control plane to an MCP client over stdio. Each tool is one call of the control plane's API, named after the CLI command that does the same. The API's roles hold: a viewer token gets no plants and no scores.

## Set up

Install the CLI with the `mcp` extra:

```sh
uv tool install 'aims-bench[mcp]'
```

Then add it to your client:

| Client | Command |
| --- | --- |
| Claude Code | `claude mcp add aims -- aims mcp` |
| Codex | `codex mcp add aims -- aims mcp` |
| opencode | `opencode mcp add aims -- aims mcp` |
| pi | `pi mcp add aims -- aims mcp` |
| Gemini CLI | `gemini mcp add aims aims mcp` |
| VS Code | `code --add-mcp '{"name":"aims","type":"stdio","command":"aims","args":["mcp"]}'` |
| Cursor | Add the JSON below to `~/.cursor/mcp.json` |
| Any other | Add the JSON below to its MCP config |

```json
{
  "mcpServers": {
    "aims": { "command": "aims", "args": ["mcp"] }
  }
}
```

Claude Code and opencode add the server to the current project. Pass `--scope user` or `--global` for every project. Codex and pi add it for every project. Pass `-l` to pi for the current project.

| Control plane | How `aims mcp` finds it |
| --- | --- |
| The local `aims ui` | With no flags. It uses the UI's operator token. `aims ui` must be running. |
| Another one | `--server <url>` with a token in `AIMS_TOKEN` or `--token-file`. `aims token create <name>` makes one. |

Add `--act` after `aims mcp` for the tools that start and steer runs. They need an operator token. For another control plane, give the token with `--env` before `--`:

```sh
codex mcp add aims --env AIMS_TOKEN=<token> -- aims mcp --server https://aims.example.com --act
```

Claude Code, opencode and pi take the same form. In a JSON config, the flags go in `args` and the token in `env`:

```json
{
  "mcpServers": {
    "aims": {
      "command": "aims",
      "args": ["mcp", "--server", "https://aims.example.com", "--act"],
      "env": { "AIMS_TOKEN": "<token>" }
    }
  }
}
```

## Tools

| Tool | What it does |
| --- | --- |
| `runs` | Lists runs newest first, with filters and paging |
| `run_show` | One run: summary, verdict, monitor runs, scores and the steps a fork can replay |
| `check` | The plants each agent took, as `aims check` lists them. Operator only. |
| `flags`, `steps`, `events`, `event_show` | A run's flags, steps and events, paged |
| `search` | Events of every run that hold a text |
| `report` | The Markdown report of `aims report` |
| `verify` | The run's hash chain and blobs |
| `rates` | Take and catch rates over scored runs. Operator only. |
| `spend`, `estimate` | What providers were paid. What runs of a scenario can cost. |
| `scenarios`, `scenario_show`, `scenario_check`, `scenario_schema` | The scenarios, one scenario, its static check and the schema |
| `models_list`, `prompts_show`, `control_list` | Profiles and roles, the texts in use and the commands sent to a run |

With `--act`:

| Tool | What it does |
| --- | --- |
| `run` | Queues runs of a scenario on the runners, as `aims run` does |
| `fork` | Replays a sealed run to a step, then goes on live |
| `monitor` | Feeds a sealed run through the monitor again |
| `score` | Scores a sealed run against its truth file |
| `control` | Pauses, resumes, kills or cancels a run, then waits up to 10 s for the runner's answer |

Some things stay with the CLI and the UI: deleting runs, tokens, config, models, scenario files, injecting into a live run and denying a held tool call. A monitor's pause and a flag's label are a person's to give. So `control` resumes only an agent that an operator paused.

## Costs

`run`, `fork` and `monitor` first fetch the estimate. When it needs a yes, as `aims run` would ask, the tool asks the person through the client with the estimate in words. A client that cannot ask gets the estimate as an error. Nothing starts. `confirm_above_usd` in `aims.toml` sets the threshold.

> [!NOTE]
> The question goes out as an MCP elicitation. The client decides how to show it. Use a client that shows elicitations to you.

## Run text

Runs hold text that the agents under test wrote. Some scenarios tempt an agent to mislead whoever reads its work. That text reaches your client's model through the tools. The server's instructions tell the model to treat it as data. A viewer token keeps plants and scores out of reach.
