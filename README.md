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

This package is a standalone library and CLI — no Django, no Celery, no
database. It's meant to be used either directly (the CLI below) or as a
pinned git dependency (`uv add git+https://.../agent-parity@vX.Y.Z`) inside a
larger project that provides its own persistence/scheduling/dashboard.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. Two ways in, no
server or database either way:

```console
uv sync
uv run agent-parity compare ad_export.csv agent_export.csv   # your own two CSVs, zero config
uv run agent-parity run --all                                # config.yaml + connectors (demo: sample_data/)
uv run pytest                                                 # 190+ tests, all offline
```

`compare` needs no vendor connector, no `config.yaml`, and no credentials at
all — see [Bring your own CSVs](#bring-your-own-csvs) right below. `run` is
the config.yaml/connector-driven path (`--all` or `--client acme`); with no
`.env` (or unset credentials in it) every connector falls back to
`sample_data/` fixtures — see [Sample data](#sample-data) below for what's in
them.

## Bring your own CSVs

The correlation engine only ever needs two DataFrames in a known shape — it
doesn't care whether they came from a connector or a plain CSV. `agent-parity
compare` is the zero-config path: no `config.yaml`, no connector, no
credentials, just two files.

The **AD CSV** is `Export-ADDevices.ps1`'s own output — hand that script to
whoever manages the domain and run it against a domain controller (see
[The deployment model](#the-deployment-model-remote-script-execution-not-direct-ad-access)
below for why it's a script instead of a direct LDAP query).

The **agent CSV** is whatever EDR/inventory tool you have, mapped into
agent-parity's own column schema (`agent_parity/agent_csv.py`) — every vendor
exports differently, so this is a one-time mapping exercise per tool rather
than something agent-parity guesses at:

| column          | required? | notes                                          |
|------------------|-----------|-------------------------------------------------|
| `hostname`       | yes       | the only required column                        |
| `os`             | no        | free-text OS name                                |
| `os_build`       | no        | exact build number if your tool reports one      |
| `vendor`         | no        | your tool's name, e.g. `crowdstrike`             |
| `agent_id`       | no        | your tool's own device/agent identifier          |
| `last_seen`      | no        | ISO 8601 timestamp; blank = never checked in      |
| `agent_version`  | no        | your tool's own version string                   |
| `platform`       | no        | e.g. `windows`/`linux`/`macos`                   |
| `machine_type`   | no        | e.g. `server`/`desktop`                          |

A column left out entirely defaults to blank/unknown for every row — only a
missing `hostname` column is an error. Once this is useful enough to want
running on a schedule against a live API instead of a one-off export file,
`config.yaml` + `agent-parity run` (see [Credentials](#credentials-configyaml--env)
below) is the next step up.

## Architecture

```
      `agent-parity compare`                    `agent-parity run` (config.yaml + connectors)
   two CSVs, zero config                  ┌──────────────────────────────────────────────┐
              │                           │            per (client, vendor)              │
              │           config.yaml ──► agent_parity/config.py ──► connector (S1/CB/BD) │
              │            + .env             │                       │            │      │
              │                                │            deploy_and_run()   fetch_inventory()
              │                                │                       │            │      │
              │                                │        Export-ADDevices.ps1     AgentDevice
              │                                │          runs REMOTELY on a     records   │
              │                                │          domain-joined endpoint    │      │
              │                                └───────────────│───────────────────│───────┘
              ▼                                                ▼                    ▼
    ad_sync/parser.py + agent_csv.py              ad_sync/parser.py          correlation/engine.py
      (CSV -> DataFrame, both sides)               (CSV -> DataFrame)   (outer merge + classification)
              └────────────────────────┬──────────────────────┘
                                       ▼
                          agent_parity/pipeline.py
              correlate_from_csvs() / run_correlation_for_client()
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
             agent_parity/cli.py                  a consuming project
          (writes output/<name>.csv)      (e.g. a hub: persists, dashboards,
                                              schedules — its own concern)
```

Everything above the `pipeline.py` line is pure, dependency-light Python:
pandas/numpy for the correlation engine, `requests`/`boto3` for the connectors
and object storage, `pyyaml` for config. No web framework, no ORM, no task
queue — a consumer decides what to do with a `CorrelationResult`.

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
`agent_parity/pipeline.py`'s `collect_ad_frame` parses and concatenates the
resulting CSVs (`agent_parity/ad_sync/parser.py`'s `concat_ad_frames`) into
one master AD DataFrame before correlation ever runs. A single-domain client
is just the one-element case of the same list — not a special code path.

Collection is tolerant of partial failure the same way per-vendor inventory
collection already is: one domain being unreachable doesn't sink the others.
Only when *every* domain fails does `run_correlation_for_client` return
`None` (nothing at all to correlate against). Per-domain outcomes show up in
`vendor_status` keyed `ad:<target_device>` (e.g. `ad:GLOBEX-DC01`), alongside
the plain vendor-name keys for agent inventory.

Vendor agent inventories aren't affected by *this specific* fan-out — a
client's domains are purely an AD-side concept — but a client's vendor-side
coverage can itself span more than one site or tenant within a console, which
works the same way; see the next section. The demo's `globex` client models
multi-domain: it has two domains (`GLOBEX-DC01` and a branch office
`GLOBEX-BR-DC01`) in `config.yaml`/`sample_data/globex/`, while `acme` stays
single-domain.

### Multi-site/tenant clients: the same shape, applied to vendor consoles

A client's coverage isn't always all under one vendor tenant either:
SentinelOne organizes endpoints into Sites within a single account, and
Carbon Black Cloud tenants (`org_key`) are fully separate orgs — some clients
run more than one. `ClientConfig.vendors[vendor_name]` (`agent_parity/config.py`)
is a tuple of "site" dicts, not a single credential block, for exactly the
same reason `ad_target_devices` is a list — a client with one site/tenant is
just the one-element case.

What "multiple" means differs by `VENDOR_SCOPE`, and that's not an arbitrary
split — it mirrors how each vendor's API is actually provisioned:

- **Per-client scope (Carbon Black)**: each additional tenant is a fully
  separate, already-real credential set (its own `api_url`/`api_id`/`api_key`/
  `org_key`). Multi-tenant support here needed zero connector changes — it's
  the existing single-tenant mechanism, just invoked once per tenant and
  concatenated, the same way `collect_ad_frame` concatenates AD domains.
- **Global scope (SentinelOne, BitDefender)**: credentials are shared
  org-wide, so "site" means filtering *that one account's* query down to the
  sites a client owns — SentinelOne's `GET /web/api/v2.1/agents` takes a
  real, documented `siteIds` filter; a client's site dict carries
  `{"site_ids": "..."}`, merged onto the shared credentials at connector-build
  time (`AppConfig.sites_for`) rather than stored as if it were a secret.
  BitDefender's equivalent (`company_id`) is modeled the same way but flagged
  in `connectors/bitdefender.py` as unverified against GravityZone's real
  multi-tenant API shape — this project already removed one fabricated
  GravityZone capability (`createCustomScriptTask`) rather than guess, and
  the same caution applies here.

Fetching is tolerant of partial failure the same way AD-domain collection is:
one site/tenant failing doesn't sink the others (`pipeline.collect_vendor_inventory`).
`vendor_status` keys stay the plain vendor name for the common single-site
case — unlike a domain's `target_device` (always a real hostname), a
vendor's default site has no meaningful name, so an index or label suffix
(`carbonblack:branch`, `carbonblack:0`) only appears once there's more than
one to distinguish (`pipeline.site_status_key`). The demo's `acme` client has
two Carbon Black tenants (its primary org plus a `label: branch` one, each
with its own fixture file since they're genuinely separate accounts — see
`connectors/base.py`'s label-aware fixture lookup), while SentinelOne/BitDefender
site filtering has unit coverage (`tests/test_connectors.py`) without a full
multi-site demo scenario wired into `sample_data/`.

### Multiple named accounts per global vendor

"Global scope" doesn't mean *one* credential set for a vendor, either — it
means every client that uses the same account shares that account's secret.
There were genuinely two separate SentinelOne consoles in practice: one for
ordinary managed-services clients ("mssp"), one for clients under active
DFIR incident response ("dfir") — a distinct engagement, a distinct console,
by design, not just a Site within one account (that's the previous section).
`VendorConfig.accounts` (`agent_parity/config.py`) is a dict of named
credential sets, not a single block — always named, even when a vendor
(BitDefender, today) only has one, the same "no special-cased single case"
principle as everywhere else in this config layer:

```yaml
sentinelone:
  scope: global
  accounts:
    mssp: { api_url: ..., api_token: ... }
    dfir: { api_url: ..., api_token: ... }
```

A client's site dict gets an `"account"` key picking which one it's in
(`config.yaml`'s `acme`/`globex` both pick `mssp`). Omitted, it resolves to
the vendor's sole account when there's exactly one — still today's implicit
default for a single-account vendor — or raises a clear `ConfigError` if
there's more than one and no client made a choice (`AppConfig._resolve_account`);
ambiguous is a config error, not a silent pick.

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
instance (`docker/docker-compose.yml` runs one) for local/dev use, or real
**AWS S3** in production, with `endpoint_url` as the only thing that changes.
It is *not* Azure Blob Storage capable — Blob doesn't speak the S3 API, so
that would need a second implementation with a different SDK, not just
different credentials.

**Storage is required for any live export** — `run_ad_export` raises a clear
error rather than falling back to the vendor channel if a live connector
reaches it with no storage configured. The one exception is fixture mode: a
non-live connector has no real endpoint to upload anything from, so it always
returns the canned `sample_data/` CSV directly, regardless of whether storage
happens to be configured. Storage is unconfigured by default in the demo path
(`STORAGE_BUCKET`/`STORAGE_ACCESS_KEY`/`STORAGE_SECRET_KEY` all resolve to
`null` with no `.env`) — that's only safe because the demo path has no live
vendor credentials either, so no script ever actually runs.

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
alongside the overall `coverage_pct`, and the classified frame is filterable
by `machine_type` so pulling "every missing or stale server, across every
client" for a report is one filter, not a manual search.

### OS end-of-life: a third prioritization axis

[endoflife.date](https://endoflife.date/) is the source for a small,
hand-typed reference table (`agent_parity/os_eol_data.json`,
`os_eol_builds_data.json`) mapping OS names — and, where possible, exact
Windows build numbers — to their end-of-life date. Every device gets
classified against today's date into `unknown` / `supported` / `eol_soon`
(within 180 days) / `end_of_life` (`agent_parity/os_eol.py`). This is
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
percentages and per-vendor gap lists fall out of `groupby`/`value_counts`
(`summarize()`).

### Credentials: config.yaml + .env

Vendors have genuinely different credential shapes: SentinelOne is one API
token per named account (see [Multiple named accounts](#multiple-named-accounts-per-global-vendor)
above); Carbon Black needs a distinct API ID / secret / org key **per
client**. `config.yaml` (committed) declares topology — which vendors exist,
their scope, each client's enabled vendors/sites/domains — with every secret
value written as a `${VAR}` reference; `.env` (gitignored; see
`.env.example`) holds the actual values. `agent_parity/config.py`'s
`load_config()` is the single entrypoint that resolves both into an
`AppConfig` — there's no database and no second config path, so this is
also exactly what a consuming project should call.

A `${VAR}` pointing at an unset variable resolves to `None`, which is
precisely what puts a connector into fixture mode — a fresh checkout with no
`.env` runs the entire pipeline against `sample_data/`.

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

## Optional: local object storage (MinIO)

Everything above runs with zero external services. The one live-infrastructure
path this package has — the AD-export object-storage handoff (see
[above](#ad-export-handoff-object-storage-instead-of-the-vendor-channel-mandatory-for-live-exports)) —
can be exercised locally against a real MinIO instance instead of just
`moto`'s simulated S3:

```console
cd docker
docker compose up -d           # starts MinIO (console at http://localhost:9001)
./smoke_test.sh                # round-trips a real object through it
```

Not part of `uv run pytest` or any fast/CI path — it needs Docker and touches
a real network. Run it manually, e.g. before cutting a release.

## Tests

`uv run pytest` — all offline, no live credentials or external services:

- **Correlation**: one test per `CoverageStatus` outcome, the
  merged-row-count-equals-union-of-join-keys invariant, FQDN/case
  normalization, configurable staleness, multi-vendor rows, and the
  high-value-asset backfill (a missing Domain Controller must be
  classified as `machine_type="server"` from AD's OS text alone, with zero
  agent data, and an agent-reported machine_type must never be overridden).
- **Pipeline collection** (`test_pipeline.py`): multi-domain AD concatenation
  and partial-failure tolerance, multi-tenant vendor concatenation and partial
  failure, `site_status_key`'s labeling rules, and `run_correlation_for_client`'s
  happy path plus its "every AD domain failed" `None` case.
- **Fixture scenarios** (`test_pipeline_sync.py`): named tests pin the
  authored gap scenarios (`acme-sql02` is missing, `acme-fs-old` is orphaned,
  …) so a fixture edit that breaks a scenario fails loudly.
- **Config resolver**: global vs. per-client scope, `${VAR}` resolution,
  fixture-mode fallback on unset secrets, named-account resolution.
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
- **Pipeline data shapes** (`test_models.py`): `normalize_hostname` edge
  cases, `ADDevice`/`AgentDevice` join-key properties, `AgentDevice.to_dict`/
  `from_dict` round-tripping (used to pass records across a process boundary,
  e.g. a consuming project's own task queue).
- **HTTP transport** (`test_rest_adapter.py`): content-type-based parsing
  (JSON/text/bytes), retry configuration, header merging, `files=` passthrough
  — `RestAdapter` in isolation, not just through a connector.

Also deliberately **not** covered here: whether a real MinIO/AWS S3 endpoint
actually works — `moto` proves the *logic* is right but never touches a real
network. That's what `docker/smoke_test.sh` is for; see
[Optional: local object storage](#optional-local-object-storage-minio) above.

## Out of scope for v1

- Persistence, scheduling, and a dashboard — deliberately left to a
  consuming project; this package's contract ends at `CorrelationResult`.
- Fuzzy hostname matching beyond normalization (a natural next step for the
  renamed-machine orphans).
- Real-time ingestion — this is a batch/on-demand tool, not a streaming one.
