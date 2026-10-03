"""Regenerate ``docs/sample-quarterly-report.pdf`` (and its PNG) from the fixtures.

Seeds a throwaway demo history (``scripts/seed_history.py``: two quarters of
coverage climbing to today's fixture numbers) and renders Acme's report for the
last complete quarter from it. Nothing touches your real run history or OS EOL
cache.

The README shows the report's first page as ``docs/sample-quarterly-report.png``,
rasterized with pypdfium2 (a dev dependency bundling PDFium, so the standard PDF
fonts render the same on any OS, unlike poppler's fontconfig substitution).

    uv run python scripts/gen_sample_quarterly_report.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "sample-quarterly-report.pdf"
PNG_OUTPUT = OUTPUT.with_suffix(".png")
#: Resolution of the README image: legible table text at GitHub's content width.
PNG_DPI = 110


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "history.db"
        env = {
            **os.environ,
            "AGENT_PARITY_DB_URL": f"sqlite:///{db}",
            "AGENT_PARITY_EOL_CACHE": str(Path(tmp) / "os_eol_cache.json"),
        }
        steps = [
            [sys.executable, str(REPO_ROOT / "scripts" / "seed_history.py"), str(db)],
            [sys.executable, "-m", "agent_parity.cli", "report", "--client", "acme", "--out-dir", tmp],
        ]
        for step in steps:
            subprocess.run(step, env=env, cwd=REPO_ROOT, check=True)  # noqa: S603 - fixed argv, no shell
        (pdf,) = Path(tmp).glob("acme-*.pdf")
        shutil.copyfile(pdf, OUTPUT)
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")

    import pypdfium2

    document = pypdfium2.PdfDocument(OUTPUT)
    try:
        document[0].render(scale=PNG_DPI / 72).to_pil().save(PNG_OUTPUT, optimize=True)
    finally:
        document.close()
    print(f"Wrote {PNG_OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
