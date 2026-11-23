import contextlib
import inspect
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from aims import __version__


def _shown_usage(e: typer.TyperException) -> None:
    from aims.report.escape import visible

    e.message = visible(e.message)


def _typeless_flags(params: list[Any]) -> list[Any]:
    """`params` with no value type shown for a flag in --help. A flag takes no value, yet typer
    0.29 puts `<bool>` beside each one in Rich help, its own --help too. Typer 0.28 and its
    plain help leave it out. An empty metavar fills the type column with nothing. Drop this
    once typer leaves it out again."""
    for param in params:
        if isinstance(param, TyperOption) and param.is_bool_flag and param.metavar is None:
            param.metavar = ""
    return params


class _Group(TyperGroup):
    """Shows the text of a usage error as the characters it holds. Click and typer put argv
    text in some messages as it is, such as `Got unexpected extra argument (...)`. Its help
    shows no type for a flag."""

    def get_params(self, ctx: Any) -> Any:
        return _typeless_flags(super().get_params(ctx))

    def make_context(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().make_context(*args, **kwargs)
        except typer.TyperException as e:
            _shown_usage(e)
            raise

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except typer.TyperException as e:
            _shown_usage(e)
            raise


class _App(typer.Typer):
    """An error no command handles ends with exit code 1 and its traceback on stderr, written
    through visible(). Typer's Rich traceback prints the error's text as it is, escape bytes
    too. Exit, usage errors, Ctrl-C and a closed pipe end in typer as before. A folder of runs
    that cannot be written ends with one line: a person can do that, it is no fault of aims.
    An answer to a question that is not UTF-8 is invalid input. The question is asked again."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        import io
        import sys

        # Python reads stdin strictly outside UTF-8 mode, so such an answer would raise.
        if isinstance(sys.stdin, io.TextIOWrapper):
            sys.stdin.reconfigure(errors="surrogateescape")
        try:
            return super().__call__(*args, **kwargs)
        except Exception as e:
            from aims.events.store import IndexUnwritable

            if isinstance(e, IndexUnwritable):
                _echo(f"error: {e}", err=True)
                raise SystemExit(1) from None
            import contextlib
            import sys
            import traceback

            from aims.report.escape import visible

            with contextlib.suppress(OSError):
                sys.stderr.write(visible("".join(traceback.format_exception(e))))
                sys.stderr.flush()
            raise SystemExit(1) from None


app = _App(name="aims", no_args_is_help=True, add_completion=False, cls=_Group)
models_app = typer.Typer(
    no_args_is_help=True, cls=_Group, help="Model profiles and provider checks."
)
app.add_typer(models_app, name="models")

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="An aims.toml to use instead of the project's."),
]
RunsDirOption = Annotated[
    Path, typer.Option(file_okay=False, help="Directory that holds the runs.")
]


def _keys(given: list[str] | None) -> list[str] | None:
    """Add the --key entries to AIMS_KEYS, so a runner the command starts has them too, and
    check the whole list before the command runs. Exit 1 with the problem."""
    import os

    from aims.models.keys import KEYS, KeysError, parse

    entries = given or []
    try:
        for entry in entries:
            parse(entry, "--key")
        text = ",".join(t for t in (os.environ.get(KEYS, ""), *entries) if t.strip())
        parse(text)
    except KeysError as e:
        _fail(str(e))
    if entries:
        os.environ[KEYS] = text
    return given


KeyOption = Annotated[
    list[str] | None,
    typer.Option(
        "--key",
        metavar="NAME=URL",
        callback=_keys,
        help="Let the key in env var NAME go to a profile whose base_url is URL or a path under"
        " it. NAME=env:VAR lets it go to a profile that reads its URL from env var VAR. env:VAR"
        " lets a profile with no key read its URL from VAR. Repeat for more. Adds to AIMS_KEYS.",
    ),
]
SCENARIO_HELP = "The scenario file, its directory, or builtin/<name> for one that ships with AIMS."


def _echo(text: str, *, err: bool = False) -> None:
    """typer.echo of a line that holds outside text: from a run, a server, a file or argv. Its
    hidden characters, such as ESC, show as text."""
    from aims.report.escape import visible

    typer.echo(visible(text), err=err)


def _scenario(value: Path | None) -> Path | None:
    """The scenario file `value` names. A bare name that is neither a path nor a built-in
    scenario is a usage error that lists the built-in ones."""
    if value is None:
        return None
    from aims.scenario.load import builtin_names, find

    path = find(value)
    parts = value.parts
    named = len(parts) == 1 or (len(parts) == 2 and parts[0] == "builtin")
    if path == value and not path.exists() and named:
        from aims.report.escape import visible

        shipped = ", ".join(f"builtin/{name}" for name in builtin_names())
        raise typer.BadParameter(
            visible(f"no file {value} and no built-in scenario of that name; built in: {shipped}")
        )
    return path


def _finite(value: float | None) -> float | None:
    """The value of a float option. nan and inf are a usage error, as each such option counts
    days, seconds or dollars."""
    import math

    if value is not None and not math.isfinite(value):
        raise typer.BadParameter("give a finite number")
    return value


def _days(value: float | None) -> float | None:
    """The value of a keep days option: finite and at most MAX_KEEP_DAYS, so the date it
    reaches back to stays one a timestamp holds."""
    value = _finite(value)
    if value is None:
        return None
    from aims.config import MAX_KEEP_DAYS

    if value > MAX_KEEP_DAYS:
        raise typer.BadParameter(f"give at most {MAX_KEEP_DAYS} days")
    return value


def _settings(config: Path | None) -> Any:
    """Every config layer merged, or exit 1 with the problem."""
    from aims.config import ConfigError, load
    from aims.models import ProfileError
    from aims.prompts import TextError

    try:
        return load(config)
    except (ConfigError, ProfileError, TextError) as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@contextlib.contextmanager
def _stoppable() -> Generator[None]:
    """Within it, the first Ctrl-C, SIGTERM or hangup cancels the task that entered it, which
    seals its run as cancelled. Later ones cancel nothing, so they cannot cut that seal. Once
    it is left they cancel nothing either, as the close after it is bounded. A third Ctrl-C or
    SIGTERM quits at once, as on_stop says. The handlers stay in until asyncio.run closes the
    loop. Without them a second Ctrl-C raises KeyboardInterrupt wherever the seal is, and a
    SIGTERM ends the process with no seal. Off the main thread, as in a test, no signal comes,
    so none is handled."""
    import asyncio
    import threading

    from aims.terminal import on_hangup, on_stop

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    main = asyncio.current_task()
    loop = asyncio.get_running_loop()
    live = True

    def stop() -> None:
        if live and main is not None and not main.cancelling():
            main.cancel()

    on_stop(loop, stop)
    on_hangup(loop, stop)
    try:
        yield
    finally:
        live = False


def _version(value: bool) -> None:
    if value:
        typer.echo(f"aims {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = None,
) -> None:
    """Run alignment experiments on model agents and monitor what they do."""


@app.command()
def init(
    user: Annotated[
        bool, typer.Option("--user", help="Write the user file instead of ./aims.toml.")
    ] = False,
    force: Annotated[bool, typer.Option(help="Replace a file that exists.")] = False,
) -> None:
    """Write a commented starter config file."""
    from aims.config import PROJECT, USER, ConfigError, user_dir
    from aims.config import init as write

    path = user_dir() / USER if user else Path.cwd() / PROJECT
    try:
        _echo(f"wrote {write(path, force=force)}")
    except ConfigError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@app.command("config")
def config_paths(
    config: ConfigOption = None,
    merged: Annotated[
        bool,
        typer.Option(
            "--merged", help="Print the settings in use instead, with where each value comes from."
        ),
    ] = False,
) -> None:
    """Show which config files are read, lowest layer first."""
    from aims.config import PROJECT, ConfigError, layers

    if merged:
        from aims.merged import merged as merged_text

        typer.echo(merged_text(_settings(config)), nl=False)
        return
    try:
        found = layers(config)
    except ConfigError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    for name, path in (("user", found.user), ("project", found.project)):
        if path is None:
            state = f"none; `aims init` writes {Path.cwd() / PROJECT}"
        else:
            state = f"{path} ({'read' if path.is_file() else 'absent'})"
        _echo(f"{name}: {state}")


@app.command()
def verify(
    run: Annotated[str, typer.Argument(help="Run id or run directory.")],
    runs_dir: RunsDirOption = Path("runs"),
) -> None:
    """Check the hash chain of a run and the blobs it references, and in a folder of runs with
    an index, that the index holds the run as it should."""
    import asyncio
    import sqlite3

    from aims.events import LocalEventStore, Verification, verify_run
    from aims.events.store import INDEX, is_run_id

    # resolved, so `.` inside a run's folder has the run's id for its name
    path = Path(run).resolve() if Path(run).is_dir() else runs_dir / run

    async def indexed() -> Verification | None:
        """What the store says of the run, as the server and a report say it. None when the
        index does not open."""
        try:
            store = await LocalEventStore.open(path.parent)
        except (OSError, sqlite3.Error) as e:
            _echo(
                f"note: the index in {path.parent} did not open, so it was not checked: {e}",
                err=True,
            )
            return None
        async with store:
            return await store.verify(path.name)

    try:
        result = None
        if is_run_id(path.name) and (path.parent / INDEX).is_file():
            result = asyncio.run(indexed())
        if result is None:
            result = verify_run(path)
    except OSError as e:
        _echo(f"error: cannot read {path}: {e.strerror or e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    problems = [
        *([result.error] if result.error else []),
        *result.blob_problems(),
    ]
    for problem in problems:
        _echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(1)
    state = "finished" if result.finished else "not finished"
    typer.echo(f"ok: {result.events} events, {state}, head {result.head}")


@app.command("check")
def check_plants(
    run: Annotated[str, typer.Argument(help="Run id.")],
    runs_dir: RunsDirOption = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Report which plants each agent of a sealed run took, from its log and truth file."""
    import asyncio

    from aims.events import LocalEventStore
    from aims.run.score import CheckError, check_run, gaps_of
    from aims.terminal import json_text

    async def main() -> tuple[list[Any], list[str]]:
        async with await LocalEventStore.open(runs_dir) as store:
            return await check_run(store, run), await gaps_of(store, run)

    try:
        rows, gaps = asyncio.run(main())
    except (CheckError, KeyError) as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    for gap in gaps:
        _echo(f"warning: {gap}", err=True)
    if json_out:
        typer.echo(json_text([row.as_dict() for row in rows]))
        return
    if not rows:
        typer.echo("no plants taken")
        return
    for row in rows:
        state = "took" if row.taken else "touched" if row.tier == "touched" else "reached"
        seqs = ",".join(str(seq) for seq in row.evidence)
        _echo(f"{row.agent} {state} {row.plant} ({row.kind}, {row.tier}) at {row.seq}: {seqs}")


@models_app.command("list")
def models_list(config: ConfigOption = None) -> None:
    """Show the model profiles, their roles and whether AIMS_KEYS and the env let each be
    called."""
    from aims.models import KeyRefused, ProfileError

    models = _settings(config).models
    roles = {}
    for role, name in models.roles.model_dump(exclude_none=True).items():
        roles.setdefault(name, []).append(role)
    for name, p in sorted(models.profiles.items()):
        try:
            p.api_key(p.url())
            state = "ready"
        except KeyRefused as e:
            state = f"needs {e.entry} in AIMS_KEYS"
        except ProfileError as e:
            state = str(e).removeprefix(f"profile {name}: ")
        role = f"  [{', '.join(roles[name])}]" if name in roles else ""
        price = _price_source(p)
        _echo(f"{name}  {p.backend}  {p.model}  reasoning {p.reasoning}  {price}  {state}{role}")
    unset = models.roles.unset()
    if unset:
        them = "them" if len(unset) > 1 else "it"
        _echo(
            f"unset roles: {', '.join(unset)}; set {them} under [roles] in aims.toml, or add a"
            " model with aims models add <model id> --base-url <url> --write --role <role>"
        )


def _price_source(p: Any) -> str:
    """Where profile `p`'s price comes from, as its config says, without reading a map."""
    from aims.models.listprice import SOURCE

    if p.price is not None:
        return "price own"
    return "no price" if p.price_key is None else f"price {SOURCE} {p.price_key}"


@models_app.command("prices")
def models_prices(
    profiles: Annotated[
        list[str] | None, typer.Argument(help="Profiles to show. By default all of them.")
    ] = None,
    search: Annotated[
        str | None,
        typer.Option(help="Also list the price map's rows whose key holds this text."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Show each profile's price and where it comes from. For a profile with no price, list the
    rows of LiteLLM's price map that may price it, to name one in its price_key."""
    import asyncio

    from aims.models import ProfileError
    from aims.models.listprice import SOURCE, PriceLists, survey

    models = _settings(config).models
    try:
        chosen = [models.get(n) for n in profiles] if profiles else list(models.profiles.values())
    except ProfileError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    chosen = sorted(chosen, key=lambda p: p.name)
    found = asyncio.run(survey(chosen, PriceLists(), search or ""))
    for p in chosen:
        for line in _price_lines(p, found):
            _echo(line)
    text = (search or "").strip()
    if text and found.checked is not None:
        _echo(f"Rows whose key holds {text}:" if found.found else f"No row's key holds {text}.")
        for line in _row_lines(found.found):
            _echo(line)
    if found.checked is not None:
        _echo(f"From {SOURCE}'s price map, fetched {found.checked:%Y-%m-%d}.")
    if (problem := found.listing.problem) is not None:
        if found.checked is None:
            _echo(f"warning: no copy of {SOURCE}'s price map is at hand: {problem}", err=True)
        else:
            _echo(f"warning: {SOURCE}'s price map could not be read again: {problem}", err=True)


def _price_lines(p: Any, found: Any) -> list[str]:
    from aims.run.estimate import listed_line, listed_price, usd

    if p.price is not None:
        x = p.price
        return [
            f"Profile {p.name}: own price: {usd(x.input)} input, {usd(x.cached)} cached,"
            f" {usd(x.output)} output per million tokens."
        ]
    listed = found.listing.find(p)
    if listed is not None:
        return [listed_line(listed_price(p.name, listed))]
    why = found.listing.why(p)
    lines = [
        f"Profile {p.name}: no price{'' if why is None else f': {why}'}, so estimates leave"
        " out its calls and caps count only the costs its provider reports."
    ]
    rows = found.candidates.get(p.name)
    if rows:
        lines.append("  Rows that may price it; name one as its price_key:")
        lines += _row_lines(rows)
    elif rows is not None:
        lines.append(f"  No row's key ends in {p.model.rpartition('/')[2]}; try --search.")
    return lines


def _row_lines(rows: list[Any]) -> list[str]:
    from aims.run.estimate import usd

    lines = []
    for r in rows:
        x = r.price
        notes = [] if r.cached_listed else ["no cached price listed"]
        if r.tier_from is not None:
            notes.append(f"more past {r.tier_from:,} prompt tokens")
        note = f" ({'; '.join(notes)})" if notes else ""
        lines.append(
            f"  {r.key}  {usd(x.input)} input, {usd(x.cached)} cached, {usd(x.output)} output{note}"
        )
    return lines


@models_app.command("check")
def models_check(
    profiles: Annotated[
        list[str] | None,
        typer.Argument(
            help="Profiles to check. By default those the roles and the \\[monitor] table name."
        ),
    ] = None,
    config: ConfigOption = None,
    runs_dir: RunsDirOption = Path("runs"),
    key: KeyOption = None,
) -> None:
    """Call each provider with small requests and report what works. Recorded as a run."""
    import asyncio
    import uuid

    from aims.models import ProfileError
    from aims.models.check import to_check
    from aims.monitor.config import named_profiles

    settings = _settings(config)
    models = settings.models
    calls, extract = named_profiles(settings.monitor)
    try:
        chosen = to_check(models, profiles or [], monitor=calls, extract=extract)
        for p in chosen:
            p.api_key(p.url())
    except ProfileError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    if not profiles:
        if unset := models.roles.unset():
            its = "them" if len(unset) > 1 else "it"
            _echo(
                f"note: unset roles: {', '.join(unset)}; no profile is checked for {its}", err=True
            )
        for name in extract:
            if name not in models.profiles:
                _echo(f"note: [monitor] extract: unknown profile {name!r} is not checked", err=True)
    run_id = str(uuid.uuid7())
    try:
        checks = asyncio.run(_check(models, chosen, runs_dir, run_id))
    except asyncio.CancelledError:
        raise typer.Exit(EXIT_CANCELLED) from None
    if any(c.verdict == "fail" for c in checks):
        raise typer.Exit(1)


@models_app.command("add")
def models_add(
    model: Annotated[str, typer.Argument(help="The model's id on its server.")],
    base_url: Annotated[
        str | None,
        typer.Option(help="The server's API URL, such as https://api.example.com/v1."),
    ] = None,
    key_env: Annotated[
        str | None,
        typer.Option(
            help="The env var that holds the server's key. Leave it out for a server that takes"
            " none."
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="A profile to reach the server as: its backend, URL, key env, headers, extra"
            " and other connection fields. Instead of --base-url and --key-env.",
        ),
    ] = None,
    name: Annotated[
        str | None, typer.Option(help="The profile's name instead of the id's last part.")
    ] = None,
    litellm: Annotated[
        str | None,
        typer.Option(
            "--litellm",
            help="The key of the model's row in LiteLLM's price map, for its price, context"
            " size and output cap. Without it the notes name the rows that may fit.",
        ),
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option(help="chat or responses. By default chat, or the --from profile's."),
    ] = None,
    sets: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Set a field to a TOML value: key=value, such as effort=low or extra.a.b=1."
            " Repeat for more. Applies last.",
        ),
    ] = None,
    unsets: Annotated[
        list[str] | None,
        typer.Option(
            "--unset",
            help="Remove a field the draft sets, such as context_tokens. Repeat for more.",
        ),
    ] = None,
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role",
            help="Give the profile a role there too: agent, sweep or judge. Repeat for more."
            " Needs --write.",
        ),
    ] = None,
    no_check: Annotated[
        bool,
        typer.Option(
            "--no-check",
            help="Skip the check. By default the model gets a few small requests first, which"
            " may cost a little. Nothing is written when one fails.",
        ),
    ] = False,
    write: Annotated[
        bool, typer.Option("--write", help="Add the profile to the project's aims.toml.")
    ] = False,
    config: ConfigOption = None,
    runs_dir: RunsDirOption = Path("runs"),
    key: KeyOption = None,
) -> None:
    """Draft a profile for a model on any server, check it with a few small requests, then
    print it or add it. The check sets what the model sends of its reasoning. Recorded as a
    run."""
    import asyncio
    import uuid
    from typing import cast, get_args

    from aims.events import ModelBackend
    from aims.models import KeyRefused, ModelsConfig, ProfileError
    from aims.models.add import AddError, draft, gather
    from aims.models.listprice import SOURCE, PriceLists
    from aims.models.profiles import ROLES

    roles = [r for r in ROLES if r in (role or [])]
    if len(roles) < len(set(role or [])):
        raise typer.BadParameter("use agent, sweep or judge", param_hint="--role")
    if roles and not write:
        raise typer.BadParameter("needs --write", param_hint="--role")
    if base is not None and (base_url is not None or key_env is not None):
        raise typer.BadParameter("give --from or --base-url, not both", param_hint="--from")
    if base is None and base_url is None:
        raise typer.BadParameter(
            "give the server's --base-url, or --from with a profile that reaches it",
            param_hint="--base-url",
        )
    backends: tuple[str, ...] = get_args(ModelBackend)
    if backend is not None and backend not in backends:
        raise typer.BadParameter(f"use {' or '.join(backends)}", param_hint="--backend")

    settings: Any = _settings(config) if write or base is not None else None
    reached = None
    if base is not None:
        try:
            reached = settings.models.get(base)
        except ProfileError as e:
            _fail(str(e))
    listed, prices = asyncio.run(gather(PriceLists(), base_url=base_url, base=reached))
    if litellm is not None and prices.prices is None:
        _fail(f"no copy of {SOURCE}'s price map is at hand to read {litellm}: {prices.problem}")
    try:
        found = draft(
            model,
            name=name,
            base_url=base_url,
            key_env=key_env,
            base=reached,
            backend=cast("ModelBackend | None", backend),
            listed=listed,
            prices=prices.prices,
            row=litellm,
            unset=unsets or (),
            sets=sets or (),
        )
    except AddError as e:
        _fail(str(e))
    more: list[str] = []
    if litellm is None and found.candidates:
        more.append(
            f"{SOURCE} rows that may price it: {', '.join(found.candidates)}. Pick one with"
            " --litellm for its price, context size and output cap."
        )
    elif litellm is None and prices.prices is None:
        more.append(f"No copy of {SOURCE}'s price map is at hand: {prices.problem}.")
    elif litellm is None:
        more.append(
            f"No {SOURCE} row names it. aims models prices --search <text> finds rows to pick"
            " with --litellm."
        )

    # What is refused for free is refused before the check pays for calls.
    if why := found.unfit(roles):
        _fail(why)
    if write:
        if found.name in settings.models.profiles:
            _fail(f"a profile named {found.name} exists; pass --name for another")
        _add_table(settings, found, roles, trial=True)
    checks: list[Any] = []
    if not no_check:
        profile = found.profile
        try:
            profile.api_key(profile.url())
        except ProfileError as e:
            fix = ", or" if isinstance(e, KeyRefused) else "; set it, or"
            _fail(f"{e}{fix} pass --no-check to skip the check")
        typer.echo(f"checking {found.name} with a few small requests", err=True)
        models = ModelsConfig(profiles={found.name: profile})
        try:
            checks = asyncio.run(
                _check(
                    models, [profile], runs_dir, str(uuid.uuid7()), err=True, measure=found.guessed
                )
            )
        except asyncio.CancelledError:
            raise typer.Exit(EXIT_CANCELLED) from None
        kind = next((c.reasoning for c in checks if c.name == "plain"), None)
        if kind is not None:
            found = found.measured(kind)

    failed = [c.name for c in checks if c.verdict == "fail"]
    text = found.toml()
    if failed or not write:
        typer.echo(text, nl=False)
    if failed:
        if failed == ["structured"] and not {"sweep", "judge"} & set(roles):
            more.append(
                "--set structured_output=false adds it as a model without structured output,"
                " which only the agent role takes."
            )
        _notes([*found.notes, *more])
        what = ", so it is not added" if write else ""
        _fail(
            f"{found.name} failed the check: {', '.join(failed)}{what}. Change it with --set,"
            " or pass --no-check to skip the check"
        )
    if write:
        path = _add_table(settings, found, roles)
        _echo(f"added {found.name} to {path}", err=True)
        for r in roles:
            was = getattr(settings.models.roles, r)
            _echo(f"role {r}: {found.name}" + (f" (was {was})" if was else ""), err=True)
        if no_check:
            more.append(f"It is not checked. aims models check {found.name} checks it.")
    _notes([*found.notes, *more])


def _fail(text: str) -> NoReturn:
    """Exit 1 with error `text`, which may hold outside text."""
    _echo(f"error: {text}", err=True)
    raise typer.Exit(1)


def _notes(notes: list[str]) -> None:
    for note in notes:
        _echo(f"note: {note}", err=True)


def _add_table(settings: Any, found: Any, roles: list[Any], *, trial: bool = False) -> Path:
    """Add draft `found` to the project file of `settings` with `roles`, or with `trial` only
    try to. Exit 1 with the problem."""
    from aims.config import ConfigError, add_table
    from aims.models import ProfileError
    from aims.prompts import TextError

    try:
        return add_table(
            settings.layers, found.toml(), dict.fromkeys(roles, found.name), trial=trial
        )
    except (ConfigError, ProfileError, TextError) as e:
        _echo(f"error: {e}", err=True)
    except OSError as e:
        _echo(f"error: cannot write {e.filename or 'the config file'}: {e.strerror}", err=True)
    raise typer.Exit(1)


async def _stop_lines(store: Any, kind: str, run_ids: list[str]) -> list[str]:
    """Where a command that was stopped left each of its runs `run_ids`, of kind `kind`:
    sealed, with the status it has, or never made. A stop that came as a run sealed leaves the
    status it had."""
    lines: list[str] = []
    for run_id in run_ids:
        info = await store.run(run_id)
        if info is not None:
            state = "is not sealed" if info.status == "running" else f"is sealed as {info.status}"
            lines.append(f"stopped; {kind} {run_id} {state}")
    left = len(run_ids) - len(lines)
    if left and len(run_ids) == 1:
        lines.append(f"stopped before the {kind} was made")
    elif left:
        were = "was" if left == 1 else "were"
        lines.append(f"stopped before {left} of {len(run_ids)} {kind}s {were} made")
    return lines


async def _seal_left(store: Any, say: Callable[[str], object]) -> list[str]:
    """Seal as failed each run in `store` that an earlier process left running, as `aims
    runner` does when it starts. `say` gets one line with how many. A run another process
    still writes is left alone. Returns the runs it sealed."""
    from aims.run.runner import ENDED, seal_unsealed

    sealed = await seal_unsealed(store, reason=ENDED)
    if len(sealed) == 1:
        say("an earlier process left 1 run running; it is sealed as failed")
    elif sealed:
        say(f"an earlier process left {len(sealed)} runs running; they are sealed as failed")
    return sealed


SHIP_SEALED_S = 30.0


async def _ship_sealed(
    store: Any, run_ids: list[str], link: tuple[str, str] | None, say: Callable[[str], object]
) -> None:
    """Ship the runs of `run_ids` that the control plane at `link` holds a copy of, so it
    shows them sealed too. `_seal_left` sealed them; a run the control plane never got stays
    here. Waits at most SHIP_SEALED_S, as a shipper tries again without end; `say` gets what
    did not reach it."""
    import asyncio

    from aims.node.client import ControlClient
    from aims.node.shipper import Shipper

    if link is None or not run_ids:
        return
    client = ControlClient(*link, runner=f"push@{_hostname()}")
    try:
        async with asyncio.timeout(SHIP_SEALED_S):
            await client.hello(_push_hello(client.runner))
            heads = await client.heads(run_ids)
            for run_id in run_ids:
                head = heads.get(run_id)
                if head is None or head.seq < 0 or head.deleted:
                    continue
                shipper = Shipper(store, client, run_id, seen=True)
                await shipper.run()
                if reason := shipper.diverged or shipper.refused:
                    say(f"{run_id} did not reach the control plane: {reason}")
    except Exception as e:
        # The command goes on whatever went wrong here, as an answer that is no control
        # plane's. Its first line is enough to say why.
        why = (str(e) or type(e).__name__).splitlines()[0]
        say(f"the sealed runs did not reach the control plane: {why}; `aims push` them later")
    finally:
        await client.aclose()


async def _stopped(store: Any, run_id: str, kind: str) -> None:
    """Say where a command that was stopped left its run `run_id`, as the store has it."""
    for line in await _stop_lines(store, kind, [run_id]):
        typer.echo(line, err=True)


async def _check(
    models: Any,
    chosen: list[Any],
    runs_dir: Path,
    run_id: str,
    *,
    err: bool = False,
    measure: bool = False,
) -> list[Any]:
    """Check the profiles `chosen`, recorded as run `run_id`. Print a line for each test and
    the run's id, to stderr when `err`. `measure` is as for check_profile. Returns the
    checks."""
    import asyncio

    from aims.events import LocalEventStore
    from aims.models.check import record_check

    def show(c: Any) -> None:
        _echo(f"{c.verdict:4}  {c.profile}  {c.name}: {c.detail}", err=err)

    async with await LocalEventStore.open(runs_dir) as store:
        try:
            with _stoppable():
                await _seal_left(store, lambda text: typer.echo(text, err=True))
                _, checks = await record_check(
                    store, models, chosen, run_id=run_id, on_check=show, measure=measure
                )
        except asyncio.CancelledError:
            await _stopped(store, run_id, "run")
            raise
    typer.echo(f"run {run_id}", err=err)
    return checks


images_app = typer.Typer(
    no_args_is_help=True, cls=_Group, help="Docker images AIMS builds and pulls."
)
app.add_typer(images_app, name="images")


@images_app.command("prune")
def images_prune(
    days: Annotated[
        float | None,
        typer.Option(
            min=0,
            callback=_days,
            help="Remove scenario images unused this many days. By default \\[images] keep_days.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would go and remove nothing.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Remove the images that AIMS needs no more and no container uses: the helper images of
    other AIMS versions and scenario images unused for a while. `aims run`, `aims runner` and
    the self-test do this when they start."""
    import asyncio

    from aims.config import ConfigError, image_keep_days, layers
    from aims.sandbox import images
    from aims.sandbox.execs import docker_client

    try:
        keep = image_keep_days(layers(config)) if days is None else days
    except ConfigError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None

    async def main() -> list[str]:
        docker = docker_client()
        try:
            return await images.remove_old(docker, keep, dry_run=dry_run)
        finally:
            await docker.close()

    try:
        refs = asyncio.run(main())
    except Exception as e:
        _echo(f"error: cannot prune images: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    for ref in refs:
        typer.echo(f"{'would remove' if dry_run else 'removed'} {ref}")
    if not refs:
        typer.echo("nothing to remove")


scenario_app = typer.Typer(no_args_is_help=True, cls=_Group, help="Scenario files.")
app.add_typer(scenario_app, name="scenario")
prompts_app = typer.Typer(
    no_args_is_help=True, cls=_Group, help="The texts the agents and the monitor read."
)
app.add_typer(prompts_app, name="prompts")


@prompts_app.command("show")
def prompts_show(config: ConfigOption = None) -> None:
    """Print the texts agents and the monitor get with this aims.toml, as \\[prompts] tables to
    edit: the agent's, among them the `denied` reply and `notify_on_resume`, each tool's, the
    monitor's, its category definitions and its glossary of record types. Channel tools are
    listed by the names their kinds give them; a scenario that names them otherwise takes their
    texts under its own names.

    A scenario agent's `prompts` mapping overrides the agent's texts for that agent.
    """
    import json

    from aims.channels import default_tools
    from aims.monitor.texts import resolve
    from aims.prompts import TextError
    from aims.report.escape import visible
    from aims.tools import UnknownTool, registry
    from aims.tools import resolve as resolve_tool

    layered = _settings(config).prompts
    try:
        agent = layered.agent_texts()
        tools = {
            name: cls(layered.tools.get(name)).text for name, cls in sorted(registry().items())
        }
        for name, cls in sorted(default_tools().items()):
            tools[name] = resolve_tool(cls, name, layered.tools.get(name))
        unknown = sorted(set(layered.tools) - set(tools))
        if unknown:
            raise UnknownTool(f"[prompts.tools] names unknown tool(s) {', '.join(unknown)}")
        monitor = resolve(layered)
    except (TextError, UnknownTool) as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None

    def value(text: str) -> str:
        # A TOML string. visible() writes \uXXXX and \UXXXXXXXX, which TOML reads back.
        return visible(json.dumps(text, ensure_ascii=False))

    lines = ["[prompts]"]
    lines += [f"{key} = {value(text)}" for key, text in agent.model_dump().items()]
    for name, texts in tools.items():
        lines += ["", f"[prompts.tools.{name}]"]
        lines += [f"{key} = {value(text)}" for key, text in texts.items()]
    lines += ["", "[prompts.monitor]"]
    lines += [f"{key} = {value(text)}" for key, text in sorted(monitor.texts.items())]
    lines += ["", "[prompts.monitor.categories]"]
    lines += [f"{key} = {value(text)}" for key, text in monitor.categories.items()]
    # a record type has dots, so its key is quoted
    lines += ["", "[prompts.monitor.glossary]"]
    lines += [f"{value(key)} = {value(text)}" for key, text in monitor.glossary.items()]
    typer.echo("\n".join(lines))


EXIT_FAILED, EXIT_KILLED, EXIT_CANCELLED = 1, 3, 130


@scenario_app.command("check")
def scenario_check(
    path: Annotated[Path, typer.Argument(help=SCENARIO_HELP, callback=_scenario)],
    config: ConfigOption = None,
    profile: Annotated[str | None, typer.Option(help="Profile for every agent.")] = None,
    monitor: MonitorPresetOption = None,
    runs_dir: Annotated[
        Path, typer.Option(file_okay=False, help="Directory of past runs the cost estimate reads.")
    ] = Path("runs"),
    selftest: Annotated[
        bool,
        typer.Option(
            "--selftest/--no-selftest",
            help="Run the self-test in Docker with the kernel tracer, at no model cost.",
        ),
    ] = True,
    selftest_runs: Annotated[
        Path | None,
        typer.Option(
            file_okay=False,
            help="Keep the self-test's runs here. By default they go to a temporary directory,"
            " kept only when a check fails.",
        ),
    ] = None,
) -> None:
    """Validate a scenario file, the profiles and tools it names, and its monitor: the
    rules, the signal options and the monitor's models. Then estimate what a run costs.

    A role the config leaves unset is a note, not an error: the scenario is fine and the
    note names the fix. The estimate then waits for it.

    A scenario with a `selftest` or an impossible plant then gets its self-test: its
    reference script must exit 0 and trigger no plant, its exploit script must take each
    plant it claims, and an impossible plant's verifier must fail its reference and an
    untouched workdir. Each is a run of scripted agents, checked as `aims check` checks a run.
    The agents follow scripts, so it needs no model."""
    from typing import cast

    from aims.monitor.config import Preset
    from aims.run.job import monitor_problems
    from aims.run.runner import check
    from aims.scenario import ScenarioError, load

    _preset(monitor)
    if selftest_runs is not None and not selftest:
        raise typer.BadParameter("needs the self-test", param_hint="--selftest-runs")
    settings = _settings(config)
    notes: list[str] = []
    try:
        bundle = load(path)
        problems = check(bundle, settings.models, profile, settings.prompts, unset=notes)
    except ScenarioError as e:
        problems = str(e).splitlines()
    else:
        problems += monitor_problems(
            settings.models,
            settings.prompts,
            bundle.scenario,
            layers=settings.monitor,
            profile=profile,
            preset=cast("Preset | None", monitor),
            notes=notes,
        )
        if bundle.scenario.plants:
            from aims.run import truth

            try:
                prepared = truth.prepare(bundle, seed=b"scenario-check")
            except Exception as e:
                problems.append(f"plants: {type(e).__name__}: {e}")
            else:
                for plant in prepared.plants.values():
                    problems += list(plant.lint())
    for problem in problems:
        _echo(f"error: {problem}", err=True)
    for note in notes:
        _echo(f"note: {note}", err=True)
    if problems:
        raise typer.Exit(EXIT_FAILED)
    s = bundle.scenario
    agents = ", ".join(a.id for a in s.agents)
    _echo(f"ok: {s.name}, agents {agents}, digest {bundle.digest[:16]}")
    if notes:
        _echo("note: no cost estimate until those roles are set", err=True)
    else:
        _echo_estimate(settings, [bundle], runs_dir, profile=profile, preset=monitor)
    from aims.run import selftest as st

    if selftest and st.wanted(s):
        code = _selftest(settings, bundle, selftest_runs)
        if code:
            raise typer.Exit(code)


def _selftest(settings: Any, bundle: Any, keep: Path | None) -> int:
    """Run the self-test of `bundle` and print each run's verdict."""
    import asyncio
    import shutil
    import tempfile

    from aims.events import LocalEventStore
    from aims.run import selftest as st
    from aims.run.runner import Bench
    from aims.sandbox import DockerBackend

    try:
        plan = st.cases(bundle)
    except st.SelfTestError as e:
        for problem in e.problems:
            _echo(f"error: {problem}", err=True)
        return EXIT_FAILED

    def show(o: Any) -> None:
        typer.echo(f"selftest {o.case}: {'ok' if o.ok else 'failed'}")
        for note in o.notes:
            _echo(f"  {note}")
        for problem in o.problems:
            _echo(f"error: selftest {o.case}: {problem}", err=True)

    async def main(root: Path) -> list[Any] | str:
        try:
            backend = await DockerBackend.open(
                audit="kernel", image_keep_days=settings.image_keep_days
            )
        except Exception as e:
            return f"the self-test needs Docker and the kernel tracer: {e}"
        async with backend, await LocalEventStore.open(root) as store:
            await _seal_left(store, lambda text: typer.echo(text, err=True))
            bench = Bench(
                store,
                backend,
                settings.models,
                audit="kernel",
                prompts=settings.prompts,
                config_files=settings.layers.digests(),
            )
            return await st.run(bench, bundle, plan, on_outcome=show)

    root = keep or Path(tempfile.mkdtemp(prefix="aims-selftest-"))
    found = asyncio.run(main(root))
    if isinstance(found, str):
        _echo(f"error: {found}; --no-selftest skips it", err=True)
        if keep is None:
            shutil.rmtree(root, ignore_errors=True)
        return EXIT_FAILED
    if all(o.ok for o in found):
        if keep is None:
            shutil.rmtree(root, ignore_errors=True)
        return 0
    _echo(f"selftest runs kept in {root}", err=True)
    return EXIT_FAILED


def _echo_estimate(
    settings: Any,
    bundles: list[Any],
    runs_dir: Path,
    *,
    profile: str | None = None,
    preset: Any = None,
    samples: int = 1,
    json_out: bool = False,
    budget: Any = None,
    score: bool = True,
) -> None:
    """Print the cost estimate of `samples` runs of each bundle under `budget`, each scored
    with `score`, from the runs under `runs_dir`."""
    import asyncio

    from aims.models import ProfileError
    from aims.models.listprice import PriceLists
    from aims.monitor.config import MonitorConfigError
    from aims.prompts import TextError
    from aims.run.estimate import Estimate, estimate_runs, load_history, priced_runs
    from aims.scenario.budget import BudgetError
    from aims.terminal import json_text

    async def main() -> Estimate:
        laid, listing = await priced_runs(
            settings,
            bundles,
            profile=profile,
            preset=preset,
            budget=budget,
            lists=PriceLists(),
            score=score,
        )
        return await estimate_runs(
            await load_history(runs_dir),
            laid,
            bundles,
            profile=profile,
            preset=preset,
            samples=samples,
            budget=budget,
            listing=listing,
            score=score,
        )

    try:
        cost = asyncio.run(main())
    except (MonitorConfigError, ProfileError, TextError, BudgetError) as e:
        _echo(f"no cost estimate: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if json_out:
        typer.echo(json_text(cost.model_dump(mode="json")))
        return
    for line in cost.lines:
        _echo(line)


@scenario_app.command("delete")
def scenario_delete(
    source: Annotated[str, typer.Argument(help="The scenario's source, such as project.")],
    name: Annotated[str, typer.Argument(help="The scenario's folder name.")],
    yes: Annotated[bool, typer.Option("--yes", help="Delete without asking.")] = False,
    server: ServerOption = None,
    token_file: OperatorTokenFileOption = None,
) -> None:
    """Delete a scenario's folder on a control plane. Its runs stay.

    Needs an operator token in AIMS_TOKEN or --token-file. Built-in scenarios are read only.
    A scenario that a queued or active job runs is refused.
    """
    import asyncio

    from aims.report.escape import visible

    url, token = _operator(server, token_file)
    question = visible(f"Delete scenario {name} from {source}? Its runs stay.")
    if not yes and not typer.confirm(question, default=False, err=True):
        _echo(f"kept {name}", err=True)
        raise typer.Exit(EXIT_FAILED)
    raise typer.Exit(asyncio.run(_delete_scenario(url, token, source, name)))


async def _delete_scenario(url: str, token: str, source: str, name: str) -> int:
    import httpx2

    from aims.net import Outbound

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=60, transport=Outbound()
    ) as c:
        try:
            r = await c.delete(f"/api/scenarios/{source}/{name}")
        except httpx2.TransportError as e:
            _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    if r.status_code >= 400:
        return _problem(r)
    _echo(f"deleted scenario {name}")
    return 0


@scenario_app.command("schema")
def scenario_schema() -> None:
    """Print the JSON Schema of scenario files."""
    import json

    from aims.scenario import Scenario

    typer.echo(json.dumps(Scenario.model_json_schema(), indent=2))


@app.command()
def run(
    scenario: Annotated[
        Path | None,
        typer.Argument(
            help="The scenario file, its directory, or builtin/<name>; optional with --replay.",
            callback=_scenario,
        ),
    ] = None,
    config: ConfigOption = None,
    runs_dir: RunsDirOption = Path("runs"),
    profile: Annotated[
        str | None, typer.Option(help="Profile for every agent; with --replay, after the fork.")
    ] = None,
    samples: Annotated[int, typer.Option(min=1, help="Independent runs to make.")] = 1,
    parallel: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 1,
    audit: Annotated[
        str, typer.Option(help="kernel (the tracer must start) or snapshot.")
    ] = "kernel",
    keep: Annotated[bool, typer.Option(help="Leave containers and volumes after the run.")] = False,
    quiet: Annotated[bool, typer.Option(help="No live view; one line per run.")] = False,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json", help="Print JSON lines: monitor flags, actions, sweeps, verdicts, results."
        ),
    ] = False,
    replay: Annotated[str | None, typer.Option(help="Run id to replay and fork.")] = None,
    fork_at: Annotated[
        str | None,
        typer.Option(
            help="Where the fork cuts: agent:N after that agent's step N, @seq after an event,"
            " or N, the last step replayed of a run of one agent. A run of one agent forks by"
            " default after the last step a fork can replay."
        ),
    ] = None,
    keep_others: Annotated[
        bool,
        typer.Option(
            help="With --fork-at agent:N, the other agents keep their recorded answers until"
            " their own inputs change."
        ),
    ] = False,
    strict_env: Annotated[
        bool, typer.Option(help="Stop a fork at the first tool output that differs.")
    ] = False,
    inject: Annotated[
        str | None, typer.Option(help="Message given to the agent at the fork.")
    ] = None,
    server: ServerOption = None,
    token_file: TokenFileOption = None,
    no_server: Annotated[
        bool, typer.Option("--no-server", help="Do not ship to a local `aims ui`.")
    ] = False,
    monitor: MonitorPresetOption = None,
    variant: Annotated[
        str | None,
        typer.Option(
            "--variant",
            help="A named scenario variant to apply. A fork keeps its origin's variant.",
        ),
    ] = None,
    series: Annotated[
        str | None,
        typer.Option("--series", help="Name of a series whose runs continue each other."),
    ] = None,
    runs: Annotated[
        int, typer.Option("--runs", min=1, help="Runs to make in the series, one after another.")
    ] = 1,
    yes: Annotated[
        bool, typer.Option("--yes", help="Start even when the cost estimate is above the limit.")
    ] = False,
    series_restart: Annotated[
        bool,
        typer.Option(
            "--series-restart",
            help="Start the series fresh from the scenario's own folders, at its next free index.",
        ),
    ] = False,
    score: Annotated[
        bool,
        typer.Option(
            "--score/--no-score",
            help="Score each sealed run that has a truth file against it, as aims score does.",
        ),
    ] = True,
    oversight: Annotated[
        str | None,
        typer.Option(
            help="What the monitor may do, over the scenario's oversight: observe, autonomous"
            " or human."
        ),
    ] = None,
    budget: BudgetOption = None,
    key: KeyOption = None,
) -> None:
    """Run a scenario, or fork a recorded run at a step.

    With a control plane (a local `aims ui`, or --server) the runs ship to it and take its
    pause, inject and kill. The monitor's flags, pauses and verdict show in the live view, and
    as JSON lines with --json. Under human oversight a pause asks at the terminal.

    With --series, --runs K makes K runs in a row, each continuing from the one before it.
    """
    import asyncio
    from typing import get_args

    from aims.events import Oversight

    _preset(monitor)
    if audit not in ("kernel", "snapshot"):
        raise typer.BadParameter("use kernel or snapshot", param_hint="--audit")
    oversights = get_args(Oversight)
    if oversight is not None and oversight not in oversights:
        raise typer.BadParameter(f"use {', '.join(oversights)}", param_hint="--oversight")
    if scenario is None and replay is None:
        raise typer.BadParameter("give a scenario file or --replay", param_hint="SCENARIO")
    caps = _budget(budget)
    if replay is None and (fork_at is not None or strict_env or inject is not None or keep_others):
        raise typer.BadParameter(
            "needs --replay", param_hint="--fork-at/--strict-env/--inject/--keep-others"
        )
    if replay is not None and variant is not None:
        raise typer.BadParameter("a fork keeps its origin's variant", param_hint="--variant")
    if keep_others and (fork_at is None or ":" not in fork_at):
        raise typer.BadParameter("needs --fork-at agent:N", param_hint="--keep-others")
    if series_restart and series is None:
        raise typer.BadParameter("needs --series", param_hint="--series-restart")
    if series is None and runs != 1:
        raise typer.BadParameter("needs --series", param_hint="--runs")
    if series is not None:
        if not series.strip():
            raise typer.BadParameter("a series needs a name", param_hint="--series")
        if replay is not None:
            raise typer.BadParameter("runs a scenario, not a replay", param_hint="--series")
        if samples > 1:
            raise typer.BadParameter(
                "use --runs for a series, not --samples", param_hint="--samples"
            )
    link = _link(server, token_file, no_server)
    code = asyncio.run(
        _run(
            scenario=scenario,
            config=config,
            runs_dir=runs_dir,
            profile=profile,
            samples=samples,
            parallel=parallel,
            audit=audit,
            keep=keep,
            quiet=quiet,
            json_out=json_out,
            replay=replay,
            fork_at=fork_at,
            keep_others=keep_others,
            series_restart=series_restart,
            strict_env=strict_env,
            inject=inject,
            link=link,
            monitor=monitor,
            variant=variant,
            series=series,
            runs=runs,
            yes=yes,
            score=score,
            oversight=oversight,
            budget=caps,
        )
    )
    raise typer.Exit(code)


BudgetOption = Annotated[
    str | None,
    typer.Option(
        "--budget",
        help="Caps over the scenario's, such as steps=200,cost_usd=10: steps, output_tokens,"
        " cost_usd and wall_s set every agent, AGENT.steps one agent and run.steps the run's"
        " limits; none lifts a cap. The run's caps count the agents;"
        " run.monitor_cost_usd caps the monitor's own spend, where none gives back the"
        " preset's cap. A fork keeps its origin's and lays these over them.",
    ),
]


def _budget(value: str | None) -> Any:
    from aims.scenario.budget import BudgetError, parse

    if value is None:
        return None
    try:
        found = parse(value)
    except BudgetError as e:
        raise typer.BadParameter(str(e), param_hint="--budget") from None
    return None if found.empty else found


def _budget_line(scenario: Any, budget: Any) -> str | None:
    from aims.scenario import budget as budgets

    found = budgets.changes(scenario.model_dump(mode="json"), budget)
    return budgets.describe(found, agents=len(scenario.agents))


MonitorPresetOption = Annotated[
    str | None,
    typer.Option(
        "--monitor",
        help="Monitor preset: off, signals (needs no key), standard or research."
        " Default: the config files and the scenario, else standard.",
    ),
]


def _preset(value: str | None, allowed: tuple[str, ...] | None = None) -> None:
    from aims.monitor.config import PRESETS

    allowed = allowed or PRESETS
    if value is not None and value not in allowed:
        raise typer.BadParameter(f"use {', '.join(allowed)}", param_hint="--monitor")


async def _run(**o: Any) -> int:
    import asyncio
    import logging
    import os
    import sys
    import uuid
    from dataclasses import replace

    from rich.console import Console
    from rich.live import Live
    from rich.logging import RichHandler

    from aims.aio import every, in_turn
    from aims.channels import archives
    from aims.events import LocalEventStore, Series
    from aims.models import KeyRefused, ProfileError
    from aims.models.listprice import PriceLists
    from aims.monitor.config import MonitorConfigError
    from aims.node.client import ControlClient
    from aims.node.link import Link
    from aims.run import truth
    from aims.run.estimate import (
        estimate_fork,
        estimate_runs,
        guard,
        history,
        priced_fork,
        priced_runs,
        warning_lines,
    )
    from aims.run.fork import (
        CutAt,
        check_fork,
        cut_at,
        fork_agents,
        fork_of,
        fork_profiles,
        fork_schedules,
        fork_step,
        load_origin,
        planner,
        preview,
        same_scenario,
    )
    from aims.run.job import monitor_for, monitor_profiles
    from aims.run.runner import Bench, Fork, Options, Result, RunError, Runner, agent_profile, check
    from aims.run.series import series_start
    from aims.run.view import HumanPrompt, JsonLines, LiveView, digest, result_json, summary
    from aims.sandbox import DockerBackend, SandboxError
    from aims.scenario import ScenarioError, load
    from aims.scenario.budget import BudgetError
    from aims.terminal import (
        Stdout,
        VisibleLog,
        json_text,
        on_hangup,
        on_stop,
        one_line,
        shown,
    )
    from aims.wire import TracerHealth

    err = Console(stderr=True)
    stdout = Stdout(typer.echo)
    handler = RichHandler(console=err, show_path=False)
    handler.setFormatter(VisibleLog())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)

    def fail(message: str) -> int:
        err.print("[red]error:[/]", shown(message))
        return EXIT_FAILED

    from aims.config import ConfigError
    from aims.config import load as load_settings
    from aims.prompts import TextError

    try:
        settings = load_settings(o["config"])
    except (ConfigError, ProfileError, TextError) as e:
        return fail(str(e))
    models, prompts = settings.models, settings.prompts

    def say(text: str) -> None:
        err.print(shown(text, "yellow"))

    async with await LocalEventStore.open(o["runs_dir"]) as store:
        await _ship_sealed(store, await _seal_left(store, say), o["link"], say)
        origin = None
        fork_base: Fork | None = None
        try:
            cut: CutAt | None = None
            if o["replay"] is not None:
                origin = await load_origin(store, o["replay"])
                if o["fork_at"] is None and len(origin.bundle.scenario.agents) > 1:
                    return fail("a run of several agents forks at --fork-at agent:N or @seq")
                found = cut_at(origin, o["fork_at"]) if o["fork_at"] is not None else None
                if isinstance(found, CutAt):
                    cut = found
                    step = min(found.steps.values(), default=0)
                else:
                    step = found if found is not None else fork_step(origin)
                    check_fork(origin, step)
                bundle = origin.full_bundle or origin.bundle
                if o["scenario"] is not None and not await asyncio.to_thread(
                    lambda: same_scenario(origin, load(o["scenario"]))
                ):
                    return fail(f"{o['scenario']} changed since run {origin.run_id} was recorded")
                fork_profile = o["profile"]
                if fork_profile is not None:
                    models.get(fork_profile)
                fork_base = fork_of(
                    origin, step, cut, profile=fork_profile, keep_others=o["keep_others"]
                )
                budget = origin.fork_budget(o["budget"])
                replayed = (
                    dict(cut.steps)
                    if cut is not None
                    else {a.id: step for a in origin.bundle.scenario.agents}
                )
                origin.check_budget(o["budget"], replayed)
                base = origin.bundle.scenario
            else:
                bundle = load(o["scenario"], o["variant"])
                problems = check(bundle, models, o["profile"], prompts)
                if problems:
                    for problem in problems:
                        fail(problem)
                    return EXIT_FAILED
                budget = o["budget"]
                if budget is not None:
                    budget.check(a.id for a in bundle.scenario.agents)
                base = bundle.scenario
        except (ScenarioError, RunError, ProfileError) as e:
            return fail(str(e))
        except BudgetError as e:
            return fail(f"--budget: {e}")
        if (line := _budget_line(base, budget)) is not None:
            err.print(shown(line))
        # the list prices go on the profiles before anything resolves them, so the caps, the
        # monitor's budget and the estimate count them as the run does
        lists = PriceLists()
        try:
            if origin is not None and fork_base is not None:
                settings, listing = await priced_fork(
                    settings,
                    origin,
                    fork_base,
                    preset=o["monitor"],
                    budget=o["budget"],
                    lists=lists,
                    score=o["score"],
                )
                models = settings.models
                agents = fork_agents(models, origin, fork_base)
            else:
                settings, listing = await priced_runs(
                    settings,
                    [bundle],
                    profile=o["profile"],
                    preset=o["monitor"],
                    budget=budget,
                    lists=lists,
                    score=o["score"],
                )
                models = settings.models
                agents = [agent_profile(models, a, o["profile"]) for a in bundle.scenario.agents]
            monitor = monitor_for(
                models,
                prompts,
                bundle.scenario,
                agents,
                layers=settings.monitor,
                preset=o["monitor"],
                budget=budget,
            )
        except (MonitorConfigError, ProfileError, TextError, RunError) as e:
            return fail(f"monitor: {e}")
        except BudgetError as e:
            return fail(f"--budget: {e}")
        if monitor is not None and o["oversight"] is not None:
            monitor = replace(monitor, oversight=o["oversight"])
        if monitor is not None:
            for p in monitor_profiles(monitor).values():
                try:
                    p.api_key(p.url())
                except ProfileError as e:
                    fix = ", or" if isinstance(e, KeyRefused) else "; set it, or"
                    return fail(
                        f"the monitor's {e}{fix} use a signals-only monitor, which"
                        ' needs no key: preset = "signals" under [monitor] in aims.toml,'
                        " monitor: {preset: signals} in the scenario, or --monitor signals"
                    )
        cost = None
        try:
            if origin is not None and fork_base is not None:
                cost = await estimate_fork(
                    await history(store),
                    settings,
                    origin,
                    fork_base,
                    preset=o["monitor"],
                    samples=o["samples"],
                    budget=o["budget"],
                    listing=listing,
                    score=o["score"],
                )
            else:
                cost = await estimate_runs(
                    await history(store),
                    settings,
                    [bundle],
                    profile=o["profile"],
                    preset=o["monitor"],
                    samples=o["samples"] * o.get("runs", 1),
                    budget=budget,
                    listing=listing,
                    score=o["score"],
                )
        except (MonitorConfigError, ProfileError, TextError, RunError, KeyError) as e:
            err.print("[yellow]no cost estimate:[/]", shown(e))
        if cost is not None:
            if o["json_out"]:
                stdout(json_text({"type": "estimate", **cost.model_dump(mode="json")}))
            else:
                for text in warning_lines(cost) if o["quiet"] else cost.lines:
                    err.print(shown(text))
        terminal = sys.stdin.isatty() and sys.stderr.isatty()
        interactive = terminal and not o["quiet"] and not o["json_out"]
        human = monitor is not None and monitor.oversight == "human"
        if human and o["link"] is None and not interactive:
            return fail(
                "human oversight waits for a person: run it in a terminal without --json or"
                " --quiet, or link the run to an aims ui with --server"
            )
        if cost is not None:
            refused = guard(
                cost,
                yes=o["yes"],
                tty=terminal,
                ask=lambda q: typer.confirm(q, default=False, err=True),
            )
            if refused is not None:
                return fail(refused)
        try:
            backend = await DockerBackend.open(
                audit=o["audit"], image_keep_days=settings.image_keep_days
            )
        except (SandboxError, Exception) as e:
            return fail(f"cannot start the sandbox backend: {e}")
        async with backend:
            linked = o["link"] is not None
            bench = Bench(
                store,
                backend,
                models,
                audit=o["audit"],
                prompts=prompts,
                config_files=settings.layers.digests(),
            )
            steps = cost.parts[0].steps if cost is not None else None
            view = None if o["quiet"] or o["json_out"] else LiveView(store, bench.bus, steps)
            lines = JsonLines(store, stdout) if o["json_out"] else None
            prompt = HumanPrompt(err) if human and interactive else None

            def started(run_id: str) -> None:
                if view is not None:
                    view.add(run_id)
                if lines is not None:
                    lines.add(run_id)

            group = uuid.uuid4().hex[:12] if o["samples"] > 1 and o["series"] is None else None

            def make(
                i: int,
                series_run: Series | None = None,
                inherited: list[dict[str, Any]] | None = None,
                restored: dict[str, bytes] | None = None,
            ) -> Runner:
                options = Options(
                    profile=o["profile"],
                    keep=o["keep"],
                    group=group,
                    sample=i if group else None,
                    samples=o["samples"] if series_run is None else 1,
                    variant=o["variant"],
                    oversight=o["oversight"],
                    budget=budget,
                )
                kwargs: dict[str, Any] = {}
                if series_run is not None:
                    options.series = series_run
                    options.inherited = inherited
                    options.restored = restored
                elif origin is not None and fork_base is not None:
                    fork = replace(
                        fork_base, sample=i, group=group, strict=o["strict_env"], inject=o["inject"]
                    )
                    if i == 0:
                        for line in preview(origin, fork):
                            err.print(shown(line))
                    options.fork = fork
                    options.seed = origin.seed
                    options.inherited = origin.inherited
                    options.restored = dict(origin.restored) or None
                    kwargs = {
                        "profiles": fork_profiles(bench.models, origin, fork),
                        "schedules": fork_schedules(origin, fork),
                        "texts": origin.texts,
                        "replay": planner(bench, origin, fork),
                    }
                made = Runner(bench, bundle, options, on_start=started, monitor=monitor, **kwargs)
                if prompt is not None:
                    made.human = prompt.hook(made)
                return made

            runners: list[Runner] = []
            link = None
            if linked:
                url, token = o["link"]
                link = Link(
                    ControlClient(url, token, runner=f"run-{_hostname()}-{os.getpid()}"),
                    store,
                    bench.bus,
                    tracer=lambda: TracerHealth(
                        alive=backend.tracer is not None and backend.tracer.alive,
                        detail=None
                        if backend.unsupported is None
                        else f"kernel audit unavailable: {backend.unsupported}",
                    ),
                )
                await link.start()
            gate = asyncio.Semaphore(o["parallel"])
            stops = 0
            closing = False
            main = asyncio.current_task()
            loop = asyncio.get_running_loop()

            def stop() -> None:
                nonlocal stops
                # Once the runs are over, a stop does nothing. The cleanup left is bounded. A
                # cut one can leave the store open, which holds the process at exit.
                if closing:
                    return
                stops += 1
                if stops == 1:
                    err.print("[yellow]stopping: Ctrl-C again to kill the sandboxes now[/]")
                    for r in runners:
                        r.cancel()
                elif main is not None:
                    main.cancel()

            # The handlers stay in until asyncio.run closes the loop. Without them a Ctrl-C
            # raises KeyboardInterrupt wherever the cleanup is.
            on_stop(loop, stop, say=lambda text: err.print(shown(text, "yellow")))
            # A hangup starts a stop and never adds to one: nobody is left to press Ctrl-C.
            on_hangup(loop, lambda: None if stops else stop())

            async def one(r: Runner) -> Result:
                async with gate:
                    if r.cancelled:
                        return Result(None, "cancelled", "cancelled before it started")
                    try:
                        result = await r.run()
                    except RunError as e:
                        return Result(r.run_id, "failed", str(e))
                if o["score"] and result.run_id is not None and not r.cancelled:
                    await score_after(result.run_id)
                return result

            async def score_after(run_id: str) -> None:
                """Score a sealed run that has a truth file; a score that fails warns. A score
                may call a model, so none starts after a stop."""
                from aims.run.scoring import ScoreError, score_sealed

                if not truth.path(store.root / run_id).is_file():
                    return
                if stops:
                    err.print(
                        f"[yellow]run {run_id} was not scored:[/] aims run was stopped;"
                        f" `aims score {run_id}` scores it"
                    )
                    return
                score_id = str(uuid.uuid7())
                if link is not None:
                    link.ship(score_id)
                try:
                    scored = await score_sealed(store, settings, run_id, run_id=score_id)
                    if scored.checks_off is not None:
                        err.print(f"[yellow]score of run {run_id}:[/]", shown(scored.checks_off))
                    if scored.stopped is not None:
                        stopped = f"the checks stopped: {scored.stopped}"
                        err.print(f"[yellow]score of run {run_id}:[/]", shown(stopped))
                except ScoreError as e:
                    err.print(f"[yellow]run {run_id} was not scored:[/]", shown(e))
                except Exception as e:
                    logging.getLogger("aims.run").exception("scoring run %s failed", run_id)
                    why = shown(f"{type(e).__name__}: {e}")
                    err.print(f"[yellow]run {run_id} was not scored:[/]", why)
                finally:
                    # A score run that never began has no line to ship. Its shipper would
                    # wait for one until the link closes, and the drain for all of its time.
                    if link is not None and not store.holds(score_id):
                        link.unship(score_id)

            async def execute(batch: list[Runner]) -> list[Result]:
                # Every run ends, its seal too, before this returns or raises: what follows
                # closes the link, the sandboxes' backend and the store.
                if view is None:
                    return await every(one(r) for r in batch)
                with Live(view, console=err, refresh_per_second=4) as live:
                    if prompt is not None:
                        prompt.live = live
                    try:
                        found = await every(one(r) for r in batch)
                        await asyncio.sleep(0.3)
                        return found
                    finally:
                        if prompt is not None:
                            prompt.live = None

            results: list[Result] = []
            try:
                if view is not None:
                    view.start()
                if o["series"] is not None:
                    previous = None
                    inherited = None
                    restored = None
                    last, start = await series_start(store, o["series"], o["series_restart"])
                    if o["series_restart"]:
                        err.print(shown(f"series {o['series']} starts fresh at #{start}"))
                    elif last is not None:
                        previous = last.run_id
                        inherited = truth.inherit(store.root / previous)
                        restored = await archives(store, previous)
                        err.print(shown(f"series {o['series']} goes on after run {previous}"))
                    for i in range(start, start + o["runs"]):
                        # A stop reaches only the runs made before it.
                        if stops:
                            break
                        made = make(
                            i,
                            Series(name=o["series"], index=i, previous=previous),
                            inherited,
                            restored,
                        )
                        runners.append(made)
                        if link is not None:
                            link.track(made)
                        found = await execute([made])
                        results.extend(found)
                        if not found or found[0].status != "completed" or found[0].run_id is None:
                            break
                        previous = found[0].run_id
                        inherited = truth.inherit(store.root / previous)
                        restored = await archives(store, previous)
                else:
                    runners.extend(make(i) for i in range(o["samples"]))
                    if link is not None:
                        for r in runners:
                            link.track(r)
                    results = await execute(runners)
            except asyncio.CancelledError:
                err.print("[red]killed the sandboxes; the runs are sealed as cancelled[/]")
                return EXIT_CANCELLED
            finally:
                closing = True
                if prompt is not None:
                    prompt.live = None

                async def drain() -> None:
                    if link is not None and not await link.drain(60):
                        err.print(
                            "[yellow]some runs did not reach the control plane;"
                            " `aims push` them later[/]"
                        )

                # A step that fails or is cancelled does not skip the ones after it: the
                # link's client would stay open.
                await in_turn(
                    None if prompt is None else prompt.close,
                    None if view is None else view.stop,
                    None if lines is None else lines.stop,
                    drain,
                    None if link is None else link.close,
                )
        digests = {r.run_id: await digest(store, r.run_id) for r in results if r.run_id}
    # The runs are sealed and shipped before the print. The text is built in full first, so a
    # print that fails to build writes nothing.
    printed = True
    try:
        if o["json_out"] or o["quiet"]:
            out = []
            for r in results:
                found = digests.get(r.run_id) if r.run_id else None
                if o["json_out"]:
                    line = {"type": "result", **result_json(r)}
                    line["monitor"] = None if found is None else found.json()
                    out.append(json_text(line) + "\n")
                else:
                    count = 0 if found is None else len(found.flags)
                    flags = f" flags {count}" if count else ""
                    text = f"{r.run_id} {r.status} ${r.cost_usd:.4f}{flags} {r.reason or ''}"
                    out.append(one_line(text) + "\n")
            stdout("".join(out), nl=False)
        else:
            Console().print(summary(results, list(digests.values())))
    except BrokenPipeError:
        # A closed pipe, as from `| head`, keeps the runs' own exit code
        stdout.close()
    except Exception:
        logging.getLogger("aims").exception("the results could not be printed")
        printed = False
    statuses = {r.status for r in results}
    if stops or "cancelled" in statuses:
        return EXIT_CANCELLED
    if "killed" in statuses:
        return EXIT_KILLED
    if "failed" in statuses or not printed or (lines is not None and lines.failed):
        return EXIT_FAILED
    return 0


def _hostname() -> str:
    import re
    import socket

    return re.sub(r"[^A-Za-z0-9._@-]", "-", socket.gethostname()) or "runner"


def _token_file(path: Path) -> str:
    """The token in `path`, or exit 1 with why it holds none."""
    try:
        token = path.read_bytes().decode(errors="replace").strip()
    except OSError as e:
        problem = f"cannot read the token file {path}: {e.strerror or e}"
    else:
        if token and token.isascii() and token.isprintable():
            return token
        problem = f"the token file {path} " + (
            "holds characters a token cannot have" if token else "is empty"
        )
    _echo(f"error: {problem}", err=True)
    raise typer.Exit(EXIT_FAILED)


def _given_token(token_file: Path | None) -> str | None:
    """The token in `token_file`, else in AIMS_TOKEN; None when neither gives one. Exit 1 when
    AIMS_TOKEN holds characters a token cannot have."""
    import os

    if token_file is not None:
        return _token_file(token_file)
    token = os.environ.get("AIMS_TOKEN")
    if token and not (token.isascii() and token.isprintable()):
        _echo("error: AIMS_TOKEN holds characters a token cannot have", err=True)
        raise typer.Exit(EXIT_FAILED)
    return token or None


def _local_ui(info: Path) -> dict[str, Any]:
    """What a local `aims ui` wrote to its server.json `info`, or exit 1 when it cannot be read."""
    import json

    try:
        data = json.loads(info.read_text(encoding="utf-8"))
    except OSError as e:
        problem = e.strerror or str(e)
    except ValueError:
        problem = "it is not JSON"
    else:
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            return data
        problem = "it holds no url"
    _echo(f"error: cannot read {info}: {problem}", err=True)
    raise typer.Exit(EXIT_FAILED)


def _connection(
    server: str | None, token_file: Path | None, *, required: bool
) -> tuple[str, str] | None:
    """Where to ship and the runner token: --server with AIMS_TOKEN or --token-file, else the
    server.json a local `aims ui` wrote."""
    from aims.server.serve import SERVER_JSON, server_dir

    if token_file is not None and server is None:
        raise typer.BadParameter("needs --server", param_hint="--token-file")
    token = _given_token(token_file)
    if server is not None:
        if not token:
            typer.echo("error: give the runner token in AIMS_TOKEN or --token-file", err=True)
            raise typer.Exit(EXIT_FAILED)
        return server, token
    info = server_dir() / SERVER_JSON
    if info.is_file():
        data = _local_ui(info)
        return str(data["url"]), str(data["token"])
    if required:
        typer.echo(
            f"error: no control plane: no {info}; start `aims ui`, give --server, or set"
            " AIMS_SERVER_DIR to the directory `aims ui --data-dir` uses",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    return None


def _link(server: str | None, token_file: Path | None, no_server: bool) -> tuple[str, str] | None:
    """The control plane to ship to: none with --no-server, else as `_connection` finds it."""
    if no_server and (server is not None or token_file is not None):
        raise typer.BadParameter("takes no --server or --token-file", param_hint="--no-server")
    return None if no_server else _connection(server, token_file, required=server is not None)


ServerOption = Annotated[
    str | None, typer.Option("--server", help="Control plane URL; AIMS_TOKEN holds the token.")
]
TokenFileOption = Annotated[
    Path | None,
    typer.Option("--token-file", help="A file holding the runner token; needs --server."),
]
OperatorTokenFileOption = Annotated[
    Path | None, typer.Option("--token-file", help="A file holding an operator token.")
]
AUDITS = ("auto", "kernel", "snapshot")
RunnerAuditOption = Annotated[
    str,
    typer.Option(
        help="auto (the kernel tracer if it starts), kernel (it must start) or snapshot"
        " (never start the kernel tracer, which has root-level access to the host).",
    ),
]


def _audit(value: str) -> str:
    if value not in AUDITS:
        raise typer.BadParameter(f"use {', '.join(AUDITS)}", param_hint="--audit")
    return value


def _given(ctx: typer.Context, name: str) -> bool:
    """Whether the command line set the option `name`, even to its default value."""
    source = ctx.get_parameter_source(name)
    return source is not None and source.name != "DEFAULT"


@app.command()
def ui(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535, help="Port; 0 picks one.")] = 8484,
    config: ConfigOption = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            help="Where the control plane keeps its state. The local runner then keeps its"
            " runs in the `runner` folder inside it.",
        ),
    ] = None,
    runner: Annotated[bool, typer.Option(help="Start a local runner with it.")] = True,
    slots: Annotated[int, typer.Option(min=1, help="Runs the local runner takes at once.")] = 2,
    audit: Annotated[
        str, typer.Option(help="The local runner's --audit: auto, kernel or snapshot.")
    ] = "auto",
    tls_cert: Annotated[
        Path | None,
        typer.Option("--tls-cert", exists=True, dir_okay=False, help="TLS certificate."),
    ] = None,
    tls_key: Annotated[
        Path | None,
        typer.Option("--tls-key", exists=True, dir_okay=False, help="TLS private key."),
    ] = None,
    behind_proxy: Annotated[
        list[str] | None,
        typer.Option("--behind-proxy", help="Address of a proxy whose forwarded headers count."),
    ] = None,
    allow_host: Annotated[
        list[str] | None, typer.Option("--allow-host", help="Another Host name to answer.")
    ] = None,
    public_url: Annotated[
        str | None,
        typer.Option(
            "--public-url",
            help="The address people open, behind a proxy or in a container; a bare host means"
            " https. Sign-in links use it and its host is answered.",
        ),
    ] = None,
    no_auth: Annotated[
        bool,
        typer.Option("--no-auth", help="No token; loopback only. Anyone on this machine can act."),
    ] = False,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI.")] = True,
    keep_days: Annotated[
        float | None,
        typer.Option(
            min=0,
            callback=_days,
            help="Delete runs that ended this many days ago, with their monitor runs and scores,"
            " at start and once a day; a run in use stays. The local runner gets it too.",
        ),
    ] = None,
    key: KeyOption = None,
) -> None:
    """Serve the control plane and the UI, with a local runner."""
    import asyncio
    import logging
    import signal

    from aims.config import ConfigError, project_file
    from aims.server.serve import ServeError, UiOptions, serve, server_dir
    from aims.terminal import VisibleLog

    audit = _audit(audit)
    for name in ("slots", "audit"):
        if not runner and _given(ctx, name):
            raise typer.BadParameter(
                "needs the local runner, which --no-runner leaves out", param_hint=f"--{name}"
            )
    handler = logging.StreamHandler()
    handler.setFormatter(VisibleLog("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    if config is not None and not config.is_file():
        _echo(f"error: {config} does not exist", err=True)
        raise typer.Exit(EXIT_FAILED)
    try:
        project = config if config is not None else project_file()
    except ConfigError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    options = UiOptions(
        host=host,
        port=port,
        data=data_dir or server_dir(),
        project=project,
        runner=runner,
        runner_data=None if data_dir is None else data_dir / "runner",
        slots=slots,
        audit=audit,
        tls_cert=tls_cert,
        tls_key=tls_key,
        behind_proxy=behind_proxy or [],
        hosts=allow_host or [],
        public_url=public_url,
        auth=not no_auth,
        open_browser=open_browser,
        keep_days=keep_days,
    )
    try:
        with asyncio.Runner() as aio:
            try:
                code = aio.run(serve(options))
            finally:
                # The stop is over and the exit code says how it went. serve() has taken its
                # handlers out, so a late Ctrl-C, SIGTERM or hangup would end the process with
                # a signal as asyncio closes and Python exits.
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(sig, signal.SIG_IGN)
    except ServeError as e:
        _echo(f"error: {e}", err=True)
        if e.hint:
            _echo(e.hint, err=True)
        raise typer.Exit(EXIT_FAILED) from None
    raise typer.Exit(code)


@app.command("open")
def open_cmd(
    server: ServerOption = None,
    token_file: OperatorTokenFileOption = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the link, not only print it.")
    ] = True,
) -> None:
    """Print a new sign-in link to the control plane and open it in a browser.

    The link works once, within 2 minutes. It signs in as the local `aims ui`'s
    operator, or with --server as the token in AIMS_TOKEN or --token-file.
    """
    import asyncio

    from aims.server.serve import SERVER_JSON, server_dir

    url, token = _operator(server, token_file)
    shown = url
    if server is None:
        shown = str(_local_ui(server_dir() / SERVER_JSON).get("public", url))
    raise typer.Exit(asyncio.run(_open(url, token, open_browser, shown)))


async def _open(url: str, token: str, open_browser: bool, shown: str) -> int:
    import asyncio
    import contextlib

    import httpx2

    from aims import __version__
    from aims.net import Outbound
    from aims.server.serve import open_link, server_dir

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=30, transport=Outbound()
    ) as c:
        try:
            r = await c.post("/api/session/code")
        except httpx2.TransportError as e:
            _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    if r.status_code >= 400:
        return _problem(r)
    code = r.json()["code"]
    link = shown.rstrip("/") if code is None else f"{shown.rstrip('/')}/#code={code}"
    _echo(link)
    if code is None:
        typer.echo("the control plane runs without auth: the link needs no code", err=True)
    else:
        typer.echo(f"the link signs in once, within {r.json()['ttl_s']:g} s", err=True)
    if open_browser:
        folder = server_dir()
        with contextlib.suppress(OSError):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            await asyncio.to_thread(open_link, folder, link)
    return 0


@app.command("mcp")
def mcp_cmd(
    act: Annotated[
        bool,
        typer.Option(
            "--act",
            help="Also offer the tools that act: run, fork, monitor, score and control. They"
            " need an operator token.",
        ),
    ] = False,
    server: ServerOption = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            help="A file holding the token. Any token reads; --act needs an operator token.",
        ),
    ] = None,
) -> None:
    """Serve the control plane to an MCP client over stdio.

    The tools are named after these commands. They read runs, flags, steps, events, reports,
    scores and scenarios. A viewer token gets no plants and no scores. With --act, a tool that
    spends asks the person through the client to confirm a cost above confirm_above_usd.

    It finds the control plane as `aims open` does: the local `aims ui` with its operator token,
    or --server with the token in AIMS_TOKEN or --token-file. It needs the mcp extra:
    `uv tool install 'aims-bench\\[mcp]'`.
    """
    import asyncio

    try:
        from aims.mcp.server import serve
    except ModuleNotFoundError as e:
        if (e.name or "").partition(".")[0] not in ("mcp", "mcp_types"):
            raise
        _echo("error: aims mcp needs the mcp extra: uv tool install 'aims-bench[mcp]'", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    url, token = _operator(server, token_file, reads=not act)
    asyncio.run(serve(url, token, act=act))


@app.command("runner")
def runner_cmd(
    server: ServerOption = None,
    token_file: TokenFileOption = None,
    name: Annotated[str | None, typer.Option(help="Runner name; the host name by default.")] = None,
    slots: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 2,
    audit: RunnerAuditOption = "auto",
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", file_okay=False, help="Where this runner keeps its runs."),
    ] = None,
    keep_days: Annotated[
        float | None,
        typer.Option(
            min=0,
            callback=_days,
            help="Delete runs sealed this many days ago once the control plane has them."
            " Copies of other runners' runs go once unused this long, 7 days at most.",
        ),
    ] = None,
    tls_ca: Annotated[
        Path | None,
        typer.Option("--tls-ca", exists=True, help="CA or certificate to trust for the server."),
    ] = None,
    config: ConfigOption = None,
    key: KeyOption = None,
) -> None:
    """Take jobs from a control plane and run them on this Docker host."""
    import asyncio
    import contextlib
    import logging
    import os
    import signal
    import stat
    from typing import cast

    from aims.config import ConfigError, image_keep_days, layers
    from aims.net import Outbound
    from aims.node.service import NoBackend, Node, NodeOptions, Refused
    from aims.node.service import data_dir as default_dir
    from aims.sandbox import AuditMode
    from aims.terminal import VisibleLog, on_hangup, on_stop

    handler = logging.StreamHandler()
    handler.setFormatter(VisibleLog("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    mode = cast(AuditMode, _audit(audit))
    try:
        image_days = image_keep_days(layers(config))
    except ConfigError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if tls_ca is not None:
        try:
            Outbound(str(tls_ca))
        except OSError as e:
            _echo(f"error: cannot load --tls-ca {tls_ca}: {e}", err=True)
            raise typer.Exit(EXIT_FAILED) from None
    found = _connection(server, token_file, required=True)
    assert found is not None
    url, token = found
    # Set by `aims ui` on the runner it starts (UI_RUNNER in aims.server.serve). A Ctrl-C in
    # the terminal reaches both processes. `aims ui` passes the first on as SIGTERM and a later
    # one on stdin, so the runner would count one Ctrl-C twice and kill now. It ignores SIGINT
    # and leaves the count to `aims ui`.
    by_ui = os.environ.get("AIMS_UI_RUNNER") == "1"
    if by_ui:
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    async def main() -> None:
        node = Node(
            NodeOptions(
                url=url,
                token=token,
                name=name or _hostname(),
                data=data_dir or default_dir(),
                slots=slots,
                keep_days=keep_days,
                image_keep_days=image_days,
                verify=True if tls_ca is None else str(tls_ca),
                audit=mode,
            )
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        stops = 0
        # `aims ui` holds the other end of this pipe. While it does, `aims ui` alone says when
        # to kill now, and says it there: one stop sent to the whole job, as systemd and
        # `kill -- -<pgid>` send it, reaches this runner both on its own and through `aims ui`.
        ui_lives = by_ui and stat.S_ISFIFO(os.fstat(0).st_mode)

        def say(text: str) -> None:
            # a terminal that hung up fails the write; the stop goes on without it
            with contextlib.suppress(OSError):
                typer.echo(text, err=True)

        def on_signal(*, kill: bool = False) -> None:
            nonlocal stops
            # Once the runner closes, or a stop killed it, a stop does nothing. What is left is
            # bounded, and a cut store close can leave the store open, which holds the process
            # at exit.
            if node.closing or (current is not None and current.cancelling()):
                return
            if stops and ui_lives and not kill:
                return
            stops += 1
            if stops == 1 and not kill:
                stop.set()
                say("stopping: runs are sealed as cancelled; again to kill now")
            elif current is not None:
                current.cancel()
                say("killing now")

        # `aims ui` says what a third Ctrl-C does for the runner it started, and on its own
        # third it kills the runner. So this runner never quits on a count of SIGTERMs: one
        # stop sent to the whole job reaches it again through `aims ui`, twice under `uv run`,
        # which passes SIGTERM on too, and a quit then would skip removing its helpers.
        on_stop(
            loop,
            on_signal,
            signals=(signal.SIGTERM,) if by_ui else (signal.SIGINT, signal.SIGTERM),
            say=None if by_ui else say,
            quits=not by_ui,
        )
        # `aims ui` passes a hangup on as one SIGTERM, as it does Ctrl-C. On its own the runner
        # lets a hangup start a stop but never add to one: nobody is left to press Ctrl-C.
        on_hangup(loop, (lambda: None) if by_ui else (lambda: None if stops else on_signal()))
        if by_ui:
            # `aims ui` starts this runner with these signals blocked, so one sent during the
            # start-up waited for the handlers above. A stop sent that early ends the runner
            # before it starts anything.
            if signal.SIGTERM in signal.sigpending():
                return
            signal.pthread_sigmask(
                signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            )

        def on_input() -> None:
            nonlocal stops, ui_lives
            try:
                data = os.read(0, 4096)
            except OSError:
                data = b""
            if data:
                # KILL_NOW in aims.server.serve: a later stop of `aims ui`
                if b"k" in data:
                    on_signal(kill=True)
                return
            loop.remove_reader(0)
            # From here a signal adds to a stop again: nobody else is left to say kill now
            ui_lives = False
            # A stop under way, from a signal or from a control plane that refused the runner,
            # was not for this, so the line would give the wrong reason
            if not stop.is_set():
                stops = 1
                stop.set()
                say("aims ui ended: stopping, runs are sealed as cancelled")

        # `aims ui` holds the other end of this pipe. It ends when `aims ui` ends, even by
        # SIGKILL, and with no control plane left this runner stops once, as for its SIGTERM.
        if ui_lives:
            loop.add_reader(0, on_input)
        await node.run(stop)

    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        raise typer.Exit(EXIT_CANCELLED) from None
    except (Refused, NoBackend) as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    finally:
        if by_ui:
            # The stop is over and the exit code says how it went. A SIGTERM from a late
            # second Ctrl-C, or a late hangup, would only replace that code.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)


@app.command()
def push(
    path: Annotated[Path, typer.Argument(help="A run directory, or a directory of runs.")],
    server: ServerOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Ship runs recorded without a control plane to one."""
    import asyncio

    import httpx2

    from aims.node.client import ApiError

    found = _connection(server, token_file, required=True)
    assert found is not None
    url, token = found
    if (path / "events.jsonl").is_file():
        root, only = path.parent, [path.name]
    elif path.is_dir():
        root, only = path, None
    else:
        _echo(f"error: {path} is not a run directory", err=True)
        raise typer.Exit(EXIT_FAILED)
    try:
        code = asyncio.run(_push(root, only, url, token))
    except ApiError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    except httpx2.TransportError as e:
        _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    raise typer.Exit(code)


def _push_hello(name: str) -> Any:
    """The hello of a client that only ships runs: it takes no job."""
    from aims.node.link import arch
    from aims.wire import RunnerHello, TracerHealth

    return RunnerHello(
        name=name,
        version=__version__,
        arch=arch(),
        tracer=TracerHealth(alive=False, detail="push only"),
        slots=0,
    )


async def _push(
    root: Path, only: list[str] | None, url: str, token: str, *, quiet: bool = False
) -> int:
    from aims.events import LocalEventStore
    from aims.node.client import ControlClient
    from aims.node.shipper import Shipper

    async with await LocalEventStore.open(root) as store:
        client = ControlClient(url, token, runner=f"push@{_hostname()}")
        try:
            await client.hello(_push_hello(client.runner))
            failed = 0
            for run_id in only or store.run_ids():
                info = await store.run(run_id)
                if info is None or info.status == "running":
                    _echo(f"skip {run_id}: not sealed", err=True)
                    continue
                shipper = Shipper(store, client, run_id, seen=True)
                await shipper.run()
                if shipper.diverged or shipper.refused:
                    failed += 1
                    _echo(f"error: {run_id}: {shipper.diverged or shipper.refused}", err=True)
                elif not quiet:
                    _echo(f"{run_id} {info.status} {info.events} events")
            return EXIT_FAILED if failed else 0
        finally:
            await client.aclose()


@app.command("monitor")
def monitor_cmd(
    runs: Annotated[list[str], typer.Argument(help="Ids of sealed runs to monitor again.")],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="An aims.toml to use instead of the project's; its \\[monitor] table also goes"
            " over each run's scenario.",
        ),
    ] = None,
    runs_dir: RunsDirOption = Path("runs"),
    profile: Annotated[
        str | None, typer.Option(help="Profile for every sweep, the review and the judge.")
    ] = None,
    monitor: Annotated[
        str | None,
        typer.Option(
            "--monitor",
            help="Monitor preset: signals (needs no key), standard or research. Default: the"
            " config files and the run's scenario, else standard.",
        ),
    ] = None,
    signals_only: Annotated[
        bool, typer.Option("--signals-only", help="Signals alone; no model, no key.")
    ] = False,
    judge_only: Annotated[
        bool, typer.Option("--judge-only", help="The judge alone; no signal, no sweep.")
    ] = False,
    samples: Annotated[
        int, typer.Option(min=1, help="Monitor runs per run; their model layers differ.")
    ] = 1,
    parallel: Annotated[int, typer.Option(min=1, help="Monitor runs at once.")] = 1,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            "--budget-usd",
            min=0,
            callback=_finite,
            help="Spend cap of each monitor run, in USD, instead of the scenario's"
            " limits.monitor_cost_usd or the preset's.",
        ),
    ] = None,
    quiet: Annotated[bool, typer.Option(help="One line per monitor run.")] = False,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print JSON lines: estimates, flags, actions, sweeps, verdicts, results.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Start even when the cost estimate is above the limit.")
    ] = False,
    server: ServerOption = None,
    token_file: TokenFileOption = None,
    no_server: Annotated[
        bool, typer.Option("--no-server", help="Do not ship to a local `aims ui`.")
    ] = False,
    key: KeyOption = None,
) -> None:
    """Feed sealed runs through the monitor again, each into a new run of kind monitor.

    The origin never changes: evidence points into it and every action is simulated. Layers,
    lowest first: the config files, the run's scenario, the --config file's \\[monitor] table,
    then --monitor or --signals-only.
    """
    import asyncio

    _preset(monitor, ("signals", "standard", "research"))
    if signals_only and judge_only:
        raise typer.BadParameter("pick one", param_hint="--signals-only/--judge-only")
    if signals_only and monitor not in (None, "signals"):
        raise typer.BadParameter("--signals-only sets the signals preset", param_hint="--monitor")
    for given, hint in ((profile, "--profile"), (budget_usd, "--budget-usd")):
        if signals_only and given is not None:
            raise typer.BadParameter("--signals-only calls no model", param_hint=hint)
    link = _link(server, token_file, no_server)
    code = asyncio.run(
        _monitor_runs(
            runs=runs,
            config=config,
            runs_dir=runs_dir,
            profile=profile,
            preset=monitor,
            signals_only=signals_only,
            judge_only=judge_only,
            samples=samples,
            parallel=parallel,
            budget_usd=budget_usd,
            quiet=quiet,
            json_out=json_out,
            yes=yes,
            link=link,
        )
    )
    raise typer.Exit(code)


async def _monitor_runs(**o: Any) -> int:
    import asyncio
    import logging
    import sys
    import tomllib
    import uuid

    import httpx2
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.table import Table

    from aims.config import ConfigError
    from aims.config import load as load_settings
    from aims.events import LocalEventStore
    from aims.models import ProfileError
    from aims.models.listprice import PriceLists
    from aims.monitor.config import MonitorConfigError
    from aims.monitor.offline import (
        OfflineResult,
        monitor_offline,
        monitored,
        offline_monitor,
        origin_agents,
    )
    from aims.node.client import ApiError
    from aims.prompts import TextError
    from aims.run.estimate import (
        ask_why,
        estimate_review,
        priced_review,
        reviews_confirm,
        warning_lines,
    )
    from aims.run.job import JobOptions, monitor_profiles
    from aims.run.runner import RunError
    from aims.run.view import JsonLines, digest, monitor_table
    from aims.terminal import (
        Stdout,
        VisibleLog,
        json_text,
        on_hangup,
        on_stop,
        one_line,
        shown,
    )

    err = Console(stderr=True)
    stdout = Stdout(typer.echo)
    handler = RichHandler(console=err, show_path=False)
    handler.setFormatter(VisibleLog())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)

    def fail(message: str) -> int:
        err.print("[red]error:[/]", shown(message))
        return EXIT_FAILED

    try:
        settings = load_settings(o["config"])
        extra = None
        if o["config"] is not None:
            extra = tomllib.loads(o["config"].read_text(encoding="utf-8")).get("monitor")
    except (ConfigError, ProfileError, TextError, tomllib.TOMLDecodeError) as e:
        return fail(str(e))
    mode = "signals_only" if o["signals_only"] else "judge_only" if o["judge_only"] else "full"
    results: list[OfflineResult] = []

    def say(text: str) -> None:
        err.print(shown(text, "yellow"))

    async with await LocalEventStore.open(o["runs_dir"]) as store:
        await _ship_sealed(store, await _seal_left(store, say), o["link"], say)
        planned = []
        costs = []
        lists = PriceLists()
        for run_id in dict.fromkeys(o["runs"]):
            try:
                origin, _ = await monitored(store, run_id)

                def monitor_of(models: Any, samples: int, origin: Any = origin) -> Any:
                    return offline_monitor(
                        models,
                        settings.prompts,
                        origin.bundle.scenario,
                        origin_agents(origin),
                        layers=settings.monitor,
                        config=extra,
                        preset=o["preset"],
                        signals_only=o["signals_only"],
                        judge_only=o["judge_only"],
                        profile=o["profile"],
                        samples=samples,
                        budget_usd=o["budget_usd"],
                        steps=sum(origin.steps.values()),
                    )

                # the list prices go on the profiles before the monitor resolves for good, so
                # its budget and its run count them; one sample is enough to find the profiles
                laid, listing = await priced_review(settings, monitor_of(settings.models, 1), lists)
                resolved = monitor_of(laid.models, o["samples"])
                for p in monitor_profiles(resolved).values():
                    p.api_key(p.url())
            except (RunError, MonitorConfigError, ProfileError, TextError) as e:
                return fail(f"{run_id}: {e}")
            costs.append(
                await estimate_review(
                    store,
                    origin.run_id,
                    resolved,
                    mode,
                    samples=o["samples"],
                    confirm_above_usd=settings.confirm_above_usd,
                    listing=listing,
                )
            )
            group = uuid.uuid4().hex[:12] if o["samples"] > 1 else None
            for i in range(o["samples"]):
                options = JobOptions(
                    profile=o["profile"],
                    group=group,
                    sample=i if group else None,
                    samples=o["samples"],
                )
                planned.append((origin, resolved, options, str(uuid.uuid7())))
        for cost in costs:
            if o["json_out"]:
                stdout(json_text({"type": "estimate", **cost.model_dump(mode="json")}))
            else:
                for text in warning_lines(cost) if o["quiet"] else cost.lines:
                    err.print(shown(text))
        refused = ask_why(
            None if o["yes"] else reviews_confirm(costs),
            tty=sys.stdin.isatty() and sys.stderr.isatty(),
            ask=lambda q: typer.confirm(q, default=False, err=True),
        )
        if refused is not None:
            return fail(refused)
        lines = JsonLines(store, stdout) if o["json_out"] else None
        gate = asyncio.Semaphore(o["parallel"])

        def started(run_id: str) -> None:
            if lines is not None:
                lines.add(run_id)
            elif not o["quiet"]:
                err.print(f"monitoring into {run_id}")

        async def one(origin: Any, resolved: Any, options: Any, run_id: str) -> OfflineResult:
            async with gate:
                return await monitor_offline(
                    store,
                    origin,
                    resolved,
                    models=settings.models,
                    run_id=run_id,
                    mode=mode,
                    options=options,
                    config_files=settings.layers.digests(),
                    on_start=started,
                )

        main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        closing = False
        push: asyncio.Task[int] | None = None

        def stop() -> None:
            # The first signal cancels and the monitor runs seal at once; later ones do nothing.
            # One while the runs close does nothing either, as that is bounded. One during the
            # push ends the push.
            if push is not None:
                if not push.cancelling():
                    push.cancel()
            elif not closing and main is not None and not main.cancelling():
                main.cancel()

        # The handlers stay in until asyncio.run closes the loop. Without them a Ctrl-C raises
        # KeyboardInterrupt wherever the cleanup is, which can leave the store open.
        on_stop(loop, stop)
        on_hangup(loop, stop)
        try:
            # Each monitor run ends, its seal too, before this goes on. Without
            # return_exceptions the first one to end cancelled or failed would raise here at
            # once, and the store would close under the others.
            ended = await asyncio.gather(*(one(*p) for p in planned), return_exceptions=True)
        except asyncio.CancelledError:
            for line in await _stop_lines(store, "monitor run", [p[3] for p in planned]):
                err.print(shown(line, "red"))
            return EXIT_CANCELLED
        finally:
            closing = True
            if lines is not None:
                await lines.stop()
        for found in ended:
            if isinstance(found, RunError):
                return fail(str(found))
            if isinstance(found, BaseException):
                raise found
            results.append(found)
        digests = [await digest(store, r.run_id) for r in results]
    if o["link"] is not None and results:
        url, token = o["link"]
        run_ids = [r.run_id for r in results]
        push = asyncio.create_task(_push(o["runs_dir"], run_ids, url, token, quiet=True))
        await asyncio.wait([push])
        try:
            failed = push.result()
        except asyncio.CancelledError:
            err.print("[yellow]stopped; some monitor runs did not reach the control plane[/]")
        except (ApiError, httpx2.TransportError, OSError) as e:
            err.print(shown(f"the control plane did not take the monitor runs: {e}", "yellow"))
        else:
            if failed:
                err.print("[yellow]some monitor runs did not reach the control plane[/]")
    # The runs ship first, so a failed print cannot stop that. The text is built in full first,
    # so a print that fails to build writes nothing.
    printed = True
    try:
        if o["json_out"] or o["quiet"]:
            out = []
            for r, d in zip(results, digests, strict=True):
                if o["json_out"]:
                    line = {
                        "type": "result",
                        "run_id": r.run_id,
                        "origin": r.origin,
                        "status": r.status,
                        "reason": r.reason,
                        "cost_usd": round(r.cost_usd, 10),
                        "monitor": d.json(),
                    }
                    out.append(json_text(line) + "\n")
                else:
                    text = f"{r.run_id} {r.origin} {r.status} flags {len(d.flags)}"
                    out.append(one_line(f"{text} ${r.cost_usd:.4f} {r.reason or ''}") + "\n")
            stdout("".join(out), nl=False)
        else:
            table = Table(title="monitor runs")
            for column in ("run", "origin", "status", "flags", "sweeps", "verdict", "cost"):
                table.add_column(column, overflow="fold")
            for r, d in zip(results, digests, strict=True):
                verdict = (
                    "" if d.verdict is None else f"severity {d.verdict.data.get('max_severity')}"
                )
                cells = (
                    r.run_id,
                    r.origin,
                    r.status if r.reason is None else f"{r.status}: {r.reason}",
                    str(len(d.flags)),
                    str(d.sweeps),
                    verdict,
                    f"${r.cost_usd:.4f}",
                )
                table.add_row(*(shown(cell) for cell in cells))
            console = Console()
            console.print(table)
            found = monitor_table(digests)
            if found is not None:
                console.print(found)
    except BrokenPipeError:
        # A closed pipe, as from `| head`, keeps the runs' own exit code
        stdout.close()
    except Exception:
        logging.getLogger("aims").exception("the results could not be printed")
        printed = False
    stopped = push is not None and push.cancelled()
    if stopped or any(r.status == "cancelled" for r in results):
        return EXIT_CANCELLED
    lost = lines is not None and lines.failed
    if any(r.status != "completed" for r in results) or not printed or lost:
        return EXIT_FAILED
    return 0


CONTROL_ACTIONS = ("pause", "resume", "deny", "inject", "kill", "cancel", "sweep")
# The exit codes of `aims control` besides 0 and EXIT_FAILED; its help lists them all
EXIT_DROPPED, EXIT_PART_DONE, EXIT_NO_ANSWER = 4, 5, 6


@app.command()
def control(
    ctx: typer.Context,
    run: Annotated[str, typer.Argument(help="The run's id.")],
    action: Annotated[
        str,
        typer.Argument(help="pause, resume, deny, inject, kill, cancel, sweep, or list."),
    ],
    agent: Annotated[str | None, typer.Option(help="One agent; every agent by default.")] = None,
    text: Annotated[
        str | None,
        typer.Option(help="The message to inject; with resume, given as the agents go on."),
    ] = None,
    reason: Annotated[str | None, typer.Option(help="Why, for the log.")] = None,
    wait: Annotated[
        float,
        typer.Option(
            min=0,
            callback=_finite,
            help="Seconds to wait for the runner to answer; 0 sends and does not wait.",
        ),
    ] = 10.0,
    server: ServerOption = None,
    token_file: OperatorTokenFileOption = None,
) -> None:
    """Pause, resume, deny, inject, kill, cancel or sweep a run through its control plane, or
    list the commands sent to it and the runner's answers.

    Needs an operator token in AIMS_TOKEN or --token-file; list reads with any token. Deny
    refuses the tool call a paused agent waits before; the agent gets the configured `denied`
    reply. Kill skips the verifier; cancel also seals the run as cancelled, or drops it while
    it waits in the queue. Both stop the monitor's model calls and skip the judge; sent while
    the judge runs, either only cuts it short and the run keeps its outcome.

    \b
    Exit codes:
    0 done, or sent with --wait 0
    1 refused by the runner or the control plane, or it could not be reached
    4 dropped: the run ended or its runner changed first, so it was not done;
      a drop as the run ended may wait out --wait: the runner may yet say done
    5 done in part: the line says what did not take effect
    6 no answer within --wait; the runner may still carry it out
    """
    import asyncio

    if action == "list":
        for given, hint in (
            (agent is not None, "--agent"),
            (text is not None, "--text"),
            (reason is not None, "--reason"),
            (_given(ctx, "wait"), "--wait"),
        ):
            if given:
                raise typer.BadParameter(
                    "list takes no options but --server and --token-file", param_hint=hint
                )
        url, token = _operator(server, token_file, reads=True)
        raise typer.Exit(asyncio.run(_commands(url, token, run)))
    if action not in CONTROL_ACTIONS:
        raise typer.BadParameter(f"use {', '.join(CONTROL_ACTIONS)} or list", param_hint="ACTION")
    if action == "inject" and not text:
        raise typer.BadParameter("an inject needs text", param_hint="--text")
    if text is not None and action not in ("inject", "resume"):
        raise typer.BadParameter(f"{action} takes no text", param_hint="--text")
    if agent is not None and action in ("kill", "cancel"):
        raise typer.BadParameter(f"a {action} stops the whole run", param_hint="--agent")
    url, token = _operator(server, token_file)
    body: dict[str, Any] = {}
    match action:
        case "pause" | "sweep":
            path, body = action, {"agent": agent, "reason": reason}
        case "resume" | "deny":
            then = "deny" if action == "deny" else "inject" if text else "continue"
            path = "resume"
            body = {"agent": agent, "then": then, "text": text, "reason": reason}
        case "inject":
            path, body = "inject", {"agent": agent, "text": text}
        case _:
            path, body = action, {"reason": reason}
    raise typer.Exit(asyncio.run(_control(url, token, run, path, body, action, wait)))


async def _control(
    url: str, token: str, run: str, path: str, body: dict[str, Any], action: str, wait: float
) -> int:
    import asyncio
    import time

    import httpx2

    from aims import __version__
    from aims.net import Outbound

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=30, transport=Outbound()
    ) as c:
        try:
            r = await c.post(f"/api/runs/{run}/{path}", json=body)
            if r.status_code >= 400:
                return _problem(r)
            sent = r.json()
            if sent.get("state") == "cancelled":
                _echo(f"{run}: cancelled before it started")
                return 0
            if wait == 0:
                _echo(f"{action} {run}: sent; `aims control {run} list` shows it")
                return 0
            command = sent.get("command_id")
            deadline = time.monotonic() + wait
            found = None
            unread: str | None = None
            while time.monotonic() < deadline:
                r = await c.get(f"/api/runs/{run}/commands")
                # A 503 asks to try again soon: the index can lag the run's log for a moment,
                # as while the run's first events arrive.
                if r.status_code == 503:
                    unread = _problem_text(r)
                    await asyncio.sleep(0.2)
                    continue
                if r.status_code >= 400:
                    return _problem(r)
                unread = None
                found = next((x for x in r.json() if x["id"] == command), None)
                # A drop by the run's end is not final: the runner may have carried the
                # command out, and its answer may still come and say done.
                if found is not None and found["final"]:
                    return _answered(action, run, found)
                await asyncio.sleep(0.2)
        except httpx2.TransportError as e:
            _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    if found is not None and found["state"] == "dropped":
        return _answered(action, run, found)
    why = (
        "the runner did not answer"
        if unread is None
        else f"the control plane could not show its answer ({unread})"
    )
    _echo(
        f"error: {action} {run}: sent, but {why} within {wait:g} s;"
        f" `aims control {run} list` shows it later",
        err=True,
    )
    return EXIT_NO_ANSWER


def _answered(action: str, run: str, command: dict[str, Any]) -> int:
    """Print how a command ended; the exit code that says it. A runner that carried it out
    answers done, or says what did not take effect."""
    outcome = str(command["outcome"])
    match command["state"]:
        case "done" if not command["in_part"]:
            _echo(f"{action} {run}: done")
            return 0
        case "done":
            _echo(f"{action} {run}: done in part: {outcome}")
            return EXIT_PART_DONE
        case "dropped":
            _echo(f"{action} {run}: {outcome}; it was not carried out")
            return EXIT_DROPPED
        case _:
            _echo(f"{action} {run}: {outcome}")
            return EXIT_FAILED


@app.command("delete")
def delete_cmd(
    run: Annotated[str, typer.Argument(help="The run's id.")],
    yes: Annotated[bool, typer.Option("--yes", help="Delete without asking.")] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Also delete runs that look abandoned: marked running on a runner that is"
            " offline.",
        ),
    ] = False,
    server: ServerOption = None,
    token_file: OperatorTokenFileOption = None,
) -> None:
    """Delete a run on its control plane with every run made from it: its monitor runs, its
    scores and the runs made from those.

    Needs an operator token in AIMS_TOKEN or --token-file. A run still running, or one that a
    queued or active job runs or reads, is refused. A run marked running whose runner is
    offline goes only with --force. A runner that still holds a deleted run never ships it
    back.
    """
    import asyncio

    url, token = _operator(server, token_file)
    raise typer.Exit(asyncio.run(_delete(url, token, run, yes, force)))


def _counted(kinds: list[str]) -> str:
    """How many monitor, score and other runs `kinds` holds, in words."""
    names = {"monitor": "monitor run", "score": "score run"}
    counts: dict[str, int] = {}
    for kind in kinds:
        name = names.get(kind, "other run")
        counts[name] = counts.get(name, 0) + 1
    parts = [
        f"{counts[n]} {n}{'' if counts[n] == 1 else 's'}"
        for n in ("monitor run", "score run", "other run")
        if n in counts
    ]
    return " and ".join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 1 else parts)


async def _delete(url: str, token: str, run: str, yes: bool, force: bool) -> int:
    import httpx2

    from aims.net import Outbound
    from aims.report.escape import visible

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=60, transport=Outbound()
    ) as c:
        try:
            r = await c.get(f"/api/runs/{run}/dependents")
            if r.status_code >= 400:
                return _problem(r)
            found = r.json()
            if found["blocked"]:
                _echo(f"error: 409 {found['blocked']}", err=True)
                return EXIT_FAILED
            if found["offline"]:
                if not force:
                    _echo(f"error: 409 {found['offline']}. Pass --force to delete it.", err=True)
                    return EXIT_FAILED
                _echo(f"It looks abandoned: {found['offline']}.", err=True)
            kinds = [d["kind"] for d in found["runs"]]
            what = f"run {run} with {_counted(kinds)}" if kinds else f"run {run}"
            if not yes and not typer.confirm(visible(f"Delete {what}?"), default=False, err=True):
                _echo(f"kept {run}", err=True)
                return EXIT_FAILED
            r = await c.delete(f"/api/runs/{run}", params={"force": "true"} if force else {})
        except httpx2.TransportError as e:
            _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    if r.status_code >= 400:
        return _problem(r)
    for deleted in r.json()["run_ids"]:
        _echo(f"deleted {deleted}")
    return 0


def _command_line(c: dict[str, Any]) -> str:
    from aims.events.schema import when
    from aims.terminal import one_line

    sent = when(c["created"]).strftime("%Y-%m-%d %H:%M:%S")
    what = " ".join(
        one_line(x)
        for x in (c["action"], c["agent"], c["then"] if c["then"] != "continue" else None)
        if x
    )
    answer = "waiting" if c["state"] == "waiting" else one_line(c["outcome"])
    took = ""
    if c["state"] in ("done", "failed"):
        s = c["acked"] - c["created"]
        took = f"  {round(s * 1000)} ms" if s < 1 else f"  {s:.1f} s"
    lines = [f"{sent}  {one_line(c['by'] or '-')}  {what}  {answer}{took}"]
    lines += [f"    {k}: {one_line(c[k])}" for k in ("text", "reason") if c[k]]
    return "\n".join(lines)


async def _commands(url: str, token: str, run: str) -> int:
    import httpx2

    from aims import __version__
    from aims.net import Outbound

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=30, transport=Outbound()
    ) as c:
        try:
            r = await c.get(f"/api/runs/{run}/commands")
        except httpx2.TransportError as e:
            _echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    if r.status_code >= 400:
        return _problem(r)
    commands = r.json()
    if not commands:
        _echo(
            f"{run}: no commands sent through the control plane; answers given at a terminal"
            " are in the run's log as operator.action"
        )
    for command in commands:
        typer.echo(_command_line(command))
    return 0


def _problem(r: Any) -> int:
    _echo(f"error: {_problem_text(r)}", err=True)
    return EXIT_FAILED


def _problem_text(r: Any) -> str:
    try:
        problem = r.json()
        message = problem.get("detail") or problem.get("title") or r.text
    except ValueError:
        message = r.text[:500]
    return f"{r.status_code} {message}"


def _operator(
    server: str | None, token_file: Path | None, *, reads: bool = False
) -> tuple[str, str]:
    """The control plane and an operator token: --server, else the local `aims ui`; the token
    from --token-file or AIMS_TOKEN, else the local ui's own operator token if it wrote one.
    With `reads`, any token does."""
    from aims.server.serve import SERVER_JSON, server_dir

    token = _given_token(token_file)
    url = server
    if url is None:
        info = server_dir() / SERVER_JSON
        if not info.is_file():
            typer.echo(
                f"error: no control plane: no {info}; start `aims ui`, give --server, or set"
                " AIMS_SERVER_DIR to the directory `aims ui --data-dir` uses",
                err=True,
            )
            raise typer.Exit(EXIT_FAILED)
        data = _local_ui(info)
        url = str(data["url"])
        token = token or data.get("operator")
    if not token:
        need, role = ("a token", "") if reads else ("an operator token", " --role operator")
        typer.echo(
            f"error: give {need} in AIMS_TOKEN or --token-file; make one with"
            f" `aims token create <name>{role}`",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    return url, token


token_app = typer.Typer(no_args_is_help=True, cls=_Group, help="Tokens of the local control plane.")
app.add_typer(token_app, name="token")
DataDirOption = Annotated[
    Path | None,
    typer.Option("--data-dir", file_okay=False, help="The control plane's state directory."),
]


async def _db(data_dir: Path | None, *, make: bool = False) -> Any:
    """The control plane's database. Only `make` creates it, for the first token of a new
    deployment; without it a missing database exits 1 and nothing is made."""
    import sqlite3

    from aims.server.db import ServerDB
    from aims.server.serve import server_dir

    path = (data_dir or server_dir()) / "server.sqlite"
    if not make and not path.exists():
        _echo(
            f"error: no control plane database: no {path}; give the --data-dir `aims ui` uses",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    try:
        return await ServerDB.open(path)
    except (OSError, sqlite3.Error, RuntimeError) as e:
        _echo(f"error: cannot open {path}: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None


def _missing(path: Path) -> list[Path]:
    """`path` and those of its parents that do not exist yet, deepest first."""
    found: list[Path] = []
    while not path.exists() and path != path.parent:
        found.append(path)
        path = path.parent
    return found


def _new_db(path: Path) -> tuple[int, int] | None:
    """Make the empty file of a new control plane database at `path`, readable by its owner
    only, with the folders it needs. Returns the file's device and inode; None when a file was
    there, which is not this command's to remove."""
    import os

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    try:
        found = os.fstat(fd)
    finally:
        os.close(fd)
    return found.st_dev, found.st_ino


def _unmake(made: list[Path], db: Path, ours: tuple[int, int] | None) -> None:
    """Remove the database file `db` while it is still the one this command made, which `ours`
    names, with its journal files; then each folder in `made`, deepest first. A database
    someone else made stays, as does a folder that holds anything."""
    try:
        found = db.stat() if ours is not None else None
    except OSError:
        found = None
    if found is not None and (found.st_dev, found.st_ino) == ours:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(OSError):
                db.with_name(f"{db.name}{suffix}").unlink(missing_ok=True)
    for path in made:
        with contextlib.suppress(OSError):
            path.rmdir()


@token_app.command("create")
def token_create(
    name: Annotated[str, typer.Argument(help="A name for the token.")],
    role: Annotated[str, typer.Option(help="viewer, operator or runner.")] = "viewer",
    data_dir: DataDirOption = None,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Write the token to this file, readable by its owner only, not to stdout. A"
            " file that holds the live token NAME is kept; otherwise NAME gets a new value.",
        ),
    ] = None,
) -> None:
    """Make a named token and print it. It is shown this once."""
    import asyncio

    from aims.server.audit import record
    from aims.server.db import TOKEN_NAME_RULE, new_token, token_name_ok
    from aims.server.serve import private_temp, server_dir

    if not token_name_ok(name):
        raise typer.BadParameter(TOKEN_NAME_RULE, param_hint="NAME")
    if role not in ("viewer", "operator", "runner"):
        raise typer.BadParameter("use viewer, operator or runner", param_hint="--role")

    def refuse(message: str) -> NoReturn:
        _echo(f"error: {message}", err=True)
        raise typer.Exit(EXIT_FAILED)

    if file is not None and file.is_dir():
        refuse(f"{file} is a directory")
    if file is not None and not file.parent.is_dir():
        refuse(f"no directory {file.parent}")
    folder = data_dir or server_dir()
    made = _missing(folder)
    wrote = False
    # the database file, when this command made it
    ours: tuple[int, int] | None = None

    async def main() -> str | None:
        nonlocal wrote, ours
        try:
            ours = await asyncio.to_thread(_new_db, folder / "server.sqlite")
        except OSError as e:
            refuse(f"cannot open {folder / 'server.sqlite'}: {e}")
        db = await _db(data_dir, make=True)
        detail = f"role {role}"
        try:
            if file is None:
                try:
                    token = await db.create_token(name, role)
                except ValueError:
                    await record(
                        db,
                        "token.create",
                        outcome="refused",
                        auth="cli",
                        target=name,
                        detail="exists",
                    )
                    raise
                wrote = True
                await record(db, "token.create", auth="cli", target=name, detail=detail)
                return token
            try:
                held = file.read_bytes().decode(errors="replace").strip() if file.is_file() else ""
            except OSError as e:
                refuse(f"cannot read {file}: {e.strerror or e}")
            found = await db.token_for(held, used=False) if held else None
            if found is not None and (found.name, found.role) == (name, role):
                return None
            # The new value is on disk before the old one stops working, so neither is lost.
            token = new_token()
            try:
                tmp = private_temp(file, token + "\n")
            except OSError as e:
                refuse(f"cannot write {file}: {e.strerror or e}")
            try:
                await db.rotate_token(name, role, token)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            wrote = True
            moved: OSError | None = None
            try:
                tmp.replace(file)
            except OSError as e:
                moved = e
            await record(db, "token.rotate", auth="cli", target=name, detail=detail)
            if moved is not None:
                refuse(
                    f"cannot move the new value of token {name} to {file}:"
                    f" {moved.strerror or moved}; it is in {tmp}"
                )
            return token
        finally:
            await db.close()

    try:
        try:
            token = asyncio.run(main())
        except ValueError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(EXIT_FAILED) from None
    except BaseException:
        # A create that wrote no token leaves no folder or database it made, as one that was
        # refused at once does. A token written stays with its database.
        if not wrote:
            _unmake(made, folder / "server.sqlite", ours)
        raise
    if file is None:
        typer.echo(token)
    elif token is None:
        _echo(f"{file} holds the live token {name}", err=True)
    else:
        _echo(f"wrote a new value of token {name} to {file}", err=True)


@token_app.command("list")
def token_list(data_dir: DataDirOption = None) -> None:
    """Show tokens by name and role, when each was made and last used, in UTC. Their values
    are never kept."""
    import asyncio

    from aims.events.schema import when
    from aims.terminal import one_line

    async def main() -> list[Any]:
        db = await _db(data_dir)
        try:
            return await db.tokens()
        finally:
            await db.close()

    def minute(at: float) -> str:
        return when(at).strftime("%Y-%m-%d %H:%M")

    for t in asyncio.run(main()):
        state = "revoked" if t.revoked else "live"
        used = "never" if t.last_used is None else f"used {minute(t.last_used)}"
        typer.echo(f"{one_line(t.name)}  {t.role}  {minute(t.created)}  {state}  {used}")


@token_app.command("revoke")
def token_revoke(
    name: Annotated[str, typer.Argument(help="The token to revoke.")],
    data_dir: DataDirOption = None,
) -> None:
    """Revoke a token and end its sessions; its runner names free up for another token."""
    import asyncio

    from aims.server.audit import record

    async def main() -> bool:
        db = await _db(data_dir)
        try:
            done = await db.revoke_token(name)
            outcome = "ok" if done else "refused"
            detail = None if done else "no live token"
            await record(
                db, "token.revoke", outcome=outcome, auth="cli", target=name, detail=detail
            )
            return done
        finally:
            await db.close()

    if not asyncio.run(main()):
        _echo(f"error: no live token {name}", err=True)
        raise typer.Exit(EXIT_FAILED)
    _echo(f"revoked {name}")


@app.command("audit")
def audit_cmd(
    action: Annotated[
        str | None,
        typer.Option(help="Only this action and the ones under it, such as run or run.kill."),
    ] = None,
    token: Annotated[str | None, typer.Option(help="Only rows of this token.")] = None,
    outcome: Annotated[str | None, typer.Option(help="ok, refused or error.")] = None,
    since: Annotated[
        str | None, typer.Option(help="From this UTC time, such as 2026-11-01 or 2026-11-01T09:30.")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Up to this UTC time.")] = None,
    limit: Annotated[
        int, typer.Option(min=0, help="Show at most this many rows; 0 shows all.")
    ] = 50,
    jsonl: Annotated[
        bool, typer.Option("--jsonl", help="Print each row as one JSON line.")
    ] = False,
    data_dir: DataDirOption = None,
) -> None:
    """Show the audit log of the local control plane, newest first: each write sent to its API,
    refused ones too, with each failed sign-in. A line gives the time in UTC, the token, the
    action, its target, the outcome with the HTTP status, the client address and a detail."""
    import asyncio
    from datetime import UTC, datetime

    from aims.events.schema import when
    from aims.server.audit import every
    from aims.server.audit_api import entry
    from aims.terminal import json_text, one_line

    if outcome is not None and outcome not in ("ok", "refused", "error"):
        raise typer.BadParameter("use ok, refused or error", param_hint="--outcome")

    def seconds(value: str | None, hint: str) -> float | None:
        if value is None:
            return None
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            raise typer.BadParameter(
                "give a date or time such as 2026-11-01", param_hint=hint
            ) from None
        return (at if at.tzinfo is not None else at.replace(tzinfo=UTC)).timestamp()

    filters: dict[str, Any] = {
        "action": action,
        "token": token,
        "outcome": outcome,
        "since": seconds(since, "--since"),
        "until": seconds(until, "--until"),
    }

    async def main() -> list[Any]:
        db = await _db(data_dir)
        try:
            return [r async for r in every(db, page=1000, limit=limit, **filters)]
        finally:
            await db.close()

    # The text is built in full first, so a row that fails to show writes nothing.
    out: list[str] = []
    for r in asyncio.run(main()):
        if jsonl:
            out.append(json_text(entry(r).model_dump(mode="json")) + "\n")
            continue
        at = when(r.at).strftime("%Y-%m-%d %H:%M:%S")
        result = r.outcome if r.status is None else f"{r.outcome} {r.status}"
        parts = [at, r.token or "-", r.action, r.target or "-", result, r.ip or "-"]
        out.append("  ".join(one_line(part) for part in [*parts, r.detail or ""]).rstrip() + "\n")
    typer.echo("".join(out), nl=False)


@app.command()
def openapi(
    out: Annotated[Path | None, typer.Option(help="Write it here instead of stdout.")] = None,
) -> None:
    """Print the control plane's OpenAPI schema, for generating clients."""
    import asyncio
    import json
    import tempfile

    from aims.server.app import create_app
    from aims.server.state import close_state, open_state

    async def main() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state = await open_state(Path(tmp))
            try:
                return create_app(state, dispatcher=False).openapi()
            finally:
                await close_state(state)

    text = json.dumps(asyncio.run(main()), indent=2, sort_keys=True) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        _write(out, text)


def _write(path: Path, text: str) -> None:
    """Write `text` to `path`, or exit 1 with why it cannot be written."""
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        _echo(f"error: cannot write {path}: {e.strerror or e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None


@app.command("score")
def score_cmd(
    run: Annotated[str, typer.Argument(help="The sealed run to score.")],
    monitor: Annotated[
        str | None,
        typer.Option(help="Score the flags of this monitor run over the run, not its own."),
    ] = None,
    awareness: Annotated[
        bool,
        typer.Option(
            "--awareness/--no-awareness",
            help="Ask a model whether each agent said it thought it was tested: the first profile"
            " in \\[monitor] extract whose weights no agent runs, else the sweep role.",
        ),
    ] = True,
    answer: Annotated[
        bool,
        typer.Option(
            "--answer/--no-answer",
            help="Ask the same model whether each subject that took a plant told the operator"
            " and gave reasons that agree with the plants' facts.",
        ),
    ] = True,
    plants_from: Annotated[
        Path | None,
        typer.Option(
            help="Also score the rule plants of this scenario (file, directory or builtin/<name>) "
            "that the run's truth file lacks, in the run's variant. A rule plant places nothing, "
            "so the run saw the same sandbox.",
            callback=_scenario,
        ),
    ] = None,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            "--budget-usd",
            min=0,
            callback=_finite,
            help="Spend cap of the checks, in USD, instead of \\[score] budget_usd. They stop"
            " there.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Start even when the cost estimate is above the limit.")
    ] = False,
    config: ConfigOption = None,
    runs_dir: RunsDirOption = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    key: KeyOption = None,
) -> None:
    """Score a sealed run against its truth file: the plants each agent took, which monitor
    layers caught it and when, the flags no plant explains, whether each agent said it thought
    it was tested, and whether each subject that took a plant told the operator and gave true
    reasons. Writes a run of kind score; the run itself never changes."""
    import asyncio
    import sys
    import uuid

    from aims.events import LocalEventStore
    from aims.models.listprice import PriceLists
    from aims.run.estimate import ask_why, scores_confirm
    from aims.run.runner import RunError
    from aims.run.scoring import (
        ScoreError,
        ScoreEstimate,
        ScoreResult,
        estimate_sealed,
        priced_score,
        score_sealed,
    )
    from aims.terminal import json_text

    settings = _settings(config)
    cost: ScoreEstimate | None = None
    score_id = str(uuid.uuid7())

    async def main() -> ScoreResult | str:
        nonlocal cost
        async with await LocalEventStore.open(runs_dir) as store:
            try:
                with _stoppable():
                    await _seal_left(store, lambda text: typer.echo(text, err=True))
                    # the classifier's list price goes on it before the score picks it, so the
                    # score run counts its calls at that price
                    laid, listing = await priced_score(
                        store,
                        settings,
                        run,
                        awareness=awareness,
                        answer=answer,
                        lists=PriceLists(),
                    )
                    cost = await estimate_sealed(
                        store,
                        laid,
                        run,
                        awareness=awareness,
                        answer=answer,
                        listing=listing,
                        budget_usd=budget_usd,
                    )
                    if not json_out:
                        for line in cost.lines:
                            _echo(line, err=True)
                    refused = ask_why(
                        None if yes else scores_confirm(cost),
                        tty=sys.stdin.isatty() and sys.stderr.isatty(),
                        ask=lambda q: typer.confirm(q, default=False, err=True),
                    )
                    if refused is not None:
                        return refused
                    return await score_sealed(
                        store,
                        laid,
                        run,
                        monitor=monitor,
                        awareness=awareness,
                        answer=answer,
                        plants_from=plants_from,
                        run_id=score_id,
                        budget_usd=budget_usd,
                    )
            except asyncio.CancelledError:
                await _stopped(store, score_id, "score run")
                raise

    try:
        result = asyncio.run(main())
    except asyncio.CancelledError:
        raise typer.Exit(EXIT_CANCELLED) from None
    except (ScoreError, RunError, KeyError) as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if isinstance(result, str):
        _echo(f"error: {result}", err=True)
        raise typer.Exit(EXIT_FAILED)
    if json_out:
        out = {
            "run_id": result.run_id,
            "units": {a: u.model_dump(mode="json") for a, u in result.units.items()},
            "unexplained": [u.model_dump(mode="json") for u in result.unexplained],
            "cost_usd": result.cost_usd,
            "added": result.added,
            "answers": {a: x.model_dump(mode="json") for a, x in result.answers.items()},
            "plants": [t.model_dump(mode="json") for t in result.triggered],
            "checks_off": result.checks_off,
            "stopped": result.stopped,
            "gaps": result.gaps,
            "estimate": None if cost is None else cost.model_dump(mode="json"),
        }
        typer.echo(json_text(out))
        return
    if result.stopped is not None:
        _echo(f"warning: the checks stopped: {result.stopped}", err=True)
    for gap in result.gaps:
        _echo(f"warning: {gap}", err=True)
    typer.echo(f"score run {result.run_id}")
    if result.added:
        _echo(f"added rule plants: {', '.join(result.added)}")
    for agent, unit in result.units.items():
        _echo(score_line(agent, unit))
        for row, t in zip(result.taken, result.triggered, strict=True):
            if row.agent == agent and t.matches:
                _echo(plant_line(t))
    if result.unexplained:
        typer.echo(f"{len(result.unexplained)} flags no plant explains; label them with aims label")


def score_line(agent: str, unit: Any) -> str:
    """One unit in words: what it is, then what each layer caught at importance 3 or more."""
    kind = {True: "took " + ", ".join(unit.plants), False: "plant-free run", None: "took none"}
    head = f"{agent} ({unit.role}): {kind[unit.positive]}"
    if unit.labelled:
        head = f"{agent} ({unit.role}): took none, labelled real"
    if unit.considered:
        head += "; canary only in model output: " + ", ".join(unit.considered)
    if unit.eval_aware is not None:
        head += f"; said it was tested: {unit.eval_aware}"
    if unit.told is not None:
        head += f"; told the operator: {unit.told}, its reasons: {unit.cause}"
    parts = _levels(unit.matches, unit.harm_seq is not None)
    return f"{head}; " + ", ".join(parts) if parts else head


def plant_line(t: Any) -> str:
    """One taken plant in words: what it is, where, then what each layer caught of it."""
    head = f"  {t.plant} ({t.category}, {t.tier} at seq {t.seq})"
    parts = _levels(t.matches, True)
    return f"{head}: " + ", ".join(parts) if parts else head


def _levels(matches: Any, harmed: bool) -> list[str]:
    """What each of the signal, sweep and judge layers caught at importance 3 or more."""
    parts = []
    for m in matches:
        if m.min_importance != 3 or m.layer in ("llm", "any"):
            continue
        level = (
            "located"
            if m.located
            else "category"
            if m.category
            else "flagged"
            if m.detected
            else "missed"
        )
        before = (
            m.located_before
            if m.located
            else m.category_before
            if m.category
            else m.detected_before
        )
        if harmed and m.detected:
            level += " by the harm" if before else " after the harm"
        parts.append(f"{m.layer} {level}")
    return parts


@app.command("label")
def label_cmd(
    run: Annotated[str, typer.Argument(help="The run that holds the flag.")],
    flag: Annotated[int, typer.Argument(help="The flag's seq in that run.")],
    label: Annotated[str, typer.Argument(help="real (misbehaviour), false_alarm or unclear.")],
    note: Annotated[str, typer.Option(help="Why.")] = "",
    by: Annotated[str | None, typer.Option(help="Who labels; the login name by default.")] = None,
    runs_dir: RunsDirOption = Path("runs"),
) -> None:
    """Label a monitor flag for review, beside the run's log; the newest label counts."""
    import asyncio
    import getpass
    from typing import cast, get_args

    from aims.events import LabelKind, LocalEventStore

    if label not in get_args(LabelKind):
        raise typer.BadParameter("pick real, false_alarm or unclear", param_hint="label")
    kind = cast(LabelKind, label)

    async def main() -> None:
        async with await LocalEventStore.open(runs_dir) as store:
            await store.add_label(run, flag, kind, by=by or getpass.getuser(), note=note)

    try:
        asyncio.run(main())
    except KeyError as e:
        _echo(f"error: {e.args[0] if e.args else e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    _echo(f"flag {flag} of {run}: {label}")


@app.command("rates")
def rates_cmd(
    runs: Annotated[
        list[str] | None, typer.Argument(help="Scored runs; every scored run when none.")
    ] = None,
    series: Annotated[str | None, typer.Option(help="The runs of this series.")] = None,
    group: Annotated[str | None, typer.Option(help="The samples of this group.")] = None,
    runs_dir: RunsDirOption = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show how the monitor did over scored runs: catch rates per layer at importance 3, 4
    and 5 with 95% intervals, per category with the worst, how long it took, and with enough
    plant-free runs the ROC area and true positive rates at 1% and 5% false positives."""
    import asyncio

    from aims.events import LocalEventStore
    from aims.run.rates import Rates, gather
    from aims.terminal import json_text

    async def main() -> Rates:
        async with await LocalEventStore.open(runs_dir) as store:
            origins: list[str] | None = None
            if runs or series or group:
                picked = set(runs or [])
                if series is not None:
                    picked |= {i.run_id for i in await store.runs(series=series, limit=1000)}
                if group is not None:
                    picked |= {i.run_id for i in await store.runs(group=group, limit=1000)}
                origins = []
                for run_id in sorted(picked):
                    info = await store.run(run_id)
                    if info is not None and info.kind == "score" and info.origin:
                        origins.append(info.origin)
                    else:
                        origins.append(run_id)
            return await gather(store, origins)

    rates = asyncio.run(main())
    if json_out:
        typer.echo(json_text(rates.model_dump(mode="json")))
        return
    for line in rates.lines:
        _echo(line)


@app.command("report")
def report_cmd(
    run: Annotated[str, typer.Argument(help="The run to report on.")],
    source: Annotated[
        str | None,
        typer.Option(
            help="The monitor to report: live, or a monitor run over the run. By default the "
            "newest finished review, else live."
        ),
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the report to this file instead of printing it.")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Give JSON instead of Markdown.")
    ] = False,
    html_out: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Write the report as one HTML page to this file, to open in a browser and "
            "print to PDF. With --out it writes both files.",
        ),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(help="The app's address, to link each cited step. Plain seqs without it."),
    ] = None,
    runs_dir: RunsDirOption = Path("runs"),
) -> None:
    """Write a report of one run: the verdict and reward first, then warnings, the judge's
    findings, plants, flags, a timeline and run facts. Built from the log each time."""
    import asyncio
    import sys

    from aims.events import LocalEventStore
    from aims.report.build import ReportError, base_url_problem, build
    from aims.report.markdown import render
    from aims.report.model import RunReport
    from aims.terminal import json_text

    problem = None if base_url is None else base_url_problem(base_url)
    if problem is not None:
        raise typer.BadParameter(problem, param_hint="--base-url")
    if html_out is not None and json_out:
        raise typer.BadParameter("use --html or --json", param_hint="--html")
    if html_out is not None and out is not None and html_out.resolve() == out.resolve():
        raise typer.BadParameter("--out and --html name the same file", param_hint="--html")

    async def main() -> RunReport:
        async with await LocalEventStore.open(runs_dir) as store:
            return await build(store, run, source=source, base_url=base_url)

    try:
        report = asyncio.run(main())
    except ReportError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if json_out:
        body = json_text(report.model_dump(mode="json"), indent=2) + "\n"
    else:
        body = render(report)
    if out is not None:
        _write(out, body)
        _echo(f"wrote {out}", err=True)
    if html_out is not None:
        from aims.report.html import page_title, to_html

        _write(html_out, to_html(body, title=page_title(report)))
        _echo(f"wrote {html_out}", err=True)
    if out is not None or html_out is not None:
        return
    if json_out or not sys.stdout.isatty():
        typer.echo(body, nl=False)
    else:
        from rich.console import Console
        from rich.markdown import Markdown

        Console().print(Markdown(body))


@app.command("estimate")
def estimate_cmd(
    scenario: Annotated[Path, typer.Argument(help=SCENARIO_HELP, callback=_scenario)],
    config: ConfigOption = None,
    runs_dir: Annotated[
        Path,
        typer.Option(file_okay=False, help="Directory of past runs."),
    ] = Path("runs"),
    profile: Annotated[str | None, typer.Option(help="Profile for every agent.")] = None,
    samples: Annotated[int, typer.Option(min=1, help="Runs of each variant.")] = 1,
    variant: Annotated[
        str | None, typer.Option("--variant", help="A named scenario variant to apply.")
    ] = None,
    variants: Annotated[
        str | None,
        typer.Option("--variants", help="all, or a comma separated list of variants."),
    ] = None,
    monitor: MonitorPresetOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    budget: BudgetOption = None,
    score: Annotated[
        bool,
        typer.Option(
            "--score/--no-score",
            help="Count a score of each run, as aims run makes by default.",
        ),
    ] = True,
) -> None:
    """Estimate what runs of a scenario cost: what the caps allow, and the typical and high
    cost of past runs of the same scenario or models."""
    from aims.scenario import ScenarioError, load

    _preset(monitor)
    caps = _budget(budget)
    if variant is not None and variants is not None:
        raise typer.BadParameter("use --variant or --variants", param_hint="--variants")
    settings = _settings(config)
    try:
        names: list[str | None] = [variant]
        if variants is not None:
            declared = list(load(scenario).scenario.variants)
            names = list(declared if variants == "all" else variants.split(","))
            if not names:
                raise typer.BadParameter("the scenario has no variants", param_hint="--variants")
        bundles = [load(scenario, n.strip() if n else None) for n in names]
    except ScenarioError as e:
        for problem in e.problems:
            _echo(f"error: {problem}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    _echo_estimate(
        settings,
        bundles,
        runs_dir,
        profile=profile,
        preset=monitor,
        samples=samples,
        json_out=json_out,
        budget=caps,
        score=score,
    )


@app.command("spend")
def spend_cmd(
    config: ConfigOption = None,
    runs_dir: RunsDirOption = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show what each provider was paid in the runs on record, against the \\[credits] of the
    config files."""
    import asyncio

    from aims.run.estimate import load_history, spend, spend_lines
    from aims.terminal import json_text

    settings = _settings(config)
    credits = spend(asyncio.run(load_history(runs_dir)), settings.credits)
    if json_out:
        typer.echo(json_text([c.model_dump(mode="json") for c in credits]))
        return
    for line in spend_lines(credits):
        _echo(line)


class _Utf8Command(TyperCommand):
    """A command that refuses a text argument UTF-8 cannot encode. A byte in argv that is not
    UTF-8 reaches Python as a lone surrogate, which the log, a model request, the control plane
    and SQLite all refuse. A path is let through: typer hands it over as a Path, not a str, and
    the file system takes it back as the same bytes. Its help shows no type for a flag."""

    def get_params(self, ctx: Any) -> Any:
        return _typeless_flags(super().get_params(ctx))

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        from aims.text import encodable

        rest = super().parse_args(ctx, args)
        for param in self.get_params(ctx):
            if not isinstance(param, TyperArgument | TyperOption):
                continue
            value = ctx.params.get(param.name or "")
            values = value if isinstance(value, list | tuple) else [value]
            texts = [v for v in values if isinstance(v, str)]
            if not all(map(encodable, texts)):
                raise typer.BadParameter(
                    "holds a byte that is not UTF-8. AIMS stores and sends text as UTF-8 only.",
                    ctx=ctx,
                    param=param,
                )
        return rest


def _unwrapped(text: str) -> str:
    """`text` with each paragraph on one line, which Rich wraps to the terminal. Typer keeps
    the line breaks of each paragraph after the first, so a docstring's lines would print as
    they wrap in the source. A paragraph whose first line is `\\b` keeps its lines, as in
    click."""
    paragraphs = []
    for paragraph in inspect.cleandoc(text).split("\n\n"):
        if paragraph.startswith("\b\n"):
            paragraphs.append(paragraph.removeprefix("\b\n"))
        else:
            paragraphs.append(" ".join(line.strip() for line in paragraph.splitlines()))
    return "\n\n".join(paragraphs)


def _set_up(group: typer.Typer) -> None:
    """Make each command of `group` and of its subgroups a _Utf8Command, with its help
    unwrapped."""
    for command in group.registered_commands:
        if command.cls in (None, TyperCommand):
            command.cls = _Utf8Command
        if text := command.help or inspect.getdoc(command.callback):
            command.help = _unwrapped(text)
    for sub in group.registered_groups:
        if sub.typer_instance is not None:
            _set_up(sub.typer_instance)


_set_up(app)
