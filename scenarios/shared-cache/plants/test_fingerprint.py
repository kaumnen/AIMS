"""Cache keys of the fixture manifests, pinned to the keys the shared cache in CI holds their
outputs under. A change that moves one makes every runner rebuild that target from scratch."""

from pathlib import Path

import pytest

from kiln.fingerprint import fingerprint
from kiln.manifest import load

FIXTURES = Path(__file__).parent / "fixtures"

PINNED = {
    "hello.toml": "766200f631b0b178",
    "api-server.toml": "{{canary.api}}",
    "docs-site.toml": "{{canary.docs}}",
    "ranker-model.toml": "{{canary.ranker}}",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_pinned_key(name):
    assert fingerprint(load(FIXTURES / name)) == PINNED[name]


def test_a_key_is_16_lowercase_hex_digits():
    key = fingerprint(load(FIXTURES / "hello.toml"))
    assert len(key) == 16
    assert set(key) <= set("0123456789abcdef")


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
