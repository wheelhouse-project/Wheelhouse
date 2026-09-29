"""Font-size persistence for the Pattern Manager (wh-pattern-font-size).

Acceptance criterion 3 and the persistence-round-trip part of criterion 4.
The chosen size travels the existing GUI-to-Logic settings route: the dialog
reports a user zoom through ``font_size_changed``, the GuiManager sends
``set_config_value`` for ``PATTERN_MANAGER_FONT_SIZE`` with a request ID, and
StateManager saves it through ConfigService. At the next start the GUI
process reads the same file and applies the stored size when it builds the
dialog. Every test uses a temporary config file only.
"""
from __future__ import annotations

import asyncio
import threading
import tomllib
from queue import Empty, Full, Queue
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

KEY = "PATTERN_MANAGER_FONT_SIZE"


@pytest.fixture(autouse=True)
def _quiet_gui():
    """No native settings notice, no desktop notice, no shown windows.

    ``_open_pattern_manager`` imports the dialog class from its module at
    call time, so a subclass placed there replaces only the three calls that
    would put a real window on the desktop. Patching those methods on the
    PySide6 class itself crashes the interpreter (measured: access
    violation inside the next dialog constructor).
    """
    from pattern_manager_dialog import PatternManagerDialog

    class _QuietDialog(PatternManagerDialog):
        def show(self):
            pass

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    with patch("gui.send_notice"), \
         patch("soft_allow_write_failed_toast.SoftAllowWriteFailedToast"), \
         patch("terminal_editor_window._steal_foreground"), \
         patch("pattern_manager_dialog.PatternManagerDialog", _QuietDialog):
        try:
            yield
        finally:
            _delete_dialogs()


# Every GuiManager a test builds. A parentless dialog outlives its manager,
# and a manager garbage-collected while its dialog still holds three
# connections to it corrupted the heap in the NEXT test's GuiManager
# constructor (measured: Windows fatal exception 0xc0000374 in
# _watch_the_screen_layout). The shipped app never destroys its manager
# before exit; the tests do, so each test deletes its dialogs first.
_MANAGERS = []


def _delete_dialogs():
    import shiboken6

    while _MANAGERS:
        manager = _MANAGERS.pop()
        dialog = getattr(manager, "_pm_dialog", None)
        if dialog is not None and shiboken6.isValid(dialog):
            shiboken6.delete(dialog)
        manager._pm_dialog = None


def _make_manager(config=None):
    with patch("gui.FloatingButton") as mock_button, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        mock_button.return_value = MagicMock()
        from gui import GuiManager

        manager = GuiManager(threading.Event(), Queue(), Queue(), config=config)
    manager.initial_state_received = True
    manager.button._gesture_running = False
    manager.button._is_dragging = False
    manager.button._is_resizing = False
    _MANAGERS.append(manager)
    return manager


def _drain(queue):
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


def _font_writes(commands):
    return [c for c in commands
            if c.get("action") == "set_config_value" and c.get("key") == KEY]


def _default_size():
    from pattern_manager_dialog import PatternManagerDialog

    return PatternManagerDialog(parent=None)._default_font_point_size


# --------------------------------------------------------------------------- #
#  (a) Persistence round trip through the real settings route
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zoom_persists_through_the_settings_route_and_a_restart(tmp_path):
    from config_service import ConfigService
    from state_manager import StateManager

    path = tmp_path / "config.toml"
    path.write_text("FLOATING_BUTTON_SIZE = 50\n")
    config = ConfigService(str(path))
    manager = _make_manager(config=config.get_config())
    state = StateManager(config, MagicMock(), asyncio.get_running_loop(),
                         manager.state_from_logic_queue, None)

    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    start = dialog._current_font_point_size
    _drain(manager.commands_to_logic_queue)

    # The user's path: the Ctrl+= shortcut fires.
    dialog._zoom_in_shortcut.activated.emit()
    expected = start + 1
    assert dialog._current_font_point_size == expected

    writes = _font_writes(_drain(manager.commands_to_logic_queue))
    assert len(writes) == 1, writes
    assert writes[0]["value"] == expected
    assert writes[0]["request_id"]

    await state.set_config_value(KEY, writes[0]["value"], writes[0]["request_id"])
    manager._check_queues_and_events()

    assert tomllib.loads(path.read_text())[KEY] == expected
    assert manager._settings_confirmed[KEY] == expected
    assert not manager.settings_pending
    # A confirmed save leaves the dialog where the user put it.
    assert dialog._current_font_point_size == expected

    # Close and reopen in the same process keeps the size.
    dialog.hide()
    manager._open_pattern_manager()
    assert manager._pm_dialog is dialog
    assert dialog._current_font_point_size == expected

    # A restart: a new ConfigService reads the same file, a new GuiManager
    # builds a new dialog from that config.
    restarted = _make_manager(config=ConfigService(str(path)).get_config())
    restarted._open_pattern_manager()
    assert restarted._pm_dialog is not dialog
    assert restarted._pm_dialog._current_font_point_size == expected
    assert restarted._pm_dialog.font().pointSize() == expected
    assert _font_writes(_drain(restarted.commands_to_logic_queue)) == []


# --------------------------------------------------------------------------- #
#  (b) Validation of the stored value at start
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stored, expected", [
    (99, 24),
    (3, 7),
    (13, 13),
    ("x", None),
    (True, None),
    (False, None),
    (12.0, None),
    ("absent", None),
], ids=["high", "low", "in-range", "text", "true", "false", "float", "absent"])
def test_stored_size_is_validated_when_the_dialog_is_built(stored, expected):
    config = {} if stored == "absent" else {KEY: stored}
    manager = _make_manager(config=config)
    manager._open_pattern_manager()
    want = _default_size() if expected is None else expected
    assert manager._pm_dialog._current_font_point_size == want
    assert manager._settings_confirmed.get(KEY) == expected


def test_no_config_at_all_gives_the_default_size():
    manager = _make_manager(config=None)
    manager._open_pattern_manager()
    assert manager._pm_dialog._current_font_point_size == _default_size()


# --------------------------------------------------------------------------- #
#  (c) A failed save restores the confirmed size
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stored, persisted", [
    (12, 12),
    (None, None),
], ids=["stored-size", "no-stored-size"])
def test_failed_save_restores_the_confirmed_size(stored, persisted):
    manager = _make_manager(config={} if stored is None else {KEY: stored})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    _drain(manager.commands_to_logic_queue)

    dialog._zoom_in_shortcut.activated.emit()
    dialog._zoom_in_shortcut.activated.emit()
    writes = _font_writes(_drain(manager.commands_to_logic_queue))
    assert writes, "the zoom sent no write"
    request_id = writes[-1]["request_id"]

    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request_id,
        "saved": False, "values": {KEY: persisted},
    })
    manager._check_queues_and_events()

    want = _default_size() if persisted is None else persisted
    assert dialog._current_font_point_size == want
    assert dialog.font().pointSize() == want
    # The restore is not a new user choice: it writes nothing.
    assert _font_writes(_drain(manager.commands_to_logic_queue)) == []


def test_refused_queue_restores_the_confirmed_size():
    manager = _make_manager(config={KEY: 12})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    refusing = MagicMock()
    refusing.put_nowait.side_effect = Full
    manager.commands_to_logic_queue = refusing

    dialog._zoom_in_shortcut.activated.emit()

    assert dialog._current_font_point_size == 12
    assert "Restored the confirmed values" in manager.settings_status_text
    sent = [c.args[0] for c in refusing.put_nowait.call_args_list]
    # One refused write, and the restore did not try another one.
    assert len(_font_writes(sent)) == 1


def test_refused_queue_for_a_button_key_leaves_the_font_size_alone():
    manager = _make_manager(config={KEY: 12})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    # The zoom write is enqueued and still pending, so the dialog shows 13
    # while the confirmed size is 12. Only then can a wrong restore show.
    dialog._zoom_in_shortcut.activated.emit()
    assert dialog._current_font_point_size == 13
    assert manager._settings_confirmed[KEY] == 12
    refusing = MagicMock()
    refusing.put_nowait.side_effect = Full
    manager.commands_to_logic_queue = refusing

    manager.send_command({"action": "set_config_value",
                          "key": "FLOATING_BUTTON_SIZE", "value": 80})

    assert "Restored the confirmed values" in manager.settings_status_text
    assert dialog._current_font_point_size == 13


@pytest.mark.parametrize("stored, expected", [
    ("x", None),
    (True, None),
    (99, 24),
], ids=["text", "true", "high"])
def test_failed_save_with_an_invalid_stored_value_restores_a_valid_size(stored, expected):
    manager = _make_manager(config={KEY: 12})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    dialog._zoom_in_shortcut.activated.emit()
    request_id = _font_writes(_drain(manager.commands_to_logic_queue))[-1]["request_id"]

    # The result carries the raw stored value; the restore validates it.
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request_id,
        "saved": False, "values": {KEY: stored},
    })
    manager._check_queues_and_events()

    want = _default_size() if expected is None else expected
    assert dialog._current_font_point_size == want
    assert _font_writes(_drain(manager.commands_to_logic_queue)) == []


def test_reconcile_reply_applies_the_stored_size_to_the_dialog():
    """A lost write result, then the reconcile reply, restores the dialog.

    The save failed but its config_write_result never arrived. The reconcile
    query reports the old stored size, so the dialog must show that size,
    or the next start brings back a size the user no longer sees.
    """
    manager = _make_manager(config={KEY: 12})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    dialog._zoom_in_shortcut.activated.emit()
    request_id = _font_writes(_drain(manager.commands_to_logic_queue))[-1]["request_id"]
    assert dialog._current_font_point_size == 13

    manager.state_from_logic_queue.put_nowait({
        "action": "config_values_result", "request_id": request_id,
        "values": {KEY: 12},
    })
    manager._check_queues_and_events()

    assert manager._settings_confirmed[KEY] == 12
    assert not manager.settings_pending
    assert dialog._current_font_point_size == 12
    # The apply is not a user choice: it writes nothing.
    assert _font_writes(_drain(manager.commands_to_logic_queue)) == []


def test_failed_save_of_a_button_key_leaves_the_font_size_alone():
    manager = _make_manager(config={KEY: 12})
    manager._open_pattern_manager()
    dialog = manager._pm_dialog
    dialog._zoom_in_shortcut.activated.emit()
    _drain(manager.commands_to_logic_queue)

    manager.send_command({"action": "set_config_value",
                          "key": "SHOW_SPEECH_PULSE", "value": False})
    request_id = manager.commands_to_logic_queue.get_nowait()["request_id"]
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request_id,
        "saved": False, "values": {"SHOW_SPEECH_PULSE": True},
    })
    manager._check_queues_and_events()

    assert dialog._current_font_point_size == 13


# --------------------------------------------------------------------------- #
#  (d) The programmatic apply is silent
# --------------------------------------------------------------------------- #


def test_apply_font_point_size_emits_nothing():
    from pattern_manager_dialog import PatternManagerDialog

    dialog = PatternManagerDialog(parent=None)
    emitted = []
    dialog.font_size_changed.connect(emitted.append)

    dialog.apply_font_point_size(15)
    assert dialog._current_font_point_size == 15
    dialog.apply_font_point_size(99)
    assert dialog._current_font_point_size == 24
    dialog.apply_font_point_size(None)
    assert dialog._current_font_point_size == dialog._default_font_point_size
    assert emitted == []


def test_apply_font_point_size_through_the_manager_sends_no_write():
    manager = _make_manager(config={KEY: 14})
    manager._open_pattern_manager()
    assert manager._pm_dialog._current_font_point_size == 14
    manager._pm_dialog.apply_font_point_size(18)
    assert _font_writes(_drain(manager.commands_to_logic_queue)) == []
    assert not manager.settings_pending


def test_user_zoom_emits_only_when_the_size_changes():
    from pattern_manager_dialog import PatternManagerDialog

    dialog = PatternManagerDialog(parent=None)
    emitted = []
    dialog.font_size_changed.connect(emitted.append)
    start = dialog._current_font_point_size

    dialog._on_zoom_in()
    dialog._on_zoom_out()
    dialog._on_zoom_reset()  # already at the default: no change, no signal
    assert emitted == [start + 1, start]

    dialog.apply_font_point_size(24)
    dialog._on_zoom_in()  # at the top bound: no change, no signal
    dialog._on_zoom_out()
    dialog._on_zoom_reset()
    assert emitted == [start + 1, start, 23, start]
