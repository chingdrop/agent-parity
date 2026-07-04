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

The original tool existed to feed a quarterly report sent to clients: show
that agent coverage was trending upward over time, and flag high-value
assets (Domain Controllers, file/storage servers) specifically, so gaps
there got prioritized over a missing agent on a random workstation. A third
axis works the same way: a device running an OS that's already end-of-life
(or soon will be) is a risk finding independent of whether an agent is
installed on it. All three are first-class in this rebuild, not just implied
by the raw data — see [High-value assets](#high-value-assets-servers-as-the-prioritization-signal)
and [OS end-of-life](#os-end-of-life-a-third-prioritization-axis) below.

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
unlocks `/admin/` for raw model browsing, and `/setup/` (same staff login)
for adding/editing clients and vendor credentials by hand instead of
re-running `import_config` — see
[Credentials](#credentials-db-backed-topology-configyaml-as-a-one-time-import)
below.

`seed_demo` runs the pipeline twice per client: once against the fixtures
as-authored (backdated a day), once through a deterministic "drift" transform
that plays out a plausible day of change — a gap remediated, an agent gone
quiet, an orphan decommissioned, a new unmanaged workstation. Every
transition on the dashboard is one of those four, on purpose. For a single
plain run, use `uv run agent_parity_web/manage.py sync_and_correlate`
(`--client <slug>` / `--all`).

```console
uv run pytest        # 200+ tests, all offline
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

(`config.yaml + .env` in the diagram is a one-time import now, not something
read on every run — see
[Credentials](#credentials-db-backed-topology-configyaml-as-a-one-time-import)
below for what replaced it.)

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

### Multi-domain clients: one export per domain, concatenated into a master list

A client isn't always a single AD domain — some span multiple domains or
forests, and no one domain controller can enumerate computer objects outside
its own domain. `ClientConfig.ad_target_devices` (`agent_parity/config.py`) is
a list, not a single hostname: `Export-ADDevices.ps1` runs once per entry, and
`dashboard/services.py`'s `collect_ad_frame` parses and concatenates the
resulting CSVs (`agent_parity/ad_sync/parser.py`'s `concat_ad_frames`) into
one master AD DataFrame before correlation ever runs. A single-domain client
is just the one-element case of the same list — not a special code path.

Collection is tolerant of partial failure the same way per-vendor inventory
collection already is: one domain being unreachable doesn't sink the others,
and the scaled (Celery) path fans out one `collect_ad_export` task per domain
the same way it already fans out one task per vendor. Only when *every*
domain fails does the run fail outright (nothing at all to correlate
against) — `dashboard/services.py`'s `finalize_run` is where both the
synchronous path and the Celery chord callback share that "no AD data" check,
rather than each re-implementing it. Per-domain outcomes show up in a run's
`vendor_status` keyed `ad:<target_device>` (e.g. `ad:GLOBEX-DC01`), alongside
the plain vendor-name keys for agent inventory.

Vendor agent inventories are unaffected by any of this — agents in every
domain still report to the same per-client vendor console, so only the AD
side of collection fans out. The demo's `globex` client models this: it has
two domains (`GLOBEX-DC01` and a branch office `GLOBEX-BR-DC01`) in
`config.yaml`/`sample_data/globex/`, while `acme` stays single-domain.

### AD-export handoff: object storage instead of the vendor channel (mandatory for live exports)

Vendor remote-execution output channels are not a reliable way to get a full
AD export back: RSO/Live Response output handling doesn't consistently
preserve exact formatting — encoding, line endings — and has real
output-size limits a large environment's export can exceed. So the handoff
doesn't go through them at all:

1. agent-parity generates a short-lived, single-object **presigned PUT URL**
   (default 15-minute expiry) — the remote endpoint never holds a standing
   storage credential, only a URL that can write exactly one key before it
   expires.
2. That URL is passed to the script as an argument (SentinelOne via RSO's
   `inputParams`, Carbon Black by appending it to the raw PowerShell command
   line — see each connector's `_live_deploy_and_run`). The script
   (`Export-ADDevices.ps1 -UploadUrl ...`) uploads its CSV directly there
   instead of printing it to stdout.
3. The vendor's remote-execution call only needs to report that the script
   *ran*; its stdout is ignored entirely.
4. agent-parity downloads the object with a plain authenticated GET (its own
   credentials, not the presigned URL) and deletes it — best-effort cleanup
   that never fails an export that already succeeded.

This is built against the **S3 API** (`boto3`), not a specific product:
`agent_parity/storage.py`'s `ObjectStorage` talks to a self-hosted **MinIO**
instance (the Docker Compose stack runs one) for local/dev/demo use, or real
**AWS S3** in production, with `endpoint_url` as the only thing that changes.
It is *not* Azure Blob Storage capable — Blob doesn't speak the S3 API, so
that would need a second implementation with a different SDK, not just
different credentials.

**Storage is required for any live export** — `run_ad_export` raises a clear
error rather than falling back to the vendor channel if a live connector
reaches it with no storage configured. The one exception is fixture mode: a
non-live connector has no real endpoint to upload anything from, so it always
returns the canned `sample_data/` CSV directly, regardless of whether storage
happens to be configured. Storage is unconfigured by default in the uv demo
path (`STORAGE_BUCKET`/`STORAGE_ACCESS_KEY`/`STORAGE_SECRET_KEY` all resolve
to `null` with no `.env`) — that's only safe because the demo path has no
live vendor credentials either, so no script ever actually runs.

### Normalizing to SentinelOne's wording

Most of the historical client base was on SentinelOne, so its API vocabulary
is what reports and dashboards were standardized on — analysts read "windows"
/ "server"/"desktop" and expect that wording regardless of which vendor
actually produced a given row. `AgentDevice` carries two fields for this:
`platform` and `machine_type`. SentinelOne's connector passes its own
`osType`/`machineType` straight through (it's the canonical source); Carbon
Black and BitDefender's connectors translate their own raw values into the
same wording:

- **Carbon Black** reports `os: "WINDOWS"` (uppercase) directly — lowercased
  to match S1's casing, no inference needed. It has no equivalent to `machineType`
  at all, so that's inferred from the OS name text instead
  (`agent_parity/models.py`'s `infer_machine_type`).
- **BitDefender** reports `machineType` as a numeric enum (its own API
  convention) — mapped to S1's string wording (`_MACHINE_TYPES` in
  `connectors/bitdefender.py`). It has no equivalent to `osType`, so `platform`
  is inferred from the OS name text (`infer_platform`).

`agent_version` is deliberately **not** touched: SentinelOne, Carbon Black,
and BitDefender each have their own real versioning scheme for their own
software. There's no honest way to make Carbon Black's sensor version look
like a SentinelOne agent version — that would be fabricating a number, not
normalizing one, so `AgentDevice.agent_version` stays exactly what each
vendor actually reports.

### High-value assets: servers as the prioritization signal

The reason this project exists in the first place: the correlated data fed a
quarterly report sent to clients, showing that agent coverage was improving
over time, and calling out high-value assets specifically — Domain
Controllers, file/storage servers — so gaps on those got prioritized over a
missing agent on a random workstation.

Domain Controllers are reliably identifiable (a distinctive OU in
`DistinguishedName`), but file/storage servers aren't — they can be named
anything, so a hostname-pattern heuristic would be guessing. The reliable
signal is simpler: **is it a Windows Server SKU at all**, via the same
`machine_type` field ("server"/"desktop") built for cross-vendor wording
congruence above. A storage server can be named anything; it can't fake
being a Windows Server.

One gap that needed closing to make this honest: `machine_type` only ever
came from the *agent* side of the merge (see `AgentDevice`'s docstring) — a
`missing_agent` row has no agent record at all, so it would have carried no
criticality signal whatsoever, which is backwards for a coverage tool (a
missing Domain Controller is exactly the row that most needs to stand out).
`correlation/engine.py`'s `backfill_machine_type` stage closes it: AD's own
OS text gets the same `infer_machine_type()` heuristic, so *every* row —
matched or not — gets a `machine_type`, without ever trying to infer
anything from a hostname.

This flows all the way through: `summarize()` reports `server_coverage_pct`
alongside the overall `coverage_pct`; the overview page shows both, plus a
second trend line so "coverage is improving" and "the assets that matter
most are covered" are both visible as trends, not just point-in-time
snapshots; the device list is filterable by `machine_type` so pulling
"every missing or stale server, across every client" for a report is one
filter, not a manual search.

### OS end-of-life: a third prioritization axis

[endoflife.date](https://endoflife.date/) is the source for a small,
hand-typed reference table (`agent_parity/os_eol_data.json`,
`os_eol_builds_data.json`) mapping OS names — and, where possible, exact
Windows build numbers — to their end-of-life date. Every device gets
classified against today's date into `unknown` / `supported` / `eol_soon`
(within 180 days) / `end_of_life` (`agent_parity/os_eol.py`). This is
independent of coverage: a *covered* end-of-life server still means the OS
itself needs upgrading — no agent fixes that — so `at_risk_counts` cross-tabs
EOL status against coverage status to surface the worst case, an unsupported
OS with no agent watching it.

Free-text OS names are ambiguous for anything past Windows 10 — "Windows 11"
alone doesn't say which feature update, and each one has its own EOL date, so
there's deliberately no bare "Windows 11" entry in the free-text table. Where
an exact Windows build number is available, it resolves that ambiguity
precisely instead:

- **Active Directory** exposes it natively — `operatingSystemVersion` (e.g.
  `"10.0 (22631)"`) is a stock schema attribute, not a fabrication.
- **SentinelOne** carries a build number in its inventory too (reconstructed
  from prior direct experience with the API, flagged in
  `connectors/sentinelone.py` as worth confirming against current docs since
  it isn't in the public API reference).
- **Carbon Black and BitDefender** have no equivalent field — devices only
  seen through those vendors fall back to the free-text table.

`extract_build_number()` (`agent_parity/os_eol.py`) parses both an AD-style
`"10.0 (22631)"` string and a full internal version string like
`"10.0.22631.3155"`, distinguishing the true build (10000–99999) from the
trailing UBR/revision component. `classify_eol_status()` in
`correlation/engine.py` prefers a build number when either side of the merge
has one — agent-reported first, then AD's — and only falls back to free-text
matching when neither does. AD's own build number is captured for *every*
device (the same backfill principle as `machine_type`), so even a
`missing_agent` row — no agent record at all — still gets a precise EOL
classification instead of `unknown`.

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
  client's own `sync_interval_hours` decides whether it's due. A second daily
  tick at 07:00 (`CELERY_TIMEZONE`, i.e. UTC) calls the same task with
  `force=True`, bypassing the per-client interval so every active client
  gets a fresh correlation before the start of business each morning.

The management command and the chord callback call the same functions in
`dashboard/services.py` — the parallelism is additive infrastructure, not a
rewrite of the pipeline.

### Credentials: DB-backed topology, config.yaml as a one-time import

Vendors have genuinely different credential shapes: SentinelOne is one API
token for the whole organization; Carbon Black needs a distinct API ID /
secret / org key **per client**. Client topology (name, slug, AD target
device, sync interval, enabled vendors) and vendor credentials both live in
the database now — a `Client` row plus one `VendorCredential` row per
(vendor, client) for per-client vendors, or per vendor alone (`client=None`)
for global ones. `VendorCredential.credentials` is encrypted at the Django
ORM layer (`dashboard/fields.py`'s `EncryptedJSONField`, built on
`cryptography.fernet` and keyed by `CREDENTIAL_ENCRYPTION_KEY`) rather than
a Postgres-only mechanism like `pgcrypto` — this app runs on SQLite in demo
mode and Postgres in scaled mode, and the encryption has to work on both.

`config.yaml` (still committed, still `${VAR}`-referenced) is now purely a
**one-time import source**, not something read at run time:

- **`manage.py import_config`** (or the setup page's own upload form, at
  `/setup/import/`) parses it exactly the way it always has
  (`agent_parity.config.load_config()`, unchanged) and writes the resulting
  `Client`/`VendorCredential` rows via `dashboard/config_db.py`'s
  `import_app_config()`. Idempotent — re-importing an unchanged file updates
  nothing.
- **`agent_parity/config.py`**'s dataclasses (`AppConfig`, `ClientConfig`,
  `VendorConfig`) stay the single contract the pipeline consumes —
  `credentials_for()`/`get_connector()`/`pick_ad_export_vendor()` are all
  reused completely unchanged. `dashboard/config_db.py`'s
  `build_app_config_from_db()` is the DB-backed counterpart to
  `load_config()`, used by every production entrypoint (management
  commands, Celery tasks) instead. `agent_parity/` itself never learns the
  DB exists — the Django/pipeline boundary from the Architecture section
  above stays intact.
- The **setup page** (`/setup/`, gated behind `staff_member_required` —
  the same bar as `/admin/`, since this is the one surface that writes
  credentials) is the ongoing way to add or edit a client and its
  credentials by hand, without touching config.yaml again. Credential form
  fields are always rendered blank, even when editing an existing value —
  leaving a field blank on submit means "keep the current value," so a
  stored secret is never echoed back into the page.
- **`.env`** (gitignored; see `.env.example`) still holds `CREDENTIAL_ENCRYPTION_KEY`,
  Django/infra secrets, and (explicitly out of scope for this feature —
  global, not per-client, and not moved to the DB) `STALE_DAYS` and the
  object-storage `STORAGE_*` variables.
- A `${VAR}` pointing at an unset variable still resolves to `None` at
  import time, which is precisely what puts a connector into fixture
  mode — a fresh checkout's `seed_demo` imports config.yaml with no `.env`
  and runs the entire pipeline on `sample_data/`, same as before.

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
`beat`, `redis`, `db` (Postgres), and `minio` (S3-compatible object storage
for the AD-export handoff — console at http://localhost:9001 in dev, login
with `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`); `docker-compose.override.yml`
is applied automatically for development. Seed data inside the stack:

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

### Smoke-testing the real stack

`uv run pytest` is fast and offline by design (fixtures, SQLite, `task_always_eager`
Celery) — which also means it structurally can't catch a broken Dockerfile, a
Celery worker that never picks up work, or a MinIO endpoint the app can't
actually reach. `docker/smoke_test.sh` covers that gap: it builds and starts
the full Compose stack, seeds demo data against a real Postgres, dispatches a
real Celery group/chord through a real Redis broker/worker
(`manage.py smoke_check_celery`), round-trips a real object through the
running MinIO server (`manage.py smoke_check_storage`), and tears everything
down.

```console
cd docker
./smoke_test.sh          # ~1-2 minutes; needs Docker running
./smoke_test.sh --keep   # leave the stack up afterward, for debugging
```

Not part of `uv run pytest` or any fast/CI path — it needs Docker, takes
noticeably longer, and is inherently less deterministic than the offline
suite (real network calls, real timing). Run it manually, e.g. before a
release, not on every commit.

## Tests

`uv run pytest` — all offline, no broker:

- **Correlation**: one test per `CoverageStatus` outcome, the
  merged-row-count-equals-union-of-join-keys invariant, FQDN/case
  normalization, configurable staleness, multi-vendor rows, and the
  high-value-asset backfill (a missing Domain Controller must be
  classified as `machine_type="server"` from AD's OS text alone, with zero
  agent data, and an agent-reported machine_type must never be overridden).
- **Chord semantics** (eager mode): one vendor failing yields a `partial`
  run with the other vendors' snapshots intact; duplicate callback delivery
  doesn't double-count (idempotency); per-client cadence gating.
- **Config resolver**: global vs. per-client scope, `${VAR}` resolution,
  fixture-mode fallback on unset secrets.
- **Connectors and parser**: fixture normalization, timestamp rebasing,
  live-mode gating on complete credentials, platform/machine_type wording
  normalized to SentinelOne's conventions (Carbon Black's uppercase `os`
  enum lowercased, BitDefender's numeric `machineType` mapped to string
  wording, `infer_platform`/`infer_machine_type` for vendors with no
  equivalent field) — and that both survive the correlation merge intact.
- **Object storage and AD-export handoff**: presigned-URL round trip against
  a mocked S3 backend (`moto` — no real MinIO/AWS S3 needed); the
  storage-vs-direct-channel branch in `script_runner.run_ad_export`, including
  that fixture mode never touches storage even when it's configured.
- **Fixture scenarios**: named tests pin the authored gap scenarios
  (`acme-sql02` is missing, `acme-fs-old` is orphaned, …) so a fixture edit
  that breaks a scenario fails loudly.
- **Pipeline data shapes** (`test_models.py`): `normalize_hostname` edge
  cases, `ADDevice`/`AgentDevice` join-key properties, `AgentDevice.to_dict`/
  `from_dict` round-tripping across the Celery JSON boundary.
- **HTTP transport** (`test_rest_adapter.py`): content-type-based parsing
  (JSON/text/bytes), retry configuration, header merging, `files=` passthrough
  — `RestAdapter` in isolation, not just through a connector.
- **ORM schema** (`test_dashboard_models.py`): `CoverageStatus` choices stay
  in lockstep with the pipeline's own enum, `__str__` methods, the
  `(client, join_key)` uniqueness constraint, cascade deletes.
- **Service-layer internals** (`test_services.py`): `_first_valid`'s
  NaN/None handling, `sync_client_from_config`'s create-vs-update (upsert)
  behavior, `persist_correlation`'s idempotency guarantee exercised directly
  rather than only through the Celery chord.
- **Dashboard views** (`test_views.py`): overview's empty state and populated
  coverage cards, device-list filtering (client/status/vendor) and
  pagination, device-detail 404 handling, the trend-data JSON endpoint —
  against a DB seeded via the real pipeline, not hand-built fixtures.
- **Admin registration** (`test_admin.py`): every model actually shows up in
  Django admin (a model added without `@admin.register` fails silently
  everywhere else).

Deliberately not unit-tested: Django settings modules, `config/celery.py`/
`wsgi.py`/`urls.py`, and `dashboard/apps.py` — these are declarative framework
wiring, not application logic; a failure there breaks every other test in the
suite (which loads them to run at all), so that failure mode is already
covered by the suite existing.

Also deliberately **not** covered here: whether the real, distributed pieces
(Celery worker/broker, MinIO) actually work together — `task_always_eager`
and `moto` prove the *logic* is right but never touch a real network or a
second process. That's what `docker/smoke_test.sh` is for; see
[Smoke-testing the real stack](#smoke-testing-the-real-stack) above.

## Out of scope for v1

- Per-client logins/permissions — `Client` scopes the data model; the
  dashboard itself is single-operator on Django auth.
- Real-time ingestion — this is a batch tool on a schedule.
- Fuzzy hostname matching beyond normalization (a natural next step for the
  renamed-machine orphans).
- A REST API layer (DRF would slot in if a JS frontend ever needed it) and
  Kubernetes (Compose demonstrates the deployment story).
