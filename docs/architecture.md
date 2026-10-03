[← Back to the README](../README.md)

# Architecture

This is the full design write-up for agent-parity, moved out of the README so the first screen stays short.
The [README](../README.md) has the quick start, the CSV schema, sample data, Docker and the test suite.

```
      `agent-parity compare`               `agent-parity run` (config.yaml + connectors)
   two CSVs, zero config             ┌──────────────────────────────────────────────┐
              │                      │            per (client, vendor)              │
              │      config.yaml ──► src/agent_parity/config.py ──► connector (S1/CB/BD) │
              │       + .env             │                       │            │      │
              │                          │            deploy_and_run()   fetch_inventory()
              │                          │                       │            │      │
              │                          │        Export-ADDevices.ps1     AgentDevice
              │                          │          runs REMOTELY on a     records   │
              │                          │          domain-joined endpoint    │      │
              │                          └───────────────│───────────────────│───────┘
              ▼                                          ▼                    ▼
    ad_export.py + agent_csv.py              ad_export.py               correlation.py
      (CSV -> DataFrame, both sides)         (CSV -> DataFrame)   (outer merge + classification)
              └────────────────────────┬──────────────────────┘
                                       ▼
                          src/agent_parity/pipeline.py
        correlate_from_csvs() / run_correlation_for_client()
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
          cli.py compare               cli.py run / tasks.py -> persistence.py
         (writes a CSV)          (SQLite history, Celery; `run --csv` also writes a CSV)
```

Everything above the `pipeline.py` line is pure, dependency-light Python:
pandas/numpy for the correlation engine, `requests`/`boto3` for the connectors and object
storage, `pyyaml` for config. Those layers import no ORM and no task queue; SQLAlchemy and
Celery live only below the line, in `scheduling/`, which decides what to do with a
`CorrelationResult`.

## The deployment model: remote script execution, not direct AD access

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
False`; it's fetch_inventory-only, and `deploy_and_run()` refuses outright (in both live and fixture mode) rather than
silently succeeding — an
organization on BitDefender alone can't have its AD export collected at all.

`src/agent_parity/script_runner.py` is the uniform entry point; each connector's
`deploy_and_run()` implements the vendor mechanics. AD collection and agent
inventory both flow through the same authenticated channel per vendor — for
whichever vendor is actually carrying the AD export. Every client needs at
least one enabled vendor with real remote-execution capability;
`src/agent_parity/config.py`'s `pick_ad_export_vendor()` picks it, preferring
SentinelOne over Carbon Black (reflecting real deployment prevalence — the
bulk of the original client base was on SentinelOne, a handful on Carbon
Black, one on BitDefender) and raising a clear `ConfigError` if a client has
neither.

All three connectors share one HTTP transport —
`agent_parity.vendor.rest_adapter` (`RestAdapter`) —
instead of a bare `requests.Session`: automatic retries with backoff on
429/5xx, content-type-aware parsing (JSON responses come back as `dict`,
text/HTML as `str`, everything else as raw `bytes`), and a single place to
add auth/proxy config if a vendor ever needs it. `connectors/base.py`'s
`_request_json()`/`_as_text()` helpers narrow that `dict | str | bytes` result
for call sites that know which one they expect.

`RestAdapter` and a few small helpers were copied from a shared library,
py-shared-tools (`src/agent_parity/vendor/`, with their tests in
`tests/vendor/`), so the repo is self-contained. This repo owns those copies
and doesn't keep them in sync with upstream. The code only this project used
has since moved into the modules that own it: the vendor-connector base in
`connectors/base.py`, the SentinelOne RSO mixin in `connectors/sentinelone.py`, the
storage-backed export in `script_runner.py`, `ObjectStorage` in `storage.py`, and the
storage config in `config.py`.

## Multi-domain clients: one export per domain, concatenated into a master list

A client isn't always a single AD domain — some span multiple domains or
forests, and no one domain controller can enumerate computer objects outside
its own domain. `ClientConfig.ad_target_devices` (`src/agent_parity/config.py`) is
a list, not a single hostname: `Export-ADDevices.ps1` runs once per entry, and
`src/agent_parity/pipeline.py`'s `collect_ad_frame` parses and concatenates the
resulting CSVs (`src/agent_parity/ad_export.py`'s `concat_ad_frames`) into
one master AD DataFrame before correlation ever runs. A single-domain client
is just the one-element case of the same list — not a special code path.

Collection is tolerant of partial failure the same way per-vendor inventory
collection already is: one domain being unreachable doesn't sink the others.
Only when *every* domain fails does `run_correlation_for_client` return
`None` (nothing at all to correlate against). Per-domain outcomes show up in
`vendor_status` keyed `ad:<target_device>` (e.g. `ad:GLOBEX-DC01`), alongside
the plain vendor-name keys for agent inventory. The demo's `globex` client
models multi-domain: it has two domains (`GLOBEX-DC01` and a branch office
`GLOBEX-BR-DC01`) in `config.yaml`/`sample_data/globex/`, while `acme` stays
single-domain.

## Multi-site/tenant: more than one site/tenant within a single vendor's console

Separately from multi-domain AD, a client can also have more than one
site/tenant *within one vendor's console* — `ClientConfig.vendors` maps a
vendor name to a tuple of dicts, one per site/tenant (almost always a
one-element tuple). What each dict holds depends on the vendor's real
credential model:

- **Carbon Black** (`scope: per_client`) — each entry is a complete,
  independent credential block. A second entry means a second, genuinely
  separate CB org (e.g. a branch office on its own tenant) — not a filter
  over shared data, since Carbon Black's `org_key` already *is* the tenant
  identifier. The connector needed **zero code changes** to support this:
  `AppConfig.sites_for` already returned each entry as-is, and
  `get_connectors` already built one connector per entry.
- **SentinelOne / BitDefender** (`scope: global`) — one shared credential
  set per named account (see "Multiple named accounts" below) covers the
  whole account, but a client's endpoints can be scoped to a slice of it via
  an optional filter key merged onto the resolved account's credentials.
  SentinelOne's is `site_ids` (a real, documented "Sites" concept —
  comma-separated site IDs matched against each item's own `siteId`, sent as
  the live `siteIds` query param or filtered locally in fixture mode).
  BitDefender's is `company_id` (GravityZone Cloud MSP's "Company" tenant
  concept, matched against `companyId`) — **this filter is not verified
  against real GravityZone API docs or a live tenant**, only plausible given
  GravityZone's own MSP company hierarchy; the same caution this project
  already applied to one other invented-then-removed GravityZone capability (`createCustomScriptTask`). An unset filter
  (the common case) means the
  whole account.

An entry can carry an optional `label` (e.g. `branch`), which does two
things: it becomes the `vendor_status` key (`carbonblack:branch` instead of
a bare index) via `pipeline.site_status_key`, and it picks a distinct
fixture file (`{vendor}_inventory_{label}.json`) via
`connectors/base.py`'s `_fixture_fetch_inventory` — a labeled tenant is real,
independent data, so it doesn't silently share the unlabeled tenant's
fixture. The demo's `acme` client is the multi-tenant one: its primary
Carbon Black org (unlabeled) plus a second `label: branch` tenant reading
`ACME_CB2_*` env vars and `sample_data/acme/carbonblack_inventory_branch.json`.

## Multiple named accounts per global vendor

"Global scope" doesn't mean *one* credential set for a vendor, either — it
means every client that uses the same account shares that account's secret.
There were genuinely two separate SentinelOne consoles in practice: one for
ordinary managed-services clients (`"mssp"`), one for clients under active
DFIR incident response (`"dfir"`) — a distinct engagement, a distinct
console, by design, not just a Site within one account (that's the previous
section — orthogonal, and composable: a site dict can carry both `"account"`
and a site filter like `site_ids` at once). `VendorConfig.accounts`
(`src/agent_parity/config.py`) is a dict of named credential sets, not a single
block — always named, even when a vendor (BitDefender, today) only has one,
the same "no special-cased single case" principle as everywhere else in this
config layer:

```yaml
sentinelone:
  scope: global
  accounts:
    mssp: { api_url: ..., api_token: ... }
    dfir: { api_url: ..., api_token: ... }
```

A client's site dict gets an `"account"` key picking which one it's in (`config.yaml`'s `acme`/`globex` both pick
`mssp`). Omitted, it resolves to
the vendor's sole account when there's exactly one — still today's implicit
default for a single-account vendor — or raises a clear `ConfigError` if
there's more than one and no client made a choice (`AppConfig._resolve_account`); ambiguous is a config error, not a
silent
pick.

## Scheduling & persistence

This package owns scheduling (Celery) and persistence (SQLAlchemy + SQLite)
directly; see [ADR 0009](decisions/0009-standalone-package-owning-scheduling-and-persistence.md).

`src/agent_parity/scheduling/db.py` is the schema — `Client` (an identity anchor only;
topology stays in `config.yaml`), `Device`, `CorrelationRun` (one row per
pipeline execution; status `pending`/`complete`/`partial`/`failed`),
`CoverageSnapshot` (one row per classified-frame row). `get_engine()`
resolves `AGENT_PARITY_DB_URL` or defaults to a local, gitignored
`agent_parity.db` SQLite file — zero setup, same spirit as `sample_data/`'s
fixture-mode fallback. No Alembic: this is a lightweight run-history store
sized for the demo/single-node case, not a migration-managed production
schema.

`src/agent_parity/scheduling/persistence.py` sits between `pipeline.py` (pure, no
persistence) and the chord callback: `finalize_run` correlates and writes
`CoverageSnapshot` rows (or marks the run `FAILED` outright when every AD
domain failed). It's **idempotent** — a duplicate call against an
already-finalized run no-ops rather than double-counting. SQLite has no
row lock like Postgres's `SELECT ... FOR UPDATE`, so this relies on SQLite's
own writer serialization — adequate at this single-node/demo scale, but a
real, disclosed difference from a Postgres-backed production database.

`src/agent_parity/scheduling/celery_app.py`/`tasks.py` are how every persisted
run is collected. `agent-parity run` and the beat schedule both dispatch the
same chord (`tasks.start_client_run`), as the original tool did: its manual
runs went through Celery too, because collecting sequentially took hours.
`run` executes the chord in-process by default, so the demo needs no broker,
or on running workers with `--workers`, where every client's fan-out runs in
parallel. The chord is one *group* of
fan-out tasks per client (one AD-export task per domain controller, one
inventory-pull task per vendor/site-tenant) feeding a *chord* callback that
runs the correlation exactly once against the client's complete result set.
Each task wraps the same per-domain or per-site helper in `pipeline.py` that
`run_correlation_for_client` loops over, so both share one set of error
handling. Fan-out tasks never raise — a broken vendor API comes back as an
`"error: ..."` status instead, so the chord still fires and the run completes `PARTIAL` rather
than not at all. `dispatch_all_clients` (the beat entrypoint) reads each
client's own `sync_interval_hours` to decide whether it's due; the beat
schedule ticks it hourly plus a forced daily 07:00 run. Each tick, and each
`run`, first marks any run still `pending` after `pending_run_timeout_hours`
(`config.yaml`, default 24) as `failed`, so a killed CLI or a dead worker
can't leave a run unfinished forever. With `--workers`, `--timeout MINUTES`
stops `run` waiting at a deadline; unfinished runs keep going on the workers.
Broker/backend default to `redis://localhost:6379/0`
(`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` to override).

```console
uv run agent-parity run --all                         # the chord, in-process
docker compose -f docker/docker-compose.yml up -d redis worker beat   # workers + the schedule
uv run agent-parity run --all --workers               # the chord, on those workers
```

Tests run Celery tasks eagerly (`task_always_eager`, no broker needed) —
`docker/smoke_check_celery.py` (via `docker/smoke_test.sh`) is the one
thing that can't prove: a real chord round-tripping through a real Redis
broker and real worker/beat containers.

## Splunk export

Real production behavior: the original tool sent every run to Splunk whole,
one event per row of the final table, and its dashboard showed the most
recent run. This package does the same. Splunk is a *sink*, never the system
of record (SQLite stays authoritative), and forwarding is entirely opt-in:
`src/agent_parity/config.py`'s `SplunkConfig.enabled` is `False` unless both
`hec_url` and `hec_token` are configured — the same opt-in shape as object
storage or a vendor's own credentials.

**Each run, whole**: `persistence.export_run_to_splunk` sends one event per
row of the classified table (`agent_parity:coverage`), each tagged with the
client, `run_id` and run start, then one summary event per run
(`agent_parity:coverage_summary`) with the run status, per-vendor outcomes,
coverage percentages and status counts. Every event is timestamped with the
run's start. The summary goes last, so it marks the run as fully sent: a
dashboard showing the latest summary's run never shows a half-ingested run.

```
index=security_coverage sourcetype=agent_parity:coverage
| eventstats max(run_id) as latest by client | where run_id=latest
```

Trend charts are a `timechart` over the summary events. Rows are separate
events rather than one event holding the whole table because Splunk truncates
events at 10,000 bytes and stops automatic JSON field extraction at 5,000 by
default. (An earlier version of this rebuild sent per-run deltas instead; it
lost gap closures, since a fixed gap's new row never matched its old one, and
re-sent everything after a failed run. A few MB per run is not a license
concern at this scale.)

```yaml
splunk:
  hec_url: ${SPLUNK_HEC_URL}
  hec_token: ${SPLUNK_HEC_TOKEN}
  index: security_coverage
  sourcetype: agent_parity:coverage
  summary_sourcetype: agent_parity:coverage_summary
```

**A Splunk outage never fails a run** — `persistence.persist_result` catches
`SplunkExportError` and logs it; the run's own `COMPLETE`/`PARTIAL` status
depends only on collection/correlation, never on whether the export
succeeded. A `FAILED` run has no table and sends nothing. Only `run` and the
schedule send to Splunk; `compare` never does.

## The quarterly report

The original tool's data fed a quarterly PDF report to each client, and
`agent-parity report` rebuilds it from the run history, one PDF per client
(`output/<client>-<quarter>.pdf`; see the
[sample](sample-quarterly-report.pdf)):

1. **Headline numbers** — coverage, server coverage, and the missing, stale
   and orphaned counts, each with its change since last quarter.
2. **Coverage trend** — a chart and table of overall and server coverage by
   quarter.
3. **High-value assets** — server coverage and the servers with a gap.
4. **Gaps to act on** — devices with no agent, agents that stopped checking
   in, and agents with no AD computer, servers first.
5. **OS end of life** — per device, with its build, and the highest-risk case:
   an end-of-life OS with no agent at all.

Each quarter is its last finished run, the state the client was in at quarter
end. `scheduling/history.py` reads the history into plain data and
`quarterly_report.py` renders it with ReportLab (an optional extra,
`agent-parity[report]`), the same split as the Splunk export. The fixtures are
static, so `tools/seed_history.py` builds a deterministic demo history in a
separate database for the sample.

## AD-export handoff: object storage instead of the vendor channel (mandatory for live exports)

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
   line — see each connector's `_live_deploy_and_run`). The script (`Export-ADDevices.ps1 -UploadUrl ...`) uploads its
   CSV directly there
   instead of printing it to stdout.
3. The vendor's remote-execution call only needs to report that the script *ran*; its stdout is ignored entirely.
4. agent-parity downloads the object with a plain authenticated GET (its own
   credentials, not the presigned URL) and deletes it — best-effort cleanup
   that never fails an export that already succeeded.

This is built against the **S3 API** (`boto3`), not a specific product:
`agent_parity.storage.ObjectStorage` talks to a
self-hosted S3-compatible server (`docker/docker-compose.yml` runs the
[Versity S3 Gateway](decisions/0011-versity-s3-gateway-for-local-dev.md)) for
local/dev use, or real **AWS S3** in production, with `endpoint_url` as the
only thing that changes. The local server was MinIO until MinIO stopped
publishing community images; swapping it needed no code change, which is the
point of targeting the API rather than a product.
It is *not* Azure Blob Storage capable — Blob doesn't speak the S3 API, so
that would need a second implementation with a different SDK, not just
different credentials.

**Storage is required for any live export** — `run_ad_export` raises a clear
error rather than falling back to the vendor channel if a live connector
reaches it with no storage configured. The one exception is fixture mode: a
non-live connector has no real endpoint to upload anything from, so it always
returns the canned `sample_data/` CSV directly, regardless of whether storage
happens to be configured. Storage is unconfigured by default in the demo path (`STORAGE_BUCKET`/`STORAGE_ACCESS_KEY`/
`STORAGE_SECRET_KEY` all resolve to
`null` with no `.env`) — that's only safe because the demo path has no live
vendor credentials either, so no script ever actually runs.

## Normalizing to SentinelOne's wording

Most of the historical client base was on SentinelOne, so its API vocabulary
is what reports were standardized on — analysts read "windows"/"server"/
"desktop" and expect that wording regardless of which vendor actually
produced a given row. `AgentDevice` carries two fields for this: `platform`
and `machine_type`. SentinelOne's connector passes its own `osType`/
`machineType` straight through (it's the canonical source); Carbon Black and
BitDefender's connectors translate their own raw values into the same
wording:

- **Carbon Black** reports `os: "WINDOWS"` (uppercase) directly — lowercased
  to match S1's casing, no inference needed. It has no equivalent to `machineType`
  at all, so that's inferred from the OS name text instead (`src/agent_parity/models.py`'s `infer_machine_type`).
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

## High-value assets: servers as the prioritization signal

The reason this project exists in the first place: the correlated data fed a
quarterly report, showing that agent coverage was improving over time, and
calling out high-value assets specifically — Domain Controllers, file/storage
servers — so gaps on those got prioritized over a missing agent on a random
workstation.

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
`correlation.py`'s `backfill_machine_type` stage closes it: AD's own
OS text gets the same `infer_machine_type()` heuristic, so *every* row —
matched or not — gets a `machine_type`, without ever trying to infer
anything from a hostname.

This flows all the way through: `summarize()` reports `server_coverage_pct`
alongside the overall `coverage_pct`, and the classified frame is filterable
by `machine_type` so pulling "every missing or stale server" for a report is
one filter, not a manual search.

## OS end-of-life: a third prioritization axis

[endoflife.date](https://endoflife.date/) is the source for the reference
data (`src/agent_parity/os_eol_data.json`, derived from its API by
`agent_parity.os_eol_live`), mapping OS names — and, where possible, exact
Windows build numbers — to their end-of-life date. Every device gets
classified against today's date into `unknown` / `supported` / `eol_soon`
(within 180 days) / `end_of_life` (`src/agent_parity/os_eol.py`). This is
independent of coverage: a *covered* end-of-life server still means the OS
itself needs upgrading — no agent fixes that — so `at_risk_status_counts`
cross-tabs EOL status against coverage status to surface the worst case, an
unsupported OS with no agent watching it.

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

`extract_build_number()` (`src/agent_parity/os_eol.py`) parses both an AD-style
`"10.0 (22631)"` string and a full internal version string like
`"10.0.22631.3155"`, distinguishing the true build (10000–99999) from the
trailing UBR/revision component. `classify_eol_status()` in
`correlation.py` prefers a build number when either side of the merge
has one — agent-reported first, then AD's — and only falls back to free-text
matching when neither does.

A build number alone doesn't identify an OS: 26100 is both Windows 11 24H2
(end of life 2026-10-13) and Windows Server 2025 (2034-11-14), and 17763 is
both Server 2019 and a short-lived semi-annual server release. So build
entries are keyed by product, and servers are looked up by their release
name first ("Windows Server 2019" is exact), using the server build table
only when the name carries no year. Before this, every Windows Server 2025
machine was flagged end of life nine years early.

The data stays current on its own, as the original tool did by querying
endoflife.date every time. `os_eol_live.refresh_cache` fetches both products,
derives the tables with the same rules as the bundled snapshot, and writes
them atomically to a cache file (`AGENT_PARITY_EOL_CACHE`, on the shared
`dbdata` volume in Docker Compose), logging any dates that changed. Beat runs
it daily at 06:30, ahead of the forced 07:00 run, and `agent-parity run`
runs it at start when the cache is more than a day old. Lookups use the
cache whenever it exists and is valid, re-reading it when it changes, and
fall back to the bundled snapshot (`os_eol_data.json`) otherwise, so an
endoflife.date outage never stops a run. `refresh_os_eol: false` in
`config.yaml` turns it off for air-gapped deployments. The bundled snapshot
in the repo is regenerated with `tools/check_eol_drift.py --write`. AD's own build number is captured for *every*
device (the same backfill principle as `machine_type`), so even a
`missing_agent` row — no agent record at all — still gets a precise EOL
classification instead of `unknown`.

## The correlation: a pandas merge, kept honest

`correlation.py` reduces the whole reconciliation to one analytical
move, structured as a `.pipe()` chain so each stage is independently
testable:

```python
(
    ad_df.pipe(add_join_key)  # hostname -> normalized join key
    .pipe(merge_with_agents, agents_df)  # outer merge, indicator=True
    .pipe(resolve_ambiguous_join_keys)  # same short name in two AD domains
    .pipe(classify_coverage, stale_days=14)  # indicator + staleness -> status
)
```

The merge indicator *is* the classification: `left_only` → `missing_agent`,
`right_only` → `orphaned_agent`, `both` → `covered` or `stale_coverage`
depending on a vectorized `last_seen` check (`np.select`). Join keys are
hostnames with the DNS suffix stripped, lowercased, and trimmed — so
`ACME-WS-014.corp.acme.example` and `acme-ws-014` correlate. Coverage
percentages fall out of `groupby`/`value_counts` (`summarize()`).

One case breaks the "a short hostname is one machine" assumption: a
multi-domain client can have `WS-001` in two domains. When an agent reported
a full DNS name, `resolve_ambiguous_join_keys` keeps only its pairing with
the AD object whose `DNSHostName` is that exact name (`match_method =
fqdn_exact`), so the other domain's machine correctly shows as
`missing_agent`. An agent that reported only `WS-001` can't be attributed,
so its rows are flagged `ambiguous_join_key`, counted in the summary, logged
as a warning and noted on `run`'s output instead of being silently trusted.

## Credentials: config.yaml + .env

Vendors have genuinely different credential shapes: SentinelOne is one API
token per named account (see "Multiple named accounts" below); Carbon Black
needs a distinct API ID / secret / org key **per client**. `config.yaml`
(committed) declares topology — which vendors exist, their scope, each
client's enabled vendors/domains — with every secret value written as a
`${VAR}` reference; `.env` (gitignored; see `.env.example`) holds the actual
values:

```yaml
vendors:
  sentinelone:
    scope: global
    accounts:
      mssp:
        api_url: ${SENTINELONE_MSSP_API_URL}
        api_token: ${SENTINELONE_MSSP_API_TOKEN}
  carbonblack:
    scope: per_client

clients:
  - name: Acme Corp
    slug: acme
    ad_target_devices: [ ACME-DC01 ]
    vendors:
      sentinelone:
        - account: mssp
      carbonblack:
        - api_url: ${ACME_CB_API_URL}
          api_id: ${ACME_CB_API_ID}
          api_key: ${ACME_CB_API_KEY}
          org_key: ${ACME_CB_ORG_KEY}
        - label: branch
          api_url: ${ACME_CB2_API_URL}
          api_id: ${ACME_CB2_API_ID}
          api_key: ${ACME_CB2_API_KEY}
          org_key: ${ACME_CB2_ORG_KEY}
```

Each vendor's value is a *list* of site/tenant entries, not a single block —
see "Multi-site/tenant" above for what a client with more than one looks like (Acme's two Carbon Black tenants above),
and "Multiple named accounts" above
for what a global vendor's `account:` key picks between.

Any vendor registered in `agent_parity.connectors.CONNECTOR_CLASSES` works
here — adding support for a vendor beyond SentinelOne/Carbon Black/BitDefender
is writing one connector class decorated `@register_connector`
(`src/agent_parity/connectors/base.py`), not editing a central table. The Celery
inventory task for it is generated from the registry too, rate-limited by the
connector's `inventory_rate_limit`.
`src/agent_parity/config.py`'s `load_config()` is the single entrypoint that
resolves `config.yaml` plus the environment into an `AppConfig`. There's no second config path: the SQLite run
history never stores topology or credentials, so this is the one place to read what's configured.

A `${VAR}` pointing at an unset variable resolves to `None`, which is
precisely what puts a connector into fixture mode — a fresh checkout with no
`.env` runs the entire pipeline against `sample_data/`.

The code doesn't read `.env` itself. Load it into the environment with `uv run --env-file .env agent-parity run`, or
with Docker Compose's `--env-file`. The original tool used python-dotenv while it was small and switched to Docker
Compose's env file once Celery and Docker came in; this rebuild keeps the second approach, so there is no dotenv
dependency.
