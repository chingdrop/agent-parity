# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A portfolio rebuild (synthetic data only, no proprietary code) of a device coverage
reconciliation tool: it correlates an Active Directory computer inventory against
EDR/security agent inventories (SentinelOne, Carbon Black, BitDefender) to find devices
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

## Commands

```console
uv sync                                     # install deps
uv run agent-parity --all                   # collect + correlate every client, write out/<slug>.csv
uv run agent-parity --client acme           # just one client

uv run pytest                               # full suite, offline, no live credentials needed
uv run pytest tests/test_correlation.py -k covered   # single test/file

docker compose -f docker/docker-compose.yml up -d    # optional: local MinIO for the live storage path
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
- **`agent_parity/correlation/engine.py`** — the pandas merge/classification core.
- **`agent_parity/pipeline.py`** — the one orchestration entrypoint (collect everything
  for a client, then correlate) that ties the above together; **`agent_parity/cli.py`**
  is a thin wrapper around it for standalone use. A consuming project (the hub) is
  expected to call `pipeline.run_correlation_for_client()` directly rather than shell
  out to the CLI, since it will want the `CorrelationResult` in-process to persist itself.

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

`run_correlation_for_client(config, client_cfg, stale_days=None)` is the one
entrypoint: collect the AD export (across every domain — see "Multi-domain clients"),
collect every enabled vendor's inventory (across every site/tenant/account — see
"Multi-site/tenant clients"), then call `correlation.engine.correlate()`. Returns
`(CorrelationResult | None, vendor_status)` — `None` only when every AD domain failed,
meaning there's nothing to correlate against. No persistence and no history live
here on purpose: that's a consuming project's job, not this package's. `agent_parity/cli.py`
is the only built-in consumer — it writes `out/<slug>.csv` and prints a summary,
nothing more.

## Connectors (`agent_parity/connectors/`)

Every connector implements `fetch_inventory()` and `deploy_and_run(script_path,
target_id)` against a shared `AgentConnector` ABC (`connectors/base.py`). **Fixture
fallback is not a test-only shim — it's the default runtime path.** `is_live` gates
on whether all `required_credentials` are present; if not, `fetch_inventory()` reads
`sample_data/<client>/<vendor>_inventory.json` and `deploy_and_run()` returns
`sample_data/<client>/ad_export_<target_id>.csv` — one file per domain
controller, since a client with multiple AD domains (`ClientConfig.ad_target_devices`,
see "Multi-domain clients" below) has a distinct export per domain, not one
shared file. Timestamps are rebased so the newest check-in is ~now
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
`requests`/`RestAdapter` dependency chain just for two pure string functions.
If a 4th vendor is
added, decide per-field whether it reports something directly-mappable
(prefer a direct map, like BitDefender's `machineType`) or needs inference
(like Carbon Black's `machine_type`) — don't guess when the vendor's raw API
actually has the field. **`agent_version` is deliberately never touched this
way** — each vendor's version numbering is real and vendor-specific; making
one look like another's would be fabricating a value, not normalizing one.

## AD-export object storage (`agent_parity/storage.py`)

S3-compatible handoff for `Export-ADDevices.ps1`'s output, wired through
`deployment/script_runner.run_ad_export`. **Mandatory for any live connector** —
not an optional upgrade. Vendor remote-execution output channels (SentinelOne
RSO's fetch-files, Carbon Black Live Response's command output) don't reliably
preserve a CSV's exact formatting (encoding, line endings) and have real
output-size limits a full AD export can exceed, so the vendor channel is never
used to carry the actual export data once a connector is live:

1. `run_ad_export` raises `ScriptExecutionError` immediately if `connector.is_live`
   and `storage is None` — a live export with no storage configured is a
   configuration error, not something to silently work around by falling back
   to the vendor channel. Don't reintroduce that fallback.
2. Otherwise it generates a presigned PUT URL (`ObjectStorage.presigned_put_url`,
   15-minute default expiry) and passes it to `deploy_and_run(..., script_args={"UploadUrl": ...})`.
3. The script uploads its own CSV there — the vendor call's return value is
   discarded entirely, since the real output never goes through it.
4. `run_ad_export` downloads with `get_object` and deletes the object
   (best-effort; failures there only log, they never fail an export that
   already succeeded).

**Fixture mode is the one exception** — `run_ad_export` checks `connector.is_live`
*before* the storage check. There's no real endpoint in fixture mode to have
uploaded anything, so it always returns the canned `sample_data/` CSV directly,
regardless of whether storage happens to be configured. This is also why the
uv demo path can leave `STORAGE_*` unset in `.env`: safe only because no vendor
has live credentials there either, so no script ever actually runs.

Built against the S3 API via `boto3`, not a specific product — MinIO
(self-hosted, via `docker/docker-compose.yml`) for local/dev, real AWS S3 in
production, same `ObjectStorage` class either way; only `endpoint_url`
changes. This is *not* Azure Blob Storage capable — different API, would need
a second implementation with a different SDK, not just different config.
`get_storage(config)` returns `None` when unconfigured (`config.storage.enabled`
is False); `config.storage.backend` only supports `"s3"` today, and
`get_storage` raises `ConfigError` for anything else.

Only SentinelOne and Carbon Black connectors accept `script_args` meaningfully
(BitDefender doesn't implement `_live_deploy_and_run` at all). SentinelOne passes
them as RSO's `inputParams`; Carbon Black appends them to the raw PowerShell
command line (`CarbonBlackConnector._powershell_args`) since Live Response's
`create process` takes a command string, not structured parameters — different
mechanisms, same `script_args: dict[str, str]` contract from `deploy_and_run`.

Tests use `moto` (`@mock_aws` / the `mock_aws()` context manager) — no real
MinIO or AWS S3 touches the test suite, and a real presigned-URL PUT/GET round
trip still gets exercised (`tests/test_storage.py`, `tests/test_script_runner.py`).
`moto` proves the code path, not the network — `docker/smoke_check_storage.py`
(run via `docker/smoke_test.sh`, Docker-only) round-trips a real object through
the actual `minio` service, including auto-creating the smoke-test bucket
(`ObjectStorage` itself has no bucket-admin methods on purpose; production
bucket provisioning is out-of-band, so that stays smoke-test-only code).

## Credential resolution (`agent_parity/config.py`)

`load_config()` parses `config.yaml` (topology) + `.env` (secrets) into `AppConfig`/
`ClientConfig`/`VendorConfig` dataclasses — every secret in `config.yaml` is a `${VAR}`
reference; an unset variable resolves to `None` rather than raising, which is exactly
what puts a connector into fixture mode. This is the *only* config entrypoint — there
is no database, so there's nothing else for a consuming project to call.
`sites_for(client_slug, vendor_name)` returns one merged config dict per site/tenant
(see "Multi-site/tenant clients" below) — it's the one place that knows `global` vs
`per_client` scope (`VENDOR_SCOPE`) — SentinelOne/BitDefender are global (same
credentials for every client), Carbon Black is per-client. When adding a vendor or a
client, this is the function whose behavior actually matters; don't special-case scope
logic in a connector or in `pipeline.py`.
`get_connectors(config, client_slug, vendor_name)` builds one connector per entry
`sites_for()` returns — almost always a one-element tuple.

`pick_ad_export_vendor(client_cfg)` picks which of a client's enabled vendors carries
the AD export — filtered to `supports_remote_execution = True` connectors, then broken
by `AD_EXPORT_VENDOR_PREFERENCE = ("sentinelone", "carbonblack")`, not alphabetically.
That preference order is a real business fact (S1 covered most of the client base, CB a
handful, BitDefender basically none) as much as a technical one — if you touch it, keep
both in mind. Raises `ConfigError` if a client has no capable vendor at all. Called from
`pipeline.collect_ad_csv`; don't reintroduce a `sorted(client_cfg.vendors)[0]`-style
pick elsewhere — that bug (silently routing AD export through whichever vendor happens
to sort first, capable or not) is exactly what this function replaced.

## Multi-domain clients (`ClientConfig.ad_target_devices`)

A client can span more than one AD domain/forest — no single domain controller can
enumerate computer objects outside its own domain, so `ad_target_devices` is a tuple,
not a single hostname, and the export script runs once per entry.
`agent_parity/pipeline.py`'s `collect_ad_frame` is the orchestrator: it loops the tuple,
calling `collect_ad_csv` + `parse_ad_export` per domain, and concatenates the results
with `agent_parity/ad_sync/parser.py`'s `concat_ad_frames` into the one master
DataFrame `correlate()` actually sees. A single-domain client (most of them) is just
the `len == 1` case of this same loop — there's no separate single-domain code path,
by design.

Tolerant of partial failure the same way per-vendor collection already is: one
domain's status is recorded independently (`f"ad:{target_device}"` in `vendor_status`,
e.g. `ad:GLOBEX-DC01`), and `collect_ad_frame` returns `None` for the frame only when
*every* domain failed — `run_correlation_for_client` is where that "nothing to
correlate against" case is handled (returns `None` up to its own caller rather than
attempting to correlate against nothing); don't duplicate that check elsewhere.

Fixture mode picks the CSV by target device — `sample_data/<client>/ad_export_<target_device>.csv`
(`connectors/base.py`'s `deploy_and_run`) — one file per domain, not one shared
`ad_export.csv`. The demo's `globex` client is intentionally multi-domain
(`GLOBEX-DC01` + a branch office `GLOBEX-BR-DC01`, both in `config.yaml` and
`sample_data/globex/`) so this path has real test/demo coverage; `acme` stays
single-domain.

## Multi-site/tenant clients (`ClientConfig.vendors`, `AppConfig.sites_for`)

The vendor-side counterpart to multi-domain AD: `ClientConfig.vendors[vendor_name]` is
`tuple[dict, ...]`, not a single credential dict — one "site" entry per tuple element.
What a site dict *is* depends on `VENDOR_SCOPE`, and that split isn't arbitrary — it
matches how each vendor's API is actually provisioned:

- **Per-client scope (Carbon Black)**: each entry is a complete, independent credential
  block (`api_url`/`api_id`/`api_key`/`org_key`) — a second entry is a second, fully
  separate tenant. No connector changes needed at all — `CarbonBlackConnector` doesn't
  know or care whether it's one of several tenants; multi-tenant support is purely
  "call the existing single-tenant mechanism N times and concatenate," the same
  pattern `collect_ad_frame` already established for AD domains.
- **Global scope (SentinelOne, BitDefender)**: each entry is small — just an optional
  site filter (e.g. `{"site_ids": "..."}`, or `{}` for "the whole account," today's
  default) — merged onto whichever named account it resolves to (see "Multiple named
  accounts" below) in `sites_for()`, never stored as if it were a secret. SentinelOne's
  `_in_scoped_sites`/`site_ids` mirrors a
  real, documented API filter (`GET /web/api/v2.1/agents?siteIds=...`); BitDefender's
  `_in_scoped_company`/`company_id` is modeled the same way but explicitly flagged in
  `connectors/bitdefender.py` as unverified against GravityZone's real multi-tenant
  shape — same caution as the `createCustomScriptTask` removal earlier in that file;
  don't remove the hedge without actually confirming it against docs or a tenant.

An optional `"label"` key in a site dict names it for display and for `vendor_status`
keys (`pipeline.site_status_key`) — never for anything security-relevant.

Fixture mode picks the inventory JSON by label the same way AD picks CSVs by target
device — `sample_data/<client>/<vendor>_inventory_<label>.json` when a site has one,
plain `<vendor>_inventory.json` when it doesn't (`connectors/base.py`'s
`_fixture_fetch_inventory`) — but unlike AD domains (physically separate resources),
SentinelOne/BitDefender sites are one account queried with a filter, so those two
vendors are *not* demoed with real multi-site fixture data, only unit-tested
(`tests/test_connectors.py`); Carbon Black tenants genuinely are separate
resources, so the demo's `acme` client has two real tenant fixtures
(`carbonblack_inventory.json` + `carbonblack_inventory_branch.json`).

## Multiple named accounts per global vendor (`VendorConfig.accounts`)

A global vendor doesn't mean *one* credential set, either: `VendorConfig.accounts`
(`agent_parity/config.py`) is `dict[str, dict]` — account name -> credentials —
always named, even a lone one (BitDefender's `"default"` today), same
"no special-cased single case" principle as `ad_target_devices`/per-client
`vendors`. This is real, not hypothetical: there were two genuinely separate
SentinelOne consoles in practice (`"mssp"` for ordinary managed-services clients,
`"dfir"` for clients under active incident response) — a distinct engagement,
a distinct console, not just a Site within one account (that's the previous
section — orthogonal, and composable: a site dict can carry both `"account"`
and a site filter like `site_ids` at once).

`AppConfig._resolve_account(client_slug, vendor, site)` is where a site's
`site.get("account")` gets resolved: explicit account name wins; omitted and the
vendor has exactly one account, use it (today's implicit default, unchanged for
every existing single-account setup); omitted and there's more than one,
`ConfigError` — ambiguous is a config error, not a silent pick; unknown account
name, `ConfigError` too. Don't special-case "just default to the first one
alphabetically" here — that's exactly the kind of silent-pick bug
`AD_EXPORT_VENDOR_PREFERENCE`/`pick_ad_export_vendor` already exists to avoid
elsewhere in this file.

## Testing conventions

- `tests/test_pipeline_sync.py` pins the specific gap scenarios authored into
  `sample_data/` by join key (e.g. `acme-sql02` is `missing_agent`, `acme-fs-old` is
  `orphaned_agent`). If you regenerate or edit the fixtures, these tests are the
  regression check — a scenario silently changing status is a fixture bug, not a test
  bug, unless the change was intentional.
- `sample_data/` fixtures were originally generated by a one-off script that was never
  committed (it lived in a scratch directory outside the repo). The committed CSV/JSON
  files are the source of truth now; there's no `generate_fixtures.py` in this repo to
  regenerate them from.
- Test coverage is intentionally close to 1:1 with source modules: `test_models.py` ↔
  `agent_parity/models.py`, `test_rest_adapter.py` ↔ `agent_parity/rest_adapter.py`,
  `test_pipeline.py` ↔ `agent_parity/pipeline.py` (the collection helpers not already
  exercised end-to-end by `test_pipeline_sync.py`), `test_config.py` ↔
  `agent_parity/config.py`. When adding a new module with real logic in it, add its
  test file alongside — don't rely on it being incidentally exercised by a
  higher-level pipeline test.
