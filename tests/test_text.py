"""Lone surrogates in outside text, written as visible escapes."""

import json

import rfc8785

from aims.text import scrub


def test_a_lone_surrogate_becomes_its_escape() -> None:
    assert scrub("echo \ud800 hi") == "echo \\ud800 hi"
    assert scrub("\udfff") == "\\udfff"
    assert scrub(json.loads('"a\\uDC00b"')) == "a\\udc00b"


def test_a_valid_pair_and_plain_text_are_left_as_they_are() -> None:
    pair = json.loads('"\\ud83d\\ude00"')
    assert pair == "\U0001f600"
    assert scrub(pair) == pair
    plain = "plain é ü 日本 \\ud800"
    assert scrub(plain) is plain


def test_strings_and_keys_of_a_parsed_value_are_scrubbed_in_place() -> None:
    value = json.loads(
        '{"a": "\\ud800", "\\udc01": [1, 2.5, true, null, "\\ud83d", {"k": ["\\udbff"]}]}'
    )
    assert scrub(value) is value
    assert value == {"a": "\\ud800", "\\udc01": [1, 2.5, True, None, "\\ud83d", {"k": ["\\udbff"]}]}
    assert list(value) == ["a", "\\udc01"]
    rfc8785.dumps(value)


def test_other_values_pass_untouched() -> None:
    for value in (None, True, 3, 2.5, [1, None], {"n": 1}):
        assert scrub(value) == value
    assert scrub({1: "\ud800"}) == {1: "\\ud800"}


def test_scrub_is_idempotent() -> None:
    once = scrub(json.loads('{"\\ud800": ["\\udfff x"]}'))
    assert scrub(json.loads(json.dumps(once))) == once


def test_any_depth_json_parses_is_walked() -> None:
    depth = 50_000
    value = json.loads("[" * depth + '"\\ud800"' + "]" * depth)
    scrub(value)
    for _ in range(depth):
        value = value[0]
    assert value == "\\ud800"
