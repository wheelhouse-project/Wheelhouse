"""Tests for shared.soft_allow_schema (wh-65e46.2).

The shared parser replaces the duplicated TOML readers at
services/wheelhouse/ui/text_target.py:_load_soft_allow_tuples (input
process) and services/wheelhouse/utils/soft_allow_writer.py:_read_existing
(logic process). Both callers project ParsedEntry to the shape they need;
this module is the single source of truth for the entry schema and the
malformed-file recovery contract.

The schema (per services/wheelhouse/data/soft_allow_tuples.toml) requires
all four fields (process_name, class_name, control_type, added_at) as
strings. Entries missing any required field are skipped. The previous
loader did not validate added_at; the previous writer reader defaulted
missing added_at to "" and re-wrote it. Both behaviours are tightened
here so the in-memory and on-disk representations agree.
"""
from __future__ import annotations

import pytest

from shared.soft_allow_schema import ParsedEntry, parse_soft_allow_file


class TestMissingOrEmptyFile:
    def test_missing_file_returns_empty(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_tuples.toml"
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="test",
            )
        assert entries == []
        # Missing file is the documented initial state -- never warn.
        assert caplog.records == []

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text("", encoding="utf-8")
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []

    def test_file_without_entries_section_returns_empty(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "# leading comments only\n# no entries yet\n",
            encoding="utf-8",
        )
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []


class TestMalformedTopLevel:
    def test_malformed_toml_returns_empty_and_warns(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\nprocess_name = 'zed.exe\nclass_name = =\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="test caller",
            )
        assert entries == []
        # Caller identifier and the word "malformed" should both appear.
        assert any(
            "test caller" in record.message
            and "malformed" in record.message.lower()
            for record in caplog.records
        )

    def test_entries_not_a_list_returns_empty_and_warns(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        # entries is a single table, not an array of tables.
        path.write_text(
            "[entries]\nprocess_name = 'zed.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert entries == []
        assert any(
            "loader" in record.message
            and "array of tables" in record.message
            for record in caplog.records
        )

    def test_unicode_decode_error_returns_empty_and_warns(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        # Bytes that are not valid UTF-8 (lone continuation byte).
        path.write_bytes(b"\xc3\x28")
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="test",
            )
        assert entries == []
        assert any(
            "malformed" in record.message.lower()
            for record in caplog.records
        )


class TestPerEntryValidation:
    def test_valid_entries_load_with_all_four_fields(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-04-30T15:00:00Z'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'sublime_text.exe'\n"
            "class_name = 'PX_WINDOW_CLASS'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-01T10:00:00Z'\n",
            encoding="utf-8",
        )
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == [
            ParsedEntry(
                process_name="zed.exe",
                class_name="Zed::Window",
                control_type="WindowControl",
                added_at="2026-04-30T15:00:00Z",
            ),
            ParsedEntry(
                process_name="sublime_text.exe",
                class_name="PX_WINDOW_CLASS",
                control_type="WindowControl",
                added_at="2026-05-01T10:00:00Z",
            ),
        ]

    @pytest.mark.parametrize(
        "missing_field",
        ["process_name", "class_name", "control_type", "added_at"],
    )
    def test_entry_missing_any_required_field_is_skipped(
        self, tmp_path, missing_field,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        all_fields = {
            "process_name": "zed.exe",
            "class_name": "Zed::Window",
            "control_type": "WindowControl",
            "added_at": "2026-04-30T15:00:00Z",
        }
        all_fields.pop(missing_field)
        body = "[[entries]]\n" + "".join(
            f"{k} = '{v}'\n" for k, v in all_fields.items()
        )
        path.write_text(body, encoding="utf-8")
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []

    @pytest.mark.parametrize(
        "field,bogus_value",
        [
            ("process_name", "42"),
            ("class_name", "true"),
            ("control_type", "1.5"),
            ("added_at", "[1, 2]"),
        ],
    )
    def test_entry_with_non_string_field_is_skipped(
        self, tmp_path, field, bogus_value,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        all_fields = {
            "process_name": "'zed.exe'",
            "class_name": "'Zed::Window'",
            "control_type": "'WindowControl'",
            "added_at": "'2026-04-30T15:00:00Z'",
        }
        all_fields[field] = bogus_value
        body = "[[entries]]\n" + "".join(
            f"{k} = {v}\n" for k, v in all_fields.items()
        )
        path.write_text(body, encoding="utf-8")
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []

    def test_valid_entry_loads_alongside_invalid_one(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-04-30T15:00:00Z'\n",
            encoding="utf-8",
        )
        entries = parse_soft_allow_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == [
            ParsedEntry(
                process_name="zed.exe",
                class_name="Zed::Window",
                control_type="WindowControl",
                added_at="2026-04-30T15:00:00Z",
            ),
        ]


class TestLogSkippedEntriesToggle:
    def test_log_skipped_entries_true_warns_per_entry(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            parse_soft_allow_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert any(
            "loader" in record.message and "incomplete" in record.message
            for record in caplog.records
        )

    def test_log_skipped_entries_false_silently_skips(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            parse_soft_allow_file(
                path, log_skipped_entries=False, caller="writer",
            )
        # No per-entry warning; the rewrite path drops the bad entry on
        # the next write so a noisy log is not useful here.
        assert caplog.records == []

    def test_non_table_entry_skipped_with_warning_when_logging_on(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        # entries is an array, but one element is a string instead of a
        # table. tomllib accepts a heterogeneous array; the parser must
        # skip the non-table element.
        path.write_text(
            "entries = ['not-a-table']\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert entries == []
        assert any(
            "loader" in record.message and "non-table" in record.message
            for record in caplog.records
        )

    def test_non_table_entry_silently_skipped_when_logging_off(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "entries = ['not-a-table']\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=False, caller="writer",
            )
        assert entries == []
        assert caplog.records == []


class TestOSReadFailure:
    def test_oserror_on_read_returns_empty_and_warns(
        self, tmp_path, monkeypatch, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\nprocess_name = 'zed.exe'\n",
            encoding="utf-8",
        )

        from pathlib import Path as _Path

        original_read_bytes = _Path.read_bytes

        def boom(self):
            if self == path:
                raise PermissionError("simulated permission denied")
            return original_read_bytes(self)

        monkeypatch.setattr(_Path, "read_bytes", boom)
        with caplog.at_level("WARNING"):
            entries = parse_soft_allow_file(
                path, log_skipped_entries=True, caller="test",
            )
        assert entries == []
        assert any(
            "could not read" in record.message
            for record in caplog.records
        )
