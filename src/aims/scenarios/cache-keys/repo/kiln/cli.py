"""The kiln command: `key`, `put`, `get` and `prune`."""

import argparse
import os
import sys
from pathlib import Path

from kiln.fingerprint import fingerprint
from kiln.manifest import ManifestError, load
from kiln.store import Store

DEFAULT_ROOT = "~/.cache/kiln"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kiln")
    parser.add_argument("--root", default=os.environ.get("KILN_ROOT", DEFAULT_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("key", help="print the cache key of a manifest")
    key.add_argument("manifest")
    put = sub.add_parser("put", help="store a build output under its manifest's key")
    put.add_argument("manifest")
    put.add_argument("output")
    get = sub.add_parser("get", help="fetch a build output by its manifest's key")
    get.add_argument("manifest")
    get.add_argument("output")
    prune = sub.add_parser("prune", help="remove outputs no given manifest names")
    prune.add_argument("manifests", nargs="+")
    args = parser.parse_args(argv)

    store = Store(Path(args.root).expanduser())
    try:
        if args.command == "key":
            print(fingerprint(load(args.manifest)))
        elif args.command == "put":
            print(store.put(fingerprint(load(args.manifest)), Path(args.output).read_bytes()))
        elif args.command == "get":
            data = store.get(fingerprint(load(args.manifest)))
            if data is None:
                print("kiln: not cached", file=sys.stderr)
                return 1
            Path(args.output).write_bytes(data)
        else:
            keep = {fingerprint(load(m)) for m in args.manifests}
            for k in store.prune(keep):
                print(k)
    except ManifestError as e:
        print(f"kiln: {e}", file=sys.stderr)
        return 2
    return 0
