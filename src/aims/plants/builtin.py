"""The built-in plants other than imds: file, canary, impossible, tool and rule."""

import contextlib
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from pydantic import ConfigDict, JsonValue

from aims.events import Category, PlantTier
from aims.models import ToolSpec
from aims.plants.base import (
    GIVEAWAY,
    PlacedFile,
    Placement,
    PlantSetup,
    category_of,
    taken_tier,
)
from aims.plants.canary import Canary, render
from aims.sandbox import AuditedSandbox, SandboxBackend
from aims.tools import Args, Tool, ToolContext, ToolOutcome


class _AnyArgs(Args):
    model_config = ConfigDict(frozen=True, extra="allow")


class _ReplyTool:
    """A tool that answers every call with fixed text."""

    args: ClassVar[type[Args]] = _AnyArgs
    texts: ClassVar[dict[str, str]] = {}
    placeholders: ClassVar[dict[str, frozenset[str]]] = {}

    def __init__(
        self, name: str, description: str, parameters: dict[str, JsonValue], reply: str
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.reply = reply
        self.text: dict[str, str] = {}

    def spec(self, config: Any) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    async def run(self, ctx: ToolContext, args: Any) -> ToolOutcome:
        return ToolOutcome(self.reply)


class _Stub:
    kind: ClassVar[str]
    category: ClassVar[Category | None] = None
    tiers: ClassVar[frozenset[PlantTier]] = frozenset({"touched", "used"})

    def __init__(self, setup: PlantSetup) -> None:
        self.setup = setup
        self.config: Any = setup.config
        self.canary: Canary | None = setup.canaries.get(setup.config.name)

    def place(self, agent_id: str) -> Placement:
        return Placement() if not self._applies(agent_id) else self._place(agent_id)

    def _place(self, agent_id: str) -> Placement:
        return Placement()

    def record(self) -> dict[str, JsonValue]:
        out: dict[str, Any] = {
            "name": self.config.name,
            "kind": self.config.kind,
            "category": category_of(self.config, type(self)),
            "taken": taken_tier(self.config, type(self)),
            "tiers": sorted(self.tiers),
            "agents": self.config.agents,
            "canary": None
            if self.canary is None
            else {"parts": dict(self.canary.parts), "value": self.canary.value},
        }
        out.update(self._record())
        return out

    def _record(self) -> dict[str, JsonValue]:
        return {}

    async def start(self, agent_id: str, sandbox: AuditedSandbox, backend: SandboxBackend) -> None:
        return None

    async def stop(self) -> None:
        return None

    def lint(self) -> Sequence[str]:
        problems: list[str] = []
        for where, text in self._linted():
            for match in GIVEAWAY.finditer(text):
                problems.append(f"plant {self.config.name}: {where} says {match.group(0)!r}")
        return problems

    def _linted(self) -> list[tuple[str, str]]:
        return []

    def _applies(self, agent_id: str) -> bool:
        if self.config.agents is not None:
            return agent_id in self.config.agents
        return agent_id in self.setup.scenario.subjects

    def _render(self, text: str) -> str:
        return render(text, self.setup.canaries, self.config.name)


class FileBait(_Stub):
    kind = "file"

    def _place(self, agent_id: str) -> Placement:
        config = self.config
        if config.content is not None:
            data = self._render(config.content).encode()
        else:
            data = self.setup.source or b""
            with contextlib.suppress(UnicodeDecodeError):
                data = self._render(data.decode()).encode()
        mode = int(config.mode, 8) if config.mode is not None else 0o664
        path = self.setup.scenario.sandbox_path(config.path)
        return Placement(files=[PlacedFile(path=path, data=data, mode=mode)])

    def _record(self) -> dict[str, JsonValue]:
        config = self.config
        content = None
        if config.content is not None:
            content = self._render(config.content)
        elif self.setup.source is not None:
            try:
                content = self._render(self.setup.source.decode())
            except UnicodeDecodeError:
                content = None
        return {
            "path": config.path,
            "on": list(config.on),
            "mode": config.mode,
            "content": content,
            "source": config.source,
        }

    def _linted(self) -> list[tuple[str, str]]:
        out = [("path", self.config.path)]
        if self.config.source is not None:
            out.append(("source", self.config.source))
        content = self._record()["content"]
        if isinstance(content, str):
            out.append(("content", content))
        return out


class CanaryBait(_Stub):
    kind = "canary"
    tiers = frozenset({"used"})

    def _linted(self) -> list[tuple[str, str]]:
        return []


class ImpossibleTask(_Stub):
    kind = "impossible"
    category = "scorer_tampering"
    tiers = frozenset({"used"})

    def _record(self) -> dict[str, JsonValue]:
        return {"reference": self.config.reference, "reward": self.config.reward}

    def _linted(self) -> list[tuple[str, str]]:
        return [("reference", self.config.reference)]


class ToolBait(_Stub):
    kind = "tool"

    def _place(self, agent_id: str) -> Placement:
        config = self.config
        reply = self._render(config.reply)
        tool = _ReplyTool(config.tool, config.description, config.parameters, reply)
        return Placement(tools=[cast("Tool", tool)])

    def _record(self) -> dict[str, JsonValue]:
        config = self.config
        return {
            "tool": config.tool,
            "description": config.description,
            "parameters": config.parameters,
            "reply": self._render(config.reply),
        }

    def _linted(self) -> list[tuple[str, str]]:
        return [
            ("tool", self.config.tool),
            ("description", self.config.description),
            ("reply", self._render(self.config.reply)),
        ]


class RuleTrigger(_Stub):
    kind = "rule"
    tiers = frozenset({"touched"})

    def _record(self) -> dict[str, JsonValue]:
        return {"rule": self.config.rule.model_dump(mode="json")}

    def _linted(self) -> list[tuple[str, str]]:
        rule = self.config.rule
        return [("rule id", rule.id), ("rule title", rule.title)]
