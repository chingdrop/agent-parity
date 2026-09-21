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
- `docs/architecture.md`, ten architecture decision records under `docs/decisions/`, and `docs/threat-model.md`.
- A README first screen with badges, a short "what it answers" list and a 60-second try-it, plus `docs/demo.tape`, a vhs
  script for a terminal recording (the GIF is not rendered yet).
- `SECURITY.md`, Dependabot (uv and GitHub Actions) and a weekly CodeQL workflow.
- CI: a `security` job running pip-audit and gitleaks (also weekly), coverage reporting with an 88% gate, ruff's
  security (`S`) rules and stricter mypy flags.
- `TODO.md`, tracking open documentation and hygiene items.

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

### Removed

- The `py-shared-tools` git dependency (its code is inlined, see above), and the Dockerfile's `git` install that existed
  only to fetch it.

### Fixed

- The tests' own HTTP calls now have timeouts.
- Stale module paths in docs and comments.

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
