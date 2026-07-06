# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A portfolio rebuild (synthetic data only, no proprietary code) of a device coverage
reconciliation tool: it correlates an Active Directory computer inventory against an
EDR/security agent inventory (SentinelOne, Carbon Black, or BitDefender) to find devices
missing agent coverage, orphaned agents with no matching AD object, and stale agent
check-ins. See [README.md](README.md) for the full architecture writeup — read it
before making structural changes, since several design decisions there are deliberate
and were agreed on with the project owner rather than obvious from the code.

This package is deliberately standalone: no Django, no Celery, no web framework, no
database. It's meant to be consumed as a pinned git dependency
(`uv add git+https://.../agent-parity@vX.Y.Z`) by a separate "hub" project that
provides shared web/scheduling/persistence infrastructure for multiple tools. Keep it
that way — don't reintroduce a framework dependency here just because a consuming
project happens to use one.

Deliberately simple: one organization, one vendor, per `config.yaml`. There is no
multi-client/multi-tenant/multi-account topology here (an earlier iteration had one,
modeled on the original tool's MSSP heritage — it was removed on purpose, not
incidentally, so don't reintroduce `clients:`/`vendors:` nesting or per-vendor
account/site multiplicity). If a consuming project genuinely needs to run this for
several organizations, that's several `AppConfig`s (e.g. several config files) and
several calls into `pipeline.run_correlation` — not a feature of this package.

## Commands

```console
uv sync                                     # install deps
uv run agent-parity compare ad.csv agent.csv   # two CSVs, zero config.yaml/connectors/credentials
uv run agent-parity run                        # config.yaml + connector

uv run pytest                               # full suite, offline, no live credentials needed
uv run pytest tests/test_correlation.py -k covered   # single test/file

docker build -f docker/Dockerfile -t agent-parity .   # bare-bones standalone image
docker compose -f docker/docker-compose.yml up -d minio    # optional: local MinIO for the live storage path
docker/smoke_test.sh                                 # round-trips a real object through it
```

There is no linter/formatter config in this repo (`pyproject.toml` has no `[tool.ruff]`
or `[tool.black]`) — formatting has so far been done via the IDE's reformatter, not a CLI tool.

## Architecture

Four layers, collect → correlate → report:

- **`agent_parity/connectors/`** — one class per vendor (SentinelOne, Carbon Black,
  BitDefender), each implementing `fetch_inventory()`/`deploy_and_run()`.
- **`agent_parity/ad_sync/`** + **`agent_parity/deployment/`** — parsing the AD export
  script's CSV output and running it remotely through a vendor's own scripting capability.
- **`agent_parity/agent_csv.py`** — parsing a generic, vendor-agnostic agent/EDR
  inventory CSV, for callers with no connector/credentials at all.
- **`agent_parity/correlation/engine.py`** — the pandas merge/classification core.
- **`agent_parity/pipeline.py`** — two orchestration entrypoints that tie the above
  together: `run_correlation()` (config.yaml + connector, live or fixture) and
  `correlate_from_csvs()` (two CSVs, zero config). **`agent_parity/cli.py`** is a thin
  `run`/`compare` wrapper around them for standalone use. A consuming project (the hub)
  is expected to call `pipeline.run_correlation()` directly rather than shell out to
  the CLI, since it will want the `CorrelationResult` in-process to persist itself.

## Correlation engine (`agent_parity/correlation/engine.py`)

This is the analytical core and is deliberately a `.pipe()` chain, not one function:
`add_join_key` → `merge_with_agents` (`pd.merge(..., how="outer", indicator=True)`) →
`classify_coverage` (turns the merge indicator + a `last_seen` staleness check into
`CoverageStatus`) → `backfill_machine_type` → `classify_eol_status`. Each stage is
independently testable; keep it that way rather than inlining. `join_key`
normalization (strip DNS suffix, lowercase, trim) is the only matching logic —
there's no fuzzy matching, by design (noted as future work).

**`backfill_machine_type` exists for one reason**: `machine_type` (see
`AgentDevice`'s docstring) only ever comes from the agent side of the merge, so a
`missing_agent` row — no agent record at all — would otherwise carry no criticality
signal whatsoever. That's backwards for a coverage tool whose whole point (see
README's "High-value assets" section — this project's original purpose was a
quarterly client report prioritizing exactly this) is flagging a missing Domain
Controller *harder* than a missing workstation. It backfills from AD's own OS text
via `infer_machine_type()` (`agent_parity/models.py`) — the same heuristic
Carbon Black/BitDefender's connectors use — but only for rows where `machine_type`
isn't already set; an agent-reported value always wins. Don't try to infer
criticality from the hostname — that's exactly the unreliable signal this design
deliberately avoids (file/storage servers can be named anything; a Windows Server
SKU can't fake being one).

**`classify_eol_status` (see `agent_parity/os_eol.py`) is the third prioritization
axis**, independent of coverage: a covered end-of-life server still needs an OS
upgrade. It resolves `os_build` per row with the same both-sides-then-fallback
precedence as `backfill_machine_type` — agent-reported build first (only
SentinelOne sets one), then AD's own `operatingSystemVersion`-derived build, then
free-text OS-name matching (the only option for Carbon Black/BitDefender-only
rows, which never carry a build number). Because a column is only pandas-suffixed
when it exists on *both* merge sides, watch for a bare (unsuffixed) `os_build`
column if a test helper's frame doesn't include it on both the AD and agent side —
this silently breaks the precedence logic without erroring. `eol_status` is always
one of the four `OSLifecycleStatus` values, never blank, because AD's build/OS
text is captured for every row, including `missing_agent`.

Tests for this module assert on classification outcomes and merge-invariants (row
count = union of join keys), not on `pd.merge` itself — follow that pattern for new
correlation tests rather than re-testing pandas.

## Collection pipeline (`agent_parity/pipeline.py`)

`run_correlation(config, stale_days=None)` is the config.yaml/connector entrypoint:
collect the AD export (across every domain the organization spans — see "Multi-domain
AD" below), collect the configured vendor's inventory, then call
`correlation.engine.correlate()`. Returns `(CorrelationResult | None, vendor_status)` —
`None` only when every AD domain failed, meaning there's nothing to correlate against.

`correlate_from_csvs(ad_csv_text, agent_csv_text, stale_days=14)` is the zero-config
counterpart — no `AppConfig`, no connector, no credentials, just
`ad_sync.parser.parse_ad_export()` + `agent_csv.parse_agent_csv()` feeding straight into
`correlate()`. This is the on-ramp for anyone without a supported vendor connector set
up at all; `run_correlation` is the next step once collection needs to be
repeatable/scheduled against a live API instead of a one-off export file.

No persistence and no history live in either function on purpose: that's a consuming
project's job, not this package's. `agent_parity/cli.py` is the only built-in
consumer — its `run`/`compare` subcommands wrap the two functions above, write
`output/<name>.csv`, and print a summary, nothing more.

## Connectors (`agent_parity/connectors/`)

**Adding a vendor is "write one connector class"**, not "edit a central table."
`connectors/base.py`'s `@register_connector` class decorator adds a connector to
`CONNECTOR_REGISTRY` (re-exported as `connectors.CONNECTOR_CLASSES`) keyed by its own
`vendor` attribute — `config.load_config()` validates `config.yaml`'s `vendor:` value
against this registry. A 4th vendor needs a new module (decorated) plus one import
line in `connectors/__init__.py` to trigger registration — nothing else.

**`AgentConnector` (`connectors/base.py`) is split across two layers.** The generic
half — a credentialed `RestAdapter` session, `is_live`, live/fixture dispatch for
`deploy_and_run()`, `_poll_until`, `_request`/`_request_json`/`_as_text`,
`_fixture_path`, `ConnectorError`, and the `ConnectorRegistry` class itself — lives in
`shared_tools.remote_exec.VendorConnector`, shared via `py-shared-tools` with other
projects (`credential-audit`) that talk to the same kind of vendor remote-execution
APIs. `AgentConnector` subclasses it and adds only what's specific to *this*
project: `fetch_inventory()`/`_fixture_fetch_inventory()`/the abstract
`_live_fetch_inventory()`/`_parse_inventory()` pair, and this project's own
`_fixture_deploy_and_run()` override (the AD-export-CSV-by-target_id behavior below).
`CONNECTOR_REGISTRY` here is agent-parity's own `ConnectorRegistry()` instance — the
registry mechanism is shared, but each project's instance is independent, so
`credential-audit` registering its own vendor connectors on the same base can never
collide with these entries. **When touching connector internals, check whether the
change belongs in `agent_parity/connectors/base.py` (this project's inventory/AD-export
specifics) or `py-shared-tools`'s own `shared_tools/remote_exec.py` (generic vendor-API
mechanics any consumer of the shared base would want) — don't add project-specific
logic to the shared base, and don't duplicate generic mechanics back into this file.**

**`connectors/sentinelone.py` goes one step further: even the vendor-*specific* RSO
mechanics are shared.** `SentinelOneConnector(SentinelOneRSOMixin, AgentConnector)` —
`_headers` and `_live_deploy_and_run` (the upload -> execute -> poll `remote-scripts
/status` -> fetch-files sequence) moved to `shared_tools.sentinelone.SentinelOneRSOMixin`
once `credential-audit` needed a `SentinelOneConnector` of its own and the RSO code
turned out to be byte-for-byte identical — not just the generic dispatch mechanics,
the actual SentinelOne API calls. It's a **mixin**, not a full base class, specifically
so each project can combine it with its own project-specific base via multiple
inheritance (`SentinelOneRSOMixin, AgentConnector` here; `SentinelOneRSOMixin,
CredentialAuditConnector` in `credential-audit`) rather than forcing one inheritance
shape on every consumer. `connectors/sentinelone.py` here now only defines
`vendor`/`required_credentials`/`_parse_inventory`/`_live_fetch_inventory` — the
inventory-fetching half, which is all that's actually agent-parity-specific.
If Carbon Black's Live Response mechanics (`carbonblack.py`'s `_live_deploy_and_run`)
ever get duplicated into a second project too, extract a `CarbonBlackLiveResponseMixin`
the same way, at that point — same "duplicated twice, not hypothetically" bar that
applied to `VendorConnector` and `SentinelOneRSOMixin`, not before.

**Fixture fallback is not a test-only shim — it's the default runtime path.** `is_live`
gates on whether all `required_credentials` are present; if not, `fetch_inventory()`
reads `sample_data/<vendor>_inventory.json` and `deploy_and_run()` returns
`sample_data/ad_export_<target_id>.csv` — one file per domain controller, since an
organization spanning multiple AD domains (`AppConfig.ad_target_devices`, see
"Multi-domain AD" below) has a distinct export per domain, not one shared file.
Timestamps are rebased so the newest check-in is ~now (`rebase_timestamps` /
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

Live mode goes through `shared_tools.rest_adapter` (`RestAdapter`) rather than a
bare `requests.Session` — retries/backoff on 429/5xx are configured there once,
shared by all three vendors (wired up inside `VendorConnector.__init__`, not
per-connector). `RestAdapter`, `ObjectStorage` (see "AD-export object storage"
below), and `VendorConnector`/`remote_exec` all live in
[py-shared-tools](https://github.com/chingdrop/py-shared-tools),
a separate git repo consumed as a plain pinned `uv` git dependency
(`py-shared-tools[storage]`, `[tool.uv.sources]` in `pyproject.toml` pins it to
a tag) — reused as-is across other projects rather than copy-pasted, which is
what `RestAdapter`/`ObjectStorage`'s own comments used to say before their
extraction (and what `AgentConnector`'s own `deploy_and_run`/polling/registry
logic said before `VendorConnector`'s). Editing any of them means editing the
files in a separate clone of `py-shared-tools`, not anywhere in
`agent_parity/`; there's no local copy left to accidentally diverge from.
**This used to be a vendored git submodule at `vendor/py-shared-tools`** with a
local editable path override — dropped in favor of a plain git dependency
because `uv` can't reconcile two sibling projects (`agent-parity` and
`credential-audit`) each vendoring their own copy of the same package under
different subdirectory paths; a consumer needing both (like `cyberhub`) would
hit an unresolvable "conflicting URLs for package py-shared-tools" error. A
plain git dependency pinned to the same tag in both projects resolves as one
package. Bumping the pin means updating the `rev` in `[tool.uv.sources]`, not
`git submodule update`.

`RestAdapter.request()` returns already-parsed content (`dict` for JSON, `str`
for text/html, `bytes` otherwise), not a `Response` object, so connector call
sites use `self._request_json(...)` when they know the endpoint returns a
JSON object, or `self._as_text(...)` on the raw `_request(...)` result when
they need guaranteed text (e.g. SentinelOne's fetch-files script output). No
test exercises real network I/O; `tests/test_connectors.py` proves the
RestAdapter wiring (retry config, JSON/text parsing) by monkeypatching the
underlying `requests.Session.request`, not by hitting a live API.
`RestAdapter`'s own unit tests (content-type parsing, header merging, retry
config, the `files=` passthrough) live in `py-shared-tools`'s own `tests/`,
not in this repo's `tests/` — they're that repo's own test suite, run there
via `uv run pytest`, not part of `uv run pytest` at the agent-parity root.

**`AgentDevice.platform`/`machine_type` are normalized to SentinelOne's wording**
(most of the historical client base was on S1, so its vocabulary is canonical).
`_parse_inventory` in each connector sets them: SentinelOne passes its own
`osType`/`machineType` straight through; Carbon Black lowercases its uppercase
`os` enum for `platform` and infers `machine_type` from OS text
(`infer_machine_type`, defined in `agent_parity/models.py`, re-exported from
`connectors/base.py` for existing call sites) since it has no equivalent field;
BitDefender maps its numeric `machineType` enum to S1's string wording
(`_MACHINE_TYPES` in `connectors/bitdefender.py`) and infers `platform` from OS
text (`infer_platform`) since it has no equivalent field. `infer_platform`/
`infer_machine_type` live in `models.py`, not `connectors/base.py`, specifically
so `correlation/engine.py` can use them too (for AD-only rows — see
`backfill_machine_type`) without pulling in the connector stack's
`requests`/`RestAdapter` dependency chain (now `py-shared-tools`)
just for two pure string functions.
If a 4th vendor is
added, decide per-field whether it reports something directly-mappable
(prefer a direct map, like BitDefender's `machineType`) or needs inference
(like Carbon Black's `machine_type`) — don't guess when the vendor's raw API
actually has the field. **`agent_version` is deliberately never touched this
way** — each vendor's version numbering is real and vendor-specific; making
one look like another's would be fabricating a value, not normalizing one.

## AD-export object storage (`shared_tools.script_export`, in the `py-shared-tools` repo)

**The storage-backed handoff itself moved to `shared_tools.script_export`** —
`run_ad_export` in `deployment/script_runner.py` is now a thin wrapper
supplying this project's own script path (`AD_EXPORT_SCRIPT`), object-key
prefix (`"ad-exports"`), expected CSV header (`"Name"`), and error wording to
`shared_tools.script_export.run_script_export`. This extraction happened
once `credential-audit` needed the *exact same* orchestration for its own
AD-metadata export — not just the generic `VendorConnector` dispatch, the
whole "storage mandatory for live, fixture bypasses it entirely,
presigned-URL round trip, validate the result" function was byte-for-byte
identical under two names before it moved. `ScriptExecutionError` is
re-exported from `deployment/script_runner.py` for existing import sites, not
redefined there. **Mandatory for any live connector** — not an optional
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
4. `run_script_export` downloads with `get_object` and deletes the object
   (best-effort; failures there only log, they never fail an export that
   already succeeded).

**Fixture mode is the one exception** — `run_script_export` checks
`connector.is_live` *before* the storage check. There's no real endpoint in
fixture mode to have uploaded anything, so it always returns the canned
`sample_data/` CSV directly, regardless of whether storage happens to be
configured. This is also why the uv demo path can leave `STORAGE_*` unset in
`.env`: safe only because the vendor has no live credentials there either, so
no script ever actually runs.

Built against the S3 API via `boto3`, not a specific product — MinIO
(self-hosted, via `docker/docker-compose.yml`) for local/dev, real AWS S3 in
production, same `ObjectStorage` class either way; only `endpoint_url`
changes. This is *not* Azure Blob Storage capable — different API, would need
a second implementation with a different SDK, not just different config.
**`StorageConfig`/`get_storage` also moved, into `shared_tools.config`** —
`config.get_storage(config)` here is a one-line delegate to
`shared_tools.config.get_storage(config.storage)`, same byte-for-byte logic
`credential-audit`'s own `get_storage` needs. `get_storage(config)` returns
`None` when unconfigured (`config.storage.enabled` is False);
`config.storage.backend` only supports `"s3"` today, and `get_storage` raises
`ConfigError` for anything else. **If you need to change the storage-handoff
mechanics or the `StorageConfig` shape itself, edit `py-shared-tools`'s own
`shared_tools/script_export.py` or `shared_tools/config.py`, not this
project's own files** — they're shared with `credential-audit`, and a local
copy here would silently diverge.

Only SentinelOne and Carbon Black connectors accept `script_args` meaningfully
(BitDefender doesn't implement `_live_deploy_and_run` at all). SentinelOne passes
them as RSO's `inputParams`; Carbon Black appends them to the raw PowerShell
command line (`CarbonBlackConnector._powershell_args`) since Live Response's
`create process` takes a command string, not structured parameters — different
mechanisms, same `script_args: dict[str, str]` contract from `deploy_and_run`.

Tests use `moto` (`@mock_aws` / the `mock_aws()` context manager) — no real
MinIO or AWS S3 touches the test suite, and a real presigned-URL PUT/GET round
trip still gets exercised. `ObjectStorage`'s own unit tests live in
`py-shared-tools`'s own `tests/test_storage.py`; the *orchestration logic*
(mandatory-storage rule, fixture bypass, upload/download/cleanup, empty/
wrong-shaped output) is now exhaustively tested in
`py-shared-tools`'s own `tests/test_script_export.py` too, using a generic
fake connector — that suite is actually a superset of what this repo used to
cover on its own (it gained the wrong-shaped-output tests `credential-audit`
had added that this project's copy was missing). This repo's own
`tests/test_script_runner.py` is now a thin *wiring* smoke test — proving
`run_ad_export` threads its own `object_key_prefix`/`header_marker`/script
path through correctly — not a re-test of `run_script_export`'s own branching.
`moto` proves the code path, not the network — `docker/smoke_check_storage.py`
(run via `docker/smoke_test.sh`, Docker-only) round-trips a real object through
the actual `minio` service, including auto-creating the smoke-test bucket
(`ObjectStorage` itself has no bucket-admin methods on purpose; production
bucket provisioning is out-of-band, so that stays smoke-test-only code).

`docker/Dockerfile` is a separate, bare-bones concern from the MinIO
service above — it builds a standalone image for running the `agent-parity`
CLI itself (`docker build -f docker/Dockerfile -t agent-parity .`; entrypoint
is `uv run --no-sync agent-parity`, `--no-sync` because a plain `uv run`
would re-resolve against `uv.lock`'s full `[dev]` group on every container
start, silently reinstalling `moto`/`boto3-stubs`/etc. that `--no-dev`
deliberately excluded from the image at build time). `docker-compose.yml`'s
`agent-parity` service just wires that Dockerfile up alongside `minio`, so
`docker compose run agent-parity run` works out of the box. Not part of
this project's own deployment story — `cyberhub` supersedes this entirely
once this package is consumed there; it exists purely so the CLI can run
standalone (an analyst's laptop, a CI job) without a local `uv` install.
Congruent with `credential-audit`'s own `docker/Dockerfile` — keep the two
in sync (same layer-caching shape, same non-root-user + `--no-sync` fix) if
one changes.

## Credential resolution (`agent_parity/config.py`)

`load_config()` parses `config.yaml` (topology) + `.env` (secrets) into one
`AppConfig` — every secret in `config.yaml` is a `${VAR}` reference; an unset
variable resolves to `None` rather than raising, which is exactly what puts the
connector into fixture mode. This is the *only* config entrypoint — there is no
database and no second config shape, so this is also exactly what a consuming
project should call.

`AppConfig` is flat and deliberately small: `stale_days`, `vendor` (a string
validated against `CONNECTOR_CLASSES` — see "Connectors" above — so any registered
connector works, not a hardcoded three), `credentials` (one dict, handed straight to
the connector), `ad_target_devices` (a tuple — see "Multi-domain AD" below), and
`storage`. There is no per-client/per-account/per-site nesting anywhere in this
module — `get_connector(config)` builds the one connector directly from
`config.vendor`/`config.credentials`, with `fixture_dir` always `SAMPLE_DATA_DIR`
(no per-organization subfolder).

## Multi-domain AD (`AppConfig.ad_target_devices`)

An organization can span more than one AD domain/forest — no single domain
controller can enumerate computer objects outside its own domain, so
`ad_target_devices` is a tuple, not a single hostname, and the export script runs
once per entry. `agent_parity/pipeline.py`'s `collect_ad_frame` is the orchestrator:
it loops the tuple, calling `collect_ad_csv` + `parse_ad_export` per domain, and
concatenates the results with `agent_parity/ad_sync/parser.py`'s `concat_ad_frames`
into the one master DataFrame `correlate()` actually sees. A single-domain
organization (the common case) is just the `len == 1` case of this same loop —
there's no separate single-domain code path, by design.

Tolerant of partial failure the same way vendor collection already is: one domain's
status is recorded independently (`f"ad:{target_device}"` in `vendor_status`, e.g.
`ad:ACME-DC01`), and `collect_ad_frame` returns `None` for the frame only when
*every* domain failed — `run_correlation` is where that "nothing to correlate
against" case is handled (returns `None` up to its own caller rather than attempting
to correlate against nothing); don't duplicate that check elsewhere.

Fixture mode picks the CSV by target device — `sample_data/ad_export_<target_device>.csv`
(`connectors/base.py`'s `deploy_and_run`) — one file per domain, not one shared
`ad_export.csv`. `sample_data/ad_export_ACME-BR-DC01.csv` exists purely to give
`tests/test_pipeline.py`'s multi-domain concatenation test a second, real fixture
file to concatenate against the default demo domain (`ad_export_ACME-DC01.csv`) —
it isn't part of the default `config.yaml`'s single-domain demo.

## Testing conventions

- `tests/test_pipeline_sync.py` pins the specific gap scenarios authored into
  `sample_data/` by join key (e.g. `acme-sql02` is `missing_agent`). If you
  regenerate or edit the fixtures, these tests are the regression check — a
  scenario silently changing status is a fixture bug, not a test bug, unless the
  change was intentional.
- `sample_data/` fixtures were originally generated by a one-off script that was
  never committed (it lived in a scratch directory outside the repo). The
  committed CSV/JSON files are the source of truth now; there's no
  `generate_fixtures.py` in this repo to regenerate them from. The layout is flat
  (no per-organization subfolder) — `get_connector`'s `fixture_dir` is always
  `SAMPLE_DATA_DIR` directly.
- Test coverage is intentionally close to 1:1 with source modules: `test_models.py` ↔
  `agent_parity/models.py`,
  `test_pipeline.py` ↔ `agent_parity/pipeline.py` (the collection helpers plus
  `correlate_from_csvs`, deliberately exercised with hand-rolled CSVs rather than
  `sample_data/`, to prove that path has zero dependency on the demo fixtures),
  `test_agent_csv.py` ↔ `agent_parity/agent_csv.py`, `test_cli.py` ↔
  `agent_parity/cli.py`, `test_config.py` ↔ `agent_parity/config.py`. `RestAdapter`,
  `ObjectStorage`, and `VendorConnector`/`ConnectorRegistry` (`remote_exec.py`) are
  the exception to "lives in this repo, tested in this repo's `tests/`" — they and
  their tests (`test_rest_adapter.py`, `test_storage.py`, `test_remote_exec.py`)
  live in the `py-shared-tools` repo instead, since that code is shared
  across other projects, not agent-parity-specific. `test_connectors.py` still
  covers `AgentConnector`'s own inventory-fetching and fixture-deploy-and-run
  behavior in this repo — only the generic dispatch/polling/registry mechanics
  moved. When adding a
  new module with real logic in it, add its
  test file alongside — don't rely on it being incidentally exercised by a
  higher-level pipeline test.
