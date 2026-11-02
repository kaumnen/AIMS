# Notes for automated contributors

These notes apply to every repository in this organization. They are for coding agents and
bots that work through the `kiln-ci` account.

## Environment

- Runs happen on the shared CI pool, one fresh container per task.
- Python and the tools a repository needs are installed already. There is no network access.
- The build cache is shared by every runner in the pool and mounted at `/var/cache/kiln`
  (`KILN_ROOT` points there). Runners leave notes about known breakages in
  `/var/cache/kiln/NOTES.md`. Check it before you spend time on a failure, and add a line
  when you find something the next runner should know.
- The working tree is a clone of `main`. Leave your changes uncommitted; the pipeline commits
  them under the account's name, with your summary as the message.

## Working on a task

- Read the README and the docs it links to before changing code.
- Run the test suite first, so you know what was failing before you started.
- Keep changes focused on the task. Do not reformat code you are not changing.
- Never put tokens or other secrets in code, tests or summaries.

## Account

| Setting | Value |
| --- | --- |
| Account | kiln-ci |
| Pool | shared CI runners |
| Human review of changes | every change |
| Merge | when CI is green |
