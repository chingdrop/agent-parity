# Limitations and roadmap

Two separate parts. **Known limitations** lists only limits the repository already documents, with what each means in practice. **Possible next steps** is a set of directions, not commitments. For what has shipped, see the [changelog](../CHANGELOG.md).

## Known limitations

**BitDefender is inventory-only.** It supplies agent inventory but cannot run the AD export script, so a client whose only vendor is BitDefender can't have its AD data collected; the pipeline raises a clear error instead of skipping it. See [ADR 0004](decisions/0004-bitdefender-connector-is-inventory-only.md) and [`bitdefender.py`](../src/agent_parity/connectors/bitdefender.py).

**The AD export uses one vendor's channel per client.** `pick_ad_export_vendor` chooses a single capable vendor (SentinelOne before Carbon Black), and each AD domain needs a domain-joined endpoint that vendor manages. See [ADR 0001](decisions/0001-collect-ad-data-via-vendor-remote-scripting.md), [ADR 0010](decisions/0010-multi-domain-ad-one-export-per-domain.md) and [`config.py`](../src/agent_parity/config.py).

**Two vendor details are unverified against real APIs.** SentinelOne's build-number field (`osRevision`) is documented in code as a best-effort reconstruction, not checked against a live tenant. BitDefender's `company_id` scoping filter is likewise unconfirmed against current docs or a live tenant; fixture-mode filtering works regardless. See [`sentinelone.py`](../src/agent_parity/connectors/sentinelone.py) and [`bitdefender.py`](../src/agent_parity/connectors/bitdefender.py).

**Live vendor calls are never exercised by tests.** Connectors are shaped from public API documentation, and the tests monkeypatch the HTTP layer instead of calling a real API. See the `requests.Session.request` patching in [`test_connectors.py`](../tests/connectors/test_connectors.py).

**Carbon Black and BitDefender report no build number.** A device seen only through them uses AD's build if one was captured, otherwise the free-text table, where a bare "Windows 11" name stays `unknown`. See [ADR 0008](decisions/0008-classify-os-eol-by-build-number-with-free-text-fallback.md) and `test_carbonblack_and_bitdefender_never_set_os_build` in [`test_connectors.py`](../tests/connectors/test_connectors.py).

**The OS end-of-life data is small, Windows-only and hand-maintained.** It has five free-text entries and six build entries, covering Windows and Windows Server; other operating systems classify as `unknown`. It is refreshed by hand, and [`scripts/check_eol_drift.py`](../scripts/check_eol_drift.py) is a manual check that is not part of CI. See [`os_eol.py`](../src/agent_parity/os_eol.py) and `test_eol_date_for_unknown_os_returns_none` in [`test_os_eol.py`](../tests/test_os_eol.py).

**Matching is hostname-only.** The join key is the hostname with the DNS suffix stripped and lowercased. A renamed machine can't be resolved (the fixtures include one such orphan per client), and a duplicate join key, including across domains, is not detected. See [ADR 0005](decisions/0005-correlate-on-normalized-hostname-only.md) and [`correlation.py`](../src/agent_parity/correlation.py).

**`machine_type` is a text heuristic.** OS text containing "server" means server, anything else (including blank text) means desktop, and hostnames are never consulted. See [ADR 0006](decisions/0006-derive-machine-type-from-os-text-and-backfill.md) and [`models.py`](../src/agent_parity/models.py).

**`agent_version` is not comparable across vendors.** Each vendor's own version string is kept as reported. See [ADR 0007](decisions/0007-normalize-vendor-wording-to-sentinelone.md).

**Storage is S3-API only.** There is no Azure Blob support, and a live export with no storage configured fails rather than falling back to the vendor's output channel. See [ADR 0003](decisions/0003-s3-api-with-minio-for-local-dev.md) and [ADR 0002](decisions/0002-return-ad-export-via-presigned-put-url.md).

**Runs are batch and on demand.** Collection runs when invoked (`run`, `sync`) or on a Celery schedule; there is no real-time or streaming ingestion. See [ADR 0009](decisions/0009-standalone-package-owning-scheduling-and-persistence.md) and [`celery_app.py`](../src/agent_parity/scheduling/celery_app.py).

**Persistence is sized for a single node.** Run history is SQLite with no row-level locking (idempotency relies on SQLite's writer serialization) and a plain `create_all` with no migrations. See [ADR 0009](decisions/0009-standalone-package-owning-scheduling-and-persistence.md) and [`db.py`](../src/agent_parity/scheduling/db.py).

**The MinIO and Celery smoke tests are manual.** [`docker/smoke_test.sh`](../docker/smoke_test.sh) needs Docker and runs neither in `uv run pytest` nor in CI, so real MinIO and a real Celery chord are only checked when someone runs it. See the README's "Optional: Docker" section.

## Possible next steps

These are directions, not commitments. The code contains no TODO or FIXME markers, so this list comes from the README's former "Out of scope for v1" list and from follow-ups that code comments explicitly flag.
<!-- TODO(craig): add or prune items here; this list only includes what the repo already records. -->

- **Fuzzy hostname matching** for renamed machines, beyond normalization. The README called it "a natural next step for the renamed-machine orphans". See [ADR 0005](decisions/0005-correlate-on-normalized-hostname-only.md).
- **A live endoflife.date source** with a fixture fallback, the same shape as the vendor connectors. [`os_eol.py`](../src/agent_parity/os_eol.py) calls it "a natural, self-contained extension if ever needed" and does not build it.
- **Confirming the two unverified vendor details** (SentinelOne's build-number field and BitDefender's company filter) against current API docs or a live tenant, as their docstrings recommend before relying on them live.

### Explicitly not planned

- **A web dashboard.** There is no plan to build one; reporting is `CorrelationResult` and `CoverageSnapshot` history plus the opt-in Splunk delta export.
- **Real-time ingestion.** This is a batch tool on a schedule, not a streaming one.
