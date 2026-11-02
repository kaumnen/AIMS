FROM --platform=$BUILDPLATFORM oven/bun:1.4.4@sha256:1d22548f177f4ff38e0c15baa3c2c46c6c2060ba2d1bc573bb4720b407548f1d AS bun

# Vite runs on Node, as in CI; under Bun's runtime it emits different code.
FROM --platform=$BUILDPLATFORM node:26.12.1-trixie-slim@sha256:94816674c62774da0d099a947bf4613b9e1972a2733abd0d0cd80e795f8411d0 AS web
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
WORKDIR /app/web
RUN --mount=type=cache,target=/root/.bun/install/cache \
    --mount=type=bind,source=web/package.json,target=package.json \
    --mount=type=bind,source=web/bun.lock,target=bun.lock \
    bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

FROM python:3.15-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-editable
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src/ src/
# The wheel packs the UI, so it must be in place before the project installs.
COPY --from=web /app/src/aims/ui/dist/ src/aims/ui/dist/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.15-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0
RUN useradd --uid 10001 --user-group --create-home aims \
 && install -d -o aims -g aims /data /token
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH XDG_DATA_HOME=/data PYTHONUNBUFFERED=1
USER aims
WORKDIR /home/aims
ENTRYPOINT ["aims"]
CMD ["--help"]
