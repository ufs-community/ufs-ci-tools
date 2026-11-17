#!/usr/bin/env python3
"""Verify committed submodule pointers without changing the caller's checkout."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger("verify_submodules")
Status = Literal["OK", "OUT_OF_SYNC", "ERROR", "EXCLUDED"]


class VerificationError(Exception):
    """A configuration, Git, or checkout error that prevents verification."""


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: StrictStr | None = None
    commit: StrictStr | None = None
    tag: StrictStr | None = None
    remote: StrictStr | None = None
    exclude: StrictBool = False

    @field_validator("branch", "commit", "tag", "remote")
    @classmethod
    def validate_nonempty_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must be a nonempty string")
        return value

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
        ):
            raise ValueError("must be a full hexadecimal SHA")
        return value

    @field_validator("branch", "tag")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is not None and (
            value.startswith("-")
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError(f"invalid branch or tag: {value!r}")
        return value

    @field_validator("remote")
    @classmethod
    def validate_remote(cls, value: str | None) -> str | None:
        if value is not None and value.startswith("-"):
            raise ValueError("remote cannot start with '-'")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Rule:
        if self.commit and self.tag:
            raise ValueError("commit and tag are mutually exclusive")
        if self.exclude and any((self.branch, self.commit, self.tag, self.remote)):
            raise ValueError("excluded entries cannot set other options")
        return self


class SubmoduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submodules: dict[StrictStr, Rule]

    @field_validator("submodules")
    @classmethod
    def validate_submodule_paths(cls, value: dict[str, Rule]) -> dict[str, Rule]:
        for path in value:
            validate_path(path)
        return value


class Result(BaseModel):
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
    try:
        return SubmoduleConfig.model_validate(data).submodules
    except ValidationError as exc:
        raise VerificationError(f"Invalid configuration: {exc}") from exc


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
            result = Result(path=full_path, current_sha=sha)
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
                Result(
                    path=path,
                    current_sha="",
                    detail="Configured path was not discovered",
                )
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
            print(
                json.dumps(
                    [result.model_dump(mode="json") for result in results], indent=2
                )
            )
        if args.step_summary:
            write_summary(results, args.step_summary)
    except (VerificationError, OSError) as exc:
        logger.error("%s", exc)
        return 2
    return int(any(result.status not in {"OK", "EXCLUDED"} for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
