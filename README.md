# ufs-ci-tools

Provides reusable GitHub Actions workflows, utility scripts, and automation templates for the UFS community.

## Tools

- [Submodule verification](docs/verify-submodules.md): verify recursive submodule
  pointers against configured commits, tags, or target branch heads, locally or
  through a reusable GitHub Action.

## Implementation pattern

Each Python tool lives in `tools/<tool-name>/` with its own `pyproject.toml`,
committed `uv.lock`, tests, and isolated uv environment. There is no root-level
Python project, shared virtual environment, or repository-wide lockfile.
Run tools with `uv run --project tools/<tool-name> --locked ...`; Actions and CI
must use the same tool-local project. Runtime Action invocations use `--no-dev`.

Reusable Actions live in `actions/<tool-name>/`. Each tool has a dedicated guide
in `docs/<tool-name>.md`, linked from this README. Keep usage details and
tool-specific configuration in that guide rather than expanding this overview.

Pre-commit's Python hooks use the owning tool's uv project. Shared file-hygiene
and YAML hooks run independently of any tool's uv project.

`.github/workflows/ci.yaml` owns CI triggers and calls each tool's
`.github/workflows/ci-<tool-name>.yaml` using `workflow_call`. Each tool workflow
owns its lint, test, and Action smoke-test jobs. The separate `ci-shared.yaml`
workflow runs repository-wide hooks once per CI run.
