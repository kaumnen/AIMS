import pytest

from aims.server.codings import allowed


@pytest.mark.parametrize(
    ("header", "want"),
    [
        (None, []),
        ("", []),
        ("gzip, deflate, br, zstd", ["br", "gzip"]),
        ("gzip", ["gzip"]),
        ("GZIP;Q=0.5, Br", ["br", "gzip"]),
        ("br;q=0, gzip", ["gzip"]),
        ("br;q=0.0, gzip;q=0.001", ["gzip"]),
        ("br;q=nope, gzip", ["gzip"]),
        ("*", ["br", "gzip"]),
        ("*;q=0, gzip", ["gzip"]),
        ("br;q=0, *", ["gzip"]),
        ("identity", []),
        (" , ;q=1, gzip ; q=1 ", ["gzip"]),
    ],
)
def test_a_header_allows_the_codings_it_names_with_weight(
    header: str | None, want: list[str]
) -> None:
    assert allowed(header, ("br", "gzip")) == want
