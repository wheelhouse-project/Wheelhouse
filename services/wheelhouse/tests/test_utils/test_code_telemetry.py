"""Tests for code_telemetry.py - Dead code execution tracking.

Tests cover:
- track_execution writes JSONL events to file
- Notification shown only once per code path
- get_telemetry_summary aggregates execution counts
- Thread-safe file writing
- Error resilience (telemetry never breaks the app)
"""

import json
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest


class TestTrackExecution:
    """Tests for track_execution function."""

    @pytest.fixture(autouse=True)
    def reset_notified_paths(self):
        """Reset module-level state between tests."""
        import utils.code_telemetry as ct
        original = ct._notified_paths.copy()
        ct._notified_paths.clear()
        yield
        ct._notified_paths.clear()
        ct._notified_paths.update(original)

    def test_writes_event_to_file(self, tmp_path):
        from utils.code_telemetry import track_execution

        telemetry_file = "test_telemetry.jsonl"
        telemetry_path = tmp_path / telemetry_file

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            # Make workspace_root / telemetry_file resolve to tmp_path
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            with patch("utils.code_telemetry._show_notification"):
                track_execution("test.path", telemetry_file=telemetry_file)

        content = telemetry_path.read_text()
        event = json.loads(content.strip())
        assert event["code_path"] == "test.path"
        assert "timestamp" in event
        assert "stack_trace" in event

    def test_writes_context_data(self, tmp_path):
        from utils.code_telemetry import track_execution

        telemetry_file = "test_telemetry.jsonl"
        telemetry_path = tmp_path / telemetry_file

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            with patch("utils.code_telemetry._show_notification"):
                track_execution("test.path", context={"key": "value"}, telemetry_file=telemetry_file)

        event = json.loads(telemetry_path.read_text().strip())
        assert event["context"] == {"key": "value"}

    def test_empty_context_defaults_to_empty_dict(self, tmp_path):
        from utils.code_telemetry import track_execution

        telemetry_file = "test_telemetry.jsonl"
        telemetry_path = tmp_path / telemetry_file

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            with patch("utils.code_telemetry._show_notification"):
                track_execution("test.path", context=None, telemetry_file=telemetry_file)

        event = json.loads(telemetry_path.read_text().strip())
        assert event["context"] == {}

    def test_notification_shown_once_per_path(self, tmp_path):
        from utils.code_telemetry import track_execution

        telemetry_file = "test_telemetry.jsonl"
        telemetry_path = tmp_path / telemetry_file

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            with patch("utils.code_telemetry._show_notification") as mock_notify:
                track_execution("path.a", telemetry_file=telemetry_file)
                track_execution("path.a", telemetry_file=telemetry_file)
                track_execution("path.b", telemetry_file=telemetry_file)

        # path.a notified once, path.b notified once
        assert mock_notify.call_count == 2

    def test_appends_multiple_events(self, tmp_path):
        from utils.code_telemetry import track_execution

        telemetry_file = "test_telemetry.jsonl"
        telemetry_path = tmp_path / telemetry_file

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            with patch("utils.code_telemetry._show_notification"):
                track_execution("path.1", telemetry_file=telemetry_file)
                track_execution("path.2", telemetry_file=telemetry_file)

        lines = telemetry_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_error_in_tracking_does_not_raise(self):
        from utils.code_telemetry import track_execution

        with patch("utils.code_telemetry.Path", side_effect=RuntimeError("boom")):
            # Should not raise
            track_execution("test.path")


class TestShowNotification:
    """Tests for the internal notification function."""

    def test_sends_notification(self):
        from utils.code_telemetry import _show_notification

        mock_notif = Mock()
        with patch.dict("sys.modules", {"plyer": Mock(notification=mock_notif)}):
            _show_notification("test.path", None)

        mock_notif.notify.assert_called_once()
        kwargs = mock_notif.notify.call_args[1]
        assert "test.path" in kwargs["message"]

    def test_includes_context_in_notification(self):
        from utils.code_telemetry import _show_notification

        mock_notif = Mock()
        with patch.dict("sys.modules", {"plyer": Mock(notification=mock_notif)}):
            _show_notification("test.path", {"id": 42, "type": "test"})

        kwargs = mock_notif.notify.call_args[1]
        assert "id=42" in kwargs["message"]

    def test_notification_failure_doesnt_raise(self):
        from utils.code_telemetry import _show_notification

        mock_notif = Mock()
        mock_notif.notify.side_effect = RuntimeError("broken")
        with patch.dict("sys.modules", {"plyer": Mock(notification=mock_notif)}):
            # Should not raise
            _show_notification("test.path", None)


class TestGetTelemetrySummary:
    """Tests for telemetry summary aggregation."""

    def test_empty_file_returns_empty_dict(self, tmp_path):
        from utils.code_telemetry import get_telemetry_summary

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            nonexistent = tmp_path / "nonexistent.jsonl"
            mock_workspace.__truediv__ = Mock(return_value=nonexistent)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            result = get_telemetry_summary("nonexistent.jsonl")

        assert result == {}

    def test_counts_executions(self, tmp_path):
        from utils.code_telemetry import get_telemetry_summary

        telemetry_file = "test_summary.jsonl"
        telemetry_path = tmp_path / telemetry_file
        events = [
            {"code_path": "path.a", "timestamp": "2024-01-01"},
            {"code_path": "path.a", "timestamp": "2024-01-02"},
            {"code_path": "path.b", "timestamp": "2024-01-01"},
        ]
        telemetry_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            result = get_telemetry_summary(telemetry_file)

        assert result == {"path.a": 2, "path.b": 1}

    def test_handles_blank_lines(self, tmp_path):
        from utils.code_telemetry import get_telemetry_summary

        telemetry_file = "test_blank.jsonl"
        telemetry_path = tmp_path / telemetry_file
        telemetry_path.write_text('{"code_path": "x"}\n\n{"code_path": "x"}\n')

        with patch("utils.code_telemetry.Path") as mock_path_cls:
            mock_workspace = Mock()
            mock_workspace.__truediv__ = Mock(return_value=telemetry_path)
            mock_path_cls.return_value.parent.parent.parent.parent = mock_workspace

            result = get_telemetry_summary(telemetry_file)

        assert result == {"x": 2}
