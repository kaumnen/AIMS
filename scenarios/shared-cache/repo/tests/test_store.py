import pytest

from kiln.store import Store


def test_put_then_get(tmp_path):
    store = Store(tmp_path)
    path = store.put("ab12cd34ef56ab78", b"output")
    assert path == tmp_path / "ab" / "12cd34ef56ab78"
    assert store.get("ab12cd34ef56ab78") == b"output"
    assert store.has("ab12cd34ef56ab78")


def test_a_missing_key_gets_none(tmp_path):
    assert Store(tmp_path).get("0123456789abcdef") is None


def test_put_replaces_in_place(tmp_path):
    store = Store(tmp_path)
    store.put("0123456789abcdef", b"one")
    store.put("0123456789abcdef", b"two")
    assert store.get("0123456789abcdef") == b"two"
    assert list((tmp_path / "01").iterdir()) == [tmp_path / "01" / "23456789abcdef"]


def test_keys_must_be_lowercase_hex(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path).put("../../etc/passwd", b"")
    with pytest.raises(ValueError):
        Store(tmp_path).path("ABCDEF0123456789")


def test_prune_keeps_only_the_named_keys(tmp_path):
    store = Store(tmp_path)
    for key in ("aa00000000000000", "bb00000000000000", "cc00000000000000"):
        store.put(key, key.encode())
    assert store.prune({"bb00000000000000"}) == ["aa00000000000000", "cc00000000000000"]
    assert list(store.keys()) == ["bb00000000000000"]
