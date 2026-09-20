# TODO

Open items for this repo.

## Docs

- [ ] Fill in the "Original deployment" result in `README.md` (the `TODO(craig)` comment). Say whether the figure is percentage points or relative, and the baseline.
- [ ] Render the demo GIF and embed it in the README's "See it" section. `docs/demo.tape` is untested because `vhs` wasn't available when it was written:
  ```bash
  brew install vhs && uv sync && vhs docs/demo.tape
  ```
  Then replace the hidden TODO comment in the README with `![agent-parity demo](docs/demo.gif)`.
- [ ] Update the project layout tree in `CONTRIBUTING.md`. It predates the `src/` move and the `scheduling/` and `shared/` subpackages.
- [ ] Update the `.gitignore` comment that points at `agent_parity/db.py`. The module is now `src/agent_parity/scheduling/db.py`.

## Design decisions (ADRs)

The ADRs in `docs/decisions/` leave a hidden `TODO(craig)` comment wherever the repo doesn't record why a choice was made or what else was considered. Replace each comment with the answer, or delete it if there was no alternative. Find them all with `grep -rn "TODO(craig)" docs/decisions`.

- [ ] [0001](docs/decisions/0001-collect-ad-data-via-vendor-remote-scripting.md): other AD collection methods you weighed besides a direct LDAP bind (WinRM, a scheduled task, a collector agent), and why they lost.
- [ ] [0002](docs/decisions/0002-return-ad-export-via-presigned-put-url.md): other ways to hand the export back besides the vendor's output channel, and why they lost.
- [ ] [0003](docs/decisions/0003-s3-api-with-minio-for-local-dev.md): why the S3 API was chosen at all. The repo records that it is S3, not why.
- [ ] [0003](docs/decisions/0003-s3-api-with-minio-for-local-dev.md): other storage backends you considered besides S3 and Azure Blob.
- [ ] [0005](docs/decisions/0005-correlate-on-normalized-hostname-only.md): why fuzzy hostname matching is left out "by design". The repo records that it is, not the reason.
- [ ] [0007](docs/decisions/0007-normalize-vendor-wording-to-sentinelone.md): other canonical vocabularies considered, such as a vendor-neutral one.
- [ ] [0010](docs/decisions/0010-multi-domain-ad-one-export-per-domain.md): alternatives weighed for handling several AD domains.

## Repo hygiene

- [ ] Add a general `*.csv` rule to `.gitignore` (with `!sample_data/**/*.csv`). Only `output/` is ignored today, so a stray CSV elsewhere would not be.
- [ ] Optionally commit a doc link checker under `scripts/` and run it in CI. `lychee` is the off-the-shelf option.

## Notes

- `docs/sample-report.md` is generated: `uv run python scripts/gen_sample_report.py`. Its OS end-of-life counts depend on the date it was generated, so regenerate it when the numbers matter.
