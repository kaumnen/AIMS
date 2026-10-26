# AIMS

Autonomous Intelligence Monitoring System.

AIMS is a bench for alignment experiments. Model agents work on tasks in isolated sandboxes, and a separate model watches every step and flags misbehaviour.

## Development

Requires uv, Bun and Docker.

```sh
uv sync
uv run pytest

cd web
bun install
bun run dev
bun run build  # writes the UI into src/aims/ui/dist
```

## License

Apache-2.0
