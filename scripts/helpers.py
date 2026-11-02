"""The release's view of the sandbox helper images, so it names and builds them exactly as a
runner would.

`uv run python scripts/helpers.py names` prints the helper names.
`uv run python scripts/helpers.py context <name> <out>` writes the build context and prints the tag.
`uv run python scripts/helpers.py check` fails unless published.json pins every helper.
"""

import argparse
import re
import sys
from pathlib import Path

from aims.sandbox.images import ASSETS, PUBLISHED, context, published

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def names() -> list[str]:
    return sorted(node.name for node in ASSETS.iterdir() if node.is_dir())


def check() -> list[str]:
    pins = published()
    want = {context(name)[0] for name in names()}
    problems = [f"no pin for {tag}" for tag in sorted(want - pins.keys())]
    problems += [f"pin for {tag}, which no helper has" for tag in sorted(pins.keys() - want)]
    problems += [
        f"{tag} is pinned to {pins[tag]!r}"
        for tag in sorted(want & pins.keys())
        if not DIGEST.fullmatch(pins[tag])
    ]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("names")
    ctx = sub.add_parser("context")
    ctx.add_argument("name", choices=names())
    ctx.add_argument("out", type=Path)
    sub.add_parser("check")
    args = parser.parse_args()
    if args.command == "names":
        print("\n".join(names()))
    elif args.command == "context":
        tag, data = context(args.name)
        args.out.write_bytes(data)
        print(tag)
    else:
        problems = check()
        for problem in problems:
            print(f"{PUBLISHED}: {problem}", file=sys.stderr)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
