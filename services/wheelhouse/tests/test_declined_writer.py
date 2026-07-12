"""Tests for utils.declined_writer (wh-27gvv).

Covers the atomic write idiom (temp file + fsync + os.replace), the
read-modify-write append helper, deduplication on the identity tuple,
and graceful failure (returns False, logs WARNING, never raises).

Mirrors test_soft_allow_writer.py for the parallel declined-tuple
storage file.
"""
from __future__ import annotations

import os
import tomllib

from utils.declined_writer import (
    append_declined_tuple,
    write_declined_tuples,
)


# --- write_declined_tuples -------------------------------------------------


class TestWriteDeclinedTuples:
    def test_writes_entries_in_documented_schema(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        ok = write_declined_tuples(
            [
                ("zed.exe", "Zed::Window", "WindowControl",
                 "2026-05-13T15:00:00Z"),
                ("explorer.exe", "Button", "ButtonControl",
                 "2026-05-13T16:00:00Z"),
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
        assert entries[0]["added_at"] == "2026-05-13T15:00:00Z"

    def test_creates_parent_directory_if_missing(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "soft_allow_declined_tuples.toml"
        ok = write_declined_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-05-13T15:00:00Z")],
            target,
        )
        assert ok is True
        assert target.exists()

    def test_empty_list_writes_empty_file(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        ok = write_declined_tuples([], target)
        assert ok is True
        assert target.exists()
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert data == {}

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        target.write_text(
            "[[entries]]\nprocess_name = 'old.exe'\n"
            "class_name = 'OldClass'\ncontrol_type = 'OldControl'\n"
            "added_at = '2025-01-01T00:00:00Z'\n",
            encoding="utf-8",
        )
        ok = write_declined_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-05-13T15:00:00Z")],
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
        target = tmp_path / "soft_allow_declined_tuples.toml"
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
            ok = write_declined_tuples(
                [("zed.exe", "Zed::Window", "WindowControl",
                  "2026-05-13T15:00:00Z")],
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
            "declined" in record.message.lower()
            for record in caplog.records
        )

    def test_returns_false_on_oserror_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        target = tmp_path / "soft_allow_declined_tuples.toml"

        def fail_replace(_src, _dst):
            raise OSError("rename failed")

        monkeypatch.setattr(os, "replace", fail_replace)
        ok = write_declined_tuples(
            [("zed.exe", "Zed::Window", "WindowControl",
              "2026-05-13T15:00:00Z")],
            target,
        )
        assert ok is False


# --- append_declined_tuple -------------------------------------------------


class TestAppendDeclinedTuple:
    def test_appends_to_empty_file(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        ok = append_declined_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-13T15:00:00Z"),
            target,
        )
        assert ok is True
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        assert data["entries"][0]["process_name"] == "zed.exe"

    def test_appends_to_missing_file(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        ok = append_declined_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-13T15:00:00Z"),
            target,
        )
        assert ok is True
        assert target.exists()

    def test_dedupes_on_identity_tuple(self, tmp_path):
        # Identity is (process, class, control_type). The existing
        # entry's added_at is canonical -- the user's original decline
        # timestamp wins on a second No click for the same control.
        target = tmp_path / "soft_allow_declined_tuples.toml"
        append_declined_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-13T15:00:00Z"),
            target,
        )
        append_declined_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-14T12:00:00Z"),
            target,
        )
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        assert data["entries"][0]["added_at"] == "2026-05-13T15:00:00Z"

    def test_appends_distinct_tuples(self, tmp_path):
        target = tmp_path / "soft_allow_declined_tuples.toml"
        append_declined_tuple(
            ("zed.exe", "Zed::Window", "WindowControl",
             "2026-05-13T15:00:00Z"),
            target,
        )
        append_declined_tuple(
            ("explorer.exe", "Button", "ButtonControl",
             "2026-05-13T16:00:00Z"),
            target,
        )
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 2


class TestConcurrentAppend:
    """Regression test for the read-modify-write race.

    Two threads calling append_declined_tuple on the same path with
    different tuples must not lose either tuple. Without the per-module
    append lock, both threads can read the same baseline, each
    serialise their own one-entry payload, and the second to call
    os.replace overwrites the first.
    """

    def test_two_concurrent_appends_both_persist(self, tmp_path):
        import threading
        from utils.declined_writer import append_declined_tuple, _read_existing

        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_bytes(b"")

        results: list[bool] = []

        def worker(t):
            results.append(append_declined_tuple(t, path))

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
