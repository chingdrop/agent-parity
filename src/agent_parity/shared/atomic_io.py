"""Crash-safe, idempotent local file writes: write-to-temp-then-rename, and
directory creation that never fails just because the directory already
exists.

A plain ``path.write_text(...)``/``write_bytes(...)`` is not atomic: a
crash, kill, or concurrent reader mid-write can observe a truncated or
half-written file. ``atomic_write`` avoids that by writing to a private temp
file in ``path``'s own parent directory, then ``os.replace``-ing it into
place — a same-filesystem rename, which POSIX (and Windows, since Python
3.3) guarantees is atomic — so a reader only ever sees the old complete file
or the new complete file, never something in between. Re-running the same
write again produces the same end state, which is the "idempotent" half of
the name.

``ensure_dir`` is the directory-creation counterpart: ``exist_ok=True`` by
construction, so a caller never needs its own "does this already exist"
branch before creating a directory it's about to write into.

Stdlib-only, no third-party dependency.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def ensure_dir(path: Path | str) -> Path:
    """Create ``path`` (and any missing parents) if it doesn't already exist.

    Idempotent: calling this on a directory that already exists is a no-op,
    not an error.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(path: Path | str, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path`` such that a reader never observes a partial
    write, and a crash mid-write never leaves ``path`` truncated.

    ``path``'s parent directory must already exist (see ``ensure_dir``) —
    this never creates it implicitly, since silently creating directories on
    a path typo is a worse failure mode than a clear ``FileNotFoundError``.
    Text (``str``) or binary (``bytes``) content is both supported; the mode
    is inferred from ``data``'s type.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as bfh:
                bfh.write(data)
                bfh.flush()
                os.fsync(bfh.fileno())
        else:
            with os.fdopen(fd, "w", encoding=encoding) as tfh:
                tfh.write(data)
                tfh.flush()
                os.fsync(tfh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # os.replace either fully succeeds or doesn't move the temp file at
        # all, so on any failure above the temp file is always still here to
        # clean up — the target path, if it already existed, is untouched.
        os.unlink(tmp_name)
        raise
