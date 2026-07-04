# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A portfolio rebuild (synthetic data only, no proprietary code) of a device coverage
reconciliation tool: it correlates an Active Directory computer inventory against
EDR/security agent inventories (SentinelOne, Carbon Black, BitDefender) to find devices
missing agent coverage, orphaned agents with no matching AD object, and stale agent
check-ins. See [README.md](README.md) for the full architecture writeup (deployment
model, Django-vs-Splunk rationale, Celery chord design, credential split) — read it
before making structural changes, since several design decisions there are deliberate
and were agreed on with the project owner rather than obvious from the code.

## Commands

```console
uv sync                                              # install deps (no Docker/Redis needed)
uv run agent_parity_web/manage.py migrate
uv run agent_parity_web/manage.py seed_demo          # two runs of history per client, from fixtures
uv run agent_parity_web/manage.py runserver

uv run agent_parity_web/manage.py sync_and_correlate [--client SLUG] [--all]  # one plain run

uv run pytest                                        # full suite, offline, no broker
uv run pytest tests/test_correlation.py -k covered   # single test/file
uv run pytest tests/test_tasks.py                    # Celery chord tests (run eager, no broker needed)
```

Docker Compose (scaled mode) lives in `docker/`; commands for dev/prod are in the README.
There is no linter/formatter config in this repo (`pyproject.toml` has no `[tool.ruff]`
or `[tool.black]`) — formatting has so far been done via the IDE's reformatter, not a CLI tool.

## Architecture: two packages, one boundary

- **`agent_parity/`** — the pipeline. Connectors, AD export parsing, the pandas
  correlation engine, the Splunk exporter. **Must stay free of Django and Celery
  imports** — it's called identically from the sync management command and from
  Celery tasks, and that boundary is load-bearing, not incidental.
- **`agent_parity_web/`** — the Django project. `dashboard/services.py` holds the
  *only* implementation of collect → correlate → persist; `management/commands/
  sync_and_correlate.py` and `dashboard/tasks.py` both call into it rather than
  duplicating logic. If you're adding a pipeline step, it almost always belongs in
  `services.py`, not in the command or the task.

`agent_parity_web/manage.py` inserts the repo root onto `sys.path` at runtime so
`agent_parity_web/config/settings/base.py` can import `agent_parity` even though
they're siblings, not nested packages. PyCharm doesn't know about this trick
statically — `agent_parity_web/` is marked as an extra source root in `.idea/*.iml`
(plus a Django facet) specifically so the IDE's inspector doesn't flood you with
false "unresolved reference: dashboard/services/config" warnings across the whole
`dashboard` app. If those come back, check the `.iml` source-root/facet config
before assuming the imports are actually broken.

## Correlation engine (`agent_parity/correlation/engine.py`)

This is the analytical core and is deliberately a `.pipe()` chain, not one function:
`add_join_key` → `merge_with_agents` (`pd.merge(..., how="outer", indicator=True)`) →
`classify_coverage` (turns the merge indicator + a `last_seen` staleness check into
`CoverageStatus`). Each stage is independently testable; keep it that way rather than
inlining. `join_key` normalization (strip DNS suffix, lowercase, trim) is the only
matching logic — there's no fuzzy matching, by design (noted as future work).

Tests for this module assert on classification outcomes and merge-invariants (row
count = union of join keys), not on `pd.merge` itself — follow that pattern for new
correlation tests rather than re-testing pandas.

## Connectors (`agent_parity/connectors/`)

Every connector implements `fetch_inventory()` and `deploy_and_run(script_path,
target_id)` against a shared `AgentConnector` ABC (`connectors/base.py`). **Fixture
fallback is not a test-only shim — it's the default runtime path.** `is_live` gates
on whether all `required_credentials` are present; if not, `fetch_inventory()` reads
`sample_data/<client>/<vendor>_inventory.json` and `deploy_and_run()` returns the
client's `ad_export.csv`, with all timestamps rebased so the newest check-in is ~now
(`rebase_timestamps` / `rebase_csv_timestamps`) — this is what keeps the authored
stale/recent split in `sample_data/` stable regardless of when the demo is run. Don't
add credential-checking logic anywhere else; it belongs in `is_live` alone.

**Not every vendor supports `deploy_and_run()` for real.** `supports_remote_execution`
(ClassVar, default `True`) gates it — `BitDefenderConnector` sets it `False` because
GravityZone's real API has no equivalent to SentinelOne's Remote Script Orchestration
or Carbon Black's Live Response, only predefined task types (scan, isolate, ...).
`deploy_and_run()` raises `ConnectorError` before the live/fixture fork when this is
`False`, so BitDefender can't accidentally "succeed" at something it doesn't really do,
even in demo mode. It's fetch_inventory-only. If a 4th vendor connector genuinely can't
run scripts either, set this the same way — don't leave `_live_deploy_and_run`
unimplemented and let it fail some other way.

Live mode goes through `agent_parity/rest_adapter.py` (`RestAdapter`, ported from a
sibling project) rather than a bare `requests.Session` — retries/backoff on
429/5xx are configured there once, shared by all three vendors. `RestAdapter.request()`
returns already-parsed content (`dict` for JSON, `str` for text/html, `bytes`
otherwise), not a `Response` object, so connector call sites use `self._request_json(...)`
when they know the endpoint returns a JSON object, or `self._as_text(...)` on the raw
`_request(...)` result when they need guaranteed text (e.g. SentinelOne's fetch-files
script output). No test exercises real network I/O; `tests/test_connectors.py` proves
the RestAdapter wiring (retry config, JSON/text parsing) by monkeypatching the
underlying `requests.Session.request`, not by hitting a live API.

## Credential resolution (`agent_parity/config.py`)

`config.yaml` (topology, committed) + `.env` (secrets, gitignored) are resolved together
by `load_config()` / `get_connector()`. Every secret in `config.yaml` is a `${VAR}`
reference; an unset variable resolves to `None` rather than raising, which is exactly
what puts a connector into fixture mode. `credentials_for(client_slug, vendor_name)`
is the one place that knows `global` vs `per_client` scope — SentinelOne/BitDefender
are global (same credentials for every client), Carbon Black is per-client. When adding
a vendor or a client, this is the function whose behavior actually matters; don't
special-case scope logic in a connector or in `services.py`.

`pick_ad_export_vendor(client_cfg)` picks which of a client's enabled vendors carries
the AD export — filtered to `supports_remote_execution = True` connectors, then broken
by `AD_EXPORT_VENDOR_PREFERENCE = ("sentinelone", "carbonblack")`, not alphabetically.
That preference order is a real business fact (S1 covered most of the client base, CB a
handful, BitDefender basically none) as much as a technical one — if you touch it, keep
both in mind. Raises `ConfigError` if a client has no capable vendor at all. Called from
`services.collect_ad_csv`; don't reintroduce a `sorted(client_cfg.vendors)[0]`-style
pick elsewhere — that bug (silently routing AD export through whichever vendor happens
to sort first, capable or not) is exactly what this function replaced.

## Celery chord (`agent_parity_web/dashboard/tasks.py`)

One fan-out task per `(client, vendor)` inventory pull plus one AD-export task per
client, feeding a chord callback (`correlate_client`) that runs the correlation once
per client against the complete result set. Three things that are easy to break if
touched carelessly:

- Fan-out tasks **return** `{"ok": False, "error": ...}` on failure — they never raise.
  If you add a new fan-out task, keep that contract; an exception there would prevent
  the chord callback from firing for every other vendor.
- The `CorrelationRun` row is created as `PENDING` *before* the chord is dispatched, and
  dispatch happens inside `transaction.on_commit(...)` (`dispatch_client`). Its ID is
  the idempotency key — `correlate_client` re-checks the run's status under
  `select_for_update()` in `services.persist_correlation` and no-ops if it's already
  finalized. Don't dispatch a chord before its `CorrelationRun` row has committed.
- `mark_run_failed` is the `link_error` backstop so a callback exception doesn't leave
  a run stuck in `PENDING` forever.

## Testing conventions

- `tests/conftest.py`'s `eager_celery` fixture runs Celery tasks in-process
  (`task_always_eager`) — no broker required for `test_tasks.py`.
- `tests/test_pipeline_sync.py` pins the specific gap scenarios authored into
  `sample_data/` by join key (e.g. `acme-sql02` is `missing_agent`, `acme-fs-old` is
  `orphaned_agent`). If you regenerate or edit the fixtures, these tests are the
  regression check — a scenario silently changing status is a fixture bug, not a test
  bug, unless the change was intentional.
- `sample_data/` fixtures were originally generated by a one-off script that was never
  committed (it lived in a scratch directory outside the repo). The committed CSV/JSON
  files are the source of truth now; there's no `generate_fixtures.py` in this repo to
  regenerate them from.
