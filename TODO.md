# TODO

Open items for this repo. Finished items are recorded in [CHANGELOG.md](CHANGELOG.md) and the git history rather than
kept here. There are no open `TODO(craig)` questions: every one in the ADRs and the threat model has been answered.

## Features

- [ ] **Quarterly report follow-ups.** `agent-parity report` is built (see the CHANGELOG); possible next steps:
  - Single out Domain Controllers. The history doesn't store the AD distinguished name, so they're reported as servers.
    Needs a column on `Device` and a migration path, which the schema doesn't have yet.
  - A combined roll-up across all clients for internal use, alongside the per-client PDFs.
  - Generate and email the reports on a schedule (a beat task after quarter end).
- [ ] **Splunk saved searches and a dashboard.** The export sends every run (one event per row plus a summary), but the
  repo has no Splunk-side content. Ship the latest-run search and the trend `timechart` from
  [docs/architecture.md](docs/architecture.md#splunk-export) as saved searches, plus a dashboard definition.
- [ ] **EOL drift check in CI.** Deployments refresh OS end-of-life data daily, but the committed snapshot
  (`src/agent_parity/os_eol_data.json`) only changes when someone runs `tools/check_eol_drift.py --write`. A weekly
  workflow could run it and open a PR when the snapshot drifts.
- [ ] **A demo fixture for a hostname shared by two AD domains.** `resolve_ambiguous_join_keys` is covered by unit tests,
  but `sample_data/` has no such case, so the demo never shows the `ambiguous_join_key` flag or an `fqdn_exact` match.
  Globex is already multi-domain. Adding one would change the fixture counts in the README, `docs/sample-report.md` and
  `tests/test_pipeline_sync.py`.

## Bugs and unverified behavior

No known bugs are open. These are things that are untested against the real thing:

- [ ] **`run --workers --timeout` against a real worker.** It is unit-tested with a faked Celery timeout; the live check
  was skipped because Docker wasn't running. Start `redis` and `worker`, then run
  `docker compose -f docker/docker-compose.yml run --rm --no-deps agent-parity run --client acme --workers --timeout 0.001`
  and confirm it reports the run as still going and the worker then finishes it.
- [ ] **SentinelOne's build-number field** (`osRevision`) is a reconstruction from memory, not checked against current API
  docs or a live tenant. See `connectors/sentinelone.py`.
- [ ] **BitDefender's `company_id` filter** is plausible from GravityZone's MSP company hierarchy but unverified against
  its API docs or a live tenant. See `connectors/bitdefender.py`.

## Release

- [ ] **Bump the version and tag a release.** `pyproject.toml` still says 1.2.0, and 71 commits have landed since the
  `v1.2.0` tag, including breaking changes (`sync` removed, `run` no longer writes a CSV by default, the Splunk sourcetype
  renamed, `MINIO_ROOT_*` renamed). Under semantic versioning that makes the next release 2.0.0. Move the CHANGELOG's
  Unreleased section under the new version when tagging.
- [ ] **Regenerate `docs/sample-report.md`** before the release (`uv run python tools/gen_sample_report.py`). Its OS
  end-of-life section is evaluated as of the day it's generated.

## Security and CI

- [ ] `main` has no branch protection or rulesets. If you add protection, require `lint`, `typecheck`, `test`, `build`,
  `security` and `Analyze (python)`. Don't require `smoke`: it only runs when the Docker stack or scheduling code
  changes, and a required check that never starts blocks the PR.
- [ ] Decide on a coverage badge. None was added on purpose; the gate is 92% (`fail_under` in pyproject.toml; measured 94.14%, line and branch).
- [ ] Optionally tighten mypy toward `strict`, one flag at a time. `--strict` now reports 90 errors in 19 files: 56 bare
  generics (`type-arg`), 16 missing annotations, 7 `no-any-return`, 6 untyped calls and 5 untyped Celery decorators.
- [ ] Pin the remaining floating images. `docker/Dockerfile` copies uv from `ghcr.io/astral-sh/uv:latest`, and compose
  uses `redis:7-alpine`. A floating tag is how the MinIO image broke the stack; the S3 server is already pinned.

## Notes

- `docs/sample-report.md` is generated: `uv run python tools/gen_sample_report.py`. Its OS end-of-life counts depend
  on the date it was generated, so regenerate it when the numbers matter.
- `src/agent_parity/os_eol_data.json` is generated too: `uv run python tools/check_eol_drift.py --write`. Never edit it
  by hand.
