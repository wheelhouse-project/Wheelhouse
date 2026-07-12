"""Tests for shared.declined_tuples_schema (wh-27gvv).

The schema parser reads soft_allow_declined_tuples.toml and projects
each entry to a DeclinedEntry dataclass. The file shape matches
soft_allow_tuples.toml: each entry is a TOML table with four required
string fields -- process_name, class_name, control_type, added_at.
Missing fields, non-string values, or non-table entries are skipped.
The top-level shape is an "entries" array of tables; a missing
"entries" key is the documented initial state and yields an empty
result silently. A non-list "entries" key is malformed user input
and is logged.

The contract mirrors shared.soft_allow_schema. The two stay separate
modules because they may diverge later (the bead description treats
them as distinct concerns: approved tuples vs declined tuples).
"""
from __future__ import annotations

import pytest

from shared.declined_tuples_schema import DeclinedEntry, parse_declined_file


class TestMissingOrEmptyFile:
    def test_missing_file_returns_empty(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        with caplog.at_level("WARNING"):
            entries = parse_declined_file(
                path, log_skipped_entries=True, caller="test",
            )
        assert entries == []
        # Missing file is the documented initial state -- never warn.
        assert caplog.records == []

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text("", encoding="utf-8")
        entries = parse_declined_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []

    def test_file_without_entries_section_returns_empty(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "# leading comments only\n# no entries yet\n",
            encoding="utf-8",
        )
        entries = parse_declined_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []


class TestMalformedTopLevel:
    def test_malformed_toml_returns_empty_and_warns(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\nprocess_name = 'zed.exe\nclass_name = =\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_declined_file(
                path, log_skipped_entries=True, caller="test caller",
            )
        assert entries == []
        assert any(
            "test caller" in record.message
            and "malformed" in record.message.lower()
            for record in caplog.records
        )

    def test_entries_not_a_list_returns_empty_and_warns(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[entries]\nprocess_name = 'zed.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_declined_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert entries == []
        assert any(
            "loader" in record.message
            and "array of tables" in record.message
            for record in caplog.records
        )


class TestPerEntryValidation:
    def test_valid_entries_load_with_all_four_fields(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T15:00:00Z'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'explorer.exe'\n"
            "class_name = 'Button'\n"
            "control_type = 'ButtonControl'\n"
            "added_at = '2026-05-13T16:00:00Z'\n",
            encoding="utf-8",
        )
        entries = parse_declined_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == [
            DeclinedEntry(
                process_name="zed.exe",
                class_name="Zed::Window",
                control_type="WindowControl",
                added_at="2026-05-13T15:00:00Z",
            ),
            DeclinedEntry(
                process_name="explorer.exe",
                class_name="Button",
                control_type="ButtonControl",
                added_at="2026-05-13T16:00:00Z",
            ),
        ]

    @pytest.mark.parametrize(
        "missing_field",
        ["process_name", "class_name", "control_type", "added_at"],
    )
    def test_entry_missing_any_required_field_is_skipped(
        self, tmp_path, missing_field,
    ):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        all_fields = {
            "process_name": "zed.exe",
            "class_name": "Zed::Window",
            "control_type": "WindowControl",
            "added_at": "2026-05-13T15:00:00Z",
        }
        all_fields.pop(missing_field)
        body = "[[entries]]\n" + "".join(
            f"{k} = '{v}'\n" for k, v in all_fields.items()
        )
        path.write_text(body, encoding="utf-8")
        entries = parse_declined_file(
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
        path = tmp_path / "soft_allow_declined_tuples.toml"
        all_fields = {
            "process_name": "'zed.exe'",
            "class_name": "'Zed::Window'",
            "control_type": "'WindowControl'",
            "added_at": "'2026-05-13T15:00:00Z'",
        }
        all_fields[field] = bogus_value
        body = "[[entries]]\n" + "".join(
            f"{k} = {v}\n" for k, v in all_fields.items()
        )
        path.write_text(body, encoding="utf-8")
        entries = parse_declined_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == []

    def test_valid_entry_loads_alongside_invalid_one(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T15:00:00Z'\n",
            encoding="utf-8",
        )
        entries = parse_declined_file(
            path, log_skipped_entries=True, caller="test",
        )
        assert entries == [
            DeclinedEntry(
                process_name="zed.exe",
                class_name="Zed::Window",
                control_type="WindowControl",
                added_at="2026-05-13T15:00:00Z",
            ),
        ]


class TestLogSkippedEntriesToggle:
    def test_log_skipped_entries_true_warns_per_entry(self, tmp_path, caplog):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            parse_declined_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert any(
            "loader" in record.message and "incomplete" in record.message
            for record in caplog.records
        )

    def test_log_skipped_entries_false_silently_skips(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            parse_declined_file(
                path, log_skipped_entries=False, caller="writer",
            )
        assert caplog.records == []

    def test_non_table_entry_skipped_with_warning_when_logging_on(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "entries = ['not-a-table']\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = parse_declined_file(
                path, log_skipped_entries=True, caller="loader",
            )
        assert entries == []
        assert any(
            "loader" in record.message and "non-table" in record.message
            for record in caplog.records
        )


class TestOSReadFailure:
    def test_oserror_on_read_returns_empty_and_warns(
        self, tmp_path, monkeypatch, caplog,
    ):
        path = tmp_path / "soft_allow_declined_tuples.toml"
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
            entries = parse_declined_file(
                path, log_skipped_entries=True, caller="test",
            )
        assert entries == []
        assert any(
            "could not read" in record.message
            for record in caplog.records
        )
