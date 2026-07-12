"""Tests for scripts/capture_rejected_controls.py.

The script's filter calls TextTargetPredicate.evaluate and appends the
control to the output file when the returned reason is
default_reject_paste_capable_class. Live UI Automation event subscription
cannot be exercised in a unit test (it requires a running Windows
desktop session), so these tests verify the decision logic the script
depends on -- specifically that the predicate the script constructs
returns the expected reason on representative synthetic inputs and
that should_capture filters only the soft-reject branch.

Covers the acceptance criterion from wh-no-go-capture: "A unit test
covers the soft-reject decision logic the script depends on, even if
no test exercises the live UI Automation event subscription."
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import uiautomation as auto

# Load the script as a module without executing main(). The script lives
# at services/wheelhouse/scripts/, which is not a Python package; load
# it by file path so pytest can import the module without the parent
# directory having an __init__.py.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "capture_rejected_controls.py"
)
_spec = importlib.util.spec_from_file_location(
    "capture_rejected_controls", _SCRIPT_PATH,
)
assert _spec is not None and _spec.loader is not None
capture_module = importlib.util.module_from_spec(_spec)
sys.modules["capture_rejected_controls"] = capture_module
_spec.loader.exec_module(capture_module)


def _mock_control(
    *,
    control_type=auto.ControlType.PaneControl,
    control_type_name="PaneControl",
    class_name="SomeEditor",
    has_text_pattern=False,
    has_value_pattern=False,
    is_focusable=True,
    is_enabled=True,
):
    """Build a mock UIA control matching the helper in tests/test_ui/test_text_target.py."""
    ctrl = MagicMock()
    ctrl.ControlType = int(control_type)
    ctrl.ControlTypeName = control_type_name
    ctrl.ClassName = class_name
    ctrl.IsKeyboardFocusable = is_focusable
    ctrl.IsEnabled = is_enabled

    def get_pattern(pid):
        if pid == auto.PatternId.TextPattern and has_text_pattern:
            return MagicMock(name="TextPattern")
        if pid == auto.PatternId.ValuePattern and has_value_pattern:
            return MagicMock(name="ValuePattern")
        return None

    ctrl.GetPattern.side_effect = get_pattern
    return ctrl


class TestSoftRejectFilter:
    """The script captures only the default_reject_paste_capable_class reason.

    Each test constructs the same predicate the script uses
    (no soft-allow data, no starter list), feeds a synthetic control,
    and asserts that should_capture agrees with the documented filter.
    """

    def test_soft_reject_control_is_captured(self):
        """A non-empty ClassName with no TextPattern, no EditControl,
        and no allow / soft-allow entry returns
        default_reject_paste_capable_class -- the exact case the
        starter list is meant to address.
        """
        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.PaneControl,
            control_type_name="PaneControl",
            class_name="ZedSearchPanel",
            has_text_pattern=False,
        )

        verdict = predicate.evaluate(ctrl, process_name="zed.exe")

        assert verdict.verdict is False
        assert verdict.reason == "default_reject_paste_capable_class"
        assert capture_module.should_capture(verdict) is True

    def test_text_pattern_accept_is_not_captured(self):
        """A control with TextPattern accepts as text_pattern_available
        and is not a starter-list candidate.
        """
        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.EditControl,
            control_type_name="EditControl",
            class_name="Edit",
            has_text_pattern=True,
        )

        verdict = predicate.evaluate(ctrl, process_name="notepad.exe")

        assert verdict.verdict is True
        assert verdict.reason == "text_pattern_available"
        assert capture_module.should_capture(verdict) is False

    def test_edit_control_accept_is_not_captured(self):
        """ControlType=EditControl without TextPattern still accepts
        via the edit_control branch and is not a candidate.
        """
        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.EditControl,
            control_type_name="EditControl",
            class_name="Edit",
            has_text_pattern=False,
            is_enabled=True,
        )

        verdict = predicate.evaluate(ctrl, process_name="notepad.exe")

        assert verdict.verdict is True
        assert verdict.reason == "edit_control"
        assert capture_module.should_capture(verdict) is False

    def test_denylist_control_type_hard_reject_is_not_captured(self):
        """A ButtonControl hard-rejects via denylist_control_type. The
        starter list is for soft-rejects only, so hard rejects must
        not be captured.
        """
        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.ButtonControl,
            control_type_name="ButtonControl",
            class_name="Button",
            has_text_pattern=False,
        )

        verdict = predicate.evaluate(ctrl, process_name="notepad.exe")

        assert verdict.verdict is False
        assert verdict.reason == "denylist_control_type"
        assert capture_module.should_capture(verdict) is False

    def test_browser_empty_class_hard_reject_is_not_captured(self):
        """The browser-process empty-ClassName hard-rejects via
        default_reject. The starter list addresses soft-rejects; the
        browser page-body case is handled by the empty-ClassName check
        before TextPattern and must not appear in the output.
        """
        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.DocumentControl,
            control_type_name="DocumentControl",
            class_name="",
            has_text_pattern=True,
        )

        verdict = predicate.evaluate(ctrl, process_name="brave.exe")

        assert verdict.verdict is False
        assert verdict.reason == "default_reject"
        assert capture_module.should_capture(verdict) is False

    def test_no_focused_control_is_not_captured(self):
        """When nothing has focus the predicate returns
        no_focused_control. The polling thread sees this and skips.
        """
        predicate = capture_module.build_capture_predicate()

        verdict = predicate.evaluate(None, process_name="")

        assert verdict.verdict is False
        assert verdict.reason == "no_focused_control"
        assert capture_module.should_capture(verdict) is False

    def test_capture_predicate_has_no_soft_allow_data(self):
        """The capture predicate must construct with no soft-allow
        entries so that a triple a user has previously approved still
        soft-rejects from the script's point of view -- the
        starter-list candidate file must include controls that would
        soft-reject on a fresh install.
        """
        predicate = capture_module.build_capture_predicate()

        assert predicate.soft_allow_tuples == frozenset()


class TestOutputAppendIntegration:
    """End-to-end check that should_capture + append_soft_allow_tuple
    produce the expected starter-list TOML schema.
    """

    def test_capture_writes_starter_schema_entry(self, tmp_path):
        """One soft-reject control written to a fresh file should
        produce one [[entries]] block with the four expected fields.
        """
        output_path = tmp_path / "starter-candidates.toml"

        predicate = capture_module.build_capture_predicate()
        ctrl = _mock_control(
            control_type=auto.ControlType.PaneControl,
            control_type_name="PaneControl",
            class_name="ZedSearchPanel",
            has_text_pattern=False,
        )

        verdict = predicate.evaluate(ctrl, process_name="zed.exe")
        assert capture_module.should_capture(verdict)

        from utils.soft_allow_writer import append_soft_allow_tuple
        added_at = capture_module._now_iso()
        ok = append_soft_allow_tuple(
            (
                verdict.process_name,
                verdict.class_name,
                verdict.control_type,
                added_at,
            ),
            output_path,
        )

        assert ok is True
        assert output_path.exists()

        from shared.soft_allow_schema import parse_soft_allow_file
        entries = parse_soft_allow_file(
            output_path, log_skipped_entries=False, caller="test",
        )
        assert len(entries) == 1
        assert entries[0].process_name == "zed.exe"
        assert entries[0].class_name == "ZedSearchPanel"
        assert entries[0].control_type == "PaneControl"
        assert entries[0].added_at == added_at

    def test_capture_dedup_on_same_triple(self, tmp_path):
        """The same triple captured twice still produces one entry."""
        output_path = tmp_path / "starter-candidates.toml"

        from utils.soft_allow_writer import append_soft_allow_tuple
        append_soft_allow_tuple(
            ("zed.exe", "ZedSearchPanel", "PaneControl", "2026-05-14T00:00:00Z"),
            output_path,
        )
        append_soft_allow_tuple(
            ("zed.exe", "ZedSearchPanel", "PaneControl", "2026-05-14T00:01:00Z"),
            output_path,
        )

        from shared.soft_allow_schema import parse_soft_allow_file
        entries = parse_soft_allow_file(
            output_path, log_skipped_entries=False, caller="test",
        )
        assert len(entries) == 1
        # The first added_at survives -- the writer keeps the original
        # timestamp when an identity match drops the new entry.
        assert entries[0].added_at == "2026-05-14T00:00:00Z"


class TestProcessNameResolution:
    """Coverage for codex review wh-no-go-capture.1.1 and wh-no-go-capture.1.2:
    process-name resolution failures return None, and the failure path
    includes the _ctypes.COMError class that a stale UIA element raises.
    """

    def test_resolve_returns_none_when_process_id_unreadable(self):
        """A control whose ProcessId read raises any of the documented
        exceptions yields None so the caller can skip the capture.
        """
        import _ctypes

        for exc in (
            _ctypes.COMError(0, "stale", (None, None, None, None, None)),
            AttributeError("ProcessId"),
            OSError(5, "denied"),
            ValueError("nan"),
            TypeError("None"),
        ):
            ctrl = MagicMock()
            type(ctrl).ProcessId = property(
                lambda _, exc=exc: (_ for _ in ()).throw(exc)
            )
            assert capture_module._resolve_process_name(ctrl) is None

    def test_resolve_returns_none_when_psutil_raises(self):
        """A process id that no longer maps to a running process must
        yield None (NoSuchProcess) so an inert empty-process_name entry
        cannot be written.
        """
        import psutil

        ctrl = MagicMock()
        ctrl.ProcessId = 999999
        with __import__("unittest").mock.patch.object(
            psutil, "Process",
            side_effect=psutil.NoSuchProcess(pid=999999),
        ):
            assert capture_module._resolve_process_name(ctrl) is None

    def test_resolve_returns_lowercase_exe_name_on_success(self):
        """The happy path returns the lowercase exe name matching what
        the runtime UIContext capture uses.
        """
        import psutil

        ctrl = MagicMock()
        ctrl.ProcessId = 1234
        proc = MagicMock()
        proc.name.return_value = "Discord.exe"
        with __import__("unittest").mock.patch.object(
            psutil, "Process", return_value=proc,
        ):
            assert capture_module._resolve_process_name(ctrl) == "discord.exe"


class TestLoadCapturedTriples:
    """Coverage for the in-memory dedup seed used by _capture_loop.

    The set is preloaded from the output file so a re-run of the
    script does not call the writer for triples already in the file.
    """

    def test_load_returns_empty_set_for_missing_file(self, tmp_path):
        path = tmp_path / "does-not-exist.toml"
        assert capture_module._load_captured_triples(path) == set()

    def test_load_returns_triples_from_existing_file(self, tmp_path):
        path = tmp_path / "starter-candidates.toml"
        from utils.soft_allow_writer import append_soft_allow_tuple
        append_soft_allow_tuple(
            ("zed.exe", "ZedSearchPanel", "PaneControl", "2026-05-14T00:00:00Z"),
            path,
        )
        append_soft_allow_tuple(
            ("discord.exe", "Edit", "EditControl", "2026-05-14T00:01:00Z"),
            path,
        )

        triples = capture_module._load_captured_triples(path)

        assert triples == {
            ("zed.exe", "ZedSearchPanel", "PaneControl"),
            ("discord.exe", "Edit", "EditControl"),
        }
