# agent-parity

Device coverage reconciliation: correlate an Active Directory computer
inventory against EDR/security agent inventories (SentinelOne, Carbon Black,
BitDefender) to answer three questions a SOC or compliance team actually
cares about:

1. **Missing coverage** — devices AD knows about that no agent is reporting on.
2. **Orphaned agents** — agents phoning home for a device AD has no record of
   (decommissioned machines, shadow IT, naming mismatches).
3. **Stale coverage** — matched devices whose agent hasn't checked in recently
   (silently failed install, network issue, tampering).

This is a from-scratch rebuild of a tool I originally built professionally,
using entirely synthetic data. No proprietary code, client data, or
credentials are involved; vendor API interactions are shaped from public API
documentation, and **everything runs against local fixtures by default** —
no live credentials required.

## Quick start (demo mode — no Docker, no Redis, no Celery)

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```console
uv sync
uv run agent_parity_web/manage.py migrate
uv run agent_parity_web/manage.py seed_demo        # two runs of history per client
uv run agent_parity_web/manage.py runserver
```

Open http://127.0.0.1:8000/ — coverage overview, per-vendor breakdown, and a
trend chart per client. `/devices/` is a filterable device list; each device
links to its status history across every run. `manage.py createsuperuser`
unlocks `/admin/` for raw model browsing.

`seed_demo` runs the pipeline twice per client: once against the fixtures
as-authored (backdated a day), once through a deterministic "drift" transform
that plays out a plausible day of change — a gap remediated, an agent gone
quiet, an orphan decommissioned, a new unmanaged workstation. Every
transition on the dashboard is one of those four, on purpose. For a single
plain run, use `uv run agent_parity_web/manage.py sync_and_correlate`
(`--client <slug>` / `--all`).

```console
uv run pytest        # 41 tests, all offline
```

## Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │            per (client, vendor)              │
                       │                                              │
  config.yaml ──► agent_parity/config.py ──► connector (S1 / CB / BD) │
   + .env              │                       │            │         │
                       │            deploy_and_run()   fetch_inventory()
                       │                       │            │         │
                       │        Export-ADDevices.ps1     AgentDevice  │
                       │          runs REMOTELY on a     records      │
                       │          domain-joined endpoint    │         │
                       └───────────────│───────────────────│──────────┘
                                       ▼                    ▼
                         ad_sync/parser.py          correlation/engine.py
                          (CSV -> DataFrame)   (outer merge + classification)
                                       └─────────┬──────────┘
                                                 ▼
                              Django ORM (system of record)
                     Client ─ Device ─ CorrelationRun ─ CoverageSnapshot
                                    │                        │
                                    ▼                        ▼
                            Django dashboard        reporting/splunk_export.py
                        (overview, list, history,     (optional HEC deltas)
                         Chart.js trend)
```

Two packages, one boundary:

- **`agent_parity/`** — the pipeline: connectors, AD parsing, the pandas
  correlation engine, the Splunk exporter. Deliberately free of Django and
  Celery imports.
- **`agent_parity_web/`** — the Django project: ORM models, the dashboard,
  the management commands, and the Celery tasks. `dashboard/services.py` is
  the single implementation of collect → correlate → persist that both
  entrypoints call.

### The deployment model: remote script execution, not direct AD access

agent-parity never binds to LDAP and holds no domain credentials. Instead,
`Export-ADDevices.ps1` is pushed to an already domain-joined, already-managed
endpoint and executed through the security vendor's own remote scripting
capability — the same trust relationship that's already in place for the
agent itself:

- **SentinelOne** — Remote Script Orchestration: upload to the script
  library, execute against a target agent, poll
  `/web/api/v2.1/remote-scripts/status`, fetch the result.
- **Carbon Black Cloud** — Live Response session: `put file` to stage the
  script, `create process` to run PowerShell, read stdout from the session.

**Not BitDefender GravityZone.** Its remote-task API is real but limited to
predefined task types (scan, isolate/deisolate, install/uninstall, patch
management, ...) — nothing equivalent to "push and run an arbitrary script."
An earlier version of this connector modeled a `createCustomScriptTask` RPC
method to paper over that, but that method doesn't actually exist in
GravityZone's public API, so it's been removed rather than left implying an
accuracy it didn't have. `BitDefenderConnector.supports_remote_execution =
False`; it's fetch_inventory-only, and `deploy_and_run()` refuses outright
(in both live and fixture mode) rather than silently succeeding.

`deployment/script_runner.py` is the uniform entry point; each connector's
`deploy_and_run()` implements the vendor mechanics. AD collection and agent
inventory both flow through the same authenticated channel per vendor — for
whichever vendor is actually carrying the AD export. Every client needs at
least one enabled vendor with real remote-execution capability;
`agent_parity/config.py`'s `pick_ad_export_vendor()` picks it, preferring
SentinelOne over Carbon Black (reflecting real deployment prevalence — the
bulk of the original client base was on SentinelOne, a handful on Carbon
Black, one on BitDefender) and raising a clear `ConfigError` if a client has
neither.

All three connectors share one HTTP transport — `agent_parity/rest_adapter.py`
(`RestAdapter`) — instead of a bare `requests.Session`: automatic retries with
backoff on 429/5xx, content-type-aware parsing (JSON responses come back as
`dict`, text/HTML as `str`, everything else as raw `bytes`), and a single place
to add auth/proxy config if a vendor ever needs it. `connectors/base.py`'s
`_request_json()`/`_as_text()` helpers narrow that `dict | str | bytes` result
for call sites that know which one they expect.

### The correlation: a pandas merge, kept honest

`correlation/engine.py` reduces the whole reconciliation to one analytical
move, structured as a `.pipe()` chain so each stage is independently
testable:

```python
(
    ad_df.pipe(add_join_key)  # hostname -> normalized join key
    .pipe(merge_with_agents, agents_df)  # outer merge, indicator=True
    .pipe(classify_coverage, stale_days=14)  # indicator + staleness -> status
)
```

The merge indicator *is* the classification: `left_only` → `missing_agent`,
`right_only` → `orphaned_agent`, `both` → `covered` or `stale_coverage`
depending on a vectorized `last_seen` check (`np.select`). Join keys are
hostnames with the DNS suffix stripped, lowercased, and trimmed — so
`ACME-WS-014.corp.acme.example` and `acme-ws-014` correlate. Coverage
percentages and per-vendor gap lists fall out of `groupby`/`value_counts`.

### Why Django replaced a Splunk dashboard

The original professional version pushed correlated results into Splunk
because stakeholders already looked there — not because a log index is the
right home for relational data. This rebuild splits the two concerns Splunk
was covering:

- **Persistence**: the Django ORM models the structure the data actually has
  — `CorrelationRun` per execution, `CoverageSnapshot` per device/vendor
  observation, FK'd to both `Device` and run. A device's history across runs
  is a query, not a correlation of disconnected log events. Admin gives free
  CRUD; migrations come with the framework.
- **Visibility**: the dashboard (server-rendered templates + one Chart.js
  chart) shows coverage %, per-vendor health, per-status counts via ORM
  aggregation — never by re-deriving pipeline logic in a view.

**Splunk stays, demoted to an optional sink** (`reporting/splunk_export.py`):
config-gated, no-op without an HEC token, emitting *deltas* (state
transitions since the previous run) as structured JSON with an explicit
sourcetype into a dedicated index. The database remains the system of record;
Splunk never re-derives classification in SPL.

### Scaling: Celery group/chord across clients

`deploy_and_run` is slow and I/O-bound — stage a script, poll a vendor's
remote-execution status, fetch output — and doing that serially across many
clients × three vendors is the real bottleneck. It's embarrassingly
parallel, and a chord is exactly its shape:

- **Fan-out (group)**: one task per (client, vendor) inventory pull plus one
  AD-export task per client. Each vendor task carries its own Celery
  `rate_limit` reflecting that vendor's real API throttling.
- **Fan-in (chord callback)**: one task per client receives the complete
  result set, runs the same pandas correlation, and persists — correlation
  never races partial state.
- **Idempotency**: the `CorrelationRun` row is created (empty, `pending`)
  *before* dispatch, and its ID rides through the callback; a retried or
  duplicated callback finds the run finalized and no-ops under a row lock.
  Dispatch happens in `transaction.on_commit()` so a worker can't observe a
  run ID that hasn't committed — the classic Celery+Django race.
- **Partial failure**: fan-out tasks return `{"ok": False, "error": ...}`
  rather than raising, so one flaky vendor API can't stop the chord; the run
  lands as `partial` with per-vendor outcomes recorded. `link_error` on the
  callback is the backstop that marks a run `failed` instead of leaving it
  `pending` forever.
- **Scheduling**: Celery beat ticks `dispatch_all_clients` hourly; each
  client's `sync_interval_hours` in config.yaml decides whether it's due.

The management command and the chord callback call the same functions in
`dashboard/services.py` — the parallelism is additive infrastructure, not a
rewrite of the pipeline.

### Credentials: config.yaml is topology, .env is secrets

Vendors have genuinely different credential shapes: SentinelOne is one API
token for the whole organization; Carbon Black needs a distinct API ID /
secret / org key **per client**. A flat `.env` can't express that asymmetry,
so it's split:

- **`config.yaml`** (committed) declares each vendor's credential `scope`
  (`global` or `per_client`) and each client's enabled vendors — with every
  secret value a `${VAR}` reference, never a literal.
- **`.env`** (gitignored; see `.env.example`) holds the actual values, plus
  Django/infra secrets.
- **`agent_parity/config.py`** resolves the references and answers the one
  question everything else asks: *given (client, vendor), which connector
  with which credentials?* Global scope returns the same token for every
  client; per-client scope returns that client's block. A `${VAR}` pointing
  at an unset variable resolves to `None`, which is precisely what puts a
  connector into fixture mode — a fresh checkout with no `.env` runs the
  entire pipeline on `sample_data/`.

## Sample data

Two synthetic clients with deliberate, reviewable gap scenarios:

|                     | Acme Corp (`acme`)                                                            | Globex (`globex`)         |
|---------------------|-------------------------------------------------------------------------------|---------------------------|
| AD computer objects | 41                                                                            | 32                        |
| Vendors             | SentinelOne + Carbon Black + BitDefender                                      | SentinelOne + BitDefender |
| Missing agent       | 5 (new server, new-hire imaging gaps, a rebuild, a disabled stray)            | 4                         |
| Stale coverage      | 3 (15–30 days quiet, one per vendor)                                          | 3                         |
| Orphaned agents     | 4 (decommissioned server, shadow-IT laptop, workgroup kiosk, renamed machine) | 3                         |

Details worth noticing: some devices report to two vendors (exercising the
one-row-per-vendor merge); one agent per client reports its FQDN while AD has
the short name (normalization resolves it); one orphan per client is a
renamed machine normalization deliberately *can't* resolve. Fixture
timestamps are rebased at load so the newest check-in is always "now" and the
authored stale/recent split stays stable regardless of when you run the demo.
The asymmetric vendor topology is what makes the chord fan out a different
task set per client.

## Scaled mode (Docker Compose + Celery)

```console
cp .env.example .env                        # fill in POSTGRES_PASSWORD at least
cd docker
docker compose up --build                   # dev: runserver, bind mounts, DEBUG
```

The base file defines `web`, `worker` (scale with `--scale worker=N`),
`beat`, `redis`, and `db` (Postgres); `docker-compose.override.yml` is
applied automatically for development. Seed data inside the stack:

```console
docker compose exec web python manage.py seed_demo
```

Production applies the prod overlay explicitly — gunicorn (the image
default), no bind mounts, restart policies, two worker replicas, secrets
from the deployment environment:

```console
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Tests

`uv run pytest` — all offline, no broker:

- **Correlation**: one test per `CoverageStatus` outcome, the
  merged-row-count-equals-union-of-join-keys invariant, FQDN/case
  normalization, configurable staleness, multi-vendor rows.
- **Chord semantics** (eager mode): one vendor failing yields a `partial`
  run with the other vendors' snapshots intact; duplicate callback delivery
  doesn't double-count (idempotency); per-client cadence gating.
- **Config resolver**: global vs. per-client scope, `${VAR}` resolution,
  fixture-mode fallback on unset secrets.
- **Connectors and parser**: fixture normalization, timestamp rebasing,
  live-mode gating on complete credentials.
- **Fixture scenarios**: named tests pin the authored gap scenarios
  (`acme-sql02` is missing, `acme-fs-old` is orphaned, …) so a fixture edit
  that breaks a scenario fails loudly.

## Out of scope for v1

- Per-client logins/permissions — `Client` scopes the data model; the
  dashboard itself is single-operator on Django auth.
- Real-time ingestion — this is a batch tool on a schedule.
- Fuzzy hostname matching beyond normalization (a natural next step for the
  renamed-machine orphans).
- A REST API layer (DRF would slot in if a JS frontend ever needed it) and
  Kubernetes (Compose demonstrates the deployment story).
