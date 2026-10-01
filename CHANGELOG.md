# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Notes on the record:

- The documented history begins at the from-scratch rebuild (first commit 2026-07-03). Nothing earlier is recorded here.
- The entries for 1.0.0 to 1.2.0 were reconstructed from the git history and the GitHub release notes, after the fact.
- The version in `pyproject.toml` did not track the tags until [Unreleased]: v1.0.0 and v1.1.0 shipped with 0.1.0 in the
  package metadata, and v1.2.0 with 1.1.0.

## [Unreleased]

### Added

- Ruff (lint and format) and mypy, with CI checks for each.
- `CONTRIBUTING.md`, and project URLs in the package metadata.
- `scripts/check_eol_drift.py`, a maintainer-run check of the committed OS end-of-life data against endoflife.date.
- `docs/sample-report.md`, generated from a real run against the fixtures by `scripts/gen_sample_report.py`.
- `docs/architecture.md`, eleven architecture decision records under `docs/decisions/`, and `docs/threat-model.md`.
- A README first screen with badges, a short "what it answers" list and a 60-second try-it, plus `docs/demo.tape`, a vhs
  script for a terminal recording, rendered as `docs/demo.gif`.
- `SECURITY.md`, Dependabot (uv and GitHub Actions) and a weekly CodeQL workflow.
- CI: a `security` job running pip-audit and gitleaks (also weekly), coverage reporting with an 88% gate, ruff's
  security (`S`) rules and stricter mypy flags.
- `TODO.md`, tracking open documentation and hygiene items.
- Runs still `pending` after `pending_run_timeout_hours` (a new `config.yaml` setting, default 24) are marked
  `failed`, checked on every beat tick and at the start of every `run`. A killed CLI or worker could otherwise leave a
  run `pending` forever.
- `.github/workflows/smoke.yml`, which runs `docker/smoke_test.sh` (a real S3 server and a real Celery chord) weekly
  and on changes to the Docker stack or scheduling code.
- Detection of a short hostname that exists in more than one AD domain. An agent that reported a full DNS name is
  matched only to the AD object with that exact `DNSHostName` (`match_method = fqdn_exact`); one that reported only the
  short name is flagged `ambiguous_join_key` on every row it matched, counted in the summary, logged and noted in
  `run`'s output. Previously one agent silently made every same-named machine look covered.
- `run --workers --timeout MINUTES`, to stop waiting at a deadline while unfinished runs keep going on the workers.
- An `inventory_rate_limit` attribute on each connector. The Celery inventory tasks are built from the connector
  registry with it, so adding a vendor needs no change to `scheduling/tasks.py`.

### Changed

- Source moved to a `src/agent_parity/` layout. Single-module folders were flattened, the scheduling modules were
  grouped into `scheduling/`, and the tests now mirror the source layout.
- The shared HTTP adapter, object storage and related helpers were inlined from `py-shared-tools` into
  `agent_parity.shared`, so the repository is self-contained and a fresh clone needs no git dependency.
- The README's architecture sections moved to `docs/architecture.md`.
- The pieces only agent-parity used were moved out of `agent_parity.shared` into the modules that own them: the vendor-connector base into `connectors/base.py`, the SentinelOne remote-script mixin into `connectors/sentinelone.py`, the storage-backed script export into `script_runner.py`, object storage into `storage.py` and the storage config into `config.py`. `agent_parity.shared` keeps the HTTP adapter, atomic writes, logging setup, tabular I/O and the `${VAR}` resolver.
- CI hardened: least-privilege permissions, actions pinned to full commit SHAs, and lint and type-check split into
  separate jobs.
- Package version set to 1.2.0, to match the latest tag.
- **Breaking:** `agent-parity run` now records every run in the SQLite history (what `sync` used to do). Pass `--csv`
  to also write `output/<client>.csv`, which `run` previously wrote by default.
- `agent-parity run` now executes the same Celery chord the beat schedule dispatches, as the original tool did:
  in-process by default, or on running workers with the new `--workers` flag. `run_and_persist_for_client` is removed;
  `finalize_run` returns the `CorrelationResult` instead of a snapshot count, and its persist-and-send half is now
  `persist_result`.
- The Celery tasks and the in-process pipeline share one per-domain and one per-site collection helper
  (`pipeline.collect_ad_domain`, `pipeline.collect_vendor_site`) instead of duplicating collection and error handling.
- **Breaking:** the Splunk export sends every run whole, one event per row (`agent_parity:coverage`) plus a summary
  event (`agent_parity:coverage_summary`), as the original tool did, instead of per-run deltas. The delta design lost
  gap closures (a fixed gap's new row never matched its old one) and re-sent everything after a failed run.
  `export_deltas_to_splunk`/`send_deltas` are replaced by `export_run_to_splunk`/`send_run`.
- The Docker Compose object store is now the Versity S3 Gateway (`s3` service, pinned image) instead of MinIO, whose
  images were withdrawn from Docker Hub. `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` are renamed `LOCAL_S3_ACCESS_KEY`/
  `LOCAL_S3_SECRET_KEY`. See ADR 0011.
- Relicensed from GPL-3.0 to MIT. `pyproject.toml` now declares `license = "MIT"`.

### Removed

- The `sync` subcommand, merged into `run` (see above).
- The `py-shared-tools` git dependency (its code is inlined, see above), and the Dockerfile's `git` install that existed
  only to fetch it.

### Fixed

- The tests' own HTTP calls now have timeouts.
- Stale module paths in docs and comments.
- The Docker image creates `/app/data`, so the compose stack's shared SQLite volume is writable. It was created
  root-owned, so the worker, beat and CLI containers could never save a run.
- On the Celery path, a malformed AD export now fails only its own domain instead of the whole run.
- Celery tasks close their database engines; each task leaked a SQLite connection.
- `docker/smoke_test.sh` runs in its own compose project, so its teardown no longer deletes a dev stack's run history.

## [1.2.0] - 2026-07-14

Restored the multi-client, scheduled shape of the original design on top of the standalone package.

### Added

- Multi-client topology in a single `config.yaml` (`clients:` and `vendors:`), with per-vendor `scope` (`global` or
  `per_client`).
- Multi-site and multi-tenant support per vendor: several Carbon Black tenants per client, and site or company scoping
  for SentinelOne and BitDefender.
- Multiple named accounts per global vendor (for example two separate SentinelOne consoles).
- Selection of the vendor that carries the AD export by capability and priority (SentinelOne before Carbon Black), with
  a clear error when a client has none.
- SQLAlchemy/SQLite run-history persistence and the `sync` CLI command.
- Celery scheduling: a per-client fan-out and fan-in with partial-failure tolerance, an hourly tick and a daily forced
  run.
- Opt-in Splunk coverage-delta export, which forwards only new or changed statuses and never fails a run.
- Atomic output writes and shared logging setup, adopted from `py-shared-tools`.

### Changed

- `py-shared-tools` is consumed as a pinned git dependency instead of a vendored submodule.
- `py-shared-tools` updated to v1.2.0.

### Fixed

- The Dockerfile build with the git dependency, and drift between the docs and the code.

## [1.1.0] - 2026-07-06

### Added

- GNU General Public License v3.
- GitHub Actions CI running the test suite and a package build on every push and pull request.
- `docker/Dockerfile` for running the CLI without a local uv install, and an `agent-parity` service in the compose file.

### Changed

- The generic HTTP adapter, object storage, vendor-connector base, SentinelOne remote-script mixin, storage
  configuration and the storage-backed AD-export handoff moved into the shared `py-shared-tools` library, consumed as a
  git submodule. agent-parity's own classes and functions kept their public interfaces as thin wrappers, with no
  intended behavior change.

### Fixed

- CI submodule resolution.

## [1.0.0] - 2026-07-05

First stable release: a standalone library and CLI, with one organization and one vendor per config.

### Added

- Connectors for SentinelOne, Carbon Black and BitDefender GravityZone, with fixture fallback to synthetic data in
  `sample_data/` when live credentials are unset.
- A pandas correlation engine that classifies each device as `covered`, `missing_agent`, `orphaned_agent` or
  `stale_coverage`, matching on a normalized hostname.
- AD collection by running `Export-ADDevices.ps1` through the vendor's own remote scripting, instead of binding to LDAP.
- An object-storage handoff for the AD export using presigned PUT URLs (S3 API, MinIO for local development), required
  for live exports.
- Multi-domain AD collection: one export per domain, concatenated, tolerant of partial failure.
- Normalization of platform and machine type to SentinelOne's wording across vendors.
- Server prioritization: `machine_type` backfilled from AD's OS text, and `server_coverage_pct` reported alongside
  overall coverage.
- OS end-of-life classification, first from a free-text table and then by exact Windows build number.
- `agent-parity run` (config plus connectors) and `agent-parity compare` (two CSVs, zero configuration), built on click.
- Pluggable connectors: adding a vendor is one decorated class.
- A `supports_remote_execution` capability flag on connectors, so a vendor that can't run scripts (BitDefender) refuses
  instead of pretending to.
- An offline test suite, a MinIO storage smoke test, and a Docker Compose file for a local MinIO.

### Changed

- Simplified to one organization and one vendor per config (restored in 1.2.0).

### Removed

- The modeled BitDefender custom-script method (`createCustomScriptTask`), removed because GravityZone's public API has
  no such call. BitDefender is now inventory-only.
- The Django dashboard, database-backed configuration and Celery scaling, taken out when the project became a standalone
  package.
- The Splunk integration (restored in 1.2.0).

[Unreleased]: https://github.com/chingdrop/agent-parity/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/chingdrop/agent-parity/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/chingdrop/agent-parity/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/chingdrop/agent-parity/releases/tag/v1.0.0
