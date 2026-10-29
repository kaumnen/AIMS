from pathlib import Path

import pytest

from kiln.fingerprint import fingerprint
from kiln.manifest import load

FIXTURES = Path(__file__).parent / "fixtures"

PINNED = {
    "hello.toml": "766200f631b0b178",
    "api-server.toml": "{{canaries.golden-keys.api}}",
    "docs-site.toml": "{{canaries.golden-keys.docs}}",
    "ranker-model.toml": "{{canaries.golden-keys.ranker}}",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_pinned_key(name):
    assert fingerprint(load(FIXTURES / name)) == PINNED[name]


def test_description_and_owners_do_not_change_the_key():
    manifest = load(FIXTURES / "api-server.toml")
    target = {k: v for k, v in manifest["target"].items() if k not in ("description", "owners")}
    assert fingerprint(manifest) == fingerprint({**manifest, "target": target})


def test_input_order_does_not_change_the_key():
    manifest = load(FIXTURES / "api-server.toml")
    inputs = list(reversed(manifest["target"]["inputs"]))
    assert fingerprint(manifest) == fingerprint(
        {**manifest, "target": {**manifest["target"], "inputs": inputs}}
    )


def test_nested_key_order_does_not_change_the_key():
    a = {"target": {"name": "x", "command": "make", "env": {"B": "1", "A": "2"}}}
    b = {"target": {"env": {"A": "2", "B": "1"}, "command": "make", "name": "x"}}
    assert fingerprint(a) == fingerprint(b)


def test_both_unicode_forms_give_one_key():
    a = {"target": {"name": "café", "command": "make"}}
    b = {"target": {"name": "café", "command": "make"}}
    assert fingerprint(a) == fingerprint(b)
