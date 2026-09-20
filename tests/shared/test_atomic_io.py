"""atomic_write/ensure_dir tests: crash-safety (no partial writes, no temp
files left behind), idempotency, and both text/binary content.
"""

import os

import pytest

from agent_parity.shared.atomic_io import atomic_write, ensure_dir


def test_ensure_dir_creates_missing_directory(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    result = ensure_dir(target)
    assert target.is_dir()
    assert result == target


def test_ensure_dir_idempotent_on_existing_directory(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    ensure_dir(target)  # must not raise
    assert target.is_dir()


def test_atomic_write_creates_new_text_file(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write(path, "hello")
    assert path.read_text() == "hello"


def test_atomic_write_creates_new_binary_file(tmp_path):
    path = tmp_path / "out.bin"
    atomic_write(path, b"\x00\x01\x02")
    assert path.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_replaces_existing_file_content(tmp_path):
    path = tmp_path / "out.txt"
    path.write_text("old content")
    atomic_write(path, "new content")
    assert path.read_text() == "new content"


def test_atomic_write_leaves_no_temp_file_behind_on_success(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write(path, "hello")
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_atomic_write_missing_parent_directory_raises(tmp_path):
    path = tmp_path / "missing_dir" / "out.txt"
    with pytest.raises(FileNotFoundError):
        atomic_write(path, "hello")


def test_atomic_write_cleans_up_temp_file_and_preserves_original_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "out.txt"
    path.write_text("original content")

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write(path, "new content")

    assert path.read_text() == "original content"
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []
