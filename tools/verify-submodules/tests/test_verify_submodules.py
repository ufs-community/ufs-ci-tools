from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fixture import add_module, commit, create_fixture, git, init

from verify_submodules import (
    Result,
    Rule,
    VerificationError,
    load_config,
    main,
    verify,
    write_summary,
)

LEAF = "vendor/outer/nested/middle/leaf with spaces"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return create_fixture(tmp_path)


def test_recursive_committed_pointers_and_named_submodules(repo: Path) -> None:
    results = verify(repo, "unused-fallback", {})
    assert [item.path for item in results] == [
        "vendor/outer",
        "vendor/outer/nested/middle",
        LEAF,
    ]
    assert all(item.status == "OK" for item in results)
    assert all(item.current_sha == item.expected_sha for item in results)


@pytest.mark.parametrize("tag", ["lightweight", "annotated"])
def test_tag_pins(repo: Path, tag: str) -> None:
    results = verify(repo, "main", {LEAF: Rule(tag=tag)})
    assert results[-1].status == "OK"
    assert results[-1].target == f"tag {tag}"


def test_branch_drift_and_full_commit_pin(repo: Path) -> None:
    remote = repo.parent / "leaf"
    pinned = git(remote, "rev-parse", "HEAD")
    (remote / "content.txt").write_text("new\n")
    commit(remote)
    assert verify(repo, "main", {})[-1].status == "OUT_OF_SYNC"
    assert verify(repo, "main", {LEAF: Rule(commit=pinned)})[-1].status == "OK"
    assert (
        verify(repo, "main", {LEAF: Rule(commit="f" * 40)})[-1].status == "OUT_OF_SYNC"
    )
    assert verify(repo, "main", {LEAF: Rule(tag="absent")})[-1].status == "ERROR"


def test_default_branch_and_yaml_override(repo: Path) -> None:
    git(repo, "config", "-f", ".gitmodules", "--unset", "submodule.named.outer.branch")
    commit(repo)
    assert verify(repo, "main", {})[0].status == "OK"
    assert verify(repo, "absent", {})[0].status == "ERROR"
    assert (
        verify(repo, "absent", {"vendor/outer": Rule(branch="main")})[0].status == "OK"
    )
    git(repo, "config", "-f", ".gitmodules", "submodule.named.outer.branch", ".")
    commit(repo)
    assert verify(repo, "main", {})[0].status == "OK"


def test_explicit_branch_wins_over_gitmodules(repo: Path) -> None:
    results = verify(repo, "main", {LEAF: Rule(branch="absent")})
    assert results[-1].status == "ERROR"


def test_exclusion_skips_descendants_and_unknown_paths_fail(repo: Path) -> None:
    results = verify(
        repo,
        "main",
        {"vendor/outer": Rule(exclude=True), LEAF: Rule(tag="absent")},
    )
    assert len(results) == 1
    assert results[0].status == "EXCLUDED"
    assert verify(repo, "main", {"typo": Rule()})[-1].status == "ERROR"


def test_uninitialized_checkout_is_not_silently_skipped(repo: Path) -> None:
    git(repo, "submodule", "deinit", "-f", "--", "vendor/outer")
    result = verify(repo, "main", {})[0]
    assert result.status == "ERROR"
    assert "Uninitialized" in result.detail


def test_checkout_head_does_not_replace_committed_gitlink(repo: Path) -> None:
    child = repo / LEAF
    git(child, "config", "user.name", "Fixture")
    git(child, "config", "user.email", "fixture@example.invalid")
    (child / "content.txt").write_text("local\n")
    new_head = commit(child)
    result = verify(repo, "main", {LEAF: Rule(commit=new_head)})[-1]
    assert result.status == "ERROR"
    assert result.current_sha != new_head
    assert "differs from committed gitlink" in result.detail


def test_canonical_remote_override_and_inaccessible_remote(repo: Path) -> None:
    git(repo / LEAF, "remote", "set-url", "origin", str(repo.parent / "absent"))
    assert verify(repo, "main", {})[-1].status == "ERROR"
    results = verify(repo, "main", {LEAF: Rule(remote=str(repo.parent / "leaf"))})
    assert results[-1].status == "OK"


def test_committed_metadata_ignores_worktree_branch_edits(repo: Path) -> None:
    git(repo, "config", "-f", ".gitmodules", "submodule.named.outer.branch", "absent")
    assert verify(repo, "main", {})[0].status == "OK"


def test_missing_committed_metadata_fails(repo: Path) -> None:
    git(repo, "rm", ".gitmodules")
    commit(repo)
    with pytest.raises(VerificationError, match=".gitmodules paths do not match"):
        verify(repo, "main", {})


def test_declared_but_uncommitted_submodule_is_not_silently_skipped(
    tmp_path: Path,
) -> None:
    root = init(tmp_path / "root")
    (root / ".gitmodules").write_text(
        '[submodule "missing"]\npath = missing\nurl = /does/not/exist\n'
    )
    commit(root)
    with pytest.raises(VerificationError, match=".gitmodules paths do not match"):
        verify(root, "main", {})


@pytest.mark.parametrize(
    "content",
    [
        "",
        "other: {}",
        "submodules: []",
        "submodules: {../escape: {}}",
        "submodules: {/absolute: {}}",
        "submodules: {a//b: {}}",
        "submodules: {a: {unknown: x}}",
        "submodules: {a: {commit: abc}}",
        "submodules: {a: {commit: 123}}",
        "submodules: {a: {commit: '" + "a" * 40 + "', tag: v1}}",
        "submodules: {a: {exclude: 'yes'}}",
        "submodules: {a: {exclude: true, branch: main}}",
        "submodules: {a: {branch: ''}}",
        "submodules: {a: {tag: '-bad'}}",
        "submodules: {a: {remote: '-bad'}}",
        "submodules: {a: {}, a: {}}",
        "submodules: {a: {branch: main, branch: other}}",
        "submodules: {a: !!python/object:builtins.object {}}",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content)
    with pytest.raises(VerificationError):
        load_config(config)


def test_valid_config_and_missing_file(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "submodules:\n"
        "  vendor/outer:\n"
        "    branch: main\n"
        "    remote: https://example.invalid/upstream\n"
        f"  '{LEAF}':\n"
        "    tag: annotated\n"
    )
    rules = load_config(config)
    assert rules[LEAF].tag == "annotated"
    assert rules["vendor/outer"].branch == "main"
    assert load_config(None) == {}
    with pytest.raises(VerificationError, match="Cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_shallow_checkout_can_use_nonadvertised_commit_pin(tmp_path: Path) -> None:
    remote = init(tmp_path / "remote")
    pinned = git(remote, "rev-parse", "HEAD")
    (remote / "content.txt").write_text("new\n")
    commit(remote)
    root = init(tmp_path / "root")
    add_module(root, remote, "child")
    git(root / "child", "checkout", pinned)
    commit(root)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth=1", root.as_uri(), str(clone))
    git(clone, "submodule", "update", "--init", "--depth=1")
    assert verify(clone, "main", {"child": Rule(commit=pinned)})[0].status == "OK"


def test_cli_exit_codes_json_and_summary(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    args = [
        "--repo-root",
        str(repo),
        "--target-branch",
        "main",
        "--json",
        "--step-summary",
        str(summary),
    ]
    assert main(args) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
    assert LEAF in summary.read_text()
    git(repo / LEAF, "remote", "set-url", "origin", str(tmp_path / "missing"))
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)[-1]["status"] == "ERROR"
    assert main([*args, "--config", str(tmp_path / "missing.yaml")]) == 2
    assert main([*args, "--repo-root", str(tmp_path / "missing-repo")]) == 2


def test_empty_repository_and_summary_escaping(tmp_path: Path) -> None:
    root = init(tmp_path / "empty")
    assert verify(root, "main", {}) == []
    summary = tmp_path / "summary.md"
    summary.write_text("Existing summary\n")
    write_summary(
        [Result(path="a|b", current_sha="", detail="<script>\nfailure")], summary
    )
    text = summary.read_text()
    assert text.startswith("Existing summary\n")
    assert "a&#124;b" in text
    assert "&lt;script&gt; failure" in text


def test_git_timeout_becomes_visible_error(repo: Path) -> None:
    with patch(
        "verify_submodules.subprocess.run",
        side_effect=subprocess.TimeoutExpired("git", 60),
    ):
        with pytest.raises(VerificationError, match="Git operation failed"):
            verify(repo, "main", {})


def test_cli_help() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "verify_submodules.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--target-branch" in completed.stdout
