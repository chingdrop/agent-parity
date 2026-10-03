# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/).
- Python 3.12, pinned in `.python-version`. uv picks it up and installs it if needed, so `uv sync` and `uv run` always
  use 3.12, matching CI.

## Setup

```bash
git clone git@github.com:chingdrop/agent-parity.git
cd agent-parity
uv sync
```

This installs `agent-parity` in editable mode along with its dev dependencies (`pytest`, `ruff`, `mypy`, `pre-commit`,
plus type stubs for `pandas`/`boto3`/
`PyYAML`).

Then install the git hook so linting, formatting, type-checking and secret scanning run
automatically on each commit:

```bash
uv run pre-commit install
```

## Project layout

```
src/agent_parity/
    cli.py             # entry point: run / compare / report subcommands
    config.py          # config.yaml + ${VAR} resolution
    models.py          # ADDevice / AgentDevice and the status enums
    ad_export.py       # parse the AD export CSV
    agent_csv.py       # parse a generic agent-inventory CSV
    connectors/        # one class per vendor (SentinelOne, Carbon Black, BitDefender)
    correlation.py     # the pandas merge/classification engine
    os_eol.py          # OS end-of-life reference data and matching
    pipeline.py        # pure collect + correlate orchestration, no persistence
    quarterly_report.py  # the quarterly PDF report
    script_runner.py   # runs the AD export script through a vendor connector
    splunk_export.py   # Splunk export (one event per row + a run summary)
    scheduling/        # SQLAlchemy schema, persistence, history, and the Celery scheduled path
    vendor/            # modules copied from py-shared-tools (HTTP adapter, atomic writes, ...)
    scripts/           # Export-ADDevices.ps1 (package data, pushed to endpoints at runtime)
tests/
    mirrors src/ (tests/connectors/, tests/scheduling/, tests/vendor/); see "Tests" below
tools/
    developer tools: sample-report generators, the EOL drift check, the demo history seeder
```

`tools/` holds scripts for maintainers, run with `uv run python tools/<name>.py`; it isn't part of the package.
`src/agent_parity/scripts/` is different: it ships in the wheel.

See [docs/architecture.md](docs/architecture.md) for the full architecture writeup — why each
piece is shaped the way it is, not just what's where.

### `vendor/`

`src/agent_parity/vendor/` holds modules copied from
[py-shared-tools](https://github.com/chingdrop/py-shared-tools) v1.3.1 (commit d54dcd6), so the repo is self-contained.
This repo owns those copies and does not keep them in sync with upstream: change them here, with their tests in
`tests/vendor/`. Each module says where it came from in a header comment.

## Running tests

```bash
uv run pytest
```

### Tests

- **Layout:** tests mirror `src/` once a repo has more than 10 test modules, which this one has. Each subpackage of
  `src/agent_parity/` (`connectors/`, `scheduling/`, `vendor/`) has a matching directory under `tests/`, with its own
  `__init__.py` (two `test_config.py` files exist, and the `__init__.py` files keep them distinct). Tests for flat
  modules stay flat in `tests/`. See CLAUDE.md's "Testing conventions".
- **Coverage:** line and branch coverage are both measured (`branch = true`). The floor is `fail_under` in
  `pyproject.toml`: the measured baseline minus 2 points, rounded down. When the baseline climbs more than 4 points
  above the floor, raise the floor in the same PR.

## Before opening a PR

Run the same checks as CI:

```bash
uv run ruff check src tests tools docker
uv run ruff format --check src tests tools docker
uv run mypy
uv run pytest --cov
```

`uv run pytest --cov` fails if coverage drops below the floor in `pyproject.toml`.

## Regenerating the sample report

`docs/sample-report.md` is generated from a real run against `sample_data/`, not written by hand. After changing
the correlation engine or the fixtures, regenerate it:

```bash
uv run python tools/gen_sample_report.py
```

It refuses to run if any connector has live credentials configured, so the published doc only ever contains
synthetic data.

## Rendering the demo GIF

`docs/demo.tape` is a [vhs](https://github.com/charmbracelet/vhs) script that types `uv run agent-parity run --csv` and
holds on the summary line. It records the real command, not scripted text. To render `docs/demo.gif`:

```bash
brew install vhs   # also installs ttyd and ffmpeg
uv sync
vhs docs/demo.tape
```

## Linting, formatting, and type-checking

```bash
uv run ruff check src tests tools docker   # lint
uv run ruff format src tests tools docker  # format
uv run mypy                                # type-check (paths and flags are in pyproject.toml)
```

`pre-commit install` (above) runs these automatically on each commit, along with gitleaks and checks for private keys
and large files; these commands are for running them manually or investigating a failure. Run
`uv run pre-commit autoupdate` periodically to move the hooks' pinned versions forward.

## Recording a design decision

Decisions are recorded as ADRs in [docs/decisions/](docs/decisions/README.md), one short file each
(`NNNN-short-title.md`), in the same sections as the existing ones: Status, Context, Decision, Alternatives considered,
Consequences and Evidence.

- Take the next number and add a row to the table in `docs/decisions/README.md`. Numbers are never reused.
- Decisions are never rewritten. To change one, add a new ADR; the only edit to the old one is its Status line,
  updated to point to the ADR that replaces it.
- Where the repo doesn't record why a choice was made, leave an HTML-comment TODO rather than guessing.

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add a short entry under
`[Unreleased]` (Added, Changed, Removed or Fixed) in the same PR as the change, and mark breaking changes
**Breaking**. Released sections are never edited; the version bump and tag happen together at release time.

## Checking doc links

[lychee](https://github.com/lycheeverse/lychee) checks every link and `#anchor` in the Markdown files
(`brew install lychee`; config in `.lychee.toml`):

```bash
lychee './**/*.md'             # everything, including external URLs
lychee --offline './**/*.md'   # relative links and anchors only
```

CI (`.github/workflows/links.yml`) runs the offline check on pushes and PRs that touch Markdown, and the full check
weekly, so an external site going down can't block a merge.
