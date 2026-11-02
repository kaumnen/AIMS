# AIMS

Autonomous Intelligence Monitoring System.

AIMS is a bench for alignment experiments. Model agents work on tasks in isolated sandboxes. A separate model watches every step and flags misbehaviour.

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

## Security

> [!WARNING]
> A runner has root on its host through the Docker socket. It also starts a privileged kernel tracer. Put runners on a dedicated machine or VM.

Pass `--audit snapshot` to run without the tracer. [SECURITY.md](SECURITY.md) explains the security model. It also says how to report a vulnerability.

## License

Apache-2.0
