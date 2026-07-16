"""capture_context() de-duplicates its per-word terminal-detection logging.

capture_context() (ui/context.py) runs once per dictated word in the Input
process. Before this change, an unchanged focused target logged the same
terminal-detection DEBUG line for every word. One 2026-07-16 session
(wheelhouse.log) logged 30 identical
"Not detected as terminal: class=_DictationTextEdit, process=python.exe"
lines for 30 words. These tests pin that an unchanged detection logs once and
that a changed detection logs again, so transitions stay visible.
"""
import logging
from unittest import mock

import pytest

import ui.context as context_mod


class _FakeControl:
    """Minimal stand-in for a uiautomation focused control."""

    def __init__(self, class_name, process_id=4321, top_class=""):
        self.ClassName = class_name
        self.ProcessId = process_id
        self._top_class = top_class

    def GetTopLevelControl(self):
        top = mock.MagicMock()
        top.ClassName = self._top_class
        return top


def _capture(class_name, process_name, top_class=""):
    """Run the real capture_context() against a mocked focused control.

    ``auto.GetFocusedControl`` and ``psutil.Process`` are the only two OS
    calls capture_context makes to identify the target; patching them lets the
    real detection and logging run without a live window.
    """
    ctrl = _FakeControl(class_name, top_class=top_class)
    proc = mock.MagicMock()
    proc.name.return_value = process_name
    with mock.patch.object(context_mod.auto, "GetFocusedControl", return_value=ctrl), \
         mock.patch.object(context_mod.psutil, "Process", return_value=proc):
        return context_mod.capture_context()


def _detection_records(caplog):
    keys = ("Target is", "Not detected as terminal")
    return [r for r in caplog.records if any(k in r.getMessage() for k in keys)]


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    # De-dup state is module-level; reset before and after so tests are
    # order-independent.
    context_mod._last_target_log = None
    yield
    context_mod._last_target_log = None


def test_unchanged_console_host_logs_once(caplog):
    with caplog.at_level(logging.DEBUG):
        _capture("", "conhost.exe")
        _capture("", "conhost.exe")
        _capture("", "conhost.exe")
    lines = [r for r in _detection_records(caplog) if "Console Host" in r.getMessage()]
    assert len(lines) == 1


def test_repeated_not_a_terminal_logs_once(caplog):
    # The exact 30x case: dictation editor focused for many words.
    with caplog.at_level(logging.DEBUG):
        for _ in range(30):
            _capture("_DictationTextEdit", "python.exe")
    lines = [r for r in _detection_records(caplog)
             if "Not detected as terminal" in r.getMessage()]
    assert len(lines) == 1


def test_changed_target_logs_again(caplog):
    # editor -> console -> editor: each transition logs once.
    with caplog.at_level(logging.DEBUG):
        _capture("_DictationTextEdit", "python.exe")
        _capture("", "conhost.exe")
        _capture("_DictationTextEdit", "python.exe")
    msgs = [r.getMessage() for r in _detection_records(caplog)]
    assert sum("Not detected as terminal" in m for m in msgs) == 2
    assert sum("Console Host" in m for m in msgs) == 1


def test_detection_result_unchanged_by_dedup():
    # De-dup must not alter the is_terminal decision each branch produces.
    assert _capture("", "conhost.exe").is_terminal is True
    assert _capture("TermControl", "windowsterminal.exe").is_terminal is True
    assert _capture("_DictationTextEdit", "python.exe").is_terminal is False
