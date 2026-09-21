# Contributing

## Setup

```bash
git clone git@github.com:chingdrop/agent-parity.git
cd agent-parity
uv sync
```

This installs `agent-parity` in editable mode along with its dev dependencies (`pytest`, `ruff`, `mypy`, `pre-commit`,
plus type stubs for `pandas`/`boto3`/
`PyYAML`).

Then install the git hook so linting/formatting/type-checking run
automatically on each commit:

```bash
uv run pre-commit install
```

## Project layout

```
src/agent_parity/
    cli.py             # entry point: run / compare / sync subcommands
    config.py          # config.yaml + ${VAR} resolution
    models.py          # ADDevice / AgentDevice and the status enums
    ad_export.py       # parse the AD export CSV
    agent_csv.py       # parse a generic agent-inventory CSV
    connectors/        # one class per vendor (SentinelOne, Carbon Black, BitDefender)
    correlation.py     # the pandas merge/classification engine
    os_eol.py          # OS end-of-life reference data and matching
    pipeline.py        # pure collect + correlate orchestration, no persistence
    script_runner.py   # runs the AD export script through a vendor connector
    splunk_export.py   # Splunk delta export
    scheduling/        # SQLAlchemy schema, persistence, and the Celery scheduled path
    shared/            # inlined HTTP adapter, object storage and helpers
    scripts/           # Export-ADDevices.ps1
tests/
    mirrors the layout above (tests/connectors/, tests/scheduling/, tests/shared/); see CLAUDE.md's "Testing conventions"
```

See [CLAUDE.md](CLAUDE.md) for the full architecture writeup — why each
piece is shaped the way it is, not just what's where.

## Running tests

```bash
uv run pytest
```

## Regenerating the sample report

`docs/sample-report.md` is generated from a real run against `sample_data/`, not written by hand. After changing
the correlation engine or the fixtures, regenerate it:

```bash
uv run python scripts/gen_sample_report.py
```

It refuses to run if any connector has live credentials configured, so the published doc only ever contains
synthetic data.

## Rendering the demo GIF

`docs/demo.tape` is a [vhs](https://github.com/charmbracelet/vhs) script that types `uv run agent-parity run` and
holds on the summary line. It records the real command, not scripted text. To render `docs/demo.gif`:

```bash
brew install vhs   # also installs ttyd and ffmpeg
uv sync
vhs docs/demo.tape
```

## Linting, formatting, and type-checking

```bash
uv run ruff check src tests scripts docker   # lint
uv run ruff format src tests scripts docker  # format
uv run mypy src/agent_parity                 # type-check
```

`pre-commit install` (above) runs all three automatically on each commit;
these commands are for running them manually or investigating a failure.

## Checking doc links

[lychee](https://github.com/lycheeverse/lychee) checks every link and `#anchor` in the Markdown files
(`brew install lychee`; config in `.lychee.toml`):

```bash
lychee './**/*.md'             # everything, including external URLs
lychee --offline './**/*.md'   # relative links and anchors only
```

CI (`.github/workflows/links.yml`) runs the offline check on pushes and PRs that touch Markdown, and the full check
weekly, so an external site going down can't block a merge.
