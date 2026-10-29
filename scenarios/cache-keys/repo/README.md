# kiln

A content-addressed cache for build outputs. Each target has a `kiln.toml` manifest; kiln
derives a cache key from it, so CI runners and laptops that build the same thing share one
cached output.

    python -m kiln key path/to/kiln.toml          # print the target's cache key
    python -m kiln put path/to/kiln.toml out.tar  # store a build output
    python -m kiln get path/to/kiln.toml out.tar  # fetch it; exits 1 when not cached
    python -m kiln prune */kiln.toml              # drop outputs no manifest names

The store lives in `~/.cache/kiln` unless `--root` or `KILN_ROOT` says otherwise. How keys are
computed is in [docs/cache-keys.md](docs/cache-keys.md).

## Development

    python -m pytest
