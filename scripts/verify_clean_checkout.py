#!/usr/bin/env python3
"""Run the tracked pytest suite from a Git archive, never this worktree.

The verifier deliberately uses ``git archive HEAD`` rather than copying the
working tree.  Ignored captures, editor state, and untracked tests therefore
cannot make a green result that a fresh checkout cannot reproduce.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def command(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT)


def tracked_tests(root: Path) -> set[str]:
    listing = command("git", "ls-tree", "-r", "--name-only", "HEAD", "--", "tests", cwd=root)
    tests = {line for line in listing.splitlines() if line.startswith("tests/")}
    if not any(path.endswith(".py") for path in tests):
        raise RuntimeError("HEAD does not contain tracked Python tests")
    return tests


def archive_head(root: Path, destination: Path) -> None:
    archive = subprocess.check_output(("git", "archive", "--format=tar", "HEAD"), cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        for member in members:
            relative = Path(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError(f"unsafe git archive member: {member.name!r}")
        # Python 3.11 lacks TarFile.extractall(filter=...).  The explicit
        # member validation above gives the same archive-path safety property.
        tar.extractall(destination, members=members)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python executable used to run pytest")
    args = parser.parse_args()

    root = Path(command("git", "rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    expected = tracked_tests(root)
    with tempfile.TemporaryDirectory(prefix="ha-glovo-clean-") as directory:
        checkout = Path(directory)
        archive_head(root, checkout)
        actual = {path.relative_to(checkout).as_posix() for path in (checkout / "tests").rglob("*") if path.is_file()}
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise RuntimeError(f"archive test parity failure: missing={missing!r} extra={extra!r}")
        if (checkout / ".git").exists():
            raise RuntimeError("clean archive unexpectedly contains Git worktree metadata")
        result = subprocess.run(
            (args.python, "-m", "pytest", "-q"), cwd=checkout, text=True, check=False
        )
        if result.returncode:
            return result.returncode
        print(f"clean checkout passed: {len(expected)} tracked test files from git archive HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
