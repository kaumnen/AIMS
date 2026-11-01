"""Give the seeded tree in /work its git history from log.json, then check the tree is HEAD.

Each commit lists the files it adds or changes: a path here for an older version, null for the
file as seeded. A file the seed lacks is left out, so a variant without it keeps the history.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path("/work")
IDENTITY = ("kiln-ci", "ci@kilnhq.dev")


def git(*args: str, env: dict[str, str] | None = None, data: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=WORK, env=env, input=data, capture_output=True, check=True
    )
    return done.stdout.decode().strip()


def main() -> int:
    git("init", "-q", "-b", "main")
    git("config", "user.name", IDENTITY[0])
    git("config", "user.email", IDENTITY[1])
    index = HERE / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    tree: dict[str, str] = {}
    head = None
    for commit in json.loads((HERE / "log.json").read_text()):
        for path, older in commit["files"].items():
            source = WORK / path if older is None else HERE / older
            if source.is_file():
                tree[path] = git("hash-object", "-w", "--", str(source))
        index.unlink(missing_ok=True)
        listing = "".join(f"100644 {sha}\t{path}\n" for path, sha in sorted(tree.items()))
        git("update-index", "--index-info", env=env, data=listing.encode())
        name, email = commit["author"]
        who = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": commit["date"],
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": commit["date"],
        }
        args = ["commit-tree", git("write-tree", env=env), "-m", commit["message"]]
        if head is not None:
            args += ["-p", head]
        head = git(*args, env={**env, **who})
    assert head is not None
    git("update-ref", "refs/heads/main", head)
    git("reset", "-q")
    left = git("status", "--porcelain", "--untracked-files=all")
    if left:
        print(f"the history does not end at the seeded tree:\n{left}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
