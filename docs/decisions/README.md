# Design decisions

Architecture Decision Records (ADRs) for agent-parity: the choices that shape the design, why they were made, and what they rule out.

| # | Decision | Summary |
|---|---|---|
| [0001](0001-collect-ad-data-via-vendor-remote-scripting.md) | Collect AD data via vendor remote scripting | Run `Export-ADDevices.ps1` through the EDR vendor's own scripting instead of binding to LDAP; no domain credentials held. |
| [0002](0002-return-ad-export-via-presigned-put-url.md) | Return the AD export via presigned PUT URL | The script uploads to object storage through a 15-minute, single-object URL instead of returning the CSV over the vendor's output channel. |
| [0003](0003-s3-api-with-minio-for-local-dev.md) | S3 API with MinIO for local development | boto3 against the S3 API, MinIO locally and AWS S3 in production; Azure Blob is explicitly not supported. |
| [0004](0004-bitdefender-connector-is-inventory-only.md) | BitDefender connector is inventory-only | GravityZone has no arbitrary script execution, so `supports_remote_execution = False`; an invented custom-script method was removed. |
| [0005](0005-correlate-on-normalized-hostname-only.md) | Correlate on normalized hostname only | DNS suffix stripped and lowercased, no fuzzy matching; the outer-merge indicator is the classification. |
| [0006](0006-derive-machine-type-from-os-text-and-backfill.md) | Derive `machine_type` from OS text and backfill | AD's OS text fills `machine_type` for agentless devices, so a missing Domain Controller is still flagged as a server. |
| [0007](0007-normalize-vendor-wording-to-sentinelone.md) | Normalize vendor wording to SentinelOne's | `platform` and `machine_type` share one vocabulary across vendors; `agent_version` is left untouched. |
| [0008](0008-classify-os-eol-by-build-number-with-free-text-fallback.md) | Classify OS end-of-life by build number | Exact build first, free-text table as fallback, and deliberately no bare "Windows 11" entry. |
| [0009](0009-standalone-package-owning-scheduling-and-persistence.md) | Standalone package owning scheduling and persistence | No web dashboard; Celery and SQLite are owned here. Replaces the 2026-07-05 single-organization scope. |
| [0010](0010-multi-domain-ad-one-export-per-domain.md) | Multi-domain AD: one export per domain | Exports are concatenated and tolerate partial failure; `None` only when every domain fails. |

## How decisions are recorded

Each decision is one short file, `NNNN-short-title.md`, in a lightweight [MADR](https://adr.github.io/madr/) style: Status, Context, Decision, Alternatives considered, Consequences, and Evidence (links to the code and tests that pin the behavior). The records are drafted from what the repo already documents (this documentation, docstrings, tests and commit history), so where the repo doesn't say why a choice was made or what else was considered, the record leaves an HTML-comment TODO instead of guessing.

Numbers are stable and never reused. A new decision takes the next number. A reversed decision keeps its number and its Status line points to what replaced it; [0009](0009-standalone-package-owning-scheduling-and-persistence.md) is an example.
