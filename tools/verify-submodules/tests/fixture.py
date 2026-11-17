"""Local Git repositories shared by pytest and the Action integration job."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=repo,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def commit(repo: Path, message: str = "fixture") -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def init(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    (path / "content.txt").write_text("initial\n")
    commit(path)
    return path


def add_module(parent: Path, remote: Path, path: str) -> None:
    git(
        parent,
        "submodule",
        "add",
        "-q",
        "-b",
        "main",
        "--name",
        f"named.{remote.name}",
        str(remote),
        path,
    )
    commit(parent, f"Add {path}")


def create_fixture(base: Path) -> Path:
    leaf = init(base / "leaf")
    git(leaf, "tag", "lightweight")
    git(leaf, "tag", "-am", "release", "annotated")
    middle = init(base / "middle")
    add_module(middle, leaf, "leaf with spaces")
    outer = init(base / "outer")
    add_module(outer, middle, "nested/middle")
    root = init(base / "root")
    add_module(root, outer, "vendor/outer")
    git(root, "submodule", "update", "--init", "--recursive")
    return root


if __name__ == "__main__":
    create_fixture(Path(sys.argv[1]).resolve())
