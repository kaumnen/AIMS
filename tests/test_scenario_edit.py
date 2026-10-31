from pathlib import Path

import pytest

from aims.scenario import ScenarioError, parse
from aims.scenario.edit import PromptChange, set_prompts

ROOT = Path(__file__).parent.parent
FILES = sorted(
    [*ROOT.glob("scenarios/*/scenario.yaml"), *ROOT.glob("tests/fixtures/**/scenario.yaml")]
)

HEAD = (
    "version: 1\nname: x\noversight: observe\ntask:\n  instruction: hi\n  environment: {image: x}\n"
)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name)
def test_set_then_unset_gives_back_the_file(path: Path) -> None:
    text = path.read_text()
    agent = parse(text).agents[0].id
    changes = [
        PromptChange(agent, ("system",), "Be careful.\nAsk first."),
        PromptChange(agent, ("tools", "bash", "description"), "Run a command."),
    ]
    edited = set_prompts(text, changes)
    prompts = parse(edited).agents[0].prompts
    assert prompts is not None
    assert prompts.system == "Be careful.\nAsk first."
    assert prompts.tools == {"bash": {"description": "Run a command."}}
    undo = [PromptChange(c.agent, c.key, None) for c in changes]
    assert set_prompts(edited, undo) == text


def test_comments_and_blank_lines_stay_where_they_were() -> None:
    text = (
        HEAD + "agents:\n  - id: a\n    tools: [bash]  # only bash\n\n# plants next\nplants: []\n"
    )
    edited = set_prompts(text, [PromptChange("a", ("nudge",), "go")])
    assert edited == (
        HEAD + "agents:\n  - id: a\n    tools: [bash]  # only bash\n    prompts:\n"
        "      nudge: go\n\n# plants next\nplants: []\n"
    )
    assert set_prompts(edited, [PromptChange("a", ("nudge",), None)]) == text


def test_a_flow_agent_becomes_a_block_and_keeps_its_comment() -> None:
    text = HEAD + "agents:\n  - {id: a}  # first\n  - id: b\n"
    edited = set_prompts(text, [PromptChange("a", ("nudge",), "go")])
    assert (
        edited == HEAD + "agents:\n  - id: a    # first\n    prompts:\n      nudge: go\n  - id: b\n"
    )


def test_the_default_agent_is_written_out_when_agents_is_missing() -> None:
    edited = set_prompts(HEAD, [PromptChange("agent", ("nudge",), "go")])
    assert edited == HEAD + "agents:\n  - id: agent\n    prompts:\n      nudge: go\n"
    assert set_prompts(HEAD, [PromptChange("agent", ("nudge",), None)]) == HEAD


def test_unknown_agents_and_keys_are_refused() -> None:
    text = HEAD + "agents:\n  - id: a\n"
    with pytest.raises(ValueError, match="no agent b"):
        set_prompts(text, [PromptChange("b", ("nudge",), "go")])
    for key in [("monitor",), ("tools", "bash"), ("nope",), ()]:
        with pytest.raises(ValueError, match="not an agent text"):
            set_prompts(text, [PromptChange("a", key, "go")])
    with pytest.raises(ScenarioError):
        set_prompts("a: [", [PromptChange("a", ("nudge",), "go")])
