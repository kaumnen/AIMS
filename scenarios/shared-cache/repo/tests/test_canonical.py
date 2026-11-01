import pytest

from kiln.canonical import canonical


def test_keys_are_sorted_at_every_level():
    value = {"b": 1, "a": {"d": [{"f": 1, "e": 2}], "c": 3}}
    assert canonical(value) == b'{"a":{"c":3,"d":[{"e":2,"f":1}]},"b":1}'


def test_there_is_no_whitespace_between_tokens():
    assert canonical({"a": [1, 2], "b": "x y"}) == b'{"a":[1,2],"b":"x y"}'


def test_text_is_written_as_utf8_not_escaped():
    assert canonical({"name": "café"}) == '{"name":"café"}'.encode()


def test_text_is_nfc_normalized():
    assert canonical({"café": "café"}) == '{"café":"café"}'.encode()


def test_integral_floats_are_written_as_integers():
    assert canonical({"jobs": 4.0, "ratio": 0.5}) == b'{"jobs":4,"ratio":0.5}'


def test_nan_is_refused():
    with pytest.raises(ValueError):
        canonical({"x": float("nan")})
