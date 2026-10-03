# 0012. Adopt the shared Python tooling standard

Status: Accepted (2026-10-03)

## Context

The same Python project standard is being applied across four of the maintainer's repositories, so their layout,
packaging metadata, test gates and hooks look and behave alike. This repo already met most of it: the Hatchling
backend, the `src/` layout, `requires-python >=3.12`, the ruff and mypy configuration, SHA-pinned actions with
least-privilege `permissions`, and tests that mirror `src/`. A few things differed:

- Developer scripts lived in a repo-root `scripts/` directory, easy to confuse with the bundled
  `src/agent_parity/scripts/` package data that the AD export depends on at runtime.
- The modules copied from py-shared-tools lived in `agent_parity.shared`, a name that didn't say they were copies this
  repo owns.
- The Python version wasn't pinned for local tools, and the license file wasn't declared in the package metadata.
- The coverage floor was a `--cov-fail-under` flag in CI, so a local `uv run pytest --cov` didn't enforce it.
- `actions/checkout` left the job's GitHub token in each runner's git config.
- Pre-commit ran ruff and mypy but no secret or private-key scanning.

<!-- TODO(craig): why the standard exists at all, and what prompted it now, beyond consistency across the four repos. -->

## Decision

- Rename the repo-root `scripts/` to `tools/`. `src/agent_parity/scripts/` (package data) is unchanged.
- Rename `agent_parity.shared` to `agent_parity.vendor` and `tests/shared/` to `tests/vendor/`, with module and
  symbol names unchanged. Each vendored module carries a header naming its source (py-shared-tools v1.3.1, commit
  d54dcd6) and saying this repo owns the copy and does not keep it in sync; `vendor/config.py` says it is a partial
  copy.
- Pin Python 3.12 in `.python-version`, and declare `license-files = ["LICENSE"]` in `pyproject.toml`.
- Move the coverage floor into `pyproject.toml` (`[tool.coverage.report] fail_under`), set to the measured baseline
  minus 2 points, rounded down: 94.14% measured, so 92. CI runs `uv run pytest --cov` without `--cov-fail-under`.
- Set `persist-credentials: false` on every `actions/checkout` step.
- Add pre-commit hooks for gitleaks (v8.30.1), `detect-private-key` and `check-added-large-files` (500 KB).

## Alternatives considered

- **Keeping the floor at 88**, the value CI enforced before: rejected, because it was set from an older 90.83%
  baseline and was already more than 4 points below today's.
- **A hook rejecting data files**, as another repo under the same standard uses: not adopted, because this repo
  commits CSV and JSON fixtures under `sample_data/` on purpose.
- **Keeping the `shared/` name**: <!-- TODO(craig): whether any name other than `vendor/` was considered. -->

## Consequences

- Imports change from `agent_parity.shared.*` to `agent_parity.vendor.*`. Nothing outside this repo imported them.
- `vendor` now names both the copied-module package and, elsewhere in the code, the EDR vendors (`VendorConfig`,
  `vendor_status`). The package is only imported, never named in config, so the overlap is in reading, not behavior.
- `uv run pytest --cov` enforces the same floor locally as in CI. The floor is raised in the same PR whenever the
  baseline exceeds it by more than 4 points.
- With the token no longer persisted, a future job that needs to push or fetch with it must opt back in on that step.
- Committing runs gitleaks against staged changes, honoring the existing `.gitleaksignore`.
- `.python-version` makes uv build `.venv` with 3.12, matching CI, where a local default could otherwise be newer.

## Evidence

- Config: [`pyproject.toml`](../../pyproject.toml), [`.python-version`](../../.python-version),
  [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml), [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
- Code: [`src/agent_parity/vendor/`](../../src/agent_parity/vendor/__init__.py), [`tools/`](../../tools/seed_history.py)
- Docs: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
