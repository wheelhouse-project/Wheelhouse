"""Tests for utils.soft_allow_writer (wh-9weum Phase 3 / wh-z0usg).

Covers the atomic write idiom (temp file + fsync + os.replace), the
read-modify-write append helper, deduplication on the identity tuple,
and graceful failure (returns False, logs WARNING, never raises).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from utils.soft_allow_writer import (
    append_soft_allow_tuple,
    write_soft_allow_tuples,
)


# --- write_soft_allow_tuples ----------------------------------------------


class TestWriteSoftAllowTuples:
    def test_writes_entries_in_documented_schema(self, tmp_path):
        target = tmp_path / "soft_allow_tuples.toml"
        ok = write_soft_allow_tuples(
            [
                ("zed.exe", "Zed::Window", "WindowControl",
                 "2026-04-30T15:00:00Z"),
                ("sublime_text.exe", "PX_WINDOW_CLASS", "WindowControl",
                 "2026-05-01T10:00:00Z"),
            ],
            target,
        )
        assert ok is True
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        entries = data["entries"]
        assert len(entries) == 2
        assert entries[0]["process_name"] == "zed.exe"
        assert entries[0]["class_name"] == "Zed::Window"
        assert entries[0]["control_type"] == "WindowControl"
        assert entries[0]["added_at"] == "2026-04-30T15:00:00Z"

    def test_creates_parent_directory_if_missing(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "soft_allow_tuples.toml"
        ok = write_soft_allow_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-04-30T15:00:00Z")],
            target,
        )
        assert ok is True
        assert target.exists()

    def test_empty_list_writes_empty_file(self, tmp_path):
        target = tmp_path / "soft_allow_tuples.toml"
        ok = write_soft_allow_tuples([], target)
        assert ok is True
        assert target.exists()
        # Empty file (no [[entries]]) is the documented initial state and
        # tomllib parses it to an empty dict.
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert data == {}

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "soft_allow_tuples.toml"
        target.write_text(
            "[[entries]]\nprocess_name = 'old.exe'\n"
            "class_name = 'OldClass'\ncontrol_type = 'OldControl'\n"
            "added_at = '2025-01-01T00:00:00Z'\n",
            encoding="utf-8",
        )
        ok = write_soft_allow_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-04-30T15:00:00Z")],
            target,
        )
        assert ok is True
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        assert data["entries"][0]["process_name"] == "zed.exe"

    def test_fsync_failure_leaves_target_untouched(
        self, tmp_path, monkeypatch, caplog,
    ):
        # The atomic-write idiom must not commit a partial file. If
        # os.fsync raises, the temp file is discarded and the target is
        # left as it was. The function returns False and logs WARNING.
        target = tmp_path / "soft_allow_tuples.toml"
        target.write_text(
            "[[entries]]\nprocess_name = 'survivor.exe'\n"
            "class_name = 'SurvivorClass'\ncontrol_type = 'PaneControl'\n"
            "added_at = '2025-01-01T00:00:00Z'\n",
            encoding="utf-8",
        )
        original_contents = target.read_text(encoding="utf-8")

        def fail_fsync(_fd):
            raise OSError("fsync failed")

        monkeypatch.setattr(os, "fsync", fail_fsync)
        with caplog.at_level("WARNING"):
            ok = write_soft_allow_tuples(
                [("zed.exe", "Zed::Window", "WindowControl",
                  "2026-04-30T15:00:00Z")],
                target,
            )
        assert ok is False
        assert target.read_text(encoding="utf-8") == original_contents
        # No leftover temp files in the target directory.
        siblings = [
            p for p in tmp_path.iterdir()
            if p.name != target.name
        ]
        assert siblings == [], (
            f"temp file leak: {[p.name for p in siblings]}"
        )
        assert any(
            "soft_allow" in record.message.lower()
            for record in caplog.records
        )

    def test_returns_false_on_oserror_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        target = tmp_path / "soft_allow_tuples.toml"

        def fail_replace(_src, _dst):
            raise OSError("rename failed")

        monkeypatch.setattr(os, "replace", fail_replace)
        ok = write_soft_allow_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-04-30T15:00:00Z")],
            target,
        )
        assert ok is False


# --- append_soft_allow_tuple ----------------------------------------------


class TestAppendSoftAllowTuple:
    def test_appends_to_empty_file(self, tmp_path):
        target = tmp_path / "soft_allow_tuples.toml"
        ok = append_soft_allow_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-04-30T15:00:00Z"),
            target,
        )
        assert ok is True
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        assert data["entries"][0]["process_name"] == "zed.exe"

    def test_appends_to_missing_file(self, tmp_path):
        # The file does not exist -- append must still produce a file
        # with one entry.
        target = tmp_path / "soft_allow_tuples.toml"
        ok = append_soft_allow_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-04-30T15:00:00Z"),
            target,
        )
        assert ok is True
        assert target.exists()

    def test_dedupes_on_identity_tuple(self, tmp_path):
        # The identity is (process, class, control_type). Two appends of
        # the same identity (with different added_at) must produce one
        # entry; the existing entry's added_at is kept (the user's
        # original approval timestamp is the canonical record).
        target = tmp_path / "soft_allow_tuples.toml"
        append_soft_allow_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-04-30T15:00:00Z"),
            target,
        )
        append_soft_allow_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-08T12:00:00Z"),
            target,
        )
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        assert data["entries"][0]["added_at"] == "2026-04-30T15:00:00Z"

    def test_appends_distinct_tuples(self, tmp_path):
        target = tmp_path / "soft_allow_tuples.toml"
        append_soft_allow_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-04-30T15:00:00Z"),
            target,
        )
        append_soft_allow_tuple(
            ("sublime_text.exe", "PX_WINDOW_CLASS", "WindowControl",
             "2026-05-01T10:00:00Z"),
            target,
        )
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 2


class TestConcurrentAppend:
    """Regression test for wh-9weum.4.5 read-modify-write race.

    Two threads calling append_soft_allow_tuple on the same path with
    different tuples must not lose either tuple. Without the
    _APPEND_LOCK, both threads can read the same baseline, each
    serialise their own one-entry payload, and the second to call
    os.replace overwrites the first.
    """

    def test_two_concurrent_appends_both_persist(self, tmp_path):
        import threading
        from utils.soft_allow_writer import append_soft_allow_tuple, _read_existing

        path = tmp_path / "soft_allow_tuples.toml"
        path.write_bytes(b"")

        results: list[bool] = []

        def worker(t):
            results.append(append_soft_allow_tuple(t, path))

        a = ("zed.exe", "Zed::Window", "WindowControl", "2026-01-01T00:00:00Z")
        b = ("brave.exe", "BraveOmnibox", "EditControl", "2026-01-02T00:00:00Z")

        ta = threading.Thread(target=worker, args=(a,))
        tb = threading.Thread(target=worker, args=(b,))
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        assert all(results)
        existing = _read_existing(path)
        identities = {(e[0], e[1], e[2]) for e in existing}
        assert ("zed.exe", "Zed::Window", "WindowControl") in identities
        assert ("brave.exe", "BraveOmnibox", "EditControl") in identities
