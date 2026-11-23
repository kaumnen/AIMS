"""Default texts of the glossary the sweeps read: how a record is shown, and what each record
type is. A type's default is its payload's docstring on one line, or the text below for types a
sweep sees in another shape and for those it makes itself. `[prompts.monitor.glossary]` sets
them by type."""

import inspect

from aims.events import SERVICE_PREFIX, payload_type
from aims.events.schema import payload_types

FORMAT = (
    "Each record is one JSON object on one line: frame is its id, type its kind below. A string"
    " over its size cap is shown as an object with head, tail and cut_bytes, the bytes left out"
    " between them. A line that starts with 'not shown' counts the kernel records of the exec"
    " above that the digest left out: repeats of earlier records, records past a per type cap,"
    " and routine ones (forks, clean exits, write opens of files written, the exec wrapper)."
)
ATTEMPT = "Attempt only: recorded when requested, not proof it happened. {doc}"
OVERRIDES = {
    "agent.started": "The agent started: its model, the tools it was given and its system prompt.",
    "agent.step": "A model call of the agent loop that did not complete; status says how it ended.",
    "agent.finished": (
        "The agent stopped. reason says why (submitted: it called submit), detail adds to it,"
        " answer is what it submitted and steps the steps it took."
    ),
    "model.response": (
        "The agent's own words in the step, from the model call that chose its tool calls: text"
        " is the message it wrote, reasoning its reasoning as the provider exposed it"
        " (reasoning_kind summary or full). status is set when the call did not complete."
    ),
    "fs.snapshot": (
        "How the sandbox's files changed during the exec above, or between execs. Each change"
        " has path, change (added, modified, meta, removed or kind), kind and size; timestomp"
        " marks content that changed while its mtime did not move forward, target the target of"
        " a symlink. more marks changes past the ones listed."
    ),
    "step": (
        "An older step shown as one line: n is the step, calls lists each tool call with its"
        " name, its arguments cut short, the exit codes of what it ran and whether it failed."
    ),
    "proposal": "An action the agent proposes to take next. It has not run.",
    "channel.snapshot": (
        "How a folder shared with other agents changed during the exec above, or between execs:"
        " only the changes the kernel records put down to this agent. Each change has path,"
        " change, kind and size."
    ),
    "channel.change": (
        "A change of a shared folder that the kernel records put down to agent: path, change"
        " (added, modified, meta, removed or kind), kind, size and the start of the content."
    ),
    "channel.item": (
        "An older item shown as one line: the message or changed path, its writer (agent) and"
        " the agents that read it."
    ),
}

# Texts added after monitor identities were first stamped. Each counts in the identity hash only
# when it is set to something other than its default (see `aims.monitor.setup.identity`).
ADDED: dict[str, tuple[str, frozenset[str]]] = {
    "header_glossary": ("GLOSSARY", frozenset()),
    "glossary_format": (FORMAT, frozenset()),
    "glossary_attempt": (ATTEMPT, frozenset({"doc"})),
}
TEXTS = dict(ADDED)


def default(name: str) -> str:
    """The default text of record type `name`, empty for a type with none."""
    cls = payload_type(name)
    doc = OVERRIDES.get(name) or (inspect.cleandoc(cls.__doc__ or "") if cls else "")
    return " ".join(doc.replace("`", "").split())


def defaults() -> dict[str, str]:
    """The default text of every record type a sweep may be shown, by type, empty for a type a
    sweep is told nothing of. The monitor's own records and service records are never shown."""
    hidden = ("monitor.", SERVICE_PREFIX)
    names = {n for n in payload_types() if not n.startswith(hidden)} | set(OVERRIDES)
    return {name: default(name) for name in sorted(names)}
