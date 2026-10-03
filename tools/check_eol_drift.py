"""Compare the committed OS EOL snapshot with endoflife.date, optionally updating it.

``src/agent_parity/os_eol_data.json`` is the bundled fallback the package ships
with. Running deployments keep their own copy current through the daily refresh
(see ``agent_parity.os_eol_live``); this script is for the copy in the repo.
Both use the same derivation (``os_eol_live.derive_lifecycle_data``), so a
clean check here means the bundled data and a fresh fetch agree exactly.

Usage:
    uv run python tools/check_eol_drift.py           # report drift, exit 1 if any
    uv run python tools/check_eol_drift.py --write   # also rewrite the committed file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_parity.os_eol_live import EOLFetchError, diff_lifecycle_data, fetch_lifecycle_data

DATA_PATH = Path(__file__).resolve().parent.parent / "src" / "agent_parity" / "os_eol_data.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the committed snapshot when it has drifted")
    args = parser.parse_args()

    try:
        live = fetch_lifecycle_data()
    except EOLFetchError as exc:
        print(exc, file=sys.stderr)
        return 2

    changes = diff_lifecycle_data(json.loads(DATA_PATH.read_text()), live)
    if not changes:
        print("No drift: the committed EOL data matches endoflife.date.")
        return 0

    print("Drift against endoflife.date:")
    for change in changes:
        print(f"  - {change}")
    if args.write:
        DATA_PATH.write_text(json.dumps(live, indent=2) + "\n")
        print(f"Wrote {DATA_PATH.relative_to(DATA_PATH.parents[2])}.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
