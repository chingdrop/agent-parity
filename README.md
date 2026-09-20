# agent-parity

Correlate an Active Directory computer inventory against an EDR/security agent inventory (SentinelOne, Carbon Black, or BitDefender) to find devices with missing, orphaned, or stale agent coverage.

[![CI](https://github.com/chingdrop/agent-parity/actions/workflows/ci.yml/badge.svg)](https://github.com/chingdrop/agent-parity/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

## What it answers

Three questions a SOC or compliance team actually cares about:

1. **Missing coverage** — devices AD knows about that no agent is reporting on.
2. **Orphaned agents** — agents phoning home for a device AD has no record of (decommissioned machines, shadow IT, naming mismatches).
3. **Stale coverage** — matched devices whose agent hasn't checked in recently (silently failed install, network issue, tampering).

Two more axes rank the gaps: **server / Domain Controller** priority (a missing DC outranks a missing laptop) and **OS end-of-life** status (an unsupported OS is a finding even with an agent installed).

## Try it in 60 seconds

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+. No credentials, no server, no network beyond installing dependencies: it runs against synthetic fixtures in `sample_data/`.

```console
git clone https://github.com/chingdrop/agent-parity.git
cd agent-parity
uv sync
uv run agent-parity run
```

Real output:

```text
[acme] 51 rows -> output/acme.csv (coverage 81.8%; covered=36, missing_agent=5, orphaned_agent=7, stale_coverage=3; ad:ACME-DC01=ok, bitdefender=ok, carbonblack:0=ok, carbonblack:branch=ok, sentinelone=ok)
```

Of the devices AD knows about, 36 are covered (81.8%); 5 have no agent, 3 have gone quiet, and 7 agents belong to no AD device. The per-device table is in `output/acme.csv`. Add `--all` to run every client (Acme and Globex).

## See it

- [Sample report](docs/sample-report.md): the coverage, high-value-asset and OS end-of-life views, generated from a real run against the fixtures.

<!-- TODO(craig): after `vhs docs/demo.tape` renders docs/demo.gif, embed it here: ![agent-parity demo](docs/demo.gif) -->

## Original deployment

This is a from-scratch rebuild of a tool I originally built professionally, using entirely synthetic data. No proprietary code, client data, or credentials are involved; vendor API interactions are shaped from public API documentation, and **everything runs against local fixtures by default** — no live credentials required.

<!-- TODO(craig): original result, e.g. coverage up 33% across ~6,000 endpoints; specify percentage points vs relative and baseline -->

## About this project

The original tool existed to feed a quarterly report: show that agent
coverage was trending upward over time, and flag high-value assets (Domain
Controllers, file/storage servers) specifically, so gaps there got
prioritized over a missing agent on a random workstation. A third axis works
the same way: a device running an OS that's already end-of-life (or soon
will be) is a risk finding independent of whether an agent is installed on
it. All three are first-class in this rebuild, not just implied by the raw
data — see [High-value assets](docs/architecture.md#high-value-assets-servers-as-the-prioritization-signal)
and [OS end-of-life](docs/architecture.md#os-end-of-life-a-third-prioritization-axis).

This package is a standalone library and CLI — no Django, no web framework —
but it does own real scheduling (Celery) and persistence (SQLAlchemy/SQLite)
directly; see [Scheduling & persistence](docs/architecture.md#scheduling--persistence). It
can be used either directly (the CLI below) or as a pinned git dependency
(`uv add git+https://.../agent-parity@vX.Y.Z`) inside a larger project. It
models a real MSSP-style topology: multiple client organizations in one
`config.yaml`, each with its own AD domain(s) and enabled vendor(s) — see
[Credentials](docs/architecture.md#credentials-configyaml--env).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. Two ways in, no
server or database either way:

```console
uv sync
uv run agent-parity compare ad_export.csv agent_export.csv   # your own two CSVs, zero config
uv run agent-parity run --all                                 # config.yaml + connectors, every client
uv run agent-parity run --client acme                         # just one client
uv run pytest                                                  # 290+ tests, all offline
```

`compare` needs no vendor connector, no `config.yaml`, and no credentials at
all — see [Bring your own CSVs](#bring-your-own-csvs) right below. `run` is
the config.yaml/connector-driven path; with no `.env` (or unset credentials
in it) every connector falls back to `sample_data/` fixtures — see
[Sample data](#sample-data) below for what's in them.

## Bring your own CSVs

The correlation engine only ever needs two DataFrames in a known shape — it
doesn't care whether they came from a connector or a plain CSV. `agent-parity
compare` is the zero-config path: no `config.yaml`, no connector, no
credentials, just two files.

The **AD CSV** is `Export-ADDevices.ps1`'s own output — hand that script to
whoever manages the domain and run it against a domain controller (see
[The deployment model](docs/architecture.md#the-deployment-model-remote-script-execution-not-direct-ad-access) for why it's a script instead of a direct LDAP query).

The **agent CSV** is whatever EDR/inventory tool you have, mapped into
agent-parity's own column schema (`src/agent_parity/agent_csv.py`) — every vendor
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
`config.yaml` + `agent-parity run` (see [Credentials](docs/architecture.md#credentials-configyaml--env)) is the next step up.

## Architecture

Four layers, collect → correlate → report: vendor connectors (plus an AD export script that runs remotely through the vendor's own scripting channel) feed a pandas correlation engine, and a thin CLI, or the SQLite/Celery scheduled path, reports on the result. Everything runs offline through fixtures by default.

The full write-up is in [docs/architecture.md](docs/architecture.md). Good places to start: [the deployment model](docs/architecture.md#the-deployment-model-remote-script-execution-not-direct-ad-access), [the correlation](docs/architecture.md#the-correlation-a-pandas-merge-kept-honest), [high-value assets](docs/architecture.md#high-value-assets-servers-as-the-prioritization-signal), [OS end-of-life](docs/architecture.md#os-end-of-life-a-third-prioritization-axis), [scheduling & persistence](docs/architecture.md#scheduling--persistence) and [credentials](docs/architecture.md#credentials-configyaml--env).

## Design decisions

The key choices behind the design, each with its context, the alternatives, the tradeoffs and the code and tests that pin it, are recorded as short ADRs. See the [index](docs/decisions/README.md).

## Security

Report vulnerabilities through the private channel in [SECURITY.md](SECURITY.md). The [threat model](docs/threat-model.md) covers which credentials and data the tool handles, where the trust boundaries are, and the residual risks. CI runs ruff, mypy, tests with a coverage gate, pip-audit and gitleaks, and CodeQL runs weekly.

## Sample data

Two synthetic clients with deliberate, reviewable gap scenarios:

|                     | Acme Corp (`acme`)                                                                                     | Globex (`globex`)         |
|---------------------|----------------------------------------------------------------------------------------------------------|---------------------------|
| AD computer objects | 44                                                                                                        | 37                        |
| Vendors             | SentinelOne + Carbon Black (2 tenants) + BitDefender                                                      | SentinelOne + BitDefender |
| Missing agent       | 5 (new server, new-hire imaging gaps, a rebuild, a disabled stray)                                        | 7                         |
| Stale coverage      | 3 (15–30 days quiet, one per vendor)                                                                      | 3                         |
| Orphaned agents     | 7 (decommissioned server, shadow-IT laptop, workgroup kiosk, renamed machine, 3 unmatched branch devices) | 3                         |

Acme's second Carbon Black tenant (`sample_data/acme/carbonblack_inventory_branch.json`)
adds 3 more devices that have no matching AD object in Acme's single AD
domain — a realistic case for a branch office whose endpoints report to a
separate tenant but haven't (yet, or ever will) been domain-joined to the
same AD — all three land as `orphaned_agent`.

Details worth noticing: some devices report to two vendors (exercising the
one-row-per-vendor merge); one agent per client reports its FQDN while AD has
the short name (normalization resolves it); one orphan per client is a
renamed machine normalization deliberately *can't* resolve. Fixture
timestamps are rebased at load so the newest check-in is always "now" and the
authored stale/recent split stays stable regardless of when you run the demo.

## Optional: Docker

This is this package's actual deployment shape now (see
[Scheduling & persistence](docs/architecture.md#scheduling--persistence)) — not a
placeholder for a separate hub project's deployment, since that plan is
archived. Enough to run the CLI standalone without a local `uv` install, run
the scheduled Celery path, or exercise the real object-storage handoff
against a local MinIO instead of `moto`'s simulated S3:

```bash
docker build -f docker/Dockerfile -t agent-parity .
docker run --rm -v "$PWD/output:/app/output" agent-parity run

# or, via compose (also brings up a local MinIO the container can reach):
docker compose -f docker/docker-compose.yml run --rm agent-parity run

# the scheduled path: Redis broker + a worker + beat, all sharing one SQLite file
docker compose -f docker/docker-compose.yml up -d redis worker beat
```

Runs fully offline by default (config.yaml's fixture-mode connector + AD
export) — no `.env` required.

The two live-infrastructure paths this package has — the AD-export
object-storage handoff (see
[the architecture doc](docs/architecture.md#ad-export-handoff-object-storage-instead-of-the-vendor-channel-mandatory-for-live-exports))
and the Celery scheduling stack — can be exercised locally against real
MinIO/Redis/worker/beat instead of just `moto`'s simulated S3 and
`task_always_eager`:

```console
cd docker
./smoke_test.sh     # brings up minio+redis+worker+beat, round-trips a real
                     # object AND a real Celery chord through them
```

Neither this nor `uv run pytest` (which never touches real infrastructure)
overlap — the smoke test needs Docker and touches a real network. Run it
manually, e.g. before cutting a release.

## Tests

`uv run pytest` — all offline, no live credentials or external services.
Test files mirror `src/agent_parity/`'s subpackage layout — `tests/connectors/`,
`tests/scheduling/` and `tests/shared/` pair with `connectors/`, `scheduling/`
and `shared/`, matching
vega-tools' convention — while everything else stays flat since its source
module does too:

- **Correlation**: one test per `CoverageStatus` outcome, the
  merged-row-count-equals-union-of-join-keys invariant, FQDN/case
  normalization, configurable staleness, multi-vendor rows, and the
  high-value-asset backfill (a missing Domain Controller must be
  classified as `machine_type="server"` from AD's OS text alone, with zero
  agent data, and an agent-reported machine_type must never be overridden).
- **Pipeline collection** (`test_pipeline.py`): multi-domain AD concatenation
  and partial-failure tolerance (Globex's two domains), and
  `run_correlation_for_client`'s happy path plus its "every AD domain
  failed" `None` case.
- **Fixture scenarios** (`test_pipeline_sync.py`): named tests pin the
  authored gap scenarios (`acme-sql02` is missing, `acme-fs-old` is
  orphaned, …) so a fixture edit that breaks a scenario fails loudly.
- **Config resolver**: global vs. per-client scope, `${VAR}` resolution,
  fixture-mode fallback on unset secrets, unknown-vendor rejection.
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
  `from_dict` round-tripping (used to pass records across a Celery task
  boundary — see below).
- **Persistence** (`test_db.py`, `test_persistence.py`): SQLAlchemy model
  round-trips and constraint enforcement, `finalize_run`'s FAILED-on-no-AD-data
  branch, and `persist_correlation`'s idempotency (a duplicate call against an
  already-finalized run must not double the snapshot count).
- **Celery tasks** (`test_tasks.py`): the fan-out/fan-in chord, run eagerly
  (`task_always_eager`, no broker) — one flaky vendor producing a `PARTIAL`
  run rather than none at all, a multi-domain client's AD exports all firing,
  the FAILED-on-no-AD-data path through the real callback, a duplicate
  callback delivery not double-counting, and `dispatch_all_clients` respecting
  (and `force`-overriding) each client's `sync_interval_hours`.
- **Splunk delta export** (`test_splunk_export.py`, plus cases in
  `test_persistence.py`): the HEC forwarder — disabled/no-op when
  unconfigured, correct envelope shape, batching above 100 events,
  `SplunkExportError` on a failed POST — and the diffing logic itself: a
  first run with no history emits every snapshot as new, a second run only
  emits genuinely changed statuses, an unchanged run emits nothing, and a
  simulated Splunk outage never fails the underlying `finalize_run`.
- **Inlined helpers in isolation** (`tests/shared/`): `RestAdapter`'s
  content-type-based parsing, retry configuration, header merging, `files=`
  passthrough; `ObjectStorage`'s presigned-URL round trip (against `moto`);
  the storage-backed script-export handoff, the vendor-connector base, the
  SentinelOne RSO mixin, config env-ref resolution, atomic file writes,
  logging setup and tabular file I/O. All run under the normal
  `uv run pytest`.

Also deliberately **not** covered here: whether a real MinIO/AWS S3 endpoint
actually works — `moto` proves the *logic* is right but never touches a real
network. That's what `docker/smoke_test.sh` is for; see
[Optional: Docker](#optional-docker) above.

## Limitations and roadmap

Known limitations, possible next steps, and what is deliberately out of scope (a web dashboard, real-time ingestion) are in [docs/limitations-and-roadmap.md](docs/limitations-and-roadmap.md). What shipped in each release is in [CHANGELOG.md](CHANGELOG.md).
