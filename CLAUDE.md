# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A portfolio rebuild (synthetic data only, no proprietary code) of a device coverage
reconciliation tool: it correlates an Active Directory computer inventory against an
EDR/security agent inventory (SentinelOne, Carbon Black, or BitDefender) to find devices
missing agent coverage, orphaned agents with no matching AD object, and stale agent
check-ins. See [docs/architecture.md](docs/architecture.md) for the full architecture writeup — read it
before making structural changes, since several design decisions there are deliberate
and were agreed on with the project owner rather than obvious from the code.

This package has no web framework and no Django — those were never real (a Django
dashboard was a rebuild-only addition that has been permanently removed) — but it is **not** a thin, dependency-free
library. It owns real scheduling (Celery) and
persistence (SQLAlchemy/SQLite) directly, as core dependencies, the same tier as
`pandas`/`requests`. Earlier in this project's history the plan was for a separate
"hub" project to own that layer instead, consumed as a pinned git dependency
(`uv add git+https://.../agent-parity@vX.Y.Z`) — that hub project is archived and
won't be developed further, so this package owns scheduling/persistence permanently,
not provisionally. Don't try to strip Celery/SQLAlchemy back out "to keep it a thin
library" — that plan is dead, not deferred.

Models a real MSSP-style topology: multiple client organizations in one `config.yaml`,
each with its own AD domain (s) and enabled vendor (s) — `clients:`/`vendors:` nesting,
`ClientConfig`/`VendorConfig`, and per-vendor `scope` (`global` vs `per_client`) are all
deliberate, not incidental. This matches what was actually run in production; Django
and the web dashboard never were (that was a rebuild-only addition, and it stays gone).
Celery-based scheduling/fan-out (see "Scheduling & persistence" below) and Splunk
export (see "Splunk export" below) are both real and both now restored —
this was a staged restoration (see recent git history), but every feature described
in this file has landed as of this revision.

## Commands

```console
uv sync                                     # install deps
uv run agent-parity compare ad.csv agent.csv   # two CSVs, zero config.yaml/connectors/credentials
uv run agent-parity run --all                  # config.yaml + connectors, every client, persisted as a CorrelationRun (SQLite)
uv run agent-parity run --client acme --csv    # just one client, and also write output/acme.csv
uv run agent-parity run --all --workers        # same chord, on running Celery workers (in parallel)
uv run agent-parity run --all --workers --timeout 90   # ... but stop waiting after 90 minutes
uv run agent-parity report --all --quarter 2026-Q3     # quarterly PDF per client, from the run history
uv run python tools/gen_sample_quarterly_report.py  # regenerate docs/sample-quarterly-report.pdf

uv run pytest                               # full suite, offline, no live credentials needed
uv run pytest tests/test_correlation.py -k covered   # single test/file
uv run pytest --cov                         # the coverage gate CI enforces (line + branch; floor in pyproject.toml)

uv run ruff check src tests tools docker  # lint (E, F, I, UP, B, SIM, S)
uv run ruff format src tests tools docker # format
uv run mypy                                 # type-check; config in pyproject.toml, not strict
lychee './**/*.md'                          # doc links + #anchors (config in .lychee.toml)

docker build -f docker/Dockerfile -t agent-parity .   # bare-bones standalone image
docker compose -f docker/docker-compose.yml up -d s3 redis worker beat   # local storage + scheduling stack
docker/smoke_test.sh                                 # round-trips a real object + a real Celery chord
```

Ruff, mypy and the coverage gate are configured in `pyproject.toml` and run in CI (`.github/workflows/ci.yml`: `lint`,
`typecheck`, `test`, `build`, and a `security` job with
pip-audit and gitleaks; CodeQL, the `links.yml` lychee check and `smoke.yml` run separately —
`smoke.yml` runs `docker/smoke_test.sh` weekly and on changes to `docker/`, the scheduling code,
`storage.py` or the lockfile). `pre-commit` runs ruff and mypy locally.
Tests are exempt from ruff's `S101`/`S106` by a per-file ignore, not by disabling the rules.

## Architecture

Four layers, collect → correlate → report:

- **`src/agent_parity/connectors/`** — one class per vendor (SentinelOne, Carbon Black,
  BitDefender), each implementing `fetch_inventory()`/`deploy_and_run()`.
- **`src/agent_parity/ad_export.py`** + **`src/agent_parity/script_runner.py`** — parsing the
  AD export script's CSV output and running it remotely through a vendor's own scripting
  capability. `src/agent_parity/scripts/Export-ADDevices.ps1` is the script itself, bundled
  into the installed package since `script_runner.py` pushes it to a live vendor at runtime,
  not just a dev-time asset.
- **`src/agent_parity/agent_csv.py`** — parsing a generic, vendor-agnostic agent/EDR
  inventory CSV, for callers with no connector/credentials at all.
- **`src/agent_parity/correlation.py`** — the pandas merge/classification core.
- **`src/agent_parity/pipeline.py`** — two orchestration entrypoints that tie the above
  together: `run_correlation_for_client()` (config.yaml + connectors, live or fixture)
  and `correlate_from_csvs()` (two CSVs, zero config). **`src/agent_parity/cli.py`** is a
  thin wrapper: `compare` calls `correlate_from_csvs()` directly, while `run` executes the
  Celery chord in `tasks.py` (in-process or on workers), which persists through
  `persistence.py` — see "Scheduling & persistence" below.

## Correlation engine (`src/agent_parity/correlation.py`)

This is the analytical core and is deliberately a `.pipe()` chain, not one function:
`add_join_key` → `merge_with_agents` (`pd.merge(..., how="outer", indicator=True)`) →
`resolve_ambiguous_join_keys` → `classify_coverage` (turns the merge indicator + a `last_seen` staleness check into
`CoverageStatus`) → `backfill_machine_type` → `classify_eol_status`. Each stage is
independently testable; keep it that way rather than inlining. `join_key`
normalization (strip DNS suffix, lowercase, trim) is the only matching logic —
there's no fuzzy matching, by design (a rename resolves itself once the agent reports the new hostname; see
[ADR 0005](docs/decisions/0005-correlate-on-normalized-hostname-only.md)). The one refinement is
`resolve_ambiguous_join_keys`, for a short hostname that exists in more than one AD domain (two
machines, one join key): an agent that reported a full DNS name keeps only its pairing with the AD
object whose `dns_hostname` equals it exactly (`match_method = "fqdn_exact"`), and pairings that
still rest on the short name alone are flagged `ambiguous_join_key` (counted as
`summary["ambiguous_join_keys"]`, logged, and shown on `run`'s summary line). It's exact string
equality, not fuzzy matching; keep it that way. It only runs when the merged frame has
`dns_hostname` or `distinguished_name` to tell AD objects apart, so hand-built test frames without
them pass through unchanged. The flag is in the frame and Splunk row events, not in
`CoverageSnapshot` (adding a column there would need a migration this schema doesn't have).

**`backfill_machine_type` exists for one reason**: `machine_type` (see
`AgentDevice`'s docstring) only ever comes from the agent side of the merge, so a
`missing_agent` row — no agent record at all — would otherwise carry no criticality
signal whatsoever. That's backwards for a coverage tool whose whole point (see
docs/architecture.md's "High-value assets" section — this project's original purpose was a
quarterly client report prioritizing exactly this) is flagging a missing Domain
Controller *harder* than a missing workstation. It backfills from AD's own OS text
via `infer_machine_type()` (`src/agent_parity/models.py`) — the same heuristic
Carbon Black/BitDefender's connectors use — but only for rows where `machine_type`
isn't already set; an agent-reported value always wins. Don't try to infer
criticality from the hostname — that's exactly the unreliable signal this design
deliberately avoids (file/storage servers can be named anything; a Windows Server
SKU can't fake being one).

**`classify_eol_status` (see `src/agent_parity/os_eol.py`) is the third prioritization
axis**, independent of coverage: a covered end-of-life server still needs an OS
upgrade. It resolves `os_build` per row with the same both-sides-then-fallback
precedence as `backfill_machine_type` — agent-reported build first (only
SentinelOne sets one), then AD's own `operatingSystemVersion`-derived build, then
free-text OS-name matching (the only option for Carbon Black/BitDefender-only
rows, which never carry a build number). **Servers are the exception** in
`os_eol.eol_status_for_device`: build numbers are shared across products (26100 is Windows 11
24H2 *and* Server 2025; 17763 is Server 2019 *and* a 2020-EOL semi-annual release), so build
entries are keyed by `(product, build)` and a server (OS text containing "server", the same
signal as `infer_machine_type`) is resolved by its named release first, falling back to the
server build table only for year-less SAC names. Don't reintroduce a build-only lookup — it
flagged every Server 2025 machine end of life from 2026-10-14. The data is
`src/agent_parity/os_eol_data.json`, a snapshot derived from endoflife.date by
`os_eol_live.derive_lifecycle_data` — regenerate it with `tools/check_eol_drift.py --write`,
never by hand, so the snapshot and any live fetch follow the same rules.
**Live refresh**: `os_eol_live.refresh_cache` (the original tool queried endoflife.date
every run) fetches and writes `os_eol.cache_path()` (`AGENT_PARITY_EOL_CACHE`, default
`os_eol_cache.json` in the working directory, gitignored; `/app/data/os_eol_cache.json` on
the shared volume in compose). Beat runs it daily at 06:30 (`tasks.refresh_os_eol_data`,
before the 07:00 forced run) and `agent-parity run` at start when the cache is over a day
old; `refresh_os_eol: false` in `config.yaml` disables both. Lookups (`os_eol._tables()`)
prefer a valid cache, re-reading it when its mtime changes, else the bundled snapshot — a
failed fetch writes nothing and never fails a run. The package-shipped JSON is deliberately
never overwritten at runtime (each container has its own copy, and a rebuild would undo it).
Tests can't reach endoflife.date: an autouse fixture in `tests/conftest.py` points the cache
at `tmp_path` and makes every fetch fail; tests of the refresh patch
`os_eol_live.fetch_lifecycle_data` over it. Because a column is only pandas-suffixed
when it exists on *both* merge sides, watch for a bare (unsuffixed) `os_build`
column if a test helper's frame doesn't include it on both the AD and agent side —
this silently breaks the precedence logic without erroring. `eol_status` is always
one of the four `OSLifecycleStatus` values, never blank, because AD's build/OS
text is captured for every row, including `missing_agent`.

Tests for this module assert on classification outcomes and merge-invariants (row
count = union of join keys), not on `pd.merge` itself — follow that pattern for new
correlation tests rather than re-testing pandas.

## Collection pipeline (`src/agent_parity/pipeline.py`)

`run_correlation_for_client(config, client_cfg, stale_days=None)` is the config.yaml/
connector entrypoint: collect the AD export (across every domain the client spans —
see "Multi-domain clients" below), collect every enabled vendor's inventory (across
every site/tenant it has), then call `correlation.correlate()`. Returns
`(CorrelationResult | None, vendor_status)` — `None` only when every AD domain failed,
meaning there's nothing to correlate against.

`correlate_from_csvs(ad_csv_text, agent_csv_text, stale_days=14)` is the zero-config
counterpart — no `AppConfig`, no connector, no credentials, just
`ad_export.parse_ad_export()` + `agent_csv.parse_agent_csv()` feeding straight into
`correlate()`. This is the on-ramp for anyone without a supported vendor connector set
up at all; `run_correlation_for_client` is the next step once collection needs to be
repeatable/scheduled against a live API instead of a one-off export file.

No persistence and no history live in either function on purpose — that's kept as a
separate layer (`src/agent_parity/scheduling/persistence.py`, see "Scheduling & persistence" below),
not because this package avoids owning persistence (it doesn't, see "What this is"
above), but because collection/correlation and persistence are a clean seam regardless
of which package owns both sides of it. `src/agent_parity/cli.py`'s `compare` subcommand
calls `correlate_from_csvs` directly and stays pure (writes a CSV, prints a summary,
nothing persisted); `run` executes the Celery chord in `src/agent_parity/scheduling/tasks.py`
(see "Scheduling & persistence" below), which persists through `persistence.py`. `run --csv`
also writes the classified frame to `output/<client>.csv` — the chord callback returns it as
CSV text in its report, so the CSV never needs a second collection pass and works even when
the worker doesn't share the CLI's filesystem. There is deliberately no un-persisted
config.yaml path in the CLI any more (a separate pure `run` and persisted `sync` used to
duplicate each other). `run_correlation_for_client` itself stays pure and is still used
directly by `tools/gen_sample_report.py` and the tests.

## Scheduling & persistence (`src/agent_parity/scheduling/`: `db.py`, `persistence.py`, `celery_app.py`, `tasks.py`)

This package owns this layer permanently (see "What this is"). Django was never part
of the original tool: a Django project was added during the rebuild, when the plan was to
grow this into an ongoing tool for other people to use rather than a reproduction of the
original, and it has since been removed. Don't describe Django as the original or
"historical" implementation of anything. It's grouped into its own `scheduling/` subpackage — unlike the
single-module folders flattened elsewhere in this file's history, these four modules are
a genuine layered subsystem (schema → persistence → scheduled callers), not a "one class
per X" grouping like `connectors/`.

**`src/agent_parity/scheduling/db.py`** is the schema: `Client` (an identity anchor only — topology
stays in `config.yaml`, this table isn't a config cache), `Device` (keyed by join key
per client), `CorrelationRun` (one row per pipeline execution; `RunStatus` is
`pending`/`complete`/`partial`/`failed`), `CoverageSnapshot` (one row per
`CorrelationResult.frame` row, FK'd to both `CorrelationRun` and `Device`). `get_engine()`
resolves `AGENT_PARITY_DB_URL` or defaults to a local `agent_parity.db` SQLite file (gitignored) — same `${VAR}`
-or-default shape `config.py`'s `AGENT_PARITY_CONFIG`
already uses. `init_db()` is a plain `Base.metadata.create_all()` — no Alembic; this is
a lightweight run-history store sized for the demo/single-node case, not a
migration-managed production schema.

**`src/agent_parity/scheduling/persistence.py`** is the layer between `pipeline.py` (pure) and a
persisted caller: `persist_correlation` loads a classified frame into `CoverageSnapshot`
rows, `persist_result` persists an already-correlated result (or marks the run `FAILED`
outright when it's `None`) and sends the run to Splunk, and `finalize_run` is the chord
callback's correlate-then-`persist_result`, returning the `CorrelationResult` (or `None`)
so the callback can report on it without re-correlating. There is no separate synchronous
persisted entrypoint: `agent-parity run` goes through the same chord as beat. **Idempotency**: `persist_correlation` re-fetches the run inside
its own transaction and no-ops if `status != PENDING` — the pre-created `CorrelationRun`
id is the idempotency key. SQLite has no row-level lock like Postgres's
`SELECT ... FOR UPDATE`, so this instead relies on
SQLite's own writer serialization (one write transaction at a time) — adequate at this
single-node/demo scale, but a real, disclosed difference from a Postgres-backed
production database, not something to treat as equivalent. Also watch for **naive vs.
aware datetimes**: SQLite has no native timezone-aware datetime type, so a value just
read back from the database comes back naive while a freshly computed one is
timezone-aware — `persistence._naive_utc()` normalizes every datetime to naive UTC
before storing or comparing; a comparison that skips it will crash the *second* time a
device's `last_seen` needs updating (this broke once during Stage 4a verification,
fixed there, not a hypothetical).

**`src/agent_parity/scheduling/celery_app.py`**/ **`tasks.py`** are how every persisted run is
collected — `agent-parity run` and beat both call `tasks.start_client_run`, the same chord
either way, exactly as the original tool did (its manual runs went through Celery too;
sequential collection took hours). `run` executes it in-process by default
(`celery_app.run_eagerly`, so the demo needs no Redis) or on real workers with
`--workers` (fails fast via a worker `ping` if none respond); every client's chord is
dispatched before any is awaited, so clients run concurrently on workers. With
`--workers` the CLI and workers must share one `AGENT_PARITY_DB_URL` — the CLI creates
the `CorrelationRun` row and the worker's callback finalizes it by id — which the compose
`agent-parity` service does via the shared `dbdata` volume. That volume only works because
`docker/Dockerfile` creates `/app/data` owned by the non-root `parity` user; without it
Docker creates the volume root-owned and no container can write the database.
`run_eagerly` sets `task_eager_propagates=False` deliberately: a task exception is then
captured in the result like on a real worker, so the chord's `link_error` still marks
the run FAILED — with propagation on, the error escapes at dispatch and the run is left
PENDING forever. The shape: one *group* of
fan-out tasks per client (one AD export task per domain controller, one inventory-pull
task per vendor/site-tenant), feeding a *chord* callback (`correlate_client`) that runs
the correlation exactly once against the client's complete result set. Each fan-out
task is a thin wrapper around the same per-unit helper `run_correlation_for_client` loops over
(`pipeline.collect_ad_domain` per domain, `pipeline.collect_vendor_site` per site/tenant),
so both paths share one set of error handling and status keys — keep it that way rather
than re-implementing collection in `tasks.py`. The helpers never raise; a failure comes
back as an `"error: ..."` status so one broken vendor API can't stop the chord from firing; the callback records per-vendor outcomes in `vendor_status`
(`COMPLETE` vs `PARTIAL`), and `mark_run_failed` (`link_error`) is the backstop for the
callback itself blowing up. `start_client_run` does a plain `session.commit()` before
dispatching the chord,
since a committed SQLite write is immediately visible to any connection opened
afterward. `dispatch_all_clients` (the beat entrypoint) reads each client's own
`ClientConfig.sync_interval_hours` to decide whether it's due; `celery_app.py`'s
`beat_schedule` ticks it hourly plus a daily 07:00 forced run (`force=True`, ignoring
cadence) — both real, settled facts from the historical schedule, not arbitrary.
**Abandoned runs**: a run whose process dies before the callback (Ctrl-C during an
in-process `run`, a killed worker) would otherwise stay PENDING forever, so
`tasks.fail_abandoned_runs` (wrapping `persistence.fail_abandoned_runs`) marks PENDING runs
older than `config.yaml`'s `pending_run_timeout_hours` (default 24 — generous because
sequential collection took hours) as FAILED, recording why under `vendor_status["run"]`.
It runs at the start of every beat tick (`dispatch_all_clients`) and every `agent-parity
run`; a late callback for such a run is discarded by `persist_correlation`'s PENDING
check. `run --workers --timeout MINUTES` stops *waiting* at one overall deadline and
exits non-zero, but deliberately leaves the run alone — the workers may still finish it,
and the abandoned-run cleanup catches it if they don't. `--timeout` without `--workers`
is a usage error (an in-process run can't be left running).
Broker/backend default to `redis://localhost:6379/0`, overridable via
`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`.

Tests run Celery tasks with `task_always_eager`/`task_eager_propagates` (the
`celery_eager` fixture in `tests/conftest.py`) — in-process, no broker needed, same
task semantics either way. `docker/smoke_check_celery.py` (via `docker/smoke_test.sh`,
Docker-only, run by `.github/workflows/smoke.yml`) is the one thing eager-mode tests structurally can't prove: a real chord
round-tripping through a real Redis broker and real `worker`/`beat` containers.

## Splunk export (`src/agent_parity/splunk_export.py`, `persistence.py`)

Real production behavior: the original tool sent **every run whole, one Splunk event per
row of the final DataFrame**, and its dashboard displayed the most recent run. This
package does the same. (An earlier version of *this rebuild* sent per-run deltas
instead; that design was the rebuild's own invention, never the original's, and was
replaced because it lost gap closures and re-sent everything after a failed run. Don't
reintroduce deltas.) Splunk is a **sink**, never the system of record — SQLite stays
authoritative — and forwarding is entirely opt-in: `SplunkConfig.enabled` is `False`
unless both `hec_url` and `hec_token` are configured, matching every other optional
integration in this project (object storage, live vendor credentials).

**One event per row, then one summary** — `persistence.export_run_to_splunk(session, run,
result, splunk)` turns the run's classified frame into one event per row (pandas'
internal `_merge` column dropped; `to_json` handles NaN/timestamps/numpy), each tagged
with `client`/`run_id`/`run_started_at`, plus a summary event (run status,
`vendor_status`, coverage percentages and status counts from `CorrelationResult.summary`).
`splunk_export.send_run` POSTs them to HEC — newline-delimited JSON envelopes with
`time` set to the run's start for every event, batched at 100 per request, rows under
`SplunkConfig.sourcetype` (`agent_parity:coverage`) and the summary **last** under
`summary_sourcetype` (`agent_parity:coverage_summary`), raising `SplunkExportError` on
any `requests.RequestException`. Sending the summary last makes it a "run fully sent"
marker: a dashboard keyed off the latest summary's `run_id` never shows a half-sent
run, and trend charts are a `timechart` over summaries. Per-row rather than one
event holding the whole frame because Splunk truncates events at 10,000 bytes and
stops automatic JSON field extraction at 5,000 by default. A FAILED run sends
nothing (no frame); PARTIAL runs are sent, with `vendor_status` in the summary.
`splunk_export.py` has no SQLAlchemy or pandas imports; it only sees plain dicts, the
same "collection knows nothing about persistence, persistence knows nothing about the
sink" boundary the rest of this file follows.

**A Splunk outage must never fail a run** — `persistence.persist_result` wraps the
`export_run_to_splunk` call in a `try`/`except SplunkExportError`, logging and
swallowing it; the run itself stays `COMPLETE`/`PARTIAL` based on collection/
correlation outcome alone. The chord callback (`tasks.correlate_client`, which has no
live `AppConfig` in scope) loads a fresh `SplunkConfig` via `load_config().splunk`, as
its own fan-out tasks already do. The pure `compare` CLI path never touches Splunk.

## Quarterly report (`src/agent_parity/quarterly_report.py`, `scheduling/history.py`)

The original tool's data fed **a quarterly PDF report to each client**: coverage climbing
quarter over quarter, high-value assets called out, the itemized gaps, and OS end-of-life.
`agent-parity report [--client X|--all] [--quarter YYYY-QN] [--quarters 4]` rebuilds it from
the run history, writing `output/<client>-<quarter>.pdf`. Same seam as the Splunk export:
`scheduling/history.py` reads SQLAlchemy and builds plain data (`QuarterlyReport`,
`QuarterPoint`, `DeviceRow`); `quarterly_report.py` renders it and never touches the database.
Each quarter is its **last finished run** (`complete`/`partial`; pending and failed never count),
and coverage uses `correlation.coverage_pct`, the one definition `summarize()` uses too — don't
compute coverage another way in the report. Counts are rows, like every other output, except
the OS end-of-life section, which is per device (`devices_by_eol`, most severe status per
hostname), since an OS is a property of the device, not of each agent on it. Servers stand in
for high-value assets, including Domain Controllers, because the history doesn't store the AD
distinguished name needed to single DCs out (adding it would need a schema migration).

ReportLab renders the PDF and is an **optional extra** (`agent-parity[report]`; also in the dev
group and the Docker image), imported only inside `render_pdf`, which raises
`ReportDependencyError` with install instructions when it's missing. Not Plotly (its PDF export
needs a headless Chrome) or WeasyPrint (needs Pango/Cairo system libraries).

The fixtures are static, so real runs give a flat trend. `tools/seed_history.py <db>` writes a
deterministic demo history to a database you name (never the default `agent_parity.db`): it
correlates the fixtures once (the final quarter) and derives two earlier quarters by reverting
whole fully-covered devices to `missing_agent` in hash order — servers less than workstations —
stopping closest to each target, so Acme goes 46.5% → 63.6% → 81.8% with servers ahead every
quarter. `tools/gen_sample_quarterly_report.py` seeds a temp DB and writes
`docs/sample-quarterly-report.pdf` from it, plus `docs/sample-quarterly-report.png` (page one, the
README's "Results" image) rasterized with `pypdfium2` (dev-only). Not `pdftoppm`: poppler asks
fontconfig for "Helvetica-Bold" by PostScript name and gets the Regular face on macOS, so every
heading lost its bold. Regenerate both whenever the report's layout changes.

## Connectors (`src/agent_parity/connectors/`)

**Adding a vendor is "write one connector class"**, not "edit a central table."
`connectors/base.py`'s `@register_connector` class decorator adds a connector to
`CONNECTOR_REGISTRY` (re-exported as `connectors.CONNECTOR_CLASSES`) keyed by its own
`vendor` attribute — `config.load_config()` validates `config.yaml`'s `vendor:` value
against this registry. A 4th vendor needs a new module (decorated) plus one import
line in `connectors/__init__.py` to trigger registration — nothing else. That includes
the Celery path: `scheduling/tasks.py` builds one `fetch_<vendor>_inventory` task per
registered connector, rate-limited by the connector's own `inventory_rate_limit` class
attribute (set it to the vendor's practical API budget; `None` means no limit). Don't
hand-write a per-vendor task in `tasks.py`.

**`connectors/base.py` holds two layers.** `VendorConnector` — a credentialed `RestAdapter`
session, `is_live`, live/fixture dispatch for `deploy_and_run()`, `_poll_until`,
`_request`/`_request_json`/`_as_text`, `_fixture_path`, `ConnectorError`, and the
`ConnectorRegistry` class — is the generic vendor-API half and knows nothing about
inventories. `AgentConnector` subclasses it and adds only what's specific to this project:
`fetch_inventory()`/`_fixture_fetch_inventory()`/the abstract
`_live_fetch_inventory()`/`_parse_inventory()` pair, and its own
`_fixture_deploy_and_run()` override (the AD-export-CSV-by-target_id behavior below).
`CONNECTOR_REGISTRY` is agent-parity's own `ConnectorRegistry()` instance. The generic half
originally lived in a shared library and was moved into this module once nothing else used
it; keep the boundary anyway — don't add inventory or AD-export specifics to `VendorConnector`.

**`connectors/sentinelone.py` keeps the RSO mechanics in a mixin.**
`SentinelOneConnector(SentinelOneRSOMixin, AgentConnector)` — `_headers` and
`_live_deploy_and_run` (the upload -> execute -> poll `remote-scripts/status` ->
fetch-files sequence) live in `SentinelOneRSOMixin`, defined above the connector in the same
module. It's a **mixin**, not a full base class, so it combines with `AgentConnector` via
multiple inheritance. `SentinelOneConnector` itself only defines
`vendor`/`required_credentials`/`_parse_inventory`/`_live_fetch_inventory` — the
inventory-fetching half.

**Fixture fallback is not a test-only shim — it's the default runtime path.** `is_live`
gates on whether all `required_credentials` are present; if not, `fetch_inventory()`
reads `sample_data/<client>/<vendor>_inventory.json` and `deploy_and_run()` returns
`sample_data/<client>/ad_export_<target_id>.csv` — one file per domain
controller, since a client with multiple AD domains (`ClientConfig.ad_target_devices`,
see "Multi-domain clients" below) has a distinct export per domain, not one
shared file. Timestamps are rebased so the newest check-in is ~now (`rebase_timestamps` /
`rebase_csv_timestamps`, still local to this project — they operate on `AgentDevice`
and an AD-export-shaped CSV, not generic enough to share) — this is what keeps the
authored stale/recent split in `sample_data/` stable regardless of when the demo is
run. Don't add credential-checking logic anywhere else; it belongs in `is_live` alone.

**Not every vendor supports `deploy_and_run()` for real.** `supports_remote_execution`
(ClassVar, default `True`, defined on the shared `VendorConnector`) gates it —
`BitDefenderConnector` sets it `False` because GravityZone's real API has no
equivalent to SentinelOne's Remote Script Orchestration or Carbon Black's Live
Response, only predefined task types (scan, isolate, ...). `deploy_and_run()` raises
`ConnectorError` before the live/fixture fork when this is `False`, so BitDefender
can't accidentally "succeed" at something it doesn't really do, even in demo mode.
It's fetch_inventory-only — an organization on BitDefender alone can't have its AD
export collected at all; `pipeline.collect_ad_csv` raises a clear `ConfigError`
rather than silently skipping it. If a 4th vendor connector genuinely can't run
scripts either, set this the same way — don't leave `_live_deploy_and_run`
unimplemented and let it fail some other way.

Live mode goes through `agent_parity.vendor.rest_adapter` (`RestAdapter`) rather than a
bare `requests.Session` — retries/backoff on 429/5xx are configured there once,
shared by all three vendors (wired up inside `VendorConnector.__init__`, not
per-connector).

**`src/agent_parity/vendor/` holds copies of a former shared library.** The HTTP adapter
(`rest_adapter.py`), `atomic_io`, `logging_setup`, `tabular_io` and the `${VAR}` resolver in
`config.py` (a partial copy: only `ConfigError` and `resolve_env_refs`) were copied from a shared
library (`py-shared-tools` v1.3.1, commit d54dcd6; each module says so in a header comment) so the
repo is self-contained — no git dependency, no submodule, and
`uv sync && uv run pytest` works from a plain clone. The standalone library is not a
dependency and is not kept in sync: this repo owns these copies, so edit the files here. Their tests live in `tests/vendor/`.
The pieces only this project used — `VendorConnector`/`ConnectorRegistry`/`ConnectorError`, the
SentinelOne RSO mixin, the storage-backed script export, `ObjectStorage` and the storage
config — were later moved out of it into the modules that own them
(`connectors/base.py`, `connectors/sentinelone.py`, `script_runner.py`, `storage.py`,
`config.py`).

Two of the shared, stdlib-only helpers are used outside the connector stack:
`src/agent_parity/scheduling/db.py`'s
`get_engine()` calls `agent_parity.vendor.atomic_io.ensure_dir()` on a file-based
`AGENT_PARITY_DB_URL`'s parent directory before `create_engine()` (a fresh
Docker-volume path with no directory yet would otherwise fail); `cli.py`'s
group callback calls `agent_parity.vendor.logging_setup.setup_logging(level=WARNING)`
so the `logger.warning`/`.exception` calls already scattered across
`pipeline.py`/`persistence.py`/`tasks.py` print with a timestamp and logger
name instead of Python's unconfigured bare-message default — `run --csv`/`compare`
also write their CSV output through `agent_parity.vendor.atomic_io.ensure_dir()`/
`atomic_write()` instead of `Path.mkdir()`/`DataFrame.to_csv(path)` directly,
so a crash mid-write can never leave a truncated CSV or DB file behind.
The library's `config_loader.ConfigLoader` and `retry.call_with_retry` were **not** inlined — nothing here uses them:
`config.py`'s own `load_config()`
already does far more domain-specific work than a generic loader, and
`RestAdapter`'s transport-level retry already covers 429/5xx (no connector has
hit the "200 OK but unusable body" failure `call_with_retry` exists for).

`RestAdapter.request()` returns already-parsed content (`dict` for JSON, `str`
for text/html, `bytes` otherwise), not a `Response` object, so connector call
sites use `self._request_json(...)` when they know the endpoint returns a
JSON object, or `self._as_text(...)` on the raw `_request(...)` result when
they need guaranteed text (e.g. SentinelOne's fetch-files script output). No
test exercises real network I/O; `tests/connectors/test_connectors.py` proves the
RestAdapter wiring (retry config, JSON/text parsing) by monkeypatching the
underlying `requests.Session.request`, not by hitting a live API.
`RestAdapter`'s own unit tests (content-type parsing, header merging, retry
config, the `files=` passthrough) live in `tests/vendor/test_rest_adapter.py`.

**`AgentDevice.platform`/`machine_type` are normalized to SentinelOne's wording**
(most of the historical client base was on S1, so its vocabulary is canonical).
`_parse_inventory` in each connector sets them: SentinelOne passes its own
`osType`/`machineType` straight through; Carbon Black lowercases its uppercase
`os` enum for `platform` and infers `machine_type` from OS text (`infer_machine_type`, defined in
`src/agent_parity/models.py`, re-exported from
`connectors/base.py` for existing call sites) since it has no equivalent field;
BitDefender maps its numeric `machineType` enum to S1's string wording (`_MACHINE_TYPES` in `connectors/bitdefender.py`)
and infers `platform` from OS
text (`infer_platform`) since it has no equivalent field. `infer_platform`/
`infer_machine_type` live in `models.py`, not `connectors/base.py`, specifically
so `correlation.py` can use them too (for AD-only rows — see
`backfill_machine_type`) without pulling in the connector stack's
`requests`/`RestAdapter` dependency chain
just for two pure string functions.
If a 4th vendor is
added, decide per-field whether it reports something directly-mappable (prefer a direct map, like BitDefender's
`machineType`) or needs inference (like Carbon Black's `machine_type`) — don't guess when the vendor's raw API
actually has the field. **`agent_version` is deliberately never touched this
way** — each vendor's version numbering is real and vendor-specific; making
one look like another's would be fabricating a value, not normalizing one.

## AD-export object storage (`agent_parity.script_runner`)

**The storage-backed handoff lives in `script_runner.py`** —
`run_script_export` implements it, and `run_ad_export` is a thin wrapper
supplying this project's own script path (`AD_EXPORT_SCRIPT`), object-key
prefix (`"ad-exports"`), expected CSV header (`"Name"`), and error wording to
`run_script_export`. `ScriptExecutionError` is defined there too. **Mandatory for any live connector** — not an optional
upgrade — because vendor remote-execution output channels (SentinelOne RSO's
fetch-files, Carbon Black Live Response's command output) don't reliably
preserve a CSV's exact formatting (encoding, line endings) and have real
output-size limits a full AD export can exceed:

1. `run_script_export` raises `ScriptExecutionError` immediately if
   `connector.is_live` and `storage is None` — a live export with no storage
   configured is a configuration error, not something to silently work
   around by falling back to the vendor channel. Don't reintroduce that
   fallback.
2. Otherwise it generates a presigned PUT URL (`ObjectStorage.presigned_put_url`,
   15-minute default expiry) and passes it to `deploy_and_run(..., script_args={"UploadUrl": ...})`.
3. The script uploads its own CSV there — the vendor call's return value is
   discarded entirely, since the real output never goes through it.
4. `run_script_export` downloads with `get_object` and deletes the object (best-effort; failures there only log, they
   never fail an export that
   already succeeded).

**Fixture mode is the one exception** — `run_script_export` checks
`connector.is_live` *before* the storage check. There's no real endpoint in
fixture mode to have uploaded anything, so it always returns the canned
`sample_data/` CSV directly, regardless of whether storage happens to be
configured. This is also why the uv demo path can leave `STORAGE_*` unset in
`.env`: safe only because the vendor has no live credentials there either, so
no script ever actually runs.

Built against the S3 API via `boto3`, not a specific product — the compose `s3` service
([Versity S3 Gateway](docs/decisions/0011-versity-s3-gateway-for-local-dev.md), pinned, serving a local
directory) for local/dev, real AWS S3 in
production, same `ObjectStorage` class either way; only `endpoint_url`
changes. It was MinIO until MinIO withdrew its community images (the stack broke on
a floating `latest` tag) — keep the local server's image pinned, and don't reintroduce
product-specific tooling (the old `mc` healthcheck) into the compose file. This is *not* Azure Blob Storage capable — different API, would need
a second implementation with a different SDK, not just different config. **`StorageConfig`/`parse_storage_config`/`build_storage` live in
`config.py`** —
`get_storage(config)` there is a one-line delegate to
`build_storage(config.storage)`.
`get_storage(config)` returns
`None` when unconfigured (`config.storage.enabled` is False);
`config.storage.backend` only supports `"s3"` today, and `build_storage` raises
`ConfigError` for anything else.

Only SentinelOne and Carbon Black connectors accept `script_args` meaningfully (BitDefender doesn't implement
`_live_deploy_and_run` at all). SentinelOne passes
them as RSO's `inputParams`; Carbon Black appends them to the raw PowerShell
command line (`CarbonBlackConnector._powershell_args`) since Live Response's
`create process` takes a command string, not structured parameters — different
mechanisms, same `script_args: dict[str, str]` contract from `deploy_and_run`.

Tests use `moto` (`@mock_aws` / the `mock_aws()` context manager) — no real
S3 server touches the test suite, and a real presigned-URL PUT/GET round
trip still gets exercised. `ObjectStorage`'s own unit tests live in
`tests/test_storage.py`; the *orchestration logic*
(mandatory-storage rule, fixture bypass, upload/download/cleanup, empty/
wrong-shaped output) is tested in `tests/test_script_runner.py` with a generic
fake connector, alongside a few *wiring* tests proving
`run_ad_export` threads its own `object_key_prefix`/`header_marker`/script
path through correctly.
`moto` proves the code path, not the network — `docker/smoke_check_storage.py`
(run via `docker/smoke_test.sh`, Docker-only) round-trips a real object through
the actual `s3` service, including auto-creating the smoke-test bucket (`ObjectStorage` itself has no bucket-admin
methods on purpose; production
bucket provisioning is out-of-band, so that stays smoke-test-only code).

`docker/Dockerfile` is a separate, bare-bones concern from the `s3`
service above — it builds a standalone image for running the `agent-parity`
CLI itself (`docker build -f docker/Dockerfile -t agent-parity .`; entrypoint
is `uv run --no-sync agent-parity`, `--no-sync` because a plain `uv run`
would re-resolve against `uv.lock`'s full `[dev]` group on every container
start, silently reinstalling `moto`/`boto3-stubs`/etc. that `--no-dev`
deliberately excluded from the image at build time). `docker-compose.yml`'s
`agent-parity` service just wires that Dockerfile up alongside `s3`, so
`docker compose run agent-parity run` works out of the box. This is now this
project's actual deployment story (see "Scheduling & persistence" above) —
not a placeholder for a separate hub project's deployment, since that plan
is archived; the `worker`/`beat`/`redis` services in the same compose file
are the scheduled path, sharing this same image. Congruent with
`credential-audit`'s own (archived) `docker/Dockerfile` history — no longer
needs to stay in sync with a project that isn't being developed further.

## Credential resolution (`src/agent_parity/config.py`)

`load_config()` parses `config.yaml` (topology) + the environment (secrets; `.env` isn't read by the code, load it
with `uv run --env-file .env ...`) into `AppConfig`/
`ClientConfig`/`VendorConfig` dataclasses — every secret in `config.yaml` is a `${VAR}`
reference; an unset variable resolves to `None` rather than raising, which is exactly
what puts a connector into fixture mode. This is the *only* config entrypoint — the
SQLite run history never stores topology or credentials, so there's no second source
of configuration to consult.
`sites_for(client_slug, vendor_name)` returns one merged dict per site/tenant a (client, vendor) pair has — almost
always a one-element tuple, more for a client
with multiple sites/tenants (see "Multi-site/tenant" below) — it's the one place
that knows `global` vs `per_client` scope (`VendorConfig.scope`) — SentinelOne/
BitDefender are global (same credentials for every client), Carbon Black is
per-client. When adding a vendor or a client, this is the function whose behavior
actually matters; don't special-case scope logic in a connector or in `pipeline.py`.
`get_connectors(config, client_slug, vendor_name)` builds one connector per entry
`sites_for()` returns and needed no changes at all when multi-site/tenant landed —
that tuple return was deliberately shaped for it from the start so `pipeline.py`
and `get_connectors` would never need to change again.

`pick_ad_export_vendor(client_cfg)` picks which of a client's enabled vendors carries
the AD export — filtered to `supports_remote_execution = True` connectors, then broken
by each connector's own `ad_export_priority` class attribute, not alphabetically. That
preference (SentinelOne before Carbon Black) is a real business fact (S1 covered most
of the client base, CB a handful, BitDefender basically none) as much as a technical
one. Raises `ConfigError` if a client has no capable vendor at all. Called from
`pipeline.collect_ad_csv`; don't reintroduce a `sorted(client_cfg.vendors)[0]`-style
pick elsewhere — that bug (silently routing AD export through whichever vendor happens
to sort first, capable or not) is exactly what this function replaced.

## Multi-domain clients (`ClientConfig.ad_target_devices`)

A client can span more than one AD domain/forest — no single domain controller can
enumerate computer objects outside its own domain, so `ad_target_devices` is a tuple,
not a single hostname, and the export script runs once per entry.
`src/agent_parity/pipeline.py`'s `collect_ad_frame` is the orchestrator: it loops the tuple,
calling `collect_ad_domain` per domain (which collects and validates that the export
parses, so a malformed export fails only its own domain), and concatenates the results
(`ad_frame_from_csvs`, via `src/agent_parity/ad_export.py`'s `concat_ad_frames`) into the one master
DataFrame `correlate()` actually sees. A single-domain client (most of them) is just
the `len == 1` case of this same loop — there's no separate single-domain code path,
by design.

Tolerant of partial failure the same way per-vendor collection already is: one
domain's status is recorded independently (`f"ad:{target_device}"` in `vendor_status`,
e.g. `ad:GLOBEX-DC01`), and `collect_ad_frame` returns `None` for the frame only when *every* domain failed —
`run_correlation_for_client` is where that "nothing to
correlate against" case is handled (returns `None` up to its own caller rather than
attempting to correlate against nothing); don't duplicate that check elsewhere.

Fixture mode picks the CSV by target device — `sample_data/<client>/ad_export_<target_device>.csv`
(`connectors/base.py`'s `deploy_and_run`) — one file per domain, not one shared
`ad_export.csv`. The demo's `globex` client is intentionally multi-domain (`GLOBEX-DC01` + a branch office
`GLOBEX-BR-DC01`, both in `config.yaml` and
`sample_data/globex/`) so this path has real test/demo coverage; `acme` stays
single-domain.

## Multi-site/tenant (`ClientConfig.vendors`, `AppConfig.sites_for`)

A client can have more than one site/tenant *within* a single vendor's console —
`ClientConfig.vendors` maps a vendor name to a tuple of dicts, one per site/tenant (almost always a one-element tuple),
mirroring the AD multi-domain shape above.
What each dict holds depends on the vendor's `scope`:

- **`per_client` (Carbon Black)**: each entry is a complete, independent credential
  block — a second entry means a second, genuinely separate CB org (e.g. a branch
  office on its own tenant). `sites_for` returns these as-is; there's nothing to
  merge them with, and the connector itself needed zero code changes to support
  this — it was always just "build one connector per site_for () entry."
- **`global` (SentinelOne, BitDefender)**: one shared credential set (per named
  account — see "Multiple named accounts" below) covers the whole account, but
  a client's endpoints can be scoped to a slice of it via an optional filter
  key merged onto the resolved account's credentials in `sites_for`.
  SentinelOne's is `site_ids` (a real, documented "Sites" concept — a
  comma-separated list matched against each item's own `siteId`, sent as the
  `GET /web/api/v2.1/agents` `siteIds` query param live, or filtered locally in
  fixture mode via `SentinelOneConnector._in_scoped_sites`). BitDefender's is
  `company_id` (GravityZone Cloud MSP's "Company" tenant concept, matched against
  `companyId` via `BitDefenderConnector._in_scoped_company`) — **this one is not
  verified against real GravityZone API docs or a live tenant**, only plausible
  given GravityZone's own MSP company hierarchy; treat it the same way this
  project already treats its one other invented-then-removed GravityZone
  capability (`createCustomScriptTask`) — confirm before relying on it live. An
  unset filter (the common case) means the whole account, unchanged from before
  this existed.

A site/tenant can carry an optional `label` (e.g. `"branch"`) which does two
things: it's what `pipeline.site_status_key` uses instead of a bare index in
`vendor_status` (`carbonblack:branch` rather than `carbonblack:1`), and it's what
`connectors/base.py`'s `_fixture_fetch_inventory` uses to pick a distinct fixture
file (`{vendor}_inventory_{label}.json`) — a labeled tenant is real, independent
data, so it shouldn't silently share the unlabeled tenant's fixture. The demo's
`acme` client is the multi-tenant one: two real Carbon Black tenants (primary,
unlabeled, plus a `label: branch` one reading `ACME_CB2_*` env vars and
`sample_data/acme/carbonblack_inventory_branch.json`).

## Multiple named accounts per global vendor (`VendorConfig.accounts`)

A global vendor doesn't mean *one* credential set, either: `VendorConfig.accounts`
is `dict[str, dict]` — account name -> credentials — always named, even a lone
one (BitDefender's `"default"` today), same "no special-cased single case"
principle as `ad_target_devices`/`ClientConfig.vendors`. This is real, not
hypothetical: there were two genuinely separate SentinelOne consoles in
practice (`"mssp"` for ordinary managed-services clients, `"dfir"` for clients
under active incident response) — a distinct engagement, a distinct console,
not just a Site within one account (that's the previous section — orthogonal,
and composable: a site dict can carry both `"account"` and a site filter like
`site_ids` at once).

`AppConfig._resolve_account(client_slug, vendor, site)` is where a site's
`site.get("account")` gets resolved: explicit account name wins; omitted and
the vendor has exactly one account, use it (today's implicit default,
unchanged for every existing single-account setup); omitted and there's more
than one, `ConfigError` — ambiguous is a config error, not a silent pick;
unknown account name, `ConfigError` too. Don't special-case "just default to
the first one alphabetically" here — that's exactly the kind of silent-pick
bug `pick_ad_export_vendor` already exists to avoid elsewhere in this file.

## Testing conventions

- Test files mirror `src/agent_parity/`'s subpackage layout, same convention
  vega-tools uses: `tests/connectors/`, `tests/scheduling/` and `tests/vendor/`
  pair with `connectors/`, `scheduling/` and `vendor/` (each with its own `__init__.py`), since
  those are genuine multi-module subpackages. Everything else stays flat in
  `tests/` because its source module is flat too — don't nest a test file
  one level deeper than its module actually lives.
- `tests/test_pipeline_sync.py` pins the specific gap scenarios authored into
  `sample_data/` by join key (e.g. `acme-sql02` is `missing_agent`). If you
  regenerate or edit the fixtures, these tests are the regression check — a
  scenario silently changing status is a fixture bug, not a test bug, unless the
  change was intentional.
- `sample_data/` fixtures were originally generated by a one-off script that was
  never committed (it lived in a scratch directory outside the repo). The
  committed CSV/JSON files are the source of truth now; there's no
  `generate_fixtures.py` in this repo to regenerate them from. The layout is
  per-client (`sample_data/<client_slug>/`) — `get_connectors`' `fixture_dir`
  is always `SAMPLE_DATA_DIR / client_slug`.
- Test coverage is intentionally close to 1:1 with source modules: `test_models.py` ↔
  `src/agent_parity/models.py`,
  `test_pipeline.py` ↔ `src/agent_parity/pipeline.py` (the collection helpers plus
  `correlate_from_csvs`, deliberately exercised with hand-rolled CSVs rather than
  `sample_data/`, to prove that path has zero dependency on the demo fixtures),
  `test_agent_csv.py` ↔ `src/agent_parity/agent_csv.py`, `test_cli.py` ↔
  `src/agent_parity/cli.py`, `test_config.py` ↔ `src/agent_parity/config.py`. The
  copied `src/agent_parity/vendor/` modules (`RestAdapter`, ...) have one test file each in
  `tests/vendor/`, all part of the normal `uv run pytest`.
  `tests/connectors/test_connectors.py` covers `AgentConnector`'s own
  inventory-fetching and fixture-deploy-and-run behavior — the generic
  dispatch/polling/registry mechanics are tested in
  `tests/connectors/test_base.py`. When adding a
  new module with real logic in it, add its
  test file alongside — don't rely on it being incidentally exercised by a
  higher-level pipeline test.
