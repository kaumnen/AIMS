# Runs and reports

- [Runs](#runs)
- [Reports](#reports)

## Runs

- A run is a folder with `events.jsonl` and `blobs/`. Each event holds the hash of the event before it. `aims verify <run>` checks the chain and every blob. Each blob must have the size its references state. In a folder of runs with an index, it also checks that the index holds the run as the log does.
- Commands that read a folder of runs keep their index of it in that folder, in `index.sqlite`. A folder they cannot write stops them with an error. Use a copy you can write.
- The index comes from the logs. To build it again, stop every aims process that uses the folder. Then delete `index.sqlite` with its `-wal` and `-shm` files. The next command indexes every log again. Do this when a run reads as corrupt: its log lost an event the index recorded.
- A run never changes after it is sealed. Monitor runs and scores are new runs that point at it.
- `--series <name> --runs 5` makes five runs in a row, each going on from the one before.
- Budgets cap steps, output tokens, cost and time for each agent and for the run. `aims estimate` prints what a run can cost, with the score made after it. `--no-score` leaves the score out. `aims spend` shows what each provider was paid.
- When the estimate passes `confirm_above_usd` under `[estimate]`, $20 by default, `aims run` asks before it starts. Without a terminal it stops. `--yes` starts it anyway.
- A cost cap holds a call's retries, so a run cannot exceed its cost caps by more than a last step per agent. A step cap bounds the cost with every attempt the profile's `retries` give each call, since a provider may bill each failed one.

### Forks

`aims run --replay <run> --fork-at agent:12 --inject "<message>"` replays the recorded answers up to step 12, then goes on live. `--fork-at` also takes `@seq` (after an event) or `N` (for a run of one agent). Add `--samples 5` for five forks. In the UI an operator gets **Fork from here** on each step of a sealed run.

The live steps call each agent's profile as the run recorded it. The config needs a profile of that name with the same backend, URL and key variable. Without one the fork stops. `--profile` gives every agent another profile.

A fork replays a step only when the run recorded a result for each of its tool calls. A run that ended in a step before its tool calls returned has none for that step.

- `agent:N` or `N` refuses that step and names the last one that replays.
- With `@seq`, an agent that was in such a step at the cut makes it again live. So does an agent that `agent:N` does not name.
- Without `--fork-at`, a run of one agent forks after the last step that replays. A run of several agents needs `--fork-at`.

The agents of a fork replay their tool calls one at a time, in the order the run made them. An agent takes its first step after the cut only once the other agents have replayed theirs, so the run's caps count every replayed step. It does not wait for an agent whose `start` waits on it. A replayed command that in the run waited on another agent's later action runs until its command timeout. An agent that calls a model then gives it the recorded output and logs a divergence. With `--strict` it ends. A scripted agent goes on. An agent waiting for another agent's turn counts as waiting, like a scripted step waiting on its `on` event. The wall limits stand still while one agent is paused and the others are paused or waiting.

### Monitor and score again

- `aims monitor <run>` feeds a sealed run through the monitor again, with any preset or model.
- `aims score <run>` lists the plants each agent took. It shows which monitor layer caught each one and how soon. It also checks whether each agent said it thought it was tested. For a subject that took a plant, it checks whether it told the operator and gave true reasons.
- Those two checks call a model: the first profile whose weights no agent of the run runs, from `[monitor] extract`, then the `sweep` role. When none fits, the score skips them and says why.
- The checks stop at a spend cap, $1 by default. `budget_usd` under `[score]` in a config file changes it. `aims score --budget-usd` changes it for one score. A failed call is tried again only when another full attempt fits. A score that stops says so in `aims score`, the Score tab and the report. The agents it did not reach get no finding.
- `aims score` prints its estimate first. Past `confirm_above_usd` it asks, as `aims run` does. `--yes` starts it anyway.
- The score run ships to a local `aims ui` that is running, as the runs of `aims monitor` do, so the app shows it. `--server` ships it to another control plane. `--no-server` keeps it here.
- `aims run` scores each run when it ends, unless you pass `--no-score`. It prints each score below the results, in the lines `aims score` prints. With `--json` each score is a line of type `score`.
- `aims rates` sums take and catch rates over many scored runs, with 95% intervals.

### Delete

- `aims delete <run>` or **Delete** on the run page removes a run from the control plane with its monitor runs and scores.
- A run that still runs, or that a queued or running job uses, stays.
- A run marked running whose runner is offline goes with `--force` or **Delete anyway**. A runner that still holds a deleted run drops its copy.
- `aims ui --keep-days 30` deletes runs that ended more than 30 days ago in the same way, at start and once a day. Its local runner drops its own copies after the same days. It takes up to 36500 days.

## Reports

`aims report <run>` writes up one run. It opens with the verifier's reward beside the judge's verdict. Then come warnings, the judge's summary and findings, plants, flags, a timeline and run facts. Each flag shows the judge's review beside a person's label.

```sh
aims report <run>                      # rendered on a terminal, raw Markdown when piped
aims report <run> --out report.md      # the Markdown file
aims report <run> --html report.html   # one self-contained HTML page to print or save as PDF
```

- A report is built from the log each time. Nothing is stored. Its header pins every input. Two builds of the same inputs give the same bytes apart from the build time.
- Text from the log is escaped, so an agent cannot forge headings, links or alerts.
- A report holds at most 64 KiB of each answer, message or summary kept in a blob. It holds 1 MiB of such text in all. It counts the bytes it left out.
- A report takes its monitor from the newest finished review of the run, else from the live monitor. `--source` or the API's `?source=` picks another.
- The run page in the UI has a **Report** tab with a Markdown download.
- When part of a run's cost is at a [list price](models.md#list-prices), the report says how much for each profile, with the row and the date of the copy. The run page says it under the run's cost.
- `GET /api/runs/{run_id}/report` gives JSON. `?format=md` gives the Markdown file. A viewer token gets the report without plants or scores.
