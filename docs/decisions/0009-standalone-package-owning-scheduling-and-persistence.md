# 0009. Standalone package that owns scheduling and persistence, with no web dashboard

Status: Accepted (2026-07-14). Replaces the 2026-07-05 single-organization scope.

## Context

On 2026-07-05 the repo was cut down to a standalone CLI, expecting a separate hub project to own the web layer and
scheduling, and simplified to one organization and one vendor per config. On 2026-07-14 that was reversed: the
simplification "didn't reflect what was actually run" by the original tool, and the planned hub project was archived, so
this package owns scheduling and persistence permanently.

## Decision

agent-parity is a standalone package and CLI with no web framework or dashboard. It owns Celery scheduling and
SQLAlchemy/SQLite persistence directly, and models multiple clients, each with its own AD domains and vendors, in one
`config.yaml`.

## Alternatives considered

- **A separate hub project owning scheduling and persistence**: planned, then archived.
- **A Django dashboard**: removed, and documented as never real (a rebuild-only addition).
- **One organization, one vendor per config** (2026-07-05): replaced on 2026-07-14.

## Consequences

- `run` and `compare` persist nothing; `sync` and the Celery tasks record run history in SQLite.
- SQLite has no row-level locking, so run idempotency relies on its writer serialization. That is adequate for
  single-node or demo scale and is a disclosed difference from Postgres.
- The schema is `create_all()`, with no migrations.
- Rules out a web UI (none is planned) and real-time ingestion; this is a batch tool.

## Evidence

- Code: [`src/agent_parity/cli.py`](../../src/agent_parity/cli.py), [
  `src/agent_parity/scheduling/persistence.py`](../../src/agent_parity/scheduling/persistence.py), [
  `src/agent_parity/scheduling/tasks.py`](../../src/agent_parity/scheduling/tasks.py), [
  `src/agent_parity/config.py`](../../src/agent_parity/config.py)
- Tests: [`tests/test_cli.py`](../../tests/test_cli.py) `test_sync_subcommand_all_persists_one_run_per_client`; [
  `tests/scheduling/test_persistence.py`](../../tests/scheduling/test_persistence.py)
  `test_persist_correlation_is_idempotent_on_duplicate_call`; [
  `tests/scheduling/test_tasks.py`](../../tests/scheduling/test_tasks.py)
  `test_chord_produces_partial_run_when_one_vendor_fails`
