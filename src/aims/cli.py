from pathlib import Path
from typing import Annotated, Any

import typer

from aims import __version__

app = typer.Typer(name="aims", no_args_is_help=True, add_completion=False)
models_app = typer.Typer(no_args_is_help=True, help="Model profiles and provider checks.")
app.add_typer(models_app, name="models")

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="An aims.toml to use instead of the project's."),
]


def _settings(config: Path | None) -> Any:
    """Every config layer merged, or exit 1 with the problem."""
    from aims.config import ConfigError, load
    from aims.models import ProfileError
    from aims.prompts import TextError

    try:
        return load(config)
    except (ConfigError, ProfileError, TextError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


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
        typer.echo(f"wrote {write(path, force=force)}")
    except ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@app.command("config")
def config_paths(config: ConfigOption = None) -> None:
    """Show which config files are read, lowest layer first."""
    from aims.config import ConfigError, layers

    try:
        found = layers(config)
    except ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    for name, path in (("user", found.user), ("project", found.project)):
        state = "none" if path is None else f"{path} ({'read' if path.is_file() else 'absent'})"
        typer.echo(f"{name}: {state}")


@app.command()
def verify(
    run: Annotated[str, typer.Argument(help="Run id or run directory.")],
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
) -> None:
    """Check the hash chain of a run and the blobs it references."""
    from aims.events import verify_run

    path = Path(run) if Path(run).is_dir() else runs_dir / run
    result = verify_run(path)
    problems = [
        *([result.error] if result.error else []),
        *(f"missing blob {sha}" for sha in result.missing_blobs),
        *(f"blob {sha} does not match its hash" for sha in result.bad_blobs),
    ]
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(1)
    state = "finished" if result.finished else "not finished"
    typer.echo(f"ok: {result.events} events, {state}, head {result.head}")


@app.command("check")
def check_plants(
    run: Annotated[str, typer.Argument(help="Run id.")],
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Report which plants each agent of a sealed run took, from its log and truth file."""
    import asyncio
    import json

    from aims.events import LocalEventStore
    from aims.run.score import CheckError, check_run

    async def main() -> list[Any]:
        async with await LocalEventStore.open(runs_dir) as store:
            return await check_run(store, run)

    try:
        rows = asyncio.run(main())
    except (CheckError, KeyError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if json_out:
        typer.echo(json.dumps([row.as_dict() for row in rows], ensure_ascii=False))
        return
    if not rows:
        typer.echo("no plants taken")
        return
    for row in rows:
        state = "took" if row.taken else "touched" if row.tier == "touched" else "reached"
        seqs = ",".join(str(seq) for seq in row.evidence)
        typer.echo(f"{row.agent} {state} {row.plant} ({row.kind}, {row.tier}) at {row.seq}: {seqs}")


@models_app.command("list")
def models_list(config: ConfigOption = None) -> None:
    """Show the model profiles and whether their keys are set."""
    import os

    models = _settings(config).models
    roles = {}
    for role, name in models.roles.model_dump().items():
        roles.setdefault(name, []).append(role)
    for name, p in sorted(models.profiles.items()):
        envs = [e for e in (p.api_key_env, p.base_url_env) if e]
        unset = [e for e in envs if not os.environ.get(e)]
        state = f"unset {', '.join(unset)}" if unset else "ready"
        role = f"  [{', '.join(roles[name])}]" if name in roles else ""
        typer.echo(f"{name}  {p.backend}  {p.model}  reasoning {p.reasoning}  {state}{role}")


@models_app.command("check")
def models_check(
    profiles: Annotated[list[str] | None, typer.Argument(help="Profiles to check.")] = None,
    config: ConfigOption = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
) -> None:
    """Call each provider with small requests and report what works. Recorded as a run."""
    import asyncio

    from aims.models import ProfileError

    models = _settings(config).models
    try:
        chosen = [models.get(n) for n in profiles] if profiles else [models.role("agent")]
        for p in chosen:
            p.url()
            p.api_key()
    except ProfileError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    failed = asyncio.run(_check(models, chosen, runs_dir))
    if failed:
        raise typer.Exit(1)


async def _check(models: Any, chosen: list[Any], runs_dir: Path) -> bool:
    from aims.events import LocalEventStore
    from aims.models.check import record_check

    def show(c: Any) -> None:
        typer.echo(f"{c.verdict:4}  {c.profile}  {c.name}: {c.detail}")

    async with await LocalEventStore.open(runs_dir) as store:
        run_id, checks = await record_check(store, models, chosen, on_check=show)
    typer.echo(f"run {run_id}")
    return any(c.verdict == "fail" for c in checks)


scenario_app = typer.Typer(no_args_is_help=True, help="Scenario files.")
app.add_typer(scenario_app, name="scenario")
prompts_app = typer.Typer(no_args_is_help=True, help="The texts the agent reads.")
app.add_typer(prompts_app, name="prompts")


@prompts_app.command("show")
def prompts_show(config: ConfigOption = None) -> None:
    """Print the texts agents and the monitor get with this aims.toml, as [prompts] tables to
    edit: the agent's, among them the `denied` reply and `notify_on_resume`, each tool's, the
    monitor's and its category definitions.

    A scenario agent's `prompts` mapping overrides the agent's texts for that agent.
    """
    import json

    from aims.monitor.texts import resolve
    from aims.prompts import TextError
    from aims.tools import UnknownTool, registry

    layered = _settings(config).prompts
    try:
        agent = layered.agent_texts()
        tools = {
            name: cls(layered.tools.get(name)).text for name, cls in sorted(registry().items())
        }
        unknown = sorted(set(layered.tools) - set(tools))
        if unknown:
            raise UnknownTool(f"[prompts.tools] names unknown tool(s) {', '.join(unknown)}")
        monitor = resolve(layered)
    except (TextError, UnknownTool) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None

    def value(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    lines = ["[prompts]"]
    lines += [f"{key} = {value(text)}" for key, text in agent.model_dump().items()]
    for name, texts in tools.items():
        lines += ["", f"[prompts.tools.{name}]"]
        lines += [f"{key} = {value(text)}" for key, text in texts.items()]
    lines += ["", "[prompts.monitor]"]
    lines += [f"{key} = {value(text)}" for key, text in sorted(monitor.texts.items())]
    lines += ["", "[prompts.monitor.categories]"]
    lines += [f"{key} = {value(text)}" for key, text in monitor.categories.items()]
    typer.echo("\n".join(lines))


EXIT_FAILED, EXIT_KILLED, EXIT_CANCELLED = 1, 3, 130


@scenario_app.command("check")
def scenario_check(
    path: Annotated[Path, typer.Argument(help="The scenario file.")],
    config: ConfigOption = None,
    profile: Annotated[str | None, typer.Option(help="Profile for every agent.")] = None,
    runs_dir: Annotated[
        Path, typer.Option(help="Directory of past runs the cost estimate reads.")
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
            help="Keep the self-test's runs here. By default they go to a temporary directory,"
            " kept only when a check fails."
        ),
    ] = None,
) -> None:
    """Validate a scenario file, the profiles and tools it names, and its monitor: the
    rules, the signal options and the monitor's models. Then estimate what a run costs.

    A scenario with a `selftest` or an impossible plant then gets its self-test: its
    reference script must exit 0 and trigger no plant, its exploit script must take each
    plant it claims, and an impossible plant's verifier must fail its reference and an
    untouched workdir. Each is a run of scripted agents, checked as `aims check` checks a run."""
    from aims.run.runner import check
    from aims.scenario import ScenarioError, load

    settings = _settings(config)
    try:
        bundle = load(path)
        problems = check(bundle, settings.models, profile, settings.prompts)
    except ScenarioError as e:
        problems = str(e).splitlines()
    else:
        problems += _monitor_problems(settings, bundle, profile)
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
        typer.echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(EXIT_FAILED)
    s = bundle.scenario
    agents = ", ".join(a.id for a in s.agents)
    typer.echo(f"ok: {s.name}, agents {agents}, digest {bundle.digest[:16]}")
    _echo_estimate(settings, [bundle], runs_dir, profile=profile)
    from aims.run import selftest as st

    if selftest and st.wanted(s):
        code = _selftest(settings, bundle, profile, selftest_runs)
        if code:
            raise typer.Exit(code)


def _selftest(settings: Any, bundle: Any, profile: str | None, keep: Path | None) -> int:
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
            typer.echo(f"error: {problem}", err=True)
        return EXIT_FAILED

    def show(o: Any) -> None:
        typer.echo(f"selftest {o.case}: {'ok' if o.ok else 'failed'}")
        for note in o.notes:
            typer.echo(f"  {note}")
        for problem in o.problems:
            typer.echo(f"error: selftest {o.case}: {problem}", err=True)

    async def main(root: Path) -> list[Any] | str:
        try:
            backend = await DockerBackend.open(audit="kernel")
        except Exception as e:
            return f"the self-test needs Docker and the kernel tracer: {e}"
        async with backend, await LocalEventStore.open(root) as store:
            bench = Bench(
                store,
                backend,
                settings.models,
                audit="kernel",
                prompts=settings.prompts,
                config_files=settings.layers.digests(),
            )
            return await st.run(bench, bundle, plan, profile=profile, on_outcome=show)

    root = keep or Path(tempfile.mkdtemp(prefix="aims-selftest-"))
    found = asyncio.run(main(root))
    if isinstance(found, str):
        typer.echo(f"error: {found}; --no-selftest skips it", err=True)
        if keep is None:
            shutil.rmtree(root, ignore_errors=True)
        return EXIT_FAILED
    if all(o.ok for o in found):
        if keep is None:
            shutil.rmtree(root, ignore_errors=True)
        return 0
    typer.echo(f"selftest runs kept in {root}", err=True)
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
) -> None:
    """Print the cost estimate of `samples` runs of each bundle, from the runs under
    `runs_dir`."""
    import asyncio
    import json

    from aims.models import ProfileError
    from aims.monitor.config import MonitorConfigError
    from aims.prompts import TextError
    from aims.run.estimate import estimate_runs, load_history

    try:
        found = asyncio.run(load_history(runs_dir))
        cost = estimate_runs(
            found, settings, bundles, profile=profile, preset=preset, samples=samples
        )
    except (MonitorConfigError, ProfileError, TextError) as e:
        typer.echo(f"no cost estimate: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if json_out:
        typer.echo(json.dumps(cost.model_dump(mode="json"), ensure_ascii=False))
        return
    for line in cost.lines:
        typer.echo(line)


def _monitor_problems(settings: Any, bundle: Any, profile: str | None) -> list[str]:
    """What stops the scenario's monitor: bad rules or signal options, and monitor models that
    do not resolve or watch an agent's own model."""
    from aims.models import ProfileError
    from aims.monitor.config import MonitorConfigError
    from aims.monitor.setup import resolve_config
    from aims.monitor.signals.engine import check_signals
    from aims.prompts import TextError
    from aims.run.job import monitor_for
    from aims.run.runner import agent_profile

    scenario = bundle.scenario
    try:
        config = resolve_config(settings.monitor, scenario)
    except MonitorConfigError as e:
        return [f"monitor: {e}"]
    problems = [f"monitor: {p}" for p in check_signals(config, scenario)]
    if problems:
        return problems
    try:
        agents = [agent_profile(settings.models, a, profile) for a in scenario.agents]
        monitor_for(settings.models, settings.prompts, scenario, agents, layers=settings.monitor)
    except (MonitorConfigError, ProfileError, TextError) as e:
        problems.append(f"monitor: {e}")
    return problems


@scenario_app.command("schema")
def scenario_schema() -> None:
    """Print the JSON Schema of scenario files."""
    import json

    from aims.scenario import Scenario

    typer.echo(json.dumps(Scenario.model_json_schema(), indent=2))


@app.command()
def run(
    scenario: Annotated[
        Path | None, typer.Argument(help="The scenario file; optional with --replay.")
    ] = None,
    config: ConfigOption = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
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
            " or N, the last step replayed of a run of one agent."
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
        str | None, typer.Option("--variant", help="A named scenario variant to apply.")
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
            help="Start the series again at 0 even when the runs dir has runs of it.",
        ),
    ] = False,
    score: Annotated[
        bool,
        typer.Option(
            "--score/--no-score",
            help="Score each sealed run that has a truth file against it, as aims score does.",
        ),
    ] = True,
) -> None:
    """Run a scenario, or fork a recorded run at a step.

    With a control plane (a local `aims ui`, or --server) the runs ship to it and take its
    pause, inject and kill. The monitor's flags, pauses and verdict show in the live view, and
    as JSON lines with --json. Under human oversight a pause asks at the terminal.

    With --series, --runs K makes K runs in a row, each continuing from the one before it.
    """
    import asyncio

    _preset(monitor)
    if audit not in ("kernel", "snapshot"):
        raise typer.BadParameter("use kernel or snapshot", param_hint="--audit")
    if scenario is None and replay is None:
        raise typer.BadParameter("give a scenario file or --replay", param_hint="SCENARIO")
    if replay is None and (fork_at is not None or strict_env or inject is not None or keep_others):
        raise typer.BadParameter(
            "needs --replay", param_hint="--fork-at/--strict-env/--inject/--keep-others"
        )
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
    link = None if no_server else _connection(server, token_file, required=server is not None)
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
        )
    )
    raise typer.Exit(code)


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


async def last_of_series(store: Any, name: str) -> tuple[Any, int] | None:
    """Where series `name` goes on: its completed run with the highest index, whose folders
    and canaries the next run inherits, and the index after every run of it; None when no run
    of it completed."""
    runs = [
        i
        for i in await store.runs(series=name, kind="scenario", limit=1000)
        if i.series_index is not None
    ]
    done = [i for i in runs if i.status == "completed"]
    if not done:
        return None
    last = max(done, key=lambda i: (i.series_index, i.run_id))
    return last, max(i.series_index for i in runs) + 1


async def _run(**o: Any) -> int:
    import asyncio
    import json
    import logging
    import os
    import signal
    import sys
    import uuid

    from rich.console import Console
    from rich.live import Live
    from rich.logging import RichHandler
    from rich.markup import escape

    from aims.channels import archives
    from aims.events import LocalEventStore, Series
    from aims.models import ProfileError
    from aims.monitor.config import MonitorConfigError
    from aims.node.client import ControlClient
    from aims.node.link import Link
    from aims.run import truth
    from aims.run.estimate import estimate_runs, guard, history
    from aims.run.fork import (
        CutAt,
        check_fork,
        cut_at,
        fork_profiles,
        fork_schedules,
        live_profiles,
        load_origin,
        planner,
        preview,
        same_scenario,
    )
    from aims.run.job import monitor_for, monitor_profiles
    from aims.run.runner import Bench, Fork, Options, Result, RunError, Runner, agent_profile, check
    from aims.run.view import HumanPrompt, JsonLines, LiveView, digest, result_json, summary
    from aims.sandbox import DockerBackend, SandboxError
    from aims.scenario import ScenarioError, load
    from aims.wire import TracerHealth

    err = Console(stderr=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err, show_path=False)],
        force=True,
    )

    def fail(message: str) -> int:
        err.print(f"[red]error:[/] {message}")
        return EXIT_FAILED

    from aims.config import ConfigError
    from aims.config import load as load_settings
    from aims.prompts import TextError

    try:
        settings = load_settings(o["config"])
    except (ConfigError, ProfileError, TextError) as e:
        return fail(str(e))
    models, prompts = settings.models, settings.prompts
    async with await LocalEventStore.open(o["runs_dir"]) as store:
        origin = None
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
                    step = found if found is not None else min(origin.steps.values(), default=0)
                    check_fork(origin, step)
                bundle = origin.full_bundle or origin.bundle
                if o["scenario"] is not None and not same_scenario(origin, load(o["scenario"])):
                    return fail(f"{o['scenario']} changed since run {origin.run_id} was recorded")
                fork_profile = o["profile"]
                if fork_profile is not None:
                    models.get(fork_profile)
            else:
                bundle = load(o["scenario"], o["variant"])
                problems = check(bundle, models, o["profile"], prompts)
                if problems:
                    for problem in problems:
                        err.print(f"[red]error:[/] {problem}")
                    return EXIT_FAILED
        except (ScenarioError, RunError, ProfileError) as e:
            return fail(str(e))
        try:
            if origin is not None and o["profile"] is None:
                agents = list(live_profiles(models, origin, step).values())
            else:
                agents = [agent_profile(models, a, o["profile"]) for a in bundle.scenario.agents]
            if origin is not None:
                agents += [p for s in origin.schedules.values() for _, p in s]
            monitor = monitor_for(
                models,
                prompts,
                bundle.scenario,
                agents,
                layers=settings.monitor,
                preset=o["monitor"],
            )
        except (MonitorConfigError, ProfileError, TextError, RunError) as e:
            return fail(escape(f"monitor: {e}"))
        if monitor is not None:
            for p in monitor_profiles(monitor).values():
                try:
                    p.url()
                    p.api_key()
                except ProfileError as e:
                    return fail(
                        escape(
                            f"the monitor's {e}; set it, or use a signals-only monitor, which"
                            ' needs no key: preset = "signals" under [monitor] in aims.toml,'
                            " monitor: {preset: signals} in the scenario, or --monitor signals"
                        )
                    )
        cost = None
        try:
            cost = estimate_runs(
                await history(store),
                settings,
                [bundle],
                profile=o["profile"],
                preset=o["monitor"],
                samples=o["samples"] * o.get("runs", 1),
            )
        except (MonitorConfigError, ProfileError, TextError) as e:
            err.print(f"[yellow]no cost estimate:[/] {escape(str(e))}")
        if cost is not None:
            if o["json_out"]:
                typer.echo(json.dumps({"type": "estimate", **cost.model_dump(mode="json")}))
            else:
                for text in cost.lines:
                    err.print(escape(text), highlight=False)
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
                return fail(escape(refused))
        try:
            backend = await DockerBackend.open(audit=o["audit"])
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
            lines = JsonLines(store, typer.echo) if o["json_out"] else None
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
                )
                kwargs: dict[str, Any] = {}
                if series_run is not None:
                    options.series = series_run
                    options.inherited = inherited
                    options.restored = restored
                elif origin is not None:
                    fork = Fork(
                        origin=origin.run_id,
                        step=step,
                        sample=i,
                        group=group,
                        strict=o["strict_env"],
                        inject=o["inject"],
                        profile=o["profile"],
                        steps=None if cut is None else dict(cut.steps),
                        cut=None if cut is None else cut.seq,
                        agent=None if cut is None else cut.agent,
                        keep_others=o["keep_others"],
                    )
                    if i == 0:
                        for line in preview(origin, fork):
                            err.print(escape(line))
                    options.fork = fork
                    options.seed = origin.seed
                    options.inherited = origin.inherited
                    options.restored = dict(origin.restored) or None
                    kwargs = {
                        "profiles": fork_profiles(bench, origin, fork),
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
                        alive=backend.tracer is not None and backend.tracer.alive
                    ),
                )
                await link.start()
            gate = asyncio.Semaphore(o["parallel"])
            stops = 0
            main = asyncio.current_task()
            loop = asyncio.get_running_loop()

            def stop() -> None:
                nonlocal stops
                stops += 1
                if stops == 1:
                    err.print("[yellow]stopping: Ctrl-C again to kill the sandboxes now[/]")
                    for r in runners:
                        r.cancel()
                elif main is not None:
                    main.cancel()

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop)

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
                """Score a sealed run that has a truth file; a score that fails warns."""
                from aims.run.scoring import ScoreError, score_sealed

                if not truth.path(store.root / run_id).is_file():
                    return
                score_id = str(uuid.uuid7())
                if link is not None:
                    link.ship(score_id)
                try:
                    await score_sealed(store, settings, run_id, run_id=score_id)
                except ScoreError as e:
                    err.print(f"[yellow]run {run_id} was not scored:[/] {escape(str(e))}")
                except Exception as e:
                    logging.getLogger("aims.run").exception("scoring run %s failed", run_id)
                    why = escape(f"{type(e).__name__}: {e}")
                    err.print(f"[yellow]run {run_id} was not scored:[/] {why}")

            async def execute(batch: list[Runner]) -> list[Result]:
                if view is None:
                    return list(await asyncio.gather(*(one(r) for r in batch)))
                with Live(view, console=err, refresh_per_second=4) as live:
                    if prompt is not None:
                        prompt.live = live
                    try:
                        found = list(await asyncio.gather(*(one(r) for r in batch)))
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
                    start = 0
                    if not o["series_restart"]:
                        found = await last_of_series(store, o["series"])
                        if found is not None:
                            last, start = found
                            previous = last.run_id
                            inherited = truth.inherit(store.root / previous)
                            restored = await archives(store, previous)
                            err.print(f"series {o['series']} goes on after run {previous}")
                    for i in range(start, start + o["runs"]):
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
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                if prompt is not None:
                    prompt.live = None
                    await prompt.close()
                if view is not None:
                    await view.stop()
                if lines is not None:
                    await lines.stop()
                if link is not None:
                    if not await link.drain(60):
                        err.print(
                            "[yellow]some runs did not reach the control plane;"
                            " `aims push` them later[/]"
                        )
                    await link.close()
        digests = {r.run_id: await digest(store, r.run_id) for r in results if r.run_id}
    if o["json_out"]:
        for r in results:
            found = digests.get(r.run_id) if r.run_id else None
            line = {"type": "result", **result_json(r)}
            line["monitor"] = None if found is None else found.json()
            typer.echo(json.dumps(line, ensure_ascii=False))
    elif o["quiet"]:
        for r in results:
            found = digests.get(r.run_id) if r.run_id else None
            flags = f" flags {len(found.flags)}" if found is not None and found.flags else ""
            line = f"{r.run_id} {r.status} ${r.cost_usd:.4f}{flags} {r.reason or ''}"
            typer.echo(line.rstrip())
    else:
        Console().print(summary(results, list(digests.values())))
    statuses = {r.status for r in results}
    if "cancelled" in statuses:
        return EXIT_CANCELLED
    if "killed" in statuses:
        return EXIT_KILLED
    if "failed" in statuses:
        return EXIT_FAILED
    return 0


def _hostname() -> str:
    import re
    import socket

    return re.sub(r"[^A-Za-z0-9._@-]", "-", socket.gethostname()) or "runner"


def _connection(
    server: str | None, token_file: Path | None, *, required: bool
) -> tuple[str, str] | None:
    """Where to ship and the runner token: --server with AIMS_TOKEN or --token-file, else the
    server.json a local `aims ui` wrote."""
    import json
    import os

    from aims.server.serve import SERVER_JSON, server_dir

    token = os.environ.get("AIMS_TOKEN")
    if token_file is not None:
        token = token_file.read_text().strip()
    if server is not None:
        if not token:
            typer.echo("error: give the runner token in AIMS_TOKEN or --token-file", err=True)
            raise typer.Exit(EXIT_FAILED)
        return server, token
    info = server_dir() / SERVER_JSON
    if info.is_file():
        data = json.loads(info.read_text())
        return str(data["url"]), str(data["token"])
    if required:
        typer.echo(
            f"error: no control plane: no {info}; start `aims ui`, give --server, or set"
            " AIMS_SERVER_DIR to the directory `aims ui --data-dir` uses",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    return None


ServerOption = Annotated[
    str | None, typer.Option("--server", help="Control plane URL; AIMS_TOKEN holds the token.")
]
TokenFileOption = Annotated[
    Path | None, typer.Option("--token-file", help="A file holding the runner token.")
]


@app.command()
def ui(
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535, help="Port; 0 picks one.")] = 8484,
    config: ConfigOption = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Where the control plane keeps its state.")
    ] = None,
    runner: Annotated[bool, typer.Option(help="Start a local runner with it.")] = True,
    slots: Annotated[int, typer.Option(min=1, help="Runs the local runner takes at once.")] = 2,
    tls_cert: Annotated[Path | None, typer.Option("--tls-cert", help="TLS certificate.")] = None,
    tls_key: Annotated[Path | None, typer.Option("--tls-key", help="TLS private key.")] = None,
    behind_proxy: Annotated[
        list[str] | None,
        typer.Option("--behind-proxy", help="Address of a proxy whose forwarded headers count."),
    ] = None,
    allow_host: Annotated[
        list[str] | None, typer.Option("--allow-host", help="Another Host name to answer.")
    ] = None,
    no_auth: Annotated[
        bool,
        typer.Option("--no-auth", help="No token; loopback only. Anyone on this machine can act."),
    ] = False,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI.")] = True,
) -> None:
    """Serve the control plane and the UI, with a local runner."""
    import asyncio
    import logging

    from aims.config import ConfigError, project_file
    from aims.server.serve import ServeError, UiOptions, serve, server_dir

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if config is not None and not config.is_file():
        typer.echo(f"error: {config} does not exist", err=True)
        raise typer.Exit(EXIT_FAILED)
    try:
        project = config if config is not None else project_file()
    except ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    options = UiOptions(
        host=host,
        port=port,
        data=data_dir or server_dir(),
        project=project,
        runner=runner,
        slots=slots,
        tls_cert=tls_cert,
        tls_key=tls_key,
        behind_proxy=behind_proxy or [],
        hosts=allow_host or [],
        auth=not no_auth,
        open_browser=open_browser,
    )
    try:
        code = asyncio.run(serve(options))
    except ServeError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    raise typer.Exit(code)


@app.command("runner")
def runner_cmd(
    server: ServerOption = None,
    token_file: TokenFileOption = None,
    name: Annotated[str | None, typer.Option(help="Runner name; the host name by default.")] = None,
    slots: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 2,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Where this runner keeps its runs.")
    ] = None,
    keep_days: Annotated[
        float | None,
        typer.Option(help="Delete runs sealed this many days ago once the control plane has them."),
    ] = None,
    tls_ca: Annotated[
        Path | None, typer.Option("--tls-ca", help="CA or certificate to trust for the server.")
    ] = None,
) -> None:
    """Take jobs from a control plane and run them on this Docker host."""
    import asyncio
    import logging
    import signal

    from aims.node.service import Node, NodeOptions
    from aims.node.service import data_dir as default_dir

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    found = _connection(server, token_file, required=True)
    assert found is not None
    url, token = found

    async def main() -> None:
        node = Node(
            NodeOptions(
                url=url,
                token=token,
                name=name or _hostname(),
                data=data_dir or default_dir(),
                slots=slots,
                keep_days=keep_days,
                verify=True if tls_ca is None else str(tls_ca),
            )
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        stops = 0

        def on_signal() -> None:
            nonlocal stops
            stops += 1
            if stops == 1:
                typer.echo("stopping: runs are sealed as cancelled; again to kill now", err=True)
                stop.set()
            elif current is not None:
                current.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
        await node.run(stop)

    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        raise typer.Exit(EXIT_CANCELLED) from None


@app.command()
def push(
    path: Annotated[Path, typer.Argument(help="A run directory, or a directory of runs.")],
    server: ServerOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Ship runs recorded without a control plane to one."""
    import asyncio

    found = _connection(server, token_file, required=True)
    assert found is not None
    url, token = found
    if (path / "events.jsonl").is_file():
        root, only = path.parent, [path.name]
    elif path.is_dir():
        root, only = path, None
    else:
        typer.echo(f"error: {path} is not a run directory", err=True)
        raise typer.Exit(EXIT_FAILED)
    raise typer.Exit(asyncio.run(_push(root, only, url, token)))


async def _push(
    root: Path, only: list[str] | None, url: str, token: str, *, quiet: bool = False
) -> int:
    from aims import __version__
    from aims.events import LocalEventStore
    from aims.node.client import ControlClient
    from aims.node.link import arch
    from aims.node.shipper import Shipper
    from aims.wire import RunnerHello, TracerHealth

    async with await LocalEventStore.open(root) as store:
        client = ControlClient(url, token, runner=f"push@{_hostname()}")
        try:
            await client.hello(
                RunnerHello(
                    name=client.runner,
                    version=__version__,
                    arch=arch(),
                    tracer=TracerHealth(alive=False, detail="push only"),
                    slots=0,
                )
            )
            failed = 0
            for run_id in only or store.run_ids():
                info = await store.run(run_id)
                if info is None or info.status == "running":
                    typer.echo(f"skip {run_id}: not sealed", err=True)
                    continue
                shipper = Shipper(store, client, run_id)
                await shipper.run()
                if shipper.diverged or shipper.refused:
                    failed += 1
                    typer.echo(f"error: {run_id}: {shipper.diverged or shipper.refused}", err=True)
                elif not quiet:
                    typer.echo(f"{run_id} {info.status} {info.events} events")
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
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
    profile: Annotated[
        str | None, typer.Option(help="Profile for every sweep, the review and the judge.")
    ] = None,
    monitor: MonitorPresetOption = None,
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
    quiet: Annotated[bool, typer.Option(help="One line per monitor run.")] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Print JSON lines: flags, actions, sweeps, verdicts, results."),
    ] = False,
    server: ServerOption = None,
    token_file: TokenFileOption = None,
    no_server: Annotated[
        bool, typer.Option("--no-server", help="Do not ship to a local `aims ui`.")
    ] = False,
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
    link = None if no_server else _connection(server, token_file, required=server is not None)
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
            quiet=quiet,
            json_out=json_out,
            link=link,
        )
    )
    raise typer.Exit(code)


async def _monitor_runs(**o: Any) -> int:
    import asyncio
    import json
    import logging
    import signal
    import tomllib
    import uuid

    import httpx2
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.markup import escape
    from rich.table import Table

    from aims.config import ConfigError
    from aims.config import load as load_settings
    from aims.events import LocalEventStore
    from aims.models import ProfileError
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
    from aims.run.job import JobOptions, monitor_profiles
    from aims.run.runner import RunError
    from aims.run.view import JsonLines, digest, monitor_table

    err = Console(stderr=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err, show_path=False)],
        force=True,
    )

    def fail(message: str) -> int:
        err.print(f"[red]error:[/] {escape(message)}")
        return EXIT_FAILED

    try:
        settings = load_settings(o["config"])
        extra = None
        if o["config"] is not None:
            extra = tomllib.loads(o["config"].read_text()).get("monitor")
    except (ConfigError, ProfileError, TextError, tomllib.TOMLDecodeError) as e:
        return fail(str(e))
    mode = "signals_only" if o["signals_only"] else "judge_only" if o["judge_only"] else "full"
    results: list[OfflineResult] = []
    async with await LocalEventStore.open(o["runs_dir"]) as store:
        planned = []
        for run_id in dict.fromkeys(o["runs"]):
            try:
                origin, _ = await monitored(store, run_id)
                resolved = offline_monitor(
                    settings.models,
                    settings.prompts,
                    origin.bundle.scenario,
                    origin_agents(origin),
                    layers=settings.monitor,
                    config=extra,
                    preset=o["preset"],
                    signals_only=o["signals_only"],
                    judge_only=o["judge_only"],
                    profile=o["profile"],
                    samples=o["samples"],
                )
                for p in monitor_profiles(resolved).values():
                    p.url()
                    p.api_key()
            except (RunError, MonitorConfigError, ProfileError, TextError) as e:
                return fail(f"{run_id}: {e}")
            group = uuid.uuid4().hex[:12] if o["samples"] > 1 else None
            for i in range(o["samples"]):
                options = JobOptions(
                    profile=o["profile"],
                    group=group,
                    sample=i if group else None,
                    samples=o["samples"],
                )
                planned.append((origin, resolved, options))
        lines = JsonLines(store, typer.echo) if o["json_out"] else None
        gate = asyncio.Semaphore(o["parallel"])

        def started(run_id: str) -> None:
            if lines is not None:
                lines.add(run_id)
            elif not o["quiet"]:
                err.print(f"monitoring into {run_id}")

        async def one(origin: Any, resolved: Any, options: Any) -> OfflineResult:
            async with gate:
                return await monitor_offline(
                    store,
                    origin,
                    resolved,
                    models=settings.models,
                    mode=mode,
                    options=options,
                    config_files=settings.layers.digests(),
                    on_start=started,
                )

        main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: main is not None and main.cancel())
        try:
            results = list(await asyncio.gather(*(one(*p) for p in planned)))
        except asyncio.CancelledError:
            err.print("[red]stopped; the monitor runs are sealed as cancelled[/]")
            return EXIT_CANCELLED
        except RunError as e:
            return fail(str(e))
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            if lines is not None:
                await lines.stop()
        digests = [await digest(store, r.run_id) for r in results]
    if o["json_out"]:
        for r, d in zip(results, digests, strict=True):
            line = {
                "type": "result",
                "run_id": r.run_id,
                "origin": r.origin,
                "status": r.status,
                "reason": r.reason,
                "cost_usd": round(r.cost_usd, 10),
                "monitor": d.json(),
            }
            typer.echo(json.dumps(line, ensure_ascii=False))
    elif o["quiet"]:
        for r, d in zip(results, digests, strict=True):
            line = f"{r.run_id} {r.origin} {r.status} flags {len(d.flags)} ${r.cost_usd:.4f}"
            typer.echo(f"{line} {r.reason or ''}".rstrip())
    else:
        table = Table(title="monitor runs")
        for column in ("run", "origin", "status", "flags", "sweeps", "verdict", "cost"):
            table.add_column(column, overflow="fold")
        for r, d in zip(results, digests, strict=True):
            verdict = "" if d.verdict is None else f"severity {d.verdict.data.get('max_severity')}"
            table.add_row(
                r.run_id,
                r.origin,
                r.status if r.reason is None else f"{r.status}: {r.reason}",
                str(len(d.flags)),
                str(d.sweeps),
                verdict,
                f"${r.cost_usd:.4f}",
            )
        out = Console()
        out.print(table)
        found = monitor_table(digests)
        if found is not None:
            out.print(found)
    if o["link"] is not None and results:
        url, token = o["link"]
        try:
            failed = await _push(o["runs_dir"], [r.run_id for r in results], url, token, quiet=True)
        except (ApiError, httpx2.TransportError, OSError) as e:
            err.print(
                f"[yellow]the control plane did not take the monitor runs: {escape(str(e))}[/]"
            )
        else:
            if failed:
                err.print("[yellow]some monitor runs did not reach the control plane[/]")
    if any(r.status == "cancelled" for r in results):
        return EXIT_CANCELLED
    return EXIT_FAILED if any(r.status != "completed" for r in results) else 0


CONTROL_ACTIONS = ("pause", "resume", "deny", "inject", "kill", "sweep")


@app.command()
def control(
    run: Annotated[str, typer.Argument(help="The run's id.")],
    action: Annotated[str, typer.Argument(help="pause, resume, deny, inject, kill or sweep.")],
    agent: Annotated[str | None, typer.Option(help="One agent; every agent by default.")] = None,
    text: Annotated[
        str | None,
        typer.Option(help="The message to inject; with resume, given as the agents go on."),
    ] = None,
    reason: Annotated[str | None, typer.Option(help="Why, for the log.")] = None,
    wait: Annotated[
        float, typer.Option(min=0, help="Seconds to wait for the runner to answer.")
    ] = 10.0,
    server: ServerOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Pause, resume, deny, inject, kill or sweep a running run through its control plane.

    Needs an operator token in AIMS_TOKEN or --token-file. Deny refuses the tool call a paused
    agent waits before; the agent gets the configured `denied` reply.
    """
    import asyncio

    if action not in CONTROL_ACTIONS:
        raise typer.BadParameter(f"use {', '.join(CONTROL_ACTIONS)}", param_hint="ACTION")
    if action == "inject" and not text:
        raise typer.BadParameter("an inject needs text", param_hint="--text")
    if text is not None and action not in ("inject", "resume"):
        raise typer.BadParameter(f"{action} takes no text", param_hint="--text")
    if agent is not None and action == "kill":
        raise typer.BadParameter("a kill stops the whole run", param_hint="--agent")
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
            path, body = "kill", {"reason": reason}
    raise typer.Exit(asyncio.run(_control(url, token, run, path, body, action, wait)))


async def _control(
    url: str, token: str, run: str, path: str, body: dict[str, Any], action: str, wait: float
) -> int:
    import asyncio
    import time

    import httpx2

    from aims import __version__

    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
    async with httpx2.AsyncClient(
        base_url=url.rstrip("/"), headers=headers, timeout=30, trust_env=False
    ) as c:
        try:
            r = await c.post(f"/api/runs/{run}/{path}", json=body)
            if r.status_code >= 400:
                return _problem(r)
            sent = r.json()
            if sent.get("state") == "cancelled":
                typer.echo(f"{run}: cancelled before it started")
                return 0
            command = sent.get("command_id")
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                r = await c.get(f"/api/runs/{run}/commands")
                if r.status_code >= 400:
                    return _problem(r)
                found = next((x for x in r.json() if x["id"] == command), None)
                if found is not None and found["acked"] is not None:
                    outcome = str(found["outcome"])
                    typer.echo(f"{action} {run}: {outcome}")
                    return EXIT_FAILED if outcome.startswith("failed") else 0
                await asyncio.sleep(0.2)
        except httpx2.TransportError as e:
            typer.echo(f"error: cannot reach the control plane at {url}: {e}", err=True)
            return EXIT_FAILED
    typer.echo(
        f"error: {action} {run}: sent, but the runner did not answer within {wait:g} s;"
        f" `GET /api/runs/{run}/commands` shows it later",
        err=True,
    )
    return EXIT_FAILED


def _problem(r: Any) -> int:
    try:
        problem = r.json()
        message = problem.get("detail") or problem.get("title") or r.text
    except ValueError:
        message = r.text[:500]
    typer.echo(f"error: {r.status_code} {message}", err=True)
    return EXIT_FAILED


def _operator(server: str | None, token_file: Path | None) -> tuple[str, str]:
    """The control plane and an operator token: --server, else the local `aims ui`; the token
    from --token-file or AIMS_TOKEN, else the local ui's own operator token if it wrote one."""
    import json
    import os

    from aims.server.serve import SERVER_JSON, server_dir

    token = os.environ.get("AIMS_TOKEN")
    if token_file is not None:
        token = token_file.read_text().strip()
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
        data = json.loads(info.read_text())
        url = str(data["url"])
        token = token or data.get("operator")
    if not token:
        typer.echo(
            "error: give an operator token in AIMS_TOKEN or --token-file; make one with"
            " `aims token create <name> --role operator`",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    return url, token


token_app = typer.Typer(no_args_is_help=True, help="Tokens of the local control plane.")
app.add_typer(token_app, name="token")
DataDirOption = Annotated[
    Path | None, typer.Option("--data-dir", help="The control plane's state directory.")
]


def _db(data_dir: Path | None) -> Any:
    from aims.server.db import ServerDB
    from aims.server.serve import server_dir

    return ServerDB.open((data_dir or server_dir()) / "server.sqlite")


@token_app.command("create")
def token_create(
    name: Annotated[str, typer.Argument(help="A name for the token.")],
    role: Annotated[str, typer.Option(help="viewer, operator or runner.")] = "viewer",
    data_dir: DataDirOption = None,
) -> None:
    """Make a named token and print it. It is shown this once."""
    import asyncio

    if role not in ("viewer", "operator", "runner"):
        raise typer.BadParameter("use viewer, operator or runner", param_hint="--role")

    async def main() -> str:
        db = await _db(data_dir)
        try:
            return await db.create_token(name, role)
        finally:
            await db.close()

    try:
        typer.echo(asyncio.run(main()))
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None


@token_app.command("list")
def token_list(data_dir: DataDirOption = None) -> None:
    """Show tokens by name and role; their values are never kept."""
    import asyncio
    from datetime import UTC, datetime

    async def main() -> list[Any]:
        db = await _db(data_dir)
        try:
            return await db.tokens()
        finally:
            await db.close()

    for t in asyncio.run(main()):
        made = datetime.fromtimestamp(t.created, UTC).strftime("%Y-%m-%d %H:%M")
        state = "revoked" if t.revoked else "live"
        typer.echo(f"{t.name}  {t.role}  {made}  {state}")


@token_app.command("revoke")
def token_revoke(
    name: Annotated[str, typer.Argument(help="The token to revoke.")],
    data_dir: DataDirOption = None,
) -> None:
    """Revoke a token and end its sessions."""
    import asyncio

    async def main() -> bool:
        db = await _db(data_dir)
        try:
            return await db.revoke_token(name)
        finally:
            await db.close()

    if not asyncio.run(main()):
        typer.echo(f"error: no live token {name}", err=True)
        raise typer.Exit(EXIT_FAILED)
    typer.echo(f"revoked {name}")


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
        out.write_text(text)


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
            help="Ask a cheap model whether each agent said it thought it was tested.",
        ),
    ] = True,
    config: ConfigOption = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Score a sealed run against its truth file: the plants each agent took, which monitor
    layers caught it and when, the flags no plant explains, and whether each agent said it
    thought it was tested. Writes a run of kind score; the run itself never changes."""
    import asyncio
    import json

    from aims.events import LocalEventStore
    from aims.run.runner import RunError
    from aims.run.scoring import ScoreError, ScoreResult, score_sealed

    settings = _settings(config)

    async def main() -> ScoreResult:
        async with await LocalEventStore.open(runs_dir) as store:
            return await score_sealed(store, settings, run, monitor=monitor, awareness=awareness)

    try:
        result = asyncio.run(main())
    except (ScoreError, RunError, KeyError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if json_out:
        out = {
            "run_id": result.run_id,
            "units": {a: u.model_dump(mode="json") for a, u in result.units.items()},
            "unexplained": [u.model_dump(mode="json") for u in result.unexplained],
            "cost_usd": result.cost_usd,
        }
        typer.echo(json.dumps(out, ensure_ascii=False))
        return
    typer.echo(f"score run {result.run_id}")
    for agent, unit in result.units.items():
        typer.echo(score_line(agent, unit))
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
    parts = []
    for m in unit.matches:
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
        if unit.harm_seq is not None and m.detected:
            level += " by the harm" if m.detected_before else " after the harm"
        parts.append(f"{m.layer} {level}")
    return f"{head}; " + ", ".join(parts) if parts else head


@app.command("label")
def label_cmd(
    run: Annotated[str, typer.Argument(help="The run that holds the flag.")],
    flag: Annotated[int, typer.Argument(help="The flag's seq in that run.")],
    label: Annotated[str, typer.Argument(help="real (misbehaviour), false_alarm or unclear.")],
    note: Annotated[str, typer.Option(help="Why.")] = "",
    by: Annotated[str | None, typer.Option(help="Who labels; the login name by default.")] = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
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
        typer.echo(f"error: {e.args[0] if e.args else e}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    typer.echo(f"flag {flag} of {run}: {label}")


@app.command("report")
def report_cmd(
    runs: Annotated[
        list[str] | None, typer.Argument(help="Scored runs; every scored run when none.")
    ] = None,
    series: Annotated[str | None, typer.Option(help="The runs of this series.")] = None,
    group: Annotated[str | None, typer.Option(help="The samples of this group.")] = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Report how the monitor did over scored runs: catch rates per layer at importance 3, 4
    and 5 with 95% intervals, per category with the worst, how long it took, and with enough
    plant-free runs the ROC area and true positive rates at 1% and 5% false positives."""
    import asyncio

    from aims.events import LocalEventStore
    from aims.run.report import Report, gather

    async def main() -> Report:
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

    report = asyncio.run(main())
    if json_out:
        typer.echo(report.model_dump_json())
        return
    for line in report.lines:
        typer.echo(line)


@app.command("estimate")
def estimate_cmd(
    scenario: Annotated[Path, typer.Argument(help="The scenario file.")],
    config: ConfigOption = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory of past runs.")] = Path("runs"),
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
) -> None:
    """Estimate what runs of a scenario cost: what the caps allow, and the typical and high
    cost of past runs of the same scenario or models."""
    from aims.scenario import ScenarioError, load

    _preset(monitor)
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
            typer.echo(f"error: {problem}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    _echo_estimate(
        settings,
        bundles,
        runs_dir,
        profile=profile,
        preset=monitor,
        samples=samples,
        json_out=json_out,
    )


@app.command("spend")
def spend_cmd(
    config: ConfigOption = None,
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show what each provider was paid in the runs on record, against the [credits] of the
    config files."""
    import asyncio
    import json

    from aims.run.estimate import load_history, spend, spend_lines

    settings = _settings(config)
    credits = spend(asyncio.run(load_history(runs_dir)), settings.credits)
    if json_out:
        typer.echo(json.dumps([c.model_dump(mode="json") for c in credits]))
        return
    for line in spend_lines(credits):
        typer.echo(line)
