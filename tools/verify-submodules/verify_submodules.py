#!/usr/bin/env python3
"""Verify committed submodule pointers without changing the caller's checkout."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

logger = logging.getLogger("verify_submodules")
Status = Literal["OK", "OUT_OF_SYNC", "ERROR", "EXCLUDED"]


class VerificationError(Exception):
    """A configuration, Git, or checkout error that prevents verification."""


@dataclass(frozen=True)
class Rule:
    branch: str | None = None
    commit: str | None = None
    tag: str | None = None
    remote: str | None = None
    exclude: bool = False


@dataclass
class Result:
    path: str
    current_sha: str
    expected_sha: str | None = None
    target: str = ""
    status: Status = "ERROR"
    detail: str = ""


def git(repo: Path, *args: str, missing_ok: bool = False) -> str:
    """Run Git with bounded, noninteractive network operations."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"Git operation failed: {exc}") from exc
    if completed.returncode and not (missing_ok and completed.returncode == 1):
        raise VerificationError(
            f"git {args[0]} failed: {completed.stderr.strip() or 'no diagnostic'}"
        )
    return completed.stdout


def validate_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise VerificationError(
            f"Invalid repository-relative submodule path: {value!r}"
        )


class ConfigLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects duplicate mapping keys."""


def unique_mapping(
    loader: ConfigLoader, node: yaml.MappingNode
) -> dict[object, object]:
    pairs = loader.construct_pairs(node)
    result: dict[object, object] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise VerificationError("Configuration keys must be strings")
        if key in result:
            raise VerificationError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


ConfigLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def load_config(path: Path | None) -> dict[str, Rule]:
    if path is None:
        return {}
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=ConfigLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise VerificationError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"submodules"}:
        raise VerificationError(
            "Configuration must contain only a 'submodules' mapping"
        )
    modules = data["submodules"]
    if not isinstance(modules, dict):
        raise VerificationError("'submodules' must be a mapping")
    rules: dict[str, Rule] = {}
    fields = {"branch", "commit", "tag", "remote", "exclude"}
    for name, options in modules.items():
        if not isinstance(name, str):
            raise VerificationError("Submodule paths must be strings")
        validate_path(name)
        if not isinstance(options, dict) or set(options) - fields:
            raise VerificationError(f"Invalid options for {name}; allowed: {fields}")
        for key, value in options.items():
            if key == "exclude":
                if type(value) is not bool:
                    raise VerificationError(f"{name}: exclude must be a boolean")
            elif not isinstance(value, str) or not value.strip():
                raise VerificationError(f"{name}: {key} must be a nonempty string")
        rule = Rule(**options)
        if rule.commit and rule.tag:
            raise VerificationError(f"{name}: commit and tag are mutually exclusive")
        if rule.exclude and set(options) != {"exclude"}:
            raise VerificationError(
                f"{name}: excluded entries cannot set other options"
            )
        if rule.commit and not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", rule.commit
        ):
            raise VerificationError(f"{name}: commit must be a full hexadecimal SHA")
        for ref in (rule.branch, rule.tag):
            if ref and (
                ref.startswith("-")
                or any(char.isspace() or ord(char) < 32 for char in ref)
            ):
                raise VerificationError(f"{name}: invalid branch or tag: {ref!r}")
        if rule.remote and rule.remote.startswith("-"):
            raise VerificationError(f"{name}: remote cannot start with '-'")
        rules[name] = rule
    return rules


def committed_modules(repo: Path) -> list[tuple[str, str, str | None]]:
    entries = git(repo, "ls-tree", "-rz", "HEAD").split("\0")
    pointers: dict[str, str] = {}
    for entry in filter(None, entries):
        metadata, path = entry.split("\t", 1)
        mode, _, sha = metadata.split()
        if mode == "160000":
            validate_path(path)
            pointers[path] = sha
    if not pointers and not git(repo, "ls-tree", "HEAD", "--", ".gitmodules").strip():
        return []
    config = git(
        repo,
        "config",
        "--null",
        "--blob",
        "HEAD:.gitmodules",
        "--get-regexp",
        r"^submodule\..*\.(path|branch)$",
        missing_ok=True,
    )
    properties: dict[str, dict[str, str]] = {}
    for entry in filter(None, config.split("\0")):
        key, value = entry.split("\n", 1)
        section, prop = key.rsplit(".", 1)
        properties.setdefault(section, {})[prop] = value
    by_path: dict[str, str | None] = {}
    for values in properties.values():
        if "path" in values:
            path = values["path"]
            if path in by_path:
                raise VerificationError(f"Duplicate .gitmodules path: {path}")
            by_path[path] = values.get("branch")
    if set(by_path) != set(pointers):
        raise VerificationError("Committed .gitmodules paths do not match gitlinks")
    return [(path, sha, by_path[path]) for path, sha in sorted(pointers.items())]


def resolve_expected(
    repo: Path, remote: str, rule: Rule, branch: str
) -> tuple[str, str]:
    if rule.commit:
        # A pinned object need not appear in ls-remote or in a shallow checkout.
        return rule.commit.lower(), f"commit {rule.commit}"
    ref = f"refs/tags/{rule.tag}" if rule.tag else f"refs/heads/{branch}"
    git(repo, "check-ref-format", ref)
    try:
        output = git(repo, "ls-remote", "--exit-code", "--", remote, ref, f"{ref}^{{}}")
    except VerificationError as exc:
        raise VerificationError(
            f"Cannot resolve {ref} from configured remote: {exc}"
        ) from exc
    refs = dict(line.split("\t", 1)[::-1] for line in output.splitlines())
    expected = refs.get(f"{ref}^{{}}") or refs.get(ref)
    if expected is None:
        raise VerificationError(f"Could not resolve {ref} on {remote}")
    return expected, f"tag {rule.tag}" if rule.tag else f"branch {branch}"


def verify(repo: Path, target_branch: str, rules: dict[str, Rule]) -> list[Result]:
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != repo.resolve():
        raise VerificationError("--repo-root must be the repository root")
    git(root, "check-ref-format", f"refs/heads/{target_branch}")
    results: list[Result] = []
    seen: set[str] = set()
    excluded: set[str] = set()

    def visit(parent: Path, prefix: str = "") -> None:
        for path, sha, configured_branch in committed_modules(parent):
            full_path = f"{prefix}/{path}" if prefix else path
            seen.add(full_path)
            rule = rules.get(full_path, Rule())
            result = Result(full_path, sha)
            results.append(result)
            if rule.exclude:
                excluded.add(full_path)
                result.status = "EXCLUDED"
                result.detail = "Explicitly excluded, including descendants"
                continue
            child = parent / path
            try:
                if not child.resolve().is_relative_to(root):
                    raise VerificationError("Submodule path escapes the repository")
                checkout_root = git(child, "rev-parse", "--show-toplevel").strip()
                if Path(checkout_root).resolve() != child.resolve():
                    raise VerificationError(
                        "Uninitialized submodule; "
                        "run submodule update --init --recursive"
                    )
                if git(child, "rev-parse", "HEAD").strip() != sha:
                    raise VerificationError(
                        "Checked-out HEAD differs from committed gitlink"
                    )
                branch = rule.branch or configured_branch or target_branch
                if branch == ".":
                    branch = target_branch
                if rule.commit:
                    remote = ""
                else:
                    remote = (
                        rule.remote or git(child, "remote", "get-url", "origin").strip()
                    )
                result.expected_sha, result.target = resolve_expected(
                    child, remote, rule, branch
                )
                result.status = "OK" if sha == result.expected_sha else "OUT_OF_SYNC"
                result.detail = (
                    "Committed pointer matches expected revision"
                    if result.status == "OK"
                    else "Committed pointer differs from expected revision"
                )
            except VerificationError as exc:
                result.detail = str(exc)
            # Only inspect descendants of the committed parent checkout.
            if result.expected_sha is not None:
                try:
                    visit(child, full_path)
                except VerificationError as exc:
                    result.status = "ERROR"
                    result.detail = str(exc)

    visit(root)
    for path in sorted(set(rules) - seen):
        if not any(path.startswith(f"{item}/") for item in excluded):
            results.append(
                Result(path, "", detail="Configured path was not discovered")
            )
    return results


def write_summary(results: list[Result], path: Path) -> None:
    def escape(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("|", "&#124;")
            .replace("\n", " ")
            .replace("\r", " ")
        )

    rows = [
        "## Submodule verification",
        "",
        "| Path | Current SHA | Expected SHA | Target | Status | Details |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        values = (
            result.path,
            result.current_sha,
            result.expected_sha or "-",
            result.target,
            result.status,
            result.detail,
        )
        rows.append("| " + " | ".join(escape(value) for value in values) + " |")
    if not results:
        rows.append("\nNo submodules found.")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(rows) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--target-branch", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true", help="Write results to stdout")
    parser.add_argument("--step-summary", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        results = verify(
            args.repo_root.resolve(), args.target_branch, load_config(args.config)
        )
        for result in results:
            log = logger.info if result.status in {"OK", "EXCLUDED"} else logger.error
            log("%s: %s: %s", result.path, result.status, result.detail)
        if not results:
            logger.info("No submodules found")
        if args.json:
            print(json.dumps([asdict(result) for result in results], indent=2))
        if args.step_summary:
            write_summary(results, args.step_summary)
    except (VerificationError, OSError) as exc:
        logger.error("%s", exc)
        return 2
    return int(any(result.status not in {"OK", "EXCLUDED"} for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
