# TODO

Open items for this repo.

## Docs

- [ ] Fill in the "Original deployment" result in `README.md` (the `TODO(craig)` comment). Say whether the figure is
  percentage points or relative, and the baseline.
- [ ] Render the demo GIF and embed it in the README's "See it" section. `docs/demo.tape` is untested because `vhs`
  wasn't available when it was written:
  ```bash
  brew install vhs && uv sync && vhs docs/demo.tape
  ```
  Then replace the hidden TODO comment in the README with `![agent-parity demo](docs/demo.gif)`.

## Design decisions (ADRs)

The ADRs in `docs/decisions/` leave a hidden `TODO(craig)` comment wherever the repo doesn't record why a choice was
made or what else was considered. Replace each comment with the answer, or delete it if there was no alternative. Find
them all with `grep -rn "TODO(craig)" docs/decisions`.

- [ ] [0001](docs/decisions/0001-collect-ad-data-via-vendor-remote-scripting.md): other AD collection methods you
  weighed besides a direct LDAP bind (WinRM, a scheduled task, a collector agent), and why they lost.
- [ ] [0002](docs/decisions/0002-return-ad-export-via-presigned-put-url.md): other ways to hand the export back besides
  the vendor's output channel, and why they lost.
- [ ] [0003](docs/decisions/0003-s3-api-with-minio-for-local-dev.md): why the S3 API was chosen at all. The repo records
  that it is S3, not why.
- [ ] [0003](docs/decisions/0003-s3-api-with-minio-for-local-dev.md): other storage backends you considered besides S3
  and Azure Blob.
- [ ] [0005](docs/decisions/0005-correlate-on-normalized-hostname-only.md): why fuzzy hostname matching is left out "by
  design". The repo records that it is, not the reason.
- [ ] [0007](docs/decisions/0007-normalize-vendor-wording-to-sentinelone.md): other canonical vocabularies considered,
  such as a vendor-neutral one.
- [ ] [0010](docs/decisions/0010-multi-domain-ad-one-export-per-domain.md): alternatives weighed for handling several AD
  domains.

## Security and CI

Set up by the security-hygiene branch; none of the new workflows has run on GitHub yet.

- [ ] Enable private vulnerability reporting (Settings → Code security). `SECURITY.md` links to it, and the link only
  works once it's on.
- [ ] Confirm the response windows in `SECURITY.md` (7 days to acknowledge, 30 to assess; these were proposed, not
  decided) and fill in the backup contact (the `TODO(craig)` comment there).
- [ ] Answer the four `TODO(craig)` questions in `docs/threat-model.md`: how you load `.env` locally, what SentinelOne
  and Carbon Black retain of script arguments, whether vendor or HTTP error text can ever include credentials, and which
  account and privileges the export script runs as. Find them with `grep -rn "TODO(craig)" docs/threat-model.md`.
- [ ] After the first CI run, check the `security` job. gitleaks couldn't be run locally, so the first run is its first
  real scan. The action is free for a personal account; if the repo moves to an organization it needs a
  `GITLEAKS_LICENSE` secret.
- [ ] Check that CodeQL "default setup" is not enabled (Settings → Code security). It conflicts with
  `.github/workflows/codeql.yml`.
- [ ] If branch protection requires status checks, update the required names: `lint`, `typecheck`, `test`, `build`,
  `security` and `Analyze (python)`. `lint` no longer runs mypy; that is `typecheck`.
- [ ] Decide on a coverage badge. None was added on purpose; the CI gate is 88% (measured 90.83%, line and branch).
- [ ] Optionally tighten mypy toward `strict`, one flag at a time. `--strict` currently reports 80 errors in 19 files:
  41 bare generics, 15 missing annotations, 10 untyped calls, 7 untyped Celery decorators, 6 `no-any-return`, 1
  `attr-defined`.

## Repo hygiene

- [ ] Add a general `*.csv` rule to `.gitignore` (with `!sample_data/**/*.csv`). Only `output/` is ignored today, so a
  stray CSV elsewhere would not be.
- [ ] Optionally commit a doc link checker under `scripts/` and run it in CI. `lychee` is the off-the-shelf option.
- [ ] `charset-normalizer` 3.4.8 is a yanked release (`uv lock` warns), pulled in at runtime through `requests`. A
  non-yanked 3.5.1 resolves: `uv lock --upgrade-package charset-normalizer`. It changes a runtime dependency, so it was
  left for you.
- [ ] Upgrade the dev-only `cryptography` 49.0.0 (PYSEC-2026-3552, fixed in 50.0.0). It arrives through `moto`, so it is
  not in the runtime audit that CI gates on: `uv lock --upgrade-package cryptography`.
- [ ] Newer majors exist for the pinned actions (`checkout` v7, `setup-uv` v10, `upload-artifact` v7, `gitleaks-action`
  v3). Dependabot will propose them; review the major bumps rather than auto-merging.
- [ ] `pyproject.toml` now says 1.2.0 to match the `v1.2.0` tag, but 21 commits have landed since that tag. Bump the
  version and tag together at the next release. The old tags never tracked the package version (`v1.0.0` and `v1.1.0`
  sit on commits that said 0.1.0).
- [ ] Optionally extend ruff to `scripts/` and `docker/`; CI lints only `src` and `tests`.

## Notes

- `docs/sample-report.md` is generated: `uv run python scripts/gen_sample_report.py`. Its OS end-of-life counts depend
  on the date it was generated, so regenerate it when the numbers matter.
