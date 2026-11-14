# Contributing

Setup is in the [Development](https://github.com/kaumnen/AIMS/blob/main/README.md#development) section of the README. Report a vulnerability as [SECURITY.md](https://github.com/kaumnen/AIMS/blob/main/SECURITY.md) says.

## Before a push

```sh
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest -n auto -m "not docker and not tracer"
```

For a change under `web/`:

```sh
cd web && bun fmt --check && bun run lint && bun check && bun run test
```

## Tests

The tests sit in one folder per area under `tests/`, such as `tests/models` and `tests/sandbox`. Run one area with `uv run pytest tests/models`.

Four marks sort the tests by what they need:

| Mark | Meaning |
| --- | --- |
| `docker` | Starts containers. Needs Docker. |
| `tracer` | Starts the privileged kernel tracer. Needs Docker. |
| `serial` | Needs the machine to itself. |
| `timing` | Has a short time limit, so it fails on a busy machine. |

The full suite runs in two parts. Part 2 runs one test at a time.

```sh
uv run pytest -n 4 --dist loadfile -m "not serial and not timing"   # part 1
uv run pytest -m "serial or timing"                                 # part 2
```

## What CI runs

CI runs only the jobs a change needs. [`.github/changes.py`](https://github.com/kaumnen/AIMS/blob/main/.github/changes.py) reads the changed paths and picks them. The first rule that names a path decides.

| Changed path | Jobs |
| --- | --- |
| `docs/`, any `*.md`, `LICENSE`, `NOTICE` | `workflows` only |
| `web/` | `web`, `image`, `ui-bench` |
| `web/openapi.json`, `web/src/lib/budget-numbers.json` | The web jobs and `python` |
| `src/aims/`, `tests/` | `python`: lint, types and the tests that need no container |
| `src/aims/server/` | `python` and `ui-bench` |
| `src/aims/` `sandbox`, `node`, `run`, `agent`, `tools`, `channels`, `scenarios` and `skills/` | `python` with the container tests |
| A container test, a test file one imports or a fixture | `python` with the container tests |
| `pyproject.toml`, `.github/`, `tests/conftest.py`, `src/aims/events/`, `text.py`, `aio.py` | Everything |
| A path no rule names, such as `Dockerfile` | Everything |
| `uv.lock`, `web/bun.lock` | Everything on Python 3.14 too |

`workflows` runs on every change. A force push or a new branch runs everything. So does the nightly run at 02:43 UTC and each release, both on Python 3.14 and 3.15.

> [!NOTE]
> A change outside the sandbox side can still break a container test. The nightly run finds it.

The `ci` job is the one required check. It passes when every job the change needs passed.

See what your commits turn on before a push. The script compares commits, so uncommitted work does not count.

```sh
uv run python .github/changes.py --base origin/main
```

A new top-level folder needs a rule in `RULES`. Until it has one, a change there runs everything.
