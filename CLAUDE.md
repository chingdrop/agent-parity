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
uv run agent_parity_web/manage.py import_config      # one-time: config.yaml -> DB (also seed_demo does this itself)

uv run pytest                                        # full suite, offline, no broker
uv run pytest tests/test_correlation.py -k covered   # single test/file
uv run pytest tests/test_tasks.py                    # Celery chord tests (run eager, no broker needed)
```

Docker Compose (scaled mode) lives in `docker/`; commands for dev/prod are in the README.
There is no linter/formatter config in this repo (`pyproject.toml` has no `[tool.ruff]`
or `[tool.black]`) — formatting has so far been done via the IDE's reformatter, not a CLI tool.

## Architecture: two packages, one boundary

- **`agent_parity/`** — the pipeline. Connectors, AD export parsing, object storage,
  the pandas correlation engine. **Must stay free of Django and Celery imports** —
  it's called identically from the sync management command and from Celery tasks,
  and that boundary is load-bearing, not incidental.
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
(self-hosted, via the Docker Compose `minio` service) for local/dev, real AWS
S3 in production, same `ObjectStorage` class either way; only `endpoint_url`
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
`moto` proves the code path, not the network — `manage.py smoke_check_storage`
(`docker/smoke_test.sh`, Docker-only) round-trips a real object through the
actual `minio` service, including auto-creating the smoke-test bucket
(`ObjectStorage` itself has no bucket-admin methods on purpose; production
bucket provisioning is out-of-band, so that stays smoke-test-only code).

## Credential resolution (`agent_parity/config.py`)

`load_config()` parses `config.yaml` (topology) + `.env` (secrets) into `AppConfig`/
`ClientConfig`/`VendorConfig` dataclasses — every secret in `config.yaml` is a `${VAR}`
reference; an unset variable resolves to `None` rather than raising, which is exactly
what puts a connector into fixture mode. `sites_for(client_slug, vendor_name)` returns
one merged config dict per site/tenant (see "Multi-site/tenant clients" below) — it's
the one place that knows `global` vs `per_client` scope (`VENDOR_SCOPE`) —
SentinelOne/BitDefender are global (same credentials for every client), Carbon Black
is per-client. When adding a vendor or a client, this is the function whose behavior
actually matters; don't special-case scope logic in a connector or in `services.py`.
`get_connectors(config, client_slug, vendor_name)` builds one connector per entry
`sites_for()` returns — almost always a one-element tuple.

**`load_config()` is no longer what production entrypoints call.** Client topology and
vendor credentials are DB-backed now (`dashboard/config_db.py`'s `build_app_config_from_db()`,
called by every management command and Celery task instead) — see "DB-backed config"
below. `load_config()` still exists and is still fully correct; it's just been
demoted to the engine behind the one-time `config.yaml` import (`manage.py import_config`,
and the setup page's upload form), and it's still what `tests/test_config.py` tests
directly.

`pick_ad_export_vendor(client_cfg)` picks which of a client's enabled vendors carries
the AD export — filtered to `supports_remote_execution = True` connectors, then broken
by `AD_EXPORT_VENDOR_PREFERENCE = ("sentinelone", "carbonblack")`, not alphabetically.
That preference order is a real business fact (S1 covered most of the client base, CB a
handful, BitDefender basically none) as much as a technical one — if you touch it, keep
both in mind. Raises `ConfigError` if a client has no capable vendor at all. Called from
`services.collect_ad_csv`; don't reintroduce a `sorted(client_cfg.vendors)[0]`-style
pick elsewhere — that bug (silently routing AD export through whichever vendor happens
to sort first, capable or not) is exactly what this function replaced.

## Multi-domain clients (`ClientConfig.ad_target_devices`)

A client can span more than one AD domain/forest — no single domain controller can
enumerate computer objects outside its own domain, so `ad_target_devices` is a tuple,
not a single hostname, and the export script runs once per entry.
`dashboard/services.py`'s `collect_ad_frame` is the orchestrator: it loops the tuple,
calling `collect_ad_csv` + `parse_ad_export` per domain, and concatenates the results
with `agent_parity/ad_sync/parser.py`'s `concat_ad_frames` into the one master
DataFrame `correlate()` actually sees. A single-domain client (most of them) is just
the `len == 1` case of this same loop — there's no separate single-domain code path,
by design.

Tolerant of partial failure the same way per-vendor collection already is: one
domain's status is recorded independently (`f"ad:{target_device}"` in `vendor_status`,
e.g. `ad:GLOBEX-DC01`), and `collect_ad_frame` returns `None` for the frame only when
*every* domain failed. `services.finalize_run` is where that "nothing to correlate
against" case is actually handled (marks the run `FAILED`) — both the synchronous path
(`run_pipeline_for_client`) and the Celery chord callback (`tasks.correlate_client`)
delegate to it rather than each re-implementing the check; don't duplicate that logic
back into either caller. On the Celery side, `dispatch_client` fans out one
`collect_ad_export` task per `ad_target_devices` entry, mirroring the existing
one-task-per-vendor fan-out exactly.

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
  default) — merged onto the vendor-level shared credentials in `sites_for()`, never
  stored as if it were a secret. SentinelOne's `_in_scoped_sites`/`site_ids` mirrors a
  real, documented API filter (`GET /web/api/v2.1/agents?siteIds=...`); BitDefender's
  `_in_scoped_company`/`company_id` is modeled the same way but explicitly flagged in
  `connectors/bitdefender.py` as unverified against GravityZone's real multi-tenant
  shape — same caution as the `createCustomScriptTask` removal earlier in that file;
  don't remove the hedge without actually confirming it against docs or a tenant.

An optional `"label"` key in a site dict names it — used for the DB row
(`VendorCredential.site_label`) and for `vendor_status` keys
(`services.site_status_key`), never for anything security-relevant. Critically, a
site's `site_label` (DB storage identity, auto-index-assigned when there's more than
one row and no explicit label) is **not** the same thing as its `"label"` key
(config-authored, semantically meaningful) — `config_db.py` stores `credentials` (and
therefore any `"label"` key) verbatim and never reintroduces `site_label` as a
`"label"`. An earlier version of this code conflated the two and broke fixture-file
lookup for unlabeled sites (see `test_config_db.py`'s dedicated regression test) — if
you touch `import_app_config`/`build_app_config_from_db`, keep them separate.

Fixture mode picks the inventory JSON by label the same way AD picks CSVs by target
device — `sample_data/<client>/<vendor>_inventory_<label>.json` when a site has one,
plain `<vendor>_inventory.json` when it doesn't (`connectors/base.py`'s
`_fixture_fetch_inventory`) — but unlike AD domains (physically separate resources),
SentinelOne/BitDefender sites are one account queried with a filter, so those two
vendors are *not* demoed with real multi-site fixture data, only unit-tested
(`tests/test_connectors.py`); Carbon Black tenants genuinely are separate
resources, so the demo's `acme` client has two real tenant fixtures
(`carbonblack_inventory.json` + `carbonblack_inventory_branch.json`).

The Celery fan-out gains a third dimension: one task per (client, vendor, site index),
with the `vendor_status` key precomputed at dispatch time
(`tasks.dispatch_client`/`services.site_status_key`) and threaded through the task's
JSON-safe payload rather than recomputed inside it — the task doesn't have (and
shouldn't need) `len(sites)` to decide whether an index suffix is warranted.

The setup page's manual form deliberately only edits the *first* site/tenant per
vendor per client (`views_setup.client_form`'s `existing_rows` picks one row by
`site_label`/`pk` order, and never uses `update_or_create` for the save — with more
than one matching row that would raise `MultipleObjectsReturned`). Additional
sites/tenants are added via config.yaml (re-)import or directly through admin;
a full add/remove-site form is a real gap, not an oversight — flagged in code, not
silently unsupported.

## DB-backed config (`agent_parity_web/dashboard/config_db.py`, `views_setup.py`)

Client topology (`Client.ad_target_devices`/`sync_interval_hours`/`enabled_vendors`) and
vendor credentials (`VendorCredential.credentials`, encrypted — see below) live in the
DB, not `config.yaml`, so they can be managed through the setup page (`/setup/`)
instead of hand-editing a committed file and restarting every process. Two symmetric
functions are the entire boundary:

- **`import_app_config(config: AppConfig)`** — upserts `Client`/`VendorCredential` rows
  from an already-loaded `AppConfig`. Idempotent (`update_or_create`). Called by
  `manage.py import_config` and the setup page's YAML upload view
  (`views_setup.import_config_yaml`) — both just wrap `load_config()` + this.
- **`build_app_config_from_db()`** — the inverse; what every production entrypoint
  calls now. Returns the exact same `AppConfig`/`ClientConfig`/`VendorConfig` shape
  `load_config()` does, so `sites_for()`/`get_connectors()`/`pick_ad_export_vendor()`
  are reused completely unchanged and `agent_parity/` never learns the DB exists — the
  Django/pipeline boundary at the top of this file stays intact. `stale_days` comes from
  the `STALE_DAYS` Django setting and `storage` is built straight from `STORAGE_*`
  env vars (`_storage_config_from_env()`) — **both are deliberately out of scope** for
  this feature (global, not per-client) and were never moved to the DB; don't add DB
  fields for them without a real reason to.
- **`VENDOR_SCOPE`** (`agent_parity/config.py`, next to `AD_EXPORT_VENDOR_PREFERENCE`)
  is the fixed global-vs-per-client fact both directions agree on — it's a real business
  fact about how each vendor's API is provisioned, not something a setup-page form should
  ever make user-editable per client.
- **`VendorCredential.credentials`** is `dashboard/fields.py`'s `EncryptedJSONField`
  (Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`) — ORM-layer, not `pgcrypto`, because this
  app runs on SQLite in demo mode and Postgres in scaled mode and the encryption has to
  work on both. Admin's `VendorCredentialAdmin` makes the field read-only for exactly one
  reason: the stock admin `Textarea` would round-trip the field's `str()` through
  `get_prep_value` on save and silently corrupt it into non-JSON text — real editing is
  `views_setup.py`'s per-vendor form (`forms.VendorCredentialForm`, one `CharField` per
  `CONNECTOR_CLASSES[vendor].required_credentials`, always rendered blank so a stored
  secret is never echoed back — a blank submitted field means "keep the current value,"
  merged in the view, not the form).
- `/setup/*` views are gated behind `staff_member_required` — the only dashboard views
  that are (everything else is intentionally open, matching this portfolio project's
  lack of a broader auth layer), because this is the one surface that writes credentials.

## Celery chord (`agent_parity_web/dashboard/tasks.py`)

One fan-out task per `(client, vendor, site/tenant)` inventory pull (see "Multi-site/tenant
clients" above — almost always just `(client, vendor)`) plus one AD-export task per
domain controller, feeding a chord callback (`correlate_client`) that runs the correlation
once per client against the complete result set. Three things that are easy to break if
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

**Beat schedule (`config/settings/base.py`'s `CELERY_BEAT_SCHEDULE`)** has two entries,
not one, and they're not redundant: `sync-all-clients` ticks `dispatch_all_clients`
hourly, respecting each client's own `sync_interval_hours` (`_client_is_due`) —
this is the steady-state cadence. `sync-all-clients-7am` is a second, daily
`crontab(hour=7, minute=0)` tick calling the *same* task with `force=True`, which
bypasses `_client_is_due` entirely — a guarantee, not a cadence, that every active
client has a fresh correlation by the start of business, independent of whatever
`sync_interval_hours` each one happens to be configured with. `hour=7` is in
`CELERY_TIMEZONE` (`TIME_ZONE`, currently `"UTC"`) — if that setting ever becomes
configurable per-deployment, this crontab needs to move with it or "7am" silently
stops meaning 7am local time.

`tests/test_tasks.py` runs all of this with `task_always_eager` — real logic, fake
transport. Nothing in the pytest suite proves a real Celery worker actually picks up
work through a real Redis broker; that's `docker/smoke_test.sh` +
`manage.py smoke_check_celery` (Docker-only, not part of `pytest`). If you change the
fan-out/fan-in wiring, the eager-mode tests can tell you the *logic* still works, but
only the smoke test can tell you the *transport* still works.

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
- Test coverage is intentionally close to 1:1 with source modules: `test_models.py` ↔
  `agent_parity/models.py`, `test_rest_adapter.py` ↔ `agent_parity/rest_adapter.py`,
  `test_dashboard_models.py` ↔ `dashboard/models.py`, `test_services.py` ↔ the parts of
  `dashboard/services.py` not already exercised by `test_pipeline_sync.py`/`test_tasks.py`,
  `test_views.py` ↔ `dashboard/views.py`, `test_fields.py` ↔ `dashboard/fields.py`,
  `test_config_db.py` ↔ `dashboard/config_db.py`, `test_setup_views.py` ↔
  `dashboard/views_setup.py`. When adding a new module with real logic in it,
  add its test file alongside — don't rely on it being incidentally exercised by a
  higher-level pipeline test, which is exactly the gap `test_views.py` filled (the views
  had zero direct coverage before, only manual browser checks).
- `tests/conftest.py`'s `db_config` fixture seeds the DB from the real config.yaml
  (`import_app_config(load_config())`) before returning `build_app_config_from_db()` —
  needed by any test that exercises a Celery task or management command directly (they
  call `build_app_config_from_db()` internally now), not by tests that build a
  `ClientConfig`/`AppConfig` themselves and pass it straight to `services.*` functions
  (those still work with a plain `load_config()`, unchanged).
- Deliberately not unit-tested: Django settings modules, `config/celery.py`/`wsgi.py`/
  `urls.py`, `dashboard/apps.py` — declarative framework wiring, not application logic.
  A failure there breaks every other test in the suite (which loads them just to run),
  so that failure mode is already covered by the suite's own existence; don't add
  tests-that-just-assert-a-constant-equals-itself for these.
