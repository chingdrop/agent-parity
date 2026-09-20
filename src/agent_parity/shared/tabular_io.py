"""Extension-dispatch tabular file I/O: read or write a DataFrame as CSV,
Excel, JSON, or HTML based on the file's extension, without the caller
picking the right pandas reader/writer method itself.

Extracted from ``vega-tools``' ``pandas_tools.read_structured_file``/
``write_structured_file`` — generic plumbing with nothing to do with that
project's own domain (DICOM/medical imaging), while ``agent-parity``'s
``cli.py`` (``_write_csv``, hardcoded to CSV) and ``medicare-rebuild``'s
``__main__.py`` (``snap_dataframe``, hardcoded to Excel) each hand-rolled a
narrower slice of the same "get a DataFrame onto/off of disk" job.

Two changes from the original: this raises :class:`TabularIOError` instead
of printing and returning ``None``/``False`` (matching every other typed
exception in this package — ``ConfigError``, ``StorageError``,
``ConnectorError``, ...), and text-based formats (csv/txt/json/html) are
written through :func:`agent_parity.shared.atomic_io.atomic_write` rather than
pandas' own ``to_csv``/etc. writing directly to the target path — the same
"a plain ``to_csv(path)`` isn't atomic" reasoning agent-parity's own
``_write_csv`` already used. Excel has no equivalent in-memory-string
round trip as cheap as the text formats', so it's written directly and is
not atomic.

Requires the ``tabular`` extra (``pandas``, ``openpyxl`` for ``.xls``/
``.xlsx``); everything else in this package works without it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

from agent_parity.shared.atomic_io import atomic_write


class TabularIOError(Exception):
    """Reading or writing a structured data file failed, or its extension
    isn't one of the supported types."""


Reader = Callable[..., pd.DataFrame]
READERS: dict[str, Reader] = {
    "csv": pd.read_csv,
    "txt": pd.read_csv,
    "xls": pd.read_excel,
    "xlsx": pd.read_excel,
    "json": pd.read_json,
    "html": lambda path, **kw: pd.read_html(path, **kw)[0],
    "htm": lambda path, **kw: pd.read_html(path, **kw)[0],
}

TextWriter = Callable[..., str]
TEXT_WRITERS: dict[str, TextWriter] = {
    "csv": lambda df, **kw: df.to_csv(**kw),
    "txt": lambda df, **kw: df.to_csv(**kw),
    "json": lambda df, **kw: df.to_json(**kw),
    "html": lambda df, **kw: df.to_html(**kw),
    "htm": lambda df, **kw: df.to_html(**kw),
}
_EXCEL_EXTENSIONS = {"xls", "xlsx"}
SUPPORTED_WRITE_EXTENSIONS = set(TEXT_WRITERS) | _EXCEL_EXTENSIONS


def read_structured_file(file_path: str | Path, file_type: str | None = None, **kwargs: Any) -> pd.DataFrame:
    """Read a CSV/Excel/JSON/HTML file into a DataFrame.

    ``file_type`` overrides extension-based dispatch (e.g. ``"csv"`` for a
    file that doesn't end in ``.csv``); ``**kwargs`` passes through verbatim
    to the underlying pandas reader. Raises :class:`TabularIOError` for an
    unsupported extension or if the underlying reader itself fails.
    """
    path = Path(file_path)
    ext = (file_type or path.suffix.lstrip(".")).lower()

    reader = READERS.get(ext)
    if reader is None:
        raise TabularIOError(f"Unsupported file type for reading: .{ext} ({path})")

    if ext in _EXCEL_EXTENSIONS:
        kwargs.setdefault("engine", "openpyxl")

    try:
        return reader(path, **kwargs)
    except Exception as exc:
        raise TabularIOError(f"Failed to read .{ext} file at {path}: {exc}") from exc


def write_structured_file(df: pd.DataFrame, file_path: str | Path, file_type: str | None = None, **kwargs: Any) -> None:
    """Write a DataFrame to a CSV/Excel/JSON/HTML file.

    ``file_type`` overrides extension-based dispatch; ``**kwargs`` passes
    through to the underlying pandas writer (e.g. ``index=False``,
    ``sheet_name="Data"``, ``orient="records"``). Text-based formats are
    written atomically (see module docstring); Excel is not. Raises
    :class:`TabularIOError` for an unsupported extension or a write failure.
    """
    path = Path(file_path)
    ext = (file_type or path.suffix.lstrip(".")).lower()

    if ext not in SUPPORTED_WRITE_EXTENSIONS:
        raise TabularIOError(f"Unsupported file type for writing: .{ext} ({path})")

    try:
        if ext in _EXCEL_EXTENSIONS:
            kwargs.setdefault("engine", "openpyxl")
            df.to_excel(path, **kwargs)
        else:
            atomic_write(path, TEXT_WRITERS[ext](df, **kwargs))
    except Exception as exc:
        raise TabularIOError(f"Failed to write .{ext} file to {path}: {exc}") from exc
