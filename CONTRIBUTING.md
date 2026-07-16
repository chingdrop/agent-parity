# Contributing

## Setup

```bash
git clone git@github.com:chingdrop/agent-parity.git
cd agent-parity
uv sync
```

This installs `agent-parity` in editable mode along with its dev dependencies
(`pytest`, `ruff`, `mypy`, `pre-commit`, plus type stubs for `pandas`/`boto3`/
`PyYAML`).

Then install the git hook so linting/formatting/type-checking run
automatically on each commit:

```bash
uv run pre-commit install
```

## Project layout

```
src/agent_parity/
    cli.py            # entry point: run/compare/sync subcommands
    config.py          # config.yaml + .env resolution
    connectors/          # one class per vendor (SentinelOne, Carbon Black, BitDefender)
    correlation/          # the pandas merge/classification engine
    pipeline.py             # pure collect+correlate orchestration, no persistence
    persistence.py            # SQLAlchemy-backed run history, layered on pipeline.py
    db.py                       # the SQLAlchemy schema itself
    celery_app.py / tasks.py     # the scheduled fan-out/fan-in path
    reporting/                    # Splunk delta export
tests/
    (one test file per module above, see CLAUDE.md's "Testing conventions")
```

See [CLAUDE.md](CLAUDE.md) for the full architecture writeup — why each
piece is shaped the way it is, not just what's where.

## Running tests

```bash
uv run pytest
```

## Linting, formatting, and type-checking

```bash
uv run ruff check src tests      # lint
uv run ruff format src tests     # format
uv run mypy src/agent_parity     # type-check
```

`pre-commit install` (above) runs all three automatically on each commit;
these commands are for running them manually or investigating a failure.
