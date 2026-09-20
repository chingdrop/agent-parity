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

## Repo hygiene

- [ ] Add a general `*.csv` rule to `.gitignore` (with `!sample_data/**/*.csv`). Only `output/` is ignored today, so a stray CSV elsewhere would not be.
- [ ] Optionally commit a doc link checker under `scripts/` and run it in CI. `lychee` is the off-the-shelf option.

## Notes

- `docs/sample-report.md` is generated: `uv run python scripts/gen_sample_report.py`. Its OS end-of-life counts depend on the date it was generated, so regenerate it when the numbers matter.
