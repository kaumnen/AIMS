# Runs and reports

- [Runs](#runs)
- [Reports](#reports)

## Runs

- A run is a folder with `events.jsonl` and `blobs/`. Each event holds the hash of the event before it. `aims verify <run>` checks the chain and every blob.
- A run never changes after it is sealed. Monitor runs and scores are new runs that point at it.
- `--series <name> --runs 5` makes five runs in a row, each going on from the one before.
- Budgets cap steps, tokens, cost and time for each agent and for the run. `aims estimate` prints what a run can cost. `aims spend` shows what each provider was paid.

### Forks

`aims run --replay <run> --fork-at agent:12 --inject "<message>"` replays the recorded answers up to step 12, then goes on live. Add `--samples 5` for five forks. The run page in the UI has **Fork from here** on each step.

### Monitor and score again

- `aims monitor <run>` feeds a sealed run through the monitor again, with any preset or model.
- `aims score <run>` lists the plants each agent took. It shows which monitor layer caught each one and how soon. It also checks whether the agent told the operator the truth.
- `aims rates` sums take and catch rates over many scored runs, with 95% intervals.

### Delete

- `aims delete <run>` or **Delete** on the run page removes a run from the control plane with its monitor runs and scores.
- A run that still runs, or that a queued job uses, stays.
- A run marked running whose runner is offline goes with `--force` or **Delete anyway**. A runner that still holds a deleted run drops its copy.
- `aims ui --keep-days 30` deletes runs that ended more than 30 days ago in the same way, at start and once a day. Its local runner drops its own copies after the same days.

## Reports

`aims report <run>` writes up one run. It opens with the verifier's reward beside the judge's verdict. Then come warnings, the judge's summary and findings, plants, flags, a timeline and run facts. Each flag shows the judge's review beside a person's label.

```sh
aims report <run>                      # rendered on a terminal, raw Markdown when piped
aims report <run> --out report.md      # the Markdown file
aims report <run> --html report.html   # one self-contained HTML page to print or save as PDF
```

- A report is built from the log each time. Nothing is stored. Its header pins every input. Two builds of the same inputs give the same bytes apart from the build time.
- Text from the log is escaped, so an agent cannot forge headings, links or alerts.
- A report takes its monitor from the newest finished review of the run, else from the live monitor. `--source` or the API's `?source=` picks another.
- The run page in the UI has a **Report** tab with a Markdown download.
- `GET /api/runs/{run_id}/report` gives JSON. `?format=md` gives the Markdown file. A viewer token gets the report without plants or scores.
