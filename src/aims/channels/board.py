"""The `board` channel: a message board reached through a post tool and a read tool.

The board lives on the host. A post is logged as `channel.message` by the agent that posted
it, a read as `channel.read` by the agent that read; `post` and `read` serve a scripted actor
the same way, with no tool call."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from pydantic import Field

from aims.channels.base import ChannelError, ChannelSetup
from aims.events import ChannelInfo, ChannelMessage, ChannelRead, Draft
from aims.models import ToolSpec
from aims.prompts import TextError, check_format
from aims.sandbox import AuditedSandbox
from aims.sandbox.base import Mount
from aims.scenario import Scenario
from aims.scenario.model import BoardChannel
from aims.scenario.model import Channel as ChannelConfig
from aims.tools import Args, Tool, ToolContext, ToolOutcome

READ_BYTES = 12 * 1024


class MessageTooLong(ChannelError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"the message is {size} bytes, over the board's {limit}")
        self.size = size
        self.limit = limit


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    agent: str
    text: str


class PostArgs(Args):
    text: str = Field(min_length=1)


class ReadArgs(Args):
    pass


class _BoardTool:
    """A tool of one board, named as the scenario names it, with texts like any tool's."""

    args: ClassVar[type[Args]]
    texts: ClassVar[dict[str, str]]
    placeholders: ClassVar[dict[str, frozenset[str]]]

    def __init__(self, board: Board, name: str, texts: Mapping[str, str] | None = None) -> None:
        self.board = board
        self.name = name
        self.text = resolve(type(self), name, texts)

    def _format(self, key: str, **values: Any) -> str:
        return self.text[key].format(channel=self.board.name, **values)


class PostTool(_BoardTool):
    args: ClassVar[type[Args]] = PostArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "Post a message to the {channel} board. Its members ({members}) can read it. A"
            " message holds at most {max_bytes} bytes."
        ),
        "posted": "Posted message {id} to {channel}.",
        "too_long": (
            "The message is {bytes} bytes, over the {max_bytes} bytes the {channel} board takes."
            " Nothing was posted."
        ),
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {
        "description": frozenset({"channel", "members", "max_bytes"}),
        "posted": frozenset({"channel", "id"}),
        "too_long": frozenset({"channel", "bytes", "max_bytes"}),
    }

    def spec(self, config: object) -> ToolSpec:
        board = self.board
        description = self._format(
            "description", members=", ".join(board.members), max_bytes=board.config.max_bytes
        )
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The message."}},
            "required": ["text"],
            "additionalProperties": False,
        }
        return ToolSpec(name=self.name, description=description, parameters=parameters)

    async def run(self, ctx: ToolContext, args: PostArgs) -> ToolOutcome:
        try:
            message = await self.board.post(ctx.agent_id, args.text, call_id=ctx.call_id)
        except MessageTooLong as e:
            return ToolOutcome(self._format("too_long", bytes=e.size, max_bytes=e.limit), True)
        except ChannelError as e:
            return ToolOutcome(str(e), error=True)
        return ToolOutcome(self._format("posted", id=message.id))


class ReadTool(_BoardTool):
    args: ClassVar[type[Args]] = ReadArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "Read the messages posted to the {channel} board since you last read it, oldest first."
        ),
        "message": "Message {id} from {agent}:\n{text}",
        "none": "No new messages on {channel}.",
        "more": "{count} more new messages wait; call {tool} again to read them.",
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {
        "description": frozenset({"channel"}),
        "message": frozenset({"channel", "id", "agent", "text"}),
        "none": frozenset({"channel"}),
        "more": frozenset({"channel", "count", "tool"}),
    }

    def spec(self, config: object) -> ToolSpec:
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        return ToolSpec(
            name=self.name, description=self._format("description"), parameters=parameters
        )

    async def run(self, ctx: ToolContext, args: ReadArgs) -> ToolOutcome:
        try:
            messages, more = await self.board.read(ctx.agent_id, call_id=ctx.call_id)
        except ChannelError as e:
            return ToolOutcome(str(e), error=True)
        if not messages:
            return ToolOutcome(self._format("none"))
        parts = [self._format("message", id=m.id, agent=m.agent, text=m.text) for m in messages]
        if more:
            parts.append(self._format("more", count=more, tool=self.name))
        return ToolOutcome("\n\n".join(parts))


def resolve(cls: type[_BoardTool], name: str, texts: Mapping[str, str] | None) -> dict[str, str]:
    """The texts of board tool `name`: the defaults of `cls` with `texts` laid over them."""
    texts = dict(texts or {})
    unknown = sorted(set(texts) - set(cls.texts))
    if unknown:
        known = ", ".join(sorted(cls.texts))
        raise TextError(f"tool {name} has no text {', '.join(unknown)}; known: {known}")
    text = {**cls.texts, **texts}
    for key, allowed in cls.placeholders.items():
        check_format(text[key], allowed, f"tools.{name}.{key}")
    return text


class Board:
    kind: ClassVar[str] = "board"

    def __init__(self, setup: ChannelSetup) -> None:
        config = setup.config
        if not isinstance(config, BoardChannel):
            raise ChannelError(f"channel {config.name}: a board takes a board config")
        self.setup = setup
        self.config = config
        self.name = config.name
        self.members = setup.scenario.members(config)
        self.posters = setup.scenario.posters(config)
        self.messages: list[Message] = []
        self._read: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def tool_texts(
        config: ChannelConfig,
        scenario: Scenario,
        agent_id: str,
        layered: Mapping[str, Mapping[str, str]],
    ) -> dict[str, dict[str, str]]:
        """The resolved texts of the board tools `agent_id` gets, by tool name."""
        if not isinstance(config, BoardChannel) or agent_id not in scenario.members(config):
            return {}
        out = {config.read_tool: resolve(ReadTool, config.read_tool, layered.get(config.read_tool))}
        if agent_id not in config.read_only:
            post = config.post_tool
            out[post] = resolve(PostTool, post, layered.get(post))
        return out

    def info(self) -> ChannelInfo:
        return ChannelInfo(
            name=self.name,
            kind=self.kind,
            members=list(self.members),
            read_only=list(self.config.read_only),
        )

    def mounts(self, agent_id: str) -> Sequence[Mount]:
        return []

    def tools(self, agent_id: str) -> Sequence[Tool]:
        if agent_id not in self.members:
            return []
        texts = self.setup.texts.get(agent_id, {})
        c = self.config
        tools: list[_BoardTool] = [ReadTool(self, c.read_tool, texts.get(c.read_tool))]
        if agent_id in self.posters:
            tools.append(PostTool(self, c.post_tool, texts.get(c.post_tool)))
        return [cast("Tool", t) for t in tools]

    async def start(self, sandboxes: Mapping[str, AuditedSandbox]) -> None:
        return None

    async def close(self, sandboxes: Mapping[str, AuditedSandbox]) -> bytes | None:
        return None

    async def post(self, agent_id: str, text: str, *, call_id: str | None = None) -> ChannelMessage:
        """Post `text` as `agent_id`, logged as `channel.message` by that agent. A scripted
        actor posts with no `call_id`. Raises MessageTooLong past `max_bytes`, ChannelError
        when the agent may not post here."""
        if agent_id not in self.posters:
            raise ChannelError(f"agent {agent_id} cannot post to the {self.name} board")
        if not text:
            raise ChannelError("a message needs text")
        size = len(text.encode())
        if size > self.config.max_bytes:
            raise MessageTooLong(size, self.config.max_bytes)
        store, run_id = self.setup.store, self.setup.run_id
        async with self._lock:
            n = len(self.messages) + 1
            content = await store.content(run_id, text)
            message = ChannelMessage(
                channel=self.name, id=n, text=content, bytes=size, call_id=call_id
            )
            await store.append(run_id, [Draft(message, agent_id)])
            self.messages.append(Message(n, agent_id, text))
        return message

    async def read(
        self, agent_id: str, *, call_id: str | None = None, limit: int = READ_BYTES
    ) -> tuple[list[Message], int]:
        """The messages posted since `agent_id` last read, oldest first, as many as fit in
        `limit` bytes but at least one, and how many more wait; logged as `channel.read` by
        that agent. Raises ChannelError for an agent that is not a member."""
        if agent_id not in self.members:
            raise ChannelError(f"agent {agent_id} is not on the {self.name} board")
        store, run_id = self.setup.store, self.setup.run_id
        async with self._lock:
            after = self._read.get(agent_id, 0)
            new = self.messages[after:]
            shown: list[Message] = []
            size = 0
            for m in new:
                size += len(m.text.encode())
                if shown and size > limit:
                    break
                shown.append(m)
            through = shown[-1].id if shown else after
            read = ChannelRead(
                channel=self.name, after=after, through=through, count=len(shown), call_id=call_id
            )
            await store.append(run_id, [Draft(read, agent_id)])
            self._read[agent_id] = through
        return shown, len(new) - len(shown)
