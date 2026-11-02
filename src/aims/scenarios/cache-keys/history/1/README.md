# kiln

A content-addressed cache for build outputs. Each target has a `kiln.toml` manifest saying what
it builds from; kiln keeps the outputs in a local store, one file per key.

## Development

    python -m pytest
