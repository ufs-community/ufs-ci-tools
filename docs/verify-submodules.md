# Submodule verification

This tool checks committed Git submodule pointers, including arbitrarily nested
submodules, against full commit SHAs, tags, or remote branch heads. It does not
update submodule pointers, fetch objects into the checkout, or change Git config.

The design builds on CECE's
[`verify_submodules.py`](https://github.com/ufs-community/CECE/blob/develop/scripts/verify_submodules.py)
and its test scenarios, with repository-specific policy moved to configuration.
CECE and UFS-WM migrations are separate from implementing this tool.

## Requirements

- Git and [uv](https://docs.astral.sh/uv/).
- Python 3.12 or newer; uv can provision Python when needed.
- A consumer checkout initialized with `git submodule update --init --recursive`.
- Network access and Git credentials for the expected remotes when checking
  branch heads or tags. Private submodule authentication belongs to the caller.

Each initialized submodule's checked-out HEAD must match the gitlink committed
in its parent. This prevents a local checkout change from concealing an incorrect
committed pointer. Discovery and branch metadata come from each parent's
committed tree, not uncommitted `.gitmodules` edits.

## Configuration

The optional YAML file contains one `submodules` mapping. Keys are exact paths
relative to the consumer repository root, not `.gitmodules` section names.
Paths may include spaces and multiple nesting levels.

```yaml
submodules:
  component:
    branch: develop
  component/vendor/library:
    tag: v1.2.3
  pinned-library:
    commit: '0123456789abcdef0123456789abcdef01234567'
  canonical-library:
    remote: https://github.com/example/canonical-library.git
    branch: main
  intentionally-unchecked:
    exclude: true
```

| Field | Behavior |
| --- | --- |
| `commit` | Exact full hexadecimal commit SHA (40 or 64 characters). |
| `tag` | Lightweight or annotated remote tag, resolved to its commit SHA. |
| `branch` | Override the target branch for this submodule. |
| `remote` | Canonical remote to query instead of the checkout's `origin`. |
| `exclude` | Explicitly skip this submodule and its entire subtree. |

`commit` and `tag` are mutually exclusive. A pin takes precedence over branch
selection. An excluded entry cannot contain other options. There are no built-in
CECE or UFS-WM exclusions or canonical remotes.

Without a pin, the target branch is selected in this order:

1. The entry's YAML `branch`.
2. The owning repository's committed `.gitmodules` branch.
3. The required `--target-branch` argument or Action input.

A branch value of `.` uses the supplied target branch, which also works for
detached pull-request checkouts. Branch overrides apply to the exact path only;
they are not inherited by descendants.

Unlisted submodules are still checked. Omit the configuration entirely, or use
`submodules: {}`, to check all discovered submodules using branch defaults.
Invalid fields, duplicate YAML keys, conflicting pins, non-string revisions,
and configured paths that are not discovered are errors. Quote SHA values to
prevent YAML from interpreting an all-digit SHA as a number.

Commit pins are compared directly with committed gitlinks, so they do not
require remote advertisement or full local history. Branch and tag checks query
the configured remote using Git. Relative submodule URLs work through the
initialized checkout's resolved `origin` URL. Canonical overrides should use
absolute URLs or paths.

**Equality is required**, not just ancestry. UFS-WM's existing check accepts a
commit that contains the upstream branch tip; that behavior is different and
must be considered during its separate migration.

## Local usage

From the `ufs-ci-tools` checkout:

```bash
uv run --project tools/verify-submodules --locked --no-dev --python 3.12 \
  python tools/verify-submodules/verify_submodules.py \
  --repo-root /path/to/consumer \
  --target-branch develop \
  --config /path/to/consumer/submodules.yaml
```

`--repo-root` defaults to the current directory and must identify the consumer
repository root. CLI configuration paths are relative to the invocation
directory. Add `--json` for a JSON array on stdout; diagnostics go to stderr.
Add `--step-summary /path/to/summary.md` to append a Markdown report.

Each result includes `path`, `current_sha`, `expected_sha`, `target`, `status`,
and `detail`. Status is `OK`, `OUT_OF_SYNC`, `ERROR`, or `EXCLUDED`.

| Exit code | Meaning |
| --- | --- |
| `0` | All discovered submodules passed or were explicitly excluded. |
| `1` | At least one submodule mismatched or could not be verified. |
| `2` | Configuration, repository discovery, or report-writing failure. |

A repository without submodules succeeds and reports that none were found.

## GitHub Action

Replace `FULL_COMMIT_SHA` with a reviewed commit of `ufs-ci-tools`.

```yaml
name: Verify submodules
on: [pull_request]
permissions:
  contents: read
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          submodules: recursive
      - uses: ufs-community/ufs-ci-tools/actions/verify-submodules@FULL_COMMIT_SHA
        with:
          target-branch: develop
          config: submodules.yaml
```

The composite Action uses its own source directory to find the isolated uv
project; it does not expect tool files in the consumer repository. It installs
the locked runtime dependencies, runs with Python 3.12, propagates failures,
and appends the verification report to the job summary.

| Input | Default |
| --- | --- |
| `repo-root` | `${{ github.workspace }}` |
| `config` | Omitted; relative paths resolve against `repo-root`. |
| `target-branch` | Required; supply the intended upstream branch. |

The Action is supported on Linux runners with Bash and Git. It does not check out
the consumer, configure credentials, or request write permissions.

## Development

```bash
uv sync --project tools/verify-submodules --locked
uv run --project tools/verify-submodules --locked pytest tools/verify-submodules/tests
uv run --project tools/verify-submodules --locked pre-commit install
uv run --project tools/verify-submodules --locked pre-commit run --all-files
```

The applicable hygiene, Ruff, mypy, and YAML hook patterns are adapted from
[`ufs-chem-assay`](https://github.com/benkozi/ufs-chem-assay).
Python validation dependencies and the lockfile belong to this tool, not the
repository root. Tests create temporary local Git repositories and do not depend
on CECE, UFS-WM, or changing public branch heads. CI also invokes the actual
composite Action against a three-level fixture and checks success and failure.

## Troubleshooting

- **Uninitialized or inconsistent checkout:** run
  `git submodule update --init --recursive` in the consumer checkout.
- **Missing branch or tag:** check the selected branch precedence, spelling, and
  canonical remote. Missing refs never fall back to another branch.
- **Remote errors:** configure Git authentication and verify connectivity.
  Noninteractive remote queries time out after 60 seconds.
- **Configured path not discovered:** check the root-relative path and recursive
  initialization. A parent failure may prevent discovering its descendants.
- **Intentional divergence:** use an explicit commit/tag pin or exclusion rather
  than suppressing a failure.
