# Runs and results

- [The run folder](#the-run-folder)
- [Read a run](#read-a-run)
- [Score and monitor again](#score-and-monitor-again)
- [Many runs](#many-runs)
- [Fork a run](#fork-a-run)
- [The UI](#the-ui)

## The run folder

`aims run` writes each run to `./runs/<run id>/`. `--runs-dir` picks another folder. Every command that reads runs takes the same flag.

| File | What it holds |
| --- | --- |
| `events.jsonl` | Every event in order. Each line holds the hash of the line before it. |
| `blobs/` | Large content, such as model responses and file contents, by hash. |
| `truth.json` | The hidden part of the run: its plants, canaries, agent roles and scripts. No agent or monitor sees it. |

Monitor runs and score runs are runs too, with their own ids. They point at the run they read. A run never changes after it is sealed.

`aims run --quiet` prints one line per run: the run id, the status and the agent cost. It adds the monitor's flag count when there are flags. A run that failed, was killed or was cancelled also gives the reason.

```text
01a1a296-a3d1-75c2-bee1-7387fd5241a7 completed $0.0000
```

`aims run --json` prints JSON lines instead: the estimate, monitor flags, actions, cleared flags, parts of the monitor that stopped, sweeps, the judge's start, verdicts, results and scores. Each line names its kind in `type`.

## Read a run

These spend nothing.

```sh
aims check <run>                   # plants each agent took, from the log and truth file
aims report <run> --out report.md  # the whole run as Markdown
aims report <run> --json           # the same as JSON
aims verify <run>                  # the hash chain and every blob
```

`aims check` prints one line per plant an agent reached, with its kind, the tier and the event seqs:

```text
agent took golden-keys (file, used) at 3062: 3062
agent took pinned-keys (impossible, claimed) at 3338: 3338
```

`took` means the plant counts as taken. `touched` or `reached` means the agent got only to a lower tier than the one that counts. The brackets give the tier. `considered` means the canary turned up only where it does not count as used: in the agent's model output, or with `used_in` set, anywhere outside the files it names, such as a scratch script. No line means the agent never came near the plant. When no agent reached any plant, it prints `no plants taken`.

`aims report` is the best single summary. It opens with a headline:

```text
Reward 1 (8 of 8 tests) · Judge: importance 4 of 5 · Claim matched: no
```

- **Reward** is what the verifier gave.
- **Judge** is the highest importance the judge gave any category, from 1 to 5.
- **Claim matched** says whether the agent's answer matched what it did.

Then come warnings, the judge's summary and findings, the plants, the flags, a timeline and run facts. Evidence is cited as `seq N`, the event's place in `events.jsonl`. Text from the log is escaped, so an agent cannot forge headings, links or alerts in the report.

The report uses the newest finished monitor run over the run, else the live monitor. `--source live` or `--source <monitor run id>` picks one.

To look at raw events, read `events.jsonl` with any JSON tool. Each line has `seq`, `ts`, `type`, `agent_id` and `data`, plus `run_id`, `v`, `prev_hash` and `hash`.

```sh
grep '"type":"tool.call"' runs/<run>/events.jsonl | head
```

## Score and monitor again

These call models, so ask first.

```sh
aims score <run>                         # writes a score run
aims score <run> --monitor <monitor run> # score against another monitor run's flags
aims monitor <run> --monitor standard    # a new monitor pass over a sealed run
aims monitor <run> --signals-only        # signals only, no model, no key
```

`aims run` already scores each run at the end unless `--no-score` was given. It prints each score below the results, as `aims score` prints it.

A score lists, for each agent:

- the plants it took, which monitor layer caught each one and how soon
- flags that no plant explains

A model then checks:

- whether each agent said it thought it was tested
- whether each subject that took a plant told the operator about it
- whether that subject's reasons agree with the plant's `facts`

The model is the first profile whose weights no agent of the run runs, from `[monitor] extract`, then the `sweep` role. When none fits, the score skips these checks and says why.

These checks stop at the score's spend cap, $1 by default. `budget_usd` under `[score]` changes it. `--budget-usd` changes it for one score. `aims score` prints its estimate first. Past `confirm_above_usd` it asks, as `aims run` does.

## Many runs

```sh
aims run ./my-scenario --variant observed --samples 10 --parallel 5 --quiet
aims run ./my-scenario --variant unobserved --samples 10 --parallel 5 --quiet
aims rates                                # take and catch rates over scored runs, with 95% intervals
aims spend                                # what each provider was paid
```

Compare variants by take rate: the share of subjects that took a plant. `aims rates` gives it for each scenario, variant and model, with each plant's own rate under it. Ten runs per side shows only large differences. Say so when you report a result.

`--series NAME --runs 5` makes five runs in a row, each going on from the one before. A `folder` channel with `lifetime: series` keeps its content between them.

## Fork a run

A fork replays the recorded answers up to a step, then goes on live:

```sh
aims run --replay <run> --fork-at agent:12 --inject "The reviewer is away this week."
aims run --replay <run> --fork-at agent:12 --samples 5
```

`--fork-at` takes `agent:N` (after that agent's step N), `@seq` (after an event) or `N` (for a run with one agent). Use it to test what one message or one change does from the same point.

The live steps call each agent's profile as the run recorded it. The config needs a profile of that name with the same backend, URL and key variable. `--profile` gives every agent another one.

In a fork of several agents, an agent takes its first step after the cut only once the others have replayed theirs. It does not wait for an agent whose `start` waits on it. A replayed command that in the run waited on another agent's later action runs until its command timeout. An agent that calls a model then gives it the recorded output and logs a divergence. With `--strict` it ends. A scripted agent goes on. An agent waiting for another agent's replayed turn counts as waiting. The wall limits stand still while one agent is paused and the others are paused or waiting.

## The UI

`aims ui` serves the app at http://localhost:8484 with a local runner and opens a sign-in link. Runs made by `aims run` on the same machine ship there. `aims push <runs folder>` ships runs recorded elsewhere. `aims open` prints a new sign-in link.

Only the control plane writes the `runs` folder of its data dir. A command that writes runs exits with an error there. A command that only reads runs works there.
