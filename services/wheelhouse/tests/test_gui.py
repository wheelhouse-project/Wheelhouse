# test_gui.py - Tests for GUI system tray, floating button, and state sync
#
# Strategy: Mock PySide6/pystray/PIL/plyer and test pure logic paths:
# icon creation, provider display names, state processing, shared memory
# parsing, command routing, and menu color mapping.

import json
import struct
from queue import Empty, Full
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from tests.test_gui_release_does_not_guess_speech import manager as _ptt_base_manager


@pytest.fixture(autouse=True)
def mock_settings_notice_widget():
    # These tests exercise queue/state logic, not native window lifetime.
    # Existing geometry tests now also send acknowledged settings commands.
    with patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast'):
        yield


class TestSettingsAcknowledgement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('route', ['ack', 'reconcile', 'broadcast'])
    async def test_settings_ack_never_confirms_newer_unsaved_live_edit(self, manager, tmp_path, monkeypatch, route):
        """Live 90 must survive, while every confirmation route reports disk 80."""
        import asyncio
        import threading
        import tomllib
        import config_service
        from state_manager import StateManager

        path = tmp_path / 'config.toml'
        path.write_text('FLOATING_BUTTON_SIZE = 50\nFLOATING_BUTTON_POS = [100, 100]\n')
        config = config_service.ConfigService(str(path))
        state = StateManager(config, MagicMock(), asyncio.get_running_loop(), manager.state_from_logic_queue, None)
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_replace = config_service.os.replace
        def held_replace(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(10):
                raise TimeoutError('test barrier not released')
            return real_replace(*args)
        monkeypatch.setattr(config_service.os, 'replace', held_replace)
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        task = asyncio.create_task(state.set_config_value('FLOATING_BUTTON_SIZE', 80, command['request_id']))
        try:
            await asyncio.wait_for(started.wait(), 5)
            config.set('FLOATING_BUTTON_SIZE', 90)
        finally:
            release.set()
            await task
        assert config.get('FLOATING_BUTTON_SIZE') == 90
        assert tomllib.loads(path.read_text())['FLOATING_BUTTON_SIZE'] == 80
        messages = []
        while not manager.state_from_logic_queue.empty():
            messages.append(manager.state_from_logic_queue.get_nowait())
        if route == 'ack':
            manager.state_from_logic_queue.put_nowait(messages[0])
        elif route == 'reconcile':
            await state.get_config_values(['FLOATING_BUTTON_SIZE'], command['request_id'])
        else:
            # Resolve the request with its known disk outcome before consuming
            # the ordinary live state broadcast that follows it.
            self.reply(manager, command['request_id'], True, 80)
            manager.state_from_logic_queue.put_nowait(messages[-1])
        manager._check_queues_and_events()
        assert manager._settings_confirmed['FLOATING_BUTTON_SIZE'] == 80
        assert config.get('FLOATING_BUTTON_SIZE') == 90

    @pytest.mark.asyncio
    @pytest.mark.parametrize('saved', [True, False])
    async def test_settings_ack_group_round_trip_through_handler_and_disk(self, manager, tmp_path, monkeypatch, saved):
        import asyncio
        import tomllib
        import tomli_w
        from config_service import ConfigService
        from state_manager import StateManager
        from main import LogicController
        from PySide6.QtCore import QPoint
        path = tmp_path / 'config.toml'
        path.write_text('FLOATING_BUTTON_SIZE = 50\nFLOATING_BUTTON_POS = [100, 100]\n')
        config = ConfigService(str(path))
        state = StateManager(config, MagicMock(), asyncio.get_running_loop(), manager.state_from_logic_queue, None)
        controller = MagicMock(spec=LogicController)
        controller.state_manager = state
        tasks = []
        controller.create_task_with_error_handling.side_effect = lambda coro, name: tasks.append(coro)
        if not saved:
            def fail_write(*args, **kwargs):
                raise OSError('disk full')
            monkeypatch.setattr(tomli_w, 'dump', fail_write)
        manager.send_resize_commit_command(80, QPoint(85, 85))
        command = manager.commands_to_logic_queue.get_nowait()
        LogicController._build_gui_handler_map(controller, command)[command['action']]()
        await tasks.pop()
        manager._check_queues_and_events()
        assert not manager.settings_pending
        expected_size, expected_pos = (80, [85, 85]) if saved else (50, [100, 100])
        manager.button.set_size.assert_called_with(expected_size)
        manager.button.move.assert_called_with(QPoint(*expected_pos))
        on_disk = tomllib.loads(path.read_text())
        assert on_disk['FLOATING_BUTTON_SIZE'] == expected_size
        assert on_disk['FLOATING_BUTTON_POS'] == expected_pos
        if not saved:
            assert 'couldn' in manager.settings_status_text.lower()

    def test_settings_ack_notice_error_does_not_block_result(self, manager):
        with patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast', side_effect=RuntimeError('Qt unavailable')):
            manager.send_size_change_command(80)
            command = manager.commands_to_logic_queue.get_nowait()
            self.reply(manager, command['request_id'], True, 80)
        assert not manager.settings_pending

    def test_settings_ack_success_waits_for_new_gesture_to_end(self, manager):
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        manager.button._gesture_running = True
        manager.button.set_size.reset_mock()
        self.reply(manager, command['request_id'], True, 80)
        manager.button.set_size.assert_not_called()
        manager.button._gesture_running = False
        manager._on_gesture_ended()
        manager.button.set_size.assert_called_with(80)

    def test_settings_ack_malformed_reply_cannot_confirm(self, manager):
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        self.reply(manager, command['request_id'], None, 80)
        assert manager.settings_pending

    def test_settings_ack_queue_drop_keeps_older_inflight_request(self, manager):
        manager.send_size_change_command(80)
        old = manager.commands_to_logic_queue.get_nowait()
        with patch.object(manager.commands_to_logic_queue, 'put_nowait', side_effect=Full):
            manager.send_size_change_command(90)
        self.reply(manager, old['request_id'], True, 80)
        manager.button.set_size.assert_called_with(80)

    def test_settings_ack_visibility_result_updates_display(self, manager):
        manager.send_command({'action': 'set_config_value',
                              'key': 'FLOATING_BUTTON_VISIBLE', 'value': False})
        command = manager.commands_to_logic_queue.get_nowait()
        manager.state_from_logic_queue.put_nowait({
            'action': 'config_write_result', 'request_id': command['request_id'],
            'saved': True, 'values': {'FLOATING_BUTTON_VISIBLE': False},
        })
        manager._check_queues_and_events()
        assert manager.button_visible is False

    def test_settings_ack_failure_with_missing_config_uses_display_default(self, manager):
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        self.reply(manager, command['request_id'], False, None)
        manager.button.set_size.assert_called_with(50)

    @pytest.fixture
    def manager(self):
        from queue import Queue
        from gui import GuiManager
        with patch('gui.FloatingButton'), patch('gui.WorkingDialog'), \
             patch('gui.pystray'), patch('gui.QTimer'), \
             patch('gui.send_notice'), \
             patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast'):
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            mgr = GuiManager(shutdown, Queue(), Queue())
            mgr.button._gesture_running = False
            mgr.state_from_logic_queue.put_nowait({
                'action': 'state_update', 'FLOATING_BUTTON_SIZE': 50,
                'FLOATING_BUTTON_POS': [100, 100],
            })
            mgr._check_queues_and_events()
            while not mgr.commands_to_logic_queue.empty():
                mgr.commands_to_logic_queue.get_nowait()
            yield mgr

    def reply(self, manager, request_id, saved, size):
        manager.state_from_logic_queue.put_nowait({
            'action': 'config_write_result', 'request_id': request_id,
            'saved': saved, 'values': {'FLOATING_BUTTON_SIZE': size},
        })
        manager._check_queues_and_events()

    def test_settings_ack_pending_until_reply(self, manager):
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        assert command.get('request_id')
        assert manager.settings_pending
        self.reply(manager, command['request_id'], True, 80)
        assert not manager.settings_pending
        manager.button.set_size.assert_called_with(80)

    def test_settings_ack_failure_reverts_confirmed_geometry(self, manager):
        manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        self.reply(manager, command.get('request_id'), False, 50)
        assert not getattr(manager, 'settings_pending', True)
        manager.button.set_size.assert_called_with(50)
        assert 'couldn' in manager.settings_status_text.lower()

    def test_settings_ack_older_reply_cannot_replace_newer_success(self, manager):
        manager.send_size_change_command(80)
        old = manager.commands_to_logic_queue.get_nowait()
        manager.send_size_change_command(90)
        new = manager.commands_to_logic_queue.get_nowait()
        self.reply(manager, new.get('request_id'), True, 90)
        self.reply(manager, old.get('request_id'), False, 50)
        manager.button.set_size.assert_called_with(90)

    def test_settings_ack_queue_full_reverts_with_notice(self, manager):
        manager.commands_to_logic_queue = MagicMock()
        manager.commands_to_logic_queue.put_nowait.side_effect = Full
        manager.send_size_change_command(80)
        assert not getattr(manager, 'settings_pending', True)
        manager.button.set_size.assert_called_with(50)
        assert 'couldn' in manager.settings_status_text.lower()

    def test_settings_ack_timeout_reads_actual_value_without_retry(self, manager):
        with patch('time.monotonic', return_value=0):
            manager.send_size_change_command(80)
        command = manager.commands_to_logic_queue.get_nowait()
        with patch('time.monotonic', return_value=60):
            manager._check_queues_and_events()
        assert not manager.commands_to_logic_queue.empty()
        query = manager.commands_to_logic_queue.get_nowait()
        assert query['action'] == 'get_config_values'
        assert query['request_id'] == command['request_id']
        assert manager.settings_pending
        manager.state_from_logic_queue.put_nowait({
            'action': 'config_values_result', 'request_id': query['request_id'],
            'values': {'FLOATING_BUTTON_SIZE': 70},
        })
        manager._check_queues_and_events()
        manager.button.set_size.assert_called_with(70)
        assert not manager.settings_pending

    def test_settings_ack_superseded_request_leaves_no_reconciliation_ghost(self, manager):
        """A fully superseded request must leave nothing behind in _settings_requests.

        _send_settings_command strips a superseded request of every key a newer
        request has taken, then deletes the record that is left holding none.
        Without that deletion the emptied record is unreachable but still
        counted, and when its own deadline passes _check_settings_timeout asks
        the logic process to reconcile a request that owns no keys at all.

        The size-only request sent first is the record that must be deleted:
        the group command after it takes both of the keys that request owns.
        The group itself then keeps FLOATING_BUTTON_POS when a later size-only
        command takes FLOATING_BUTTON_SIZE from it, so one live request still
        reaches _check_settings_timeout at t=60. That live request is what
        makes the reconciliation query observable: the single query that run
        emits must carry the surviving key, and the emptied record must not add
        a keyless query of its own ahead of it.
        """
        from PySide6.QtCore import QPoint

        with patch('time.monotonic', return_value=0):
            manager.send_size_change_command(80)
            manager.commands_to_logic_queue.get_nowait()
            # Takes both of the first request's keys, emptying that record.
            manager.send_resize_commit_command(90, QPoint(200, 300))
            group = manager.commands_to_logic_queue.get_nowait()
            # Takes only the group's size, so the group keeps its position.
            manager.send_size_change_command(100)
            newest = manager.commands_to_logic_queue.get_nowait()
            self.reply(manager, newest['request_id'], True, 100)
            assert list(manager._settings_requests) == [group['request_id']]
        # Every deadline is long past by now. The group owns FLOATING_BUTTON_POS
        # and nothing else, so exactly one reconciliation query is due, and a
        # record that survived being emptied would put a keyless query in front
        # of it.
        with patch('time.monotonic', return_value=60):
            manager._check_queues_and_events()
        query = manager.commands_to_logic_queue.get_nowait()
        assert query['action'] == 'get_config_values'
        assert query['request_id'] == group['request_id']
        assert query['keys'] == ['FLOATING_BUTTON_POS']
        assert manager.commands_to_logic_queue.empty()
        assert 'checking the current settings' in manager.settings_status_text.lower()
        manager.state_from_logic_queue.put_nowait({
            'action': 'config_values_result', 'request_id': group['request_id'],
            'values': {'FLOATING_BUTTON_POS': [200, 300]},
        })
        manager._check_queues_and_events()
        assert not manager.settings_pending
        assert manager.settings_status_text == ''
        manager._settings_notice.close.assert_called_once()

    def test_settings_ack_timeout_retries_are_bounded_then_fail(self, manager):
        # wh-codex-merge-audit.4.1.3: three re-arms, then a definite failure.
        clock = {'now': 0.0}
        with patch('time.monotonic', side_effect=lambda: clock['now']):
            manager.send_size_change_command(80)
            command = manager.commands_to_logic_queue.get_nowait()
            request_id = command['request_id']
            for _ in range(3):
                clock['now'] += 5.0
                manager._check_settings_timeout()
                query = manager.commands_to_logic_queue.get_nowait()
                assert query['action'] == 'get_config_values'
                assert query['request_id'] == request_id
            clock['now'] += 5.0
            manager._check_settings_timeout()
            assert not manager.settings_pending
            assert 'couldn' in manager.settings_status_text.lower()
            assert manager.commands_to_logic_queue.empty()
            # The retired request_id is never re-armed again.
            clock['now'] += 5.0
            manager._check_settings_timeout()
            assert manager.commands_to_logic_queue.empty()
        # A late result for the cleared request_id takes the stale-reply path.
        self.reply(manager, request_id, True, 80)
        assert not manager.settings_pending

    def test_settings_ack_timeout_reconcile_send_failure_fails_request(self, manager):
        # wh-codex-merge-audit.4.1.3: an unusable queue ends the wait at once.
        with patch('time.monotonic', return_value=0):
            manager.send_size_change_command(80)
        manager.commands_to_logic_queue.get_nowait()
        with patch('time.monotonic', return_value=60), \
             patch.object(manager.commands_to_logic_queue, 'put_nowait',
                          side_effect=OSError('queue torn down')):
            manager._check_settings_timeout()
        assert not manager.settings_pending
        assert 'couldn' in manager.settings_status_text.lower()

    def test_settings_ack_notice_delivery_failure_is_logged_not_raised(self, manager):
        # wh-codex-merge-audit.4.1.6: notice_text.send_notice does not catch
        # its own delivery failures; every caller must.
        raised = None
        with patch('gui.send_notice', side_effect=RuntimeError('notification area gone')), \
             patch('gui.logger') as log:
            try:
                manager._settings_show_status("Couldn't save settings.", failure=True)
            except Exception as exc:
                raised = exc
            logged = log.exception.called
        assert raised is None, f'send_notice failure escaped _settings_show_status: {raised!r}'
        assert logged, 'the send_notice failure was not logged'
        assert manager.settings_status_text == "Couldn't save settings."

    def test_settings_ack_timeout_error_cannot_drop_the_drain_tick(self, manager):
        # wh-codex-merge-audit.4.1.6: the per-tick timeout check must not cost
        # the tick the messages already waiting in the queue.
        manager.button.set_size.reset_mock()
        manager.state_from_logic_queue.put_nowait({
            'action': 'state_update', 'FLOATING_BUTTON_SIZE': 70,
            'FLOATING_BUTTON_POS': [10, 20],
        })
        raised = None
        with patch.object(manager, '_check_settings_timeout',
                          side_effect=RuntimeError('settings notice backend gone')), \
             patch('gui.logger') as log:
            try:
                manager._check_queues_and_events()
            except Exception as exc:
                raised = exc
            logged = log.exception.called or log.error.called
        assert raised is None, f'the settings timeout check escaped the drain tick: {raised!r}'
        assert logged, 'the settings timeout failure was not logged'
        manager.button.set_size.assert_called_with(70)


class TestSettingsAckNoticeFallback:
    """The send_notice fallback in GuiManager._settings_show_status.

    Nothing else reaches the user when the small notice box cannot render, so
    these tests drive the real utils.notice_text.send_notice and stub only
    plyer, the external notification backend. TestSettingsAcknowledgement
    patches gui.send_notice, which would turn an assertion here into a
    statement about a mock rather than about delivery.
    """

    @pytest.fixture
    def delivered(self):
        from types import SimpleNamespace
        calls = []
        stub = SimpleNamespace(notify=lambda **arguments: calls.append(arguments))
        # send_notice does `from plyer import notification` on every call, so
        # the package attribute is the boundary; the Proxy behind it would
        # otherwise start a real Windows notification thread.
        with patch('plyer.notification', stub):
            yield calls

    @pytest.fixture
    def manager(self, delivered):
        from queue import Queue
        from gui import GuiManager
        with patch('gui.FloatingButton'), patch('gui.WorkingDialog'),              patch('gui.pystray'), patch('gui.QTimer'),              patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast'):
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            mgr = GuiManager(shutdown, Queue(), Queue())
            mgr.button._gesture_running = False
            mgr.state_from_logic_queue.put_nowait({
                'action': 'state_update', 'FLOATING_BUTTON_SIZE': 50,
                'FLOATING_BUTTON_POS': [100, 100],
            })
            mgr._check_queues_and_events()
            while not mgr.commands_to_logic_queue.empty():
                mgr.commands_to_logic_queue.get_nowait()
            del delivered[:]
            yield mgr

    def test_settings_ack_notice_reaches_user_when_box_cannot_render(self, manager, delivered):
        """The Windows notice is the whole of the user's information here.

        The notice box constructor raises, so _settings_show_status leaves
        rendered False and the fallback is the only remaining path. Delivery is
        measured at plyer, past send_notice's own availability checks and text
        fitting, so removing the fallback call leaves the list empty.
        """
        with patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast',
                   side_effect=RuntimeError('Qt unavailable')):
            manager.send_size_change_command(80)
        assert delivered == [{'title': 'WheelHouse settings',
                              'message': 'Saving settings. Waiting for confirmation.',
                              'timeout': 15}]

# wh-pytest-flaky-segfault: many classes here construct GuiManager,
# which builds real Qt widgets; without a QApplication Qt aborts the
# whole interpreter (no traceback, output lost). The session-scoped
# qapp fixture (higher scope, so instantiated before every class or
# function fixture) guarantees one exists even in isolation runs.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


@pytest.fixture
def ptt_pending_manager(_ptt_base_manager, monkeypatch):
    monkeypatch.setattr("gui.send_notice", MagicMock(return_value=True))
    return _ptt_base_manager


def _ptt_reply(manager, command, enabled, reason="", **extra):
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": enabled,
        "ptt_active": True, "ptt_request_id": command.get("request_id"),
        "ptt_refusal_reason": reason, **extra,
    })
    manager._check_queues_and_events()


def test_ptt_pending_then_confirm(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    manager.button.set_ptt_feedback.assert_called_with(
        "pending", "Push to talk requested. Waiting for listening confirmation.")
    assert command.get("request_id")
    _ptt_reply(manager, command, True)
    manager.button.set_state.assert_called_with(True)
    manager.button.set_ptt_feedback.assert_called_with("", "Listening. Release to stop push to talk.")


def test_ptt_pending_then_refuse(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    _ptt_reply(manager, command, False, "Sonos is playing.")
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    manager.button.set_ptt_feedback.assert_called_with("refused", "Sonos is playing.")


def test_ptt_pending_ignores_an_older_hold_reply(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    old = manager.commands_to_logic_queue.get_nowait()
    manager._stop_ptt()
    manager.commands_to_logic_queue.get_nowait()
    manager._start_ptt()
    current = manager.commands_to_logic_queue.get_nowait()
    manager.button.set_state.reset_mock()
    _ptt_reply(manager, old, True)
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    _ptt_reply(manager, current, False, "Sonos is playing.")
    manager.button.set_ptt_feedback.assert_called_with("refused", "Sonos is playing.")


def test_ptt_pending_feedback_is_distinct_and_accessible(qtbot):
    from gui import FloatingButton
    button = FloatingButton()
    qtbot.addWidget(button)

    def painted_color():
        # Inspect the real paint branch's brush without showing a native window.
        with patch("gui.QPainter") as painter:
            button.paintEvent(None)
            return painter.return_value.setBrush.call_args_list[0].args[0].color()

    button.set_indeterminate(False)
    button.set_state(False)
    off = painted_color()
    button.set_state(True)
    red = painted_color()
    button.set_ptt_feedback("pending", "Waiting for listening confirmation.")
    pending = painted_color()
    assert pending not in (off, red)
    assert "Waiting" in button.accessibleDescription()
    button.set_ptt_feedback("refused", "Sonos is playing.")
    refused = painted_color()
    assert refused not in (red, pending)
    assert button.accessibleDescription() == "Sonos is playing."
    assert button.toolTip() == "Sonos is playing."


def _relative_luminance(color):
    """WCAG 2.x relative luminance of an sRGB colour. Alpha plays no part."""
    channels = []
    for value in (color.red(), color.green(), color.blue()):
        srgb = value / 255
        channels.append(srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(first, second):
    """WCAG 2.x contrast ratio between two sRGB colours, lighter over darker."""
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("feedback", ["pending", "refused"])
def test_ptt_pending_glyph_meets_the_non_text_contrast_minimum(qtbot, feedback):
    """The glyph is the only non-colour channel these two fills have.

    A colour-blind or low-vision user reads the state from the glyph, so it
    needs the WCAG 2.x 3.0:1 minimum for a graphical object against the fill
    it is drawn on. Both colours are read back from the real paint branch.
    """
    from PySide6.QtGui import QColor
    from gui import FloatingButton
    button = FloatingButton()
    qtbot.addWidget(button)
    button.set_indeterminate(False)
    button.set_ptt_feedback(feedback, "Waiting for listening confirmation.")
    with patch("gui.QPainter") as painter:
        button.paintEvent(None)
        fill = painter.return_value.setBrush.call_args_list[0].args[0].color()
        glyph = next(call.args[0] for call in painter.return_value.setPen.call_args_list
                     if isinstance(call.args[0], QColor))
    ratio = _contrast_ratio(fill, glyph)
    assert ratio >= 3.0, f"{feedback} glyph {glyph.getRgb()} on {fill.getRgb()} is {ratio:.2f}:1"


def test_ptt_pending_missing_reply_stays_non_recording(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    manager._ptt_ack_timer.start.assert_called_with(5000)
    command = manager.commands_to_logic_queue.get_nowait()
    manager._ptt_pending_timeout()
    manager.button.set_state.assert_called_with(False)
    assert "not been confirmed" in manager.button.set_ptt_feedback.call_args.args[1]
    assert manager.commands_to_logic_queue.qsize() == 1
    assert manager.commands_to_logic_queue.get_nowait() == {"action": "request_initial_state"}
    assert manager.commands_to_logic_queue.empty()  # Read current state, never retry the write.
    _ptt_reply(manager, command, True)
    manager.button.set_state.assert_called_with(True)


def test_ptt_pending_tokenless_state_cannot_confirm_before_timeout(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    manager.commands_to_logic_queue.get_nowait()
    _ptt_reply(manager, {}, True, ptt_active=False)
    manager.button.set_state.assert_called_with(False)
    assert manager._ptt_feedback == "pending"


@pytest.mark.parametrize("released", [False, True])
def test_ptt_pending_timeout_still_ignores_older_hold_reply(ptt_pending_manager, released):
    manager = ptt_pending_manager
    manager._start_ptt()
    old = manager.commands_to_logic_queue.get_nowait()
    manager._stop_ptt()
    manager.commands_to_logic_queue.get_nowait()
    manager._start_ptt()
    current = manager.commands_to_logic_queue.get_nowait()
    manager._ptt_pending_timeout()
    assert manager.commands_to_logic_queue.qsize() == 1
    assert manager.commands_to_logic_queue.get_nowait() == {"action": "request_initial_state"}
    if released:
        manager._stop_ptt()
        assert manager.commands_to_logic_queue.get_nowait()["action"] == "ptt_stop"
    manager.button.set_state.reset_mock()
    _ptt_reply(manager, old, True)
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    _ptt_reply(manager, current, True)
    manager.button.set_state.assert_called_with(not released)
    _ptt_reply(manager, current, False, ptt_active=False)
    manager.button.set_state.assert_called_with(False)


def test_ptt_pending_release_before_reply_never_confirms_the_hold(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    manager._stop_ptt()
    _ptt_reply(manager, command, True)
    assert True not in [c.args[0] for c in manager.button.set_state.call_args_list]
    _ptt_reply(manager, command, False, ptt_active=False)
    manager.button.set_ptt_feedback.assert_called_with("", "Push to talk released.")


def test_ptt_pending_toggle_release_stops_asking_for_a_release(ptt_pending_manager, qtbot):
    """A toggle-mode hold ends with listening still on and nothing left to release."""
    from gui import FloatingButton
    manager = ptt_pending_manager
    button = FloatingButton()
    qtbot.addWidget(button)
    manager.button = button
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    _ptt_reply(manager, command, True)
    assert button.accessibleDescription() == "Listening. Release to stop push to talk."
    manager._stop_ptt()
    # The hold restored the pre-hold setting, so this stopped reply carries
    # speech_enabled True. The description must report that state, not
    # instruct a release that has already happened.
    _ptt_reply(manager, command, True, ptt_active=False)
    assert button.accessibleDescription() == "Listening."
    assert button.toolTip() == "Listening."


def test_ptt_pending_queue_refusal_does_not_mask_later_listening(ptt_pending_manager):
    manager = ptt_pending_manager
    with patch.object(manager.commands_to_logic_queue, "put_nowait", side_effect=Full):
        manager._start_ptt()
    manager.button.set_state.assert_called_with(False)
    assert "queue is full" in manager.button.set_ptt_feedback.call_args.args[1]
    manager._stop_ptt()
    manager.state_from_logic_queue.put_nowait({"action": "state_update", "speech_enabled": True})
    manager._check_queues_and_events()
    manager.button.set_state.assert_called_with(True)


def test_ptt_pending_queue_refusal_survives_ordinary_off_state(ptt_pending_manager, qtbot):
    from gui import FloatingButton
    manager = ptt_pending_manager
    button = FloatingButton()
    qtbot.addWidget(button)
    manager.button = button
    with patch.object(manager.commands_to_logic_queue, "put_nowait", side_effect=Full):
        manager._start_ptt()
    reason = button.accessibleDescription()
    assert "queue is full" in reason
    assert manager._ptt_held is True and manager._ptt_request_id is None
    manager.state_from_logic_queue.put_nowait({"action": "state_update", "speech_enabled": False})
    manager._check_queues_and_events()
    assert button._is_enabled is False and manager._ptt_feedback == "refused"
    assert button.accessibleDescription() == reason
    assert button.toolTip() == reason


def test_ptt_pending_repeated_refusal_shows_one_notice(ptt_pending_manager, monkeypatch):
    """Logic repeats the same refused state every three seconds; the toast must not.

    Without the de-duplication a hold refused because Sonos is playing raises a
    fresh Windows toast every three seconds until the safety cutoff.
    """
    notice = MagicMock(return_value=True)
    monkeypatch.setattr("gui.send_notice", notice)
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    _ptt_reply(manager, command, False, "Sonos is playing.")
    _ptt_reply(manager, command, False, "Sonos is playing.")
    # Both updates reached the feedback writer, so one notice is de-duplication
    # rather than a second update that never arrived.
    assert [c.args for c in manager.button.set_ptt_feedback.call_args_list].count(
        ("refused", "Sonos is playing.")) == 2
    assert notice.call_count == 1


def test_ptt_pending_notice_failure_does_not_stop_the_state_update(ptt_pending_manager, monkeypatch):
    """An unavailable notice service costs the toast, never the rest of the update."""
    monkeypatch.setattr("gui.send_notice", MagicMock(side_effect=OSError("no notice service")))
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    manager.button.set_state.reset_mock()
    _ptt_reply(manager, command, False, "Sonos is playing.")
    manager.button.set_ptt_feedback.assert_called_with("refused", "Sonos is playing.")
    # update_ui_state runs after _handle_ptt_state. An escaping notice failure
    # would skip it, and the button would keep the appearance it had.
    manager.button.set_state.assert_called_with(False)


@pytest.mark.parametrize("speech_restored", [False, True])
def test_ptt_pending_hold_ended_underneath_the_user_asks_for_a_new_hold(ptt_pending_manager, speech_restored):
    """The safety cutoff ends a hold the user is still holding.

    Releasing changes nothing then, so the button must report the ended hold
    instead of a release the user has not made. The cutoff restores the
    pre-hold setting, which is on in toggle mode and off in push-to-talk.

    Restored ON means the engine is listening. The appearance must then stay
    listening and only the text may carry the ended hold: "refused" paints the
    purple fill documented as "not listening", and update_ui_state turns the
    button off for it, so routing this cell there claims an off microphone
    while the engine transcribes -- the failure
    wh-ptt-release-disables-speech.1.5 ruled against and
    wh-codex-merge-audit.7.1.1 found reintroduced.
    """
    import gui
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    _ptt_reply(manager, command, True)
    assert manager._ptt_held is True
    _ptt_reply(manager, command, speech_restored, ptt_active=False)
    if speech_restored:
        manager.button.set_ptt_feedback.assert_called_with("", "Listening. Push to talk ended.")
        # Every channel must agree with the text. A "refused" feedback would
        # force set_state(False) in update_ui_state and raise a toast.
        manager.button.set_state.assert_called_with(True)
        gui.send_notice.assert_not_called()
    else:
        manager.button.set_ptt_feedback.assert_called_with(
            "refused", "Push to talk ended. Release and hold again to listen.")


def test_ptt_pending_safety_end_does_not_mask_later_listening(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._start_ptt()
    command = manager.commands_to_logic_queue.get_nowait()
    _ptt_reply(manager, command, False, ptt_active=False)
    manager._stop_ptt()
    manager.state_from_logic_queue.put_nowait({"action": "state_update", "speech_enabled": True})
    manager._check_queues_and_events()
    manager.button.set_state.assert_called_with(True)


def test_ptt_pending_shutdown_stops_notice_timer(ptt_pending_manager):
    manager = ptt_pending_manager
    manager._ptt_ack_timer = MagicMock()  # Independent of the mocked queue timer.
    manager._start_ptt()
    manager.shutdown_event.set()
    manager.button.set_ptt_feedback.reset_mock()
    manager._ptt_pending_timeout()
    manager.button.set_ptt_feedback.assert_not_called()
    with patch("gui.QApplication"):
        manager._shutdown_gui()
    manager._ptt_ack_timer.stop.assert_called_once()


# -----------------------------------------------------------------------
# create_icon_image
# -----------------------------------------------------------------------

class TestCreateIconImage:

    def test_creates_rgba_image_with_correct_size(self):
        from gui import create_icon_image
        img = create_icon_image((200, 0, 0))
        assert img.size == (64, 64)
        assert img.mode == "RGBA"

    def test_different_colors_produce_different_images(self):
        from gui import create_icon_image
        red = create_icon_image((200, 0, 0))
        green = create_icon_image((0, 200, 0))
        # The pixel data should differ
        assert red.tobytes() != green.tobytes()

    def test_transparent_background(self):
        from gui import create_icon_image
        img = create_icon_image((200, 0, 0))
        # Corner pixel should be transparent (outside the ellipse)
        corner = img.getpixel((0, 0))
        assert corner[3] == 0  # Alpha channel is 0


# -----------------------------------------------------------------------
# GuiManager._get_provider_display_name
# -----------------------------------------------------------------------

class TestGetProviderDisplayName:

    @pytest.fixture
    def manager(self):
        """Create a GuiManager with mocked dependencies."""
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            cmds_q = MagicMock()
            state_q = MagicMock()
            mgr = GuiManager(shutdown, cmds_q, state_q)
            return mgr

    def test_returns_dynamic_display_name(self, manager):
        manager.stt_provider_display_names = {"my_provider": "My Custom STT"}
        assert manager._get_provider_display_name("my_provider") == "My Custom STT"

    def test_falls_back_to_hardcoded_names(self, manager):
        manager.stt_provider_display_names = {}
        assert manager._get_provider_display_name("google_remote") == "Google Cloud (WebSocket)"
        assert manager._get_provider_display_name("google") == "Google Cloud"

    def test_falls_back_to_title_case(self, manager):
        manager.stt_provider_display_names = {}
        result = manager._get_provider_display_name("some_new_provider")
        assert result == "Some New Provider"

    def test_dynamic_names_take_priority_over_hardcoded(self, manager):
        manager.stt_provider_display_names = {"google": "My Google Override"}
        assert manager._get_provider_display_name("google") == "My Google Override"


# -----------------------------------------------------------------------
# GuiManager._is_provider_checked
# -----------------------------------------------------------------------

class TestIsProviderChecked:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            return mgr

    def test_returns_true_when_matching(self, manager):
        assert manager._is_provider_checked("google", "google") is True

    def test_returns_false_when_not_matching(self, manager):
        assert manager._is_provider_checked("google", "google_remote") is False


# -----------------------------------------------------------------------
# GuiManager.send_command
# -----------------------------------------------------------------------

class TestSendCommand:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            cmds_q = MagicMock()
            mgr = GuiManager(MagicMock(), cmds_q, MagicMock())
            return mgr

    def test_puts_command_on_queue(self, manager):
        cmd = {'action': 'test_action'}
        manager.send_command(cmd)
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(cmd)

    def test_handles_full_queue_gracefully(self, manager):
        manager.commands_to_logic_queue.put_nowait.side_effect = Full()
        # Should not raise
        manager.send_command({'action': 'test'})


# -----------------------------------------------------------------------
# GuiManager.send_toggle_speech_command
# -----------------------------------------------------------------------

class TestSendToggleSpeechCommand:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            return mgr

    def test_does_nothing_before_initial_state(self, manager):
        manager.initial_state_received = False
        manager.send_toggle_speech_command()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_sends_toggle_after_initial_state(self, manager):
        manager.initial_state_received = True
        manager.send_toggle_speech_command()
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(
            {'action': 'toggle_speech_enabled_state'}
        )


# -----------------------------------------------------------------------
# GuiManager._check_queues_and_events - state message processing
# -----------------------------------------------------------------------

class TestCheckQueuesAndEvents:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton") as mock_button_cls, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"), \
             patch("gui.QPoint") as mock_qpoint:
            mock_button = MagicMock()
            mock_button_cls.return_value = mock_button
            mock_pystray.Icon.return_value = MagicMock()
            mock_qpoint.side_effect = lambda *args: MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            state_q = MagicMock()
            mgr = GuiManager(shutdown, MagicMock(), state_q)
            mgr.button = mock_button
            # A plain MagicMock reports every attribute as truthy, so without
            # this the stand-in button claims to be mid-gesture and tests read
            # as passing for the wrong reason. Tests that want a gesture
            # running set these back to True themselves.
            mock_button._is_resizing = False
            mock_button._is_dragging = False
            mock_button._gesture_running = False
            return mgr

    def test_processes_initial_state(self, manager):
        msg = {
            'action': 'initial_state',
            'speech_enabled': True,
            'button_visible': True,
            'FLOATING_BUTTON_SIZE': 60,
            'FLOATING_BUTTON_POS': [200, 300],
            'stt_provider': 'google',
            'stt_providers_available': ['google', 'google_remote'],
            'stt_provider_display_names': {'google': 'Google Cloud'},
            'interim_results_enabled': False,
            'SHOW_SPEECH_PULSE': True,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        assert manager.initial_state_received is True
        assert manager.speech_enabled is True
        assert manager.stt_provider == 'google'
        assert manager.stt_providers_available == ['google', 'google_remote']
        assert manager.interim_results_enabled is False
        manager.button.set_indeterminate.assert_called_with(False)
        manager.button.set_size.assert_called_with(60)

    def test_processes_state_update(self, manager):
        manager.initial_state_received = True  # Already initialized
        msg = {
            'action': 'state_update',
            'speech_enabled': False,
            'button_visible': False,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        assert manager.speech_enabled is False
        assert manager.button_visible is False
        # set_indeterminate(False) should NOT be called again
        manager.button.set_indeterminate.assert_not_called()

    def test_a_state_update_during_a_resize_leaves_the_size_alone(self, manager):
        """The stored size is the one from before the drag started.

        Size and position are saved when the drag ends, not while it runs, so
        during a drag the settings still hold the old geometry. An unrelated
        message from the Logic process -- speech being suppressed, a provider
        reporting in, a push-to-talk correction -- would put that old geometry
        back on screen while the user is still dragging.
        """
        manager.initial_state_received = True
        manager.button._is_resizing = True
        manager.button._gesture_running = True
        manager.button._is_dragging = False
        msg = {
            'action': 'state_update',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': 50,
            'FLOATING_BUTTON_POS': [100, 100],
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        manager.button.set_size.assert_not_called()
        manager.button.move.assert_not_called()

    def test_a_state_update_during_a_move_leaves_the_position_alone(self, manager):
        """A move drag has the same problem: the stored position is stale."""
        manager.initial_state_received = True
        manager.button._is_resizing = False
        manager.button._is_dragging = True
        manager.button._gesture_running = True
        msg = {
            'action': 'state_update',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': 50,
            'FLOATING_BUTTON_POS': [100, 100],
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        manager.button.set_size.assert_not_called()
        manager.button.move.assert_not_called()

    def test_a_state_update_with_no_gesture_running_does_apply(self, manager):
        """Skipping is only for a gesture in progress, not in general."""
        manager.initial_state_received = True
        manager.button._is_resizing = False
        manager.button._is_dragging = False
        msg = {
            'action': 'state_update',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': 70,
            'FLOATING_BUTTON_POS': [100, 100],
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        manager.button.set_size.assert_called_with(70)
        manager.button.move.assert_called()

    def test_the_first_state_lets_the_button_accept_gestures(self, manager):
        """The button is on screen before its stored geometry arrives.

        start() shows the button and only afterwards asks the Logic process
        for the stored state. A gesture begun in that gap measures against the
        default size and place, and the arriving state then replaces both
        underneath it. So the button refuses gestures until this point.
        """
        manager.initial_state_received = False
        manager.button._is_resizing = False
        manager.button._is_dragging = False
        msg = {
            'action': 'initial_state',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': 70,
            'FLOATING_BUTTON_POS': [100, 100],
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

        manager.button.set_size.assert_called_with(70)
        manager.button.set_ready_for_gestures.assert_called_with(True)

    def _state_message(self, size, pos):
        return {
            'action': 'state_update',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': size,
            'FLOATING_BUTTON_POS': pos,
        }

    def _deliver(self, manager, msg):
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

    def test_geometry_held_back_during_a_gesture_is_applied_when_it_ends(self, manager):
        """Skipping a change is not the same as discarding it.

        A state update that arrives mid-gesture carries a real change often
        enough -- the settings window, another machine's copy of the file, a
        reset. Dropping it left the button at a size no later message would
        ever correct, because the Logic process only sends geometry it already
        believes the button has.
        """
        manager.initial_state_received = True
        manager.button._is_resizing = True
        manager.button._gesture_running = True
        manager.button._is_dragging = False
        self._deliver(manager, self._state_message(70, [100, 100]))
        manager.button.set_size.assert_not_called()

        manager.button._is_resizing = False
        manager._on_gesture_ended()

        manager.button.set_size.assert_called_with(70)
        manager.button.move.assert_called()

    def test_only_the_newest_held_back_geometry_is_applied(self, manager):
        manager.initial_state_received = True
        manager.button._is_resizing = True
        manager.button._gesture_running = True
        manager.button._is_dragging = False
        self._deliver(manager, self._state_message(70, [100, 100]))
        self._deliver(manager, self._state_message(90, [200, 200]))

        manager.button._is_resizing = False
        manager._on_gesture_ended()

        manager.button.set_size.assert_called_once_with(90)

    def test_the_manager_listens_for_the_end_of_a_gesture(self, manager):
        """Every other test here calls the handler directly.

        Without this one the handler could be correct and never run, and the
        held-back geometry would sit there until the next message that arrives
        with no gesture in progress.
        """
        manager.button.gesture_ended.connect.assert_called_once_with(
            manager._on_gesture_ended
        )

    def test_a_gesture_ending_with_nothing_held_back_changes_nothing(self, manager):
        manager.initial_state_received = True

        manager._on_gesture_ended()

        manager.button.set_size.assert_not_called()
        manager.button.move.assert_not_called()

    def test_held_back_geometry_is_applied_only_once(self, manager):
        manager.initial_state_received = True
        manager.button._is_resizing = True
        manager.button._gesture_running = True
        manager.button._is_dragging = False
        self._deliver(manager, self._state_message(70, [100, 100]))

        manager.button._is_resizing = False
        manager._on_gesture_ended()
        manager.button.set_size.reset_mock()
        manager._on_gesture_ended()

        manager.button.set_size.assert_not_called()

    def test_the_size_the_user_dragged_to_beats_the_held_back_size(self, manager):
        """The gesture's own result is newer than anything held back.

        The resize is reported before the gesture is reported as over, so
        applying the held-back geometry afterwards would undo, on screen, the
        size the user just dragged to.
        """
        manager.initial_state_received = True
        manager.button._is_resizing = True
        manager.button._gesture_running = True
        manager.button._is_dragging = False
        self._deliver(manager, self._state_message(70, [100, 100]))

        manager.button._is_resizing = False
        manager.send_resize_commit_command(90, MagicMock(x=lambda: 5, y=lambda: 6))
        manager._on_gesture_ended()

        manager.button.set_size.assert_not_called()

    def test_the_place_the_user_dragged_to_beats_the_held_back_position(self, manager):
        manager.initial_state_received = True
        manager.button._is_resizing = False
        manager.button._is_dragging = True
        manager.button._gesture_running = True
        self._deliver(manager, self._state_message(70, [100, 100]))

        manager.button._is_dragging = False
        manager.send_pos_change_command(MagicMock(x=lambda: 5, y=lambda: 6))
        manager._on_gesture_ended()

        manager.button.move.assert_not_called()

    def test_handles_empty_queue(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = Empty()
        # Should not raise
        manager._check_queues_and_events()

    def test_shutdown_event_triggers_shutdown(self, manager):
        manager.shutdown_event.is_set.return_value = True
        with patch.object(manager, '_shutdown_gui') as mock_shutdown:
            manager._check_queues_and_events()
            mock_shutdown.assert_called_once()

    def test_show_notification_action(self, manager):
        msg = {
            'action': 'show_notification',
            'title': 'Test Title',
            'message': 'Test message',
            'timeout': 3,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch("plyer.notification") as mock_notification:
            mock_notification.notify = MagicMock()
            manager._check_queues_and_events()
            mock_notification.notify.assert_called_once_with(
                title='Test Title',
                message='Test message',
                timeout=3,
            )

    def test_click_first_use_hint_action_routes_to_renderer(self, manager):
        # wh-9f3t.60.3 / wh-r3xy1: an action=="click_first_use_hint" message
        # must route to _show_first_use_hint, which surfaces the verbatim
        # wording through the OS info-notice path.
        msg = {
            'action': 'click_first_use_hint',
            'message': 'Wheelhouse can speed up clicks in this app...',
            'trace_id': 'trace-x',
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, '_show_first_use_hint') as mock_render:
            manager._check_queues_and_events()
            mock_render.assert_called_once_with(msg)

    # --- The dialog messages carry the launch they belong to -----------

    def test_a_show_working_message_carries_its_launch_to_the_dialog(self, manager):
        msg = {'action': 'show_working', 'message': 'Loading Parakeet', 'owner': 'stt:7'}
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        manager._check_queues_and_events()
        manager.working_dialog.show_working.assert_called_once_with('Loading Parakeet', 'stt:7')

    def test_a_hide_working_message_carries_its_launch_to_the_dialog(self, manager):
        msg = {'action': 'hide_working', 'owner': 'stt:7'}
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        manager._check_queues_and_events()
        manager.working_dialog.hide_working.assert_called_once_with('stt:7')

    def test_a_show_working_message_without_a_launch_names_none(self, manager):
        # The dispatch contract for a message with no "owner" key, not
        # a claim about who sends one: every shipped sender now names
        # its operation. What this pins is that an absent key reaches
        # the dialog as None -- an ordinary claim that matches only an
        # unowned dialog -- rather than raising a KeyError in the queue
        # reader (wh-launch-addressed-notices, wh-dialog-ownership-token).
        msg = {'action': 'show_working', 'message': 'Asking'}
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        manager._check_queues_and_events()
        manager.working_dialog.show_working.assert_called_once_with('Asking', None)

    def test_a_hide_working_message_without_a_launch_names_none(self, manager):
        msg = {'action': 'hide_working'}
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        manager._check_queues_and_events()
        manager.working_dialog.hide_working.assert_called_once_with(None)


# -----------------------------------------------------------------------
# GuiManager._show_first_use_hint render path (wh-9f3t.60.3 / wh-r3xy1)
# -----------------------------------------------------------------------
#
# Driven on a bare GuiManager (object.__new__) so the test does not run the
# crash-prone full constructor / dispatch loop in this worktree; the bound
# _show_first_use_hint method is exercised directly. The dispatch ROUTING is
# covered by test_click_first_use_hint_action_routes_to_renderer above; the
# Logic side (which emits the action) is covered in test_click_first_use_hint.


class TestShowFirstUseHint:
    def _bare_manager(self):
        from gui import GuiManager
        return GuiManager.__new__(GuiManager)

    def test_surfaces_message_verbatim_via_notification(self):
        manager = self._bare_manager()
        with patch("plyer.notification") as mock_notification:
            mock_notification.notify = MagicMock()
            manager._show_first_use_hint({
                'action': 'click_first_use_hint',
                'message': 'hint body text',
                'trace_id': 'trace-y',
            })
            mock_notification.notify.assert_called_once_with(
                title='Wheelhouse',
                message='hint body text',
                timeout=8,
            )

    def test_empty_message_does_not_notify(self):
        manager = self._bare_manager()
        with patch("plyer.notification") as mock_notification:
            mock_notification.notify = MagicMock()
            manager._show_first_use_hint({'action': 'click_first_use_hint'})
            mock_notification.notify.assert_not_called()

    def test_bad_payload_does_not_raise(self):
        manager = self._bare_manager()
        with patch("plyer.notification") as mock_notification:
            mock_notification.notify = MagicMock()
            # A non-dict slips past schema; the renderer must swallow it.
            manager._show_first_use_hint(None)  # type: ignore[arg-type]


# -----------------------------------------------------------------------
# GuiManager._check_activity_shm
# -----------------------------------------------------------------------

class TestCheckActivityShm:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton") as mock_button_cls, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_button = MagicMock()
            mock_button_cls.return_value = mock_button
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            mgr.button = mock_button
            return mgr

    def test_noop_when_shm_is_none(self, manager):
        manager._gui_shm = None
        manager._check_activity_shm()
        # Should not crash or call button methods
        manager.button.set_activity_state.assert_not_called()

    def test_reads_hearing_state(self, manager):
        data = json.dumps({'state': 'hearing', 'utterance_id': 1}).encode('utf-8')
        buf = struct.pack('>I', len(data)) + data
        buf = buf + b'\x00' * (256 - len(buf))  # Pad buffer
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm
        manager.show_speech_pulse = True

        manager._check_activity_shm()

        manager.button.set_activity_state.assert_called_with('hearing')
        assert manager._last_activity_state == 'hearing'
        assert manager._last_activity_utterance_id == 1

    def test_skips_duplicate_state(self, manager):
        data = json.dumps({'state': 'hearing', 'utterance_id': 1}).encode('utf-8')
        buf = struct.pack('>I', len(data)) + data
        buf = buf + b'\x00' * (256 - len(buf))
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm
        manager.show_speech_pulse = True
        manager._last_activity_state = 'hearing'
        manager._last_activity_utterance_id = 1

        manager._check_activity_shm()

        # No change, should not call set_activity_state
        manager.button.set_activity_state.assert_not_called()

    def test_hearing_suppressed_when_pulse_disabled(self, manager):
        data = json.dumps({'state': 'hearing', 'utterance_id': 1}).encode('utf-8')
        buf = struct.pack('>I', len(data)) + data
        buf = buf + b'\x00' * (256 - len(buf))
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm
        manager.show_speech_pulse = False

        manager._check_activity_shm()

        # When pulse disabled, hearing becomes idle
        manager.button.set_activity_state.assert_called_with('idle')

    def test_confirmed_shown_even_when_pulse_disabled(self, manager):
        data = json.dumps({'state': 'confirmed', 'utterance_id': 1}).encode('utf-8')
        buf = struct.pack('>I', len(data)) + data
        buf = buf + b'\x00' * (256 - len(buf))
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm
        manager.show_speech_pulse = False

        manager._check_activity_shm()

        # Confirmed is always shown, even with pulse disabled
        manager.button.set_activity_state.assert_called_with('confirmed')

    def test_invalid_size_skipped(self, manager):
        # Size > 200 is treated as invalid
        buf = struct.pack('>I', 999) + b'\x00' * 252
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm

        manager._check_activity_shm()

        manager.button.set_activity_state.assert_not_called()

    def test_zero_size_skipped(self, manager):
        buf = struct.pack('>I', 0) + b'\x00' * 252
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm

        manager._check_activity_shm()

        manager.button.set_activity_state.assert_not_called()


# -----------------------------------------------------------------------
# GuiManager.update_tray_menu - color mapping
# -----------------------------------------------------------------------

class TestUpdateTrayMenu:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_icon = MagicMock()
            mock_pystray.Icon.return_value = mock_icon
            mock_pystray.MenuItem = MagicMock
            mock_pystray.Menu = MagicMock
            mock_pystray.Menu.SEPARATOR = "---"
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            mgr.icon = mock_icon
            return mgr

    def test_updating_the_tray_menu_never_touches_the_icon(self, manager):
        """The user asked for one unchanging picture in the notification area.

        It used to be a coloured circle: grey before startup finished, red
        while speech was on, blue in push-to-talk, grey again when off. All of
        that is gone. The menu still rebuilds, because its checkmarks do
        follow the state.
        """
        manager.icon.icon = "the wheelhouse icon"

        for received, enabled, mode in [
            (False, False, "toggle"),
            (True, True, "toggle"),
            (True, False, "toggle"),
            (True, False, "push_to_talk"),
        ]:
            manager.initial_state_received = received
            manager.speech_enabled = enabled
            manager.speech_interaction_mode = mode
            manager.update_tray_menu()

            assert manager.icon.icon == "the wheelhouse icon"

    def test_updating_the_tray_menu_still_rebuilds_the_menu(self, manager):
        """The checkmarks in the menu do follow the state, unlike the icon."""
        manager.icon.menu = None

        manager.update_tray_menu()

        assert manager.icon.menu is not None


class TestTheTrayIconPicture:

    def test_the_tray_icon_is_the_wheelhouse_icon_file(self):
        """Not a drawn shape. The bytes must come from the shipped file."""
        from pathlib import Path

        from PIL import Image

        import gui

        expected = Image.open(
            Path(gui.__file__).parent / "WheelHouse.ico"
        ).convert("RGBA").resize((64, 64), Image.LANCZOS)

        loaded = gui.load_tray_icon()

        assert loaded.tobytes() == expected.tobytes()

    def test_an_unreadable_icon_file_still_produces_an_icon(self):
        """A tray with no icon at all is worse than a plain shape.

        The file sits beside gui.py in a source checkout and inside the bundle
        in a build. If a build ever stops shipping it, the notification area
        should still show something the user can right-click.
        """
        import gui

        with patch("gui.Image.open", side_effect=OSError("no such file")):
            loaded = gui.load_tray_icon()

        assert loaded is not None
        assert loaded.size == (64, 64)

    def test_the_icon_is_handed_to_the_tray_when_it_is_created(self):
        """Set once, at creation. Nothing sets it again afterwards."""
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"), \
             patch("gui.load_tray_icon") as mock_load:
            mock_load.return_value = "the wheelhouse icon"
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            GuiManager(MagicMock(), MagicMock(), MagicMock())

            _, kwargs = mock_pystray.Icon.call_args
            assert kwargs["icon"] == "the wheelhouse icon"


# -----------------------------------------------------------------------
# GuiManager.exit_app
# -----------------------------------------------------------------------

class TestExitApp:

    def test_sets_shutdown_event(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            mgr = GuiManager(shutdown, MagicMock(), MagicMock())
            mgr.exit_app()
            shutdown.set.assert_called_once()


# -----------------------------------------------------------------------
# Adversarial: malformed shared memory data
# -----------------------------------------------------------------------

class TestAdversarialShm:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton") as mock_button_cls, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_button = MagicMock()
            mock_button_cls.return_value = mock_button
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            mgr.button = mock_button
            return mgr

    def test_corrupted_json_in_shm_silently_ignored(self, manager):
        """Invalid JSON in shared memory should not crash the GUI."""
        bad_data = b"not valid json at all"
        buf = struct.pack('>I', len(bad_data)) + bad_data
        buf = buf + b'\x00' * (256 - len(buf))
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm

        # Should not raise
        manager._check_activity_shm()
        manager.button.set_activity_state.assert_not_called()

    def test_truncated_buffer_silently_ignored(self, manager):
        """Buffer shorter than 4 bytes should not crash."""
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(b'\x00\x00')  # Too short
        manager._gui_shm = mock_shm

        # Should not raise (struct.unpack will fail, caught by except)
        manager._check_activity_shm()
        manager.button.set_activity_state.assert_not_called()

    def test_missing_state_key_defaults_to_idle(self, manager):
        """JSON without 'state' key should default to 'idle'."""
        data = json.dumps({'utterance_id': 5}).encode('utf-8')
        buf = struct.pack('>I', len(data)) + data
        buf = buf + b'\x00' * (256 - len(buf))
        mock_shm = MagicMock()
        mock_shm.buf = bytearray(buf)
        manager._gui_shm = mock_shm
        manager.show_speech_pulse = True

        manager._check_activity_shm()

        manager.button.set_activity_state.assert_called_with('idle')


# -----------------------------------------------------------------------
# GuiManager command helpers
# -----------------------------------------------------------------------

class TestCommandHelpers:

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            return mgr

    def test_toggle_button_visibility_sends_command(self, manager):
        manager.toggle_button_visibility()
        command = manager.commands_to_logic_queue.put_nowait.call_args.args[0]
        assert command['action'] == 'set_config_value'
        assert command['key'] == 'FLOATING_BUTTON_VISIBLE'
        assert command['value'] is False
        assert command['request_id'] in manager._settings_requests

    def test_toggle_interim_results_sends_command(self, manager):
        manager.toggle_interim_results()
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'toggle_interim_results'}
        )

    def test_switch_stt_provider_sends_command(self, manager):
        manager.switch_stt_provider("google_remote")
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'switch_stt_provider', 'provider': 'google_remote'}
        )

    def test_send_size_change_command(self, manager):
        with patch('soft_allow_write_failed_toast.SoftAllowWriteFailedToast'):
            manager.send_size_change_command(75)
        command = dict(manager.commands_to_logic_queue.put_nowait.call_args.args[0])
        assert command.pop('request_id')
        assert command == {'action': 'set_config_value', 'key': 'FLOATING_BUTTON_SIZE', 'value': 75}


# -----------------------------------------------------------------------
# WorkingDialog
# -----------------------------------------------------------------------

class TestWorkingDialog:
    """Tests for WorkingDialog - the 'working' indicator shown during long ops."""

    @pytest.fixture
    def dialog(self, qapp):
        """Create a WorkingDialog instance for testing."""
        from gui import WorkingDialog
        dlg = WorkingDialog()
        yield dlg
        dlg.close()

    def test_show_working_makes_dialog_visible(self, dialog):
        dialog.show_working("Loading speech recognition provider")
        assert dialog.isVisible()

    def test_show_working_sets_message_text(self, dialog):
        dialog.show_working("Loading speech recognition provider")
        assert "Loading speech recognition provider" in dialog._message_label.text()

    def test_hide_working_hides_dialog(self, dialog):
        dialog.show_working("Loading speech recognition provider")
        dialog.hide_working()
        assert not dialog.isVisible()

    def test_show_working_while_visible_updates_message(self, dialog):
        dialog.show_working("Loading speech recognition provider")
        dialog.show_working("Switching speech provider")
        assert "Switching speech provider" in dialog._message_label.text()
        assert dialog.isVisible()

    def test_dot_animation_cycles(self, dialog):
        dialog.show_working("Loading")
        # Initial state: no dots
        assert dialog._dot_count == 0
        # Simulate timer ticks
        dialog._animate_dots()
        assert dialog._dot_count == 1
        assert dialog._message_label.text() == "Loading."
        dialog._animate_dots()
        assert dialog._dot_count == 2
        assert dialog._message_label.text() == "Loading.."
        dialog._animate_dots()
        assert dialog._dot_count == 3
        assert dialog._message_label.text() == "Loading..."
        dialog._animate_dots()
        assert dialog._dot_count == 0
        assert dialog._message_label.text() == "Loading"

    def test_hide_working_stops_animation_timer(self, dialog):
        dialog.show_working("Loading")
        assert dialog._dot_timer.isActive()
        dialog.hide_working()
        assert not dialog._dot_timer.isActive()

    def test_hide_working_when_not_visible_is_safe(self, dialog):
        # Should not raise
        dialog.hide_working()
        assert not dialog.isVisible()

    # --- The dialog carries the launch it belongs to -------------------
    #
    # Every provider shares this one dialog, so a monitor for a launch
    # the user has already replaced could dismiss the replacement's
    # loading display. The launcher cannot close that window itself: it
    # reads "am I still current" and acts afterwards, and the switch can
    # land in between (wh-launch-generation.2.9). The decision moves
    # here because the owner comparison ends in the same state whichever
    # order the two messages arrive in. Racing producer threads make
    # both orders reachable, and the first two tests below pin one order
    # each (wh-launch-addressed-notices).

    def test_a_dismiss_addressed_to_a_replaced_launch_is_dropped(self, dialog):
        dialog.show_working("Loading Google Cloud STT", "stt:1")
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working("stt:1")
        assert dialog.isVisible()
        assert "Loading Parakeet" in dialog._message_label.text()

    def test_a_dismiss_that_arrives_before_the_replacement_show_still_ends_right(
        self, dialog
    ):
        # The other reachable order. The replaced launch's monitor can
        # complete its put before the thread starting the replacement
        # stamps and puts its show, so the dismiss arrives first. It
        # applies -- the old launch does still own the dialog -- and the
        # show then raises it for the new launch.
        dialog.show_working("Loading Google Cloud STT", "stt:1")
        dialog.hide_working("stt:1")
        dialog.show_working("Loading Parakeet", "stt:2")
        assert dialog.isVisible()
        assert "Loading Parakeet" in dialog._message_label.text()
        assert dialog._owner == "stt:2"

    def test_a_dropped_dismiss_leaves_the_animation_running(self, dialog):
        dialog.show_working("Loading Google Cloud STT", "stt:1")
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working("stt:1")
        assert dialog._dot_timer.isActive()

    def test_a_dismiss_from_the_launch_that_owns_the_dialog_applies(self, dialog):
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working("stt:2")
        assert not dialog.isVisible()

    # --- Ownership is required, not merely honoured when both name it --
    #
    # The pair that used to stand here asserted the opposite: that a
    # dismiss naming no operation closed whatever was up, and that a
    # dismiss naming one closed a dialog nobody owned. That is the
    # defect. An AI request finishing would close the "Loading
    # <engine>" dialog of a provider switch started after it, and the
    # engine went on starting with nothing on screen to say so
    # (wh-dialog-ownership-token, and wh-launch-addressed-notices.2.1
    # where Codex first reported it).

    def test_a_dismiss_naming_no_operation_leaves_an_owned_dialog_up(
        self, dialog
    ):
        # The reported defect, in the smallest form that shows it: the
        # AI request's close carries nothing, the provider start owns
        # the dialog, and the plaque must stay up.
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working()
        assert dialog.isVisible()
        assert "Loading Parakeet" in dialog._message_label.text()

    def test_a_dismiss_naming_another_operation_leaves_the_dialog_up(
        self, dialog
    ):
        # The same rule seen from the other side. "ai:1" is not the
        # owner, so it may not take the dialog down, and the source
        # prefixes are what stop AI request 2 and launch 2 colliding.
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working("ai:1")
        assert dialog.isVisible()

    def test_a_dismiss_naming_no_operation_still_closes_an_unowned_dialog(
        self, dialog
    ):
        # An operation that names no owner still ends its own dialog.
        # Both sides are None, so they match.
        dialog.show_working("Rewriting...")
        dialog.hide_working()
        assert not dialog.isVisible()

    def test_a_dropped_dismiss_from_another_operation_leaves_the_animation(
        self, dialog
    ):
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working()
        assert dialog._dot_timer.isActive()

    def test_the_owner_does_not_outlive_the_dialog_it_owned(self, dialog):
        # A launch that hides its own dialog must not leave its token
        # behind: the next unowned show would otherwise inherit it and
        # its own close, which names nothing, would be dropped against a
        # dialog it does raise.
        dialog.show_working("Loading Parakeet", "stt:2")
        dialog.hide_working("stt:2")
        dialog.show_working("Rewriting...")
        dialog.hide_working()
        assert not dialog.isVisible()

    # --- The startup plaque is a placeholder, not an operation ---------
    #
    # gui.py raises "Starting" before anything else exists, and what
    # ends startup does not always know a launch token: the WebSocket
    # ready path sends `_stt_client_generations.get(websocket)`, which
    # is None whenever no launch stamped that connection -- there is no
    # launcher at all, or the counter has not moved yet. Under a plain
    # equality rule that dismiss would be dropped and the plaque would
    # stay on screen for the rest of the session. Nothing can outlive
    # startup, so any dismiss ends it.

    def test_a_dismiss_naming_no_operation_closes_the_startup_plaque(
        self, dialog
    ):
        from services.wheelhouse.shared.dialog_owner import STARTUP_OWNER

        dialog.show_working("Starting", STARTUP_OWNER)
        dialog.hide_working()
        assert not dialog.isVisible()

    def test_a_launchs_dismiss_closes_the_startup_plaque(self, dialog):
        from services.wheelhouse.shared.dialog_owner import STARTUP_OWNER

        dialog.show_working("Starting", STARTUP_OWNER)
        dialog.hide_working("stt:1")
        assert not dialog.isVisible()

    def test_a_real_operation_that_takes_the_plaque_is_then_owned(
        self, dialog
    ):
        # The placeholder rule must not leak: once a launch has claimed
        # the dialog, a dismiss naming nothing is dropped again.
        from services.wheelhouse.shared.dialog_owner import STARTUP_OWNER

        dialog.show_working("Starting", STARTUP_OWNER)
        dialog.show_working("Loading Parakeet", "stt:1")
        dialog.hide_working()
        assert dialog.isVisible()

    def test_dialog_is_frameless(self, dialog):
        from PySide6.QtCore import Qt
        assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint


# -----------------------------------------------------------------------
# GuiManager working dialog IPC
# -----------------------------------------------------------------------

class TestGuiManagerWorkingDialog:
    """Tests for GuiManager handling show_working/hide_working IPC actions."""

    @pytest.fixture
    def manager(self, qapp):
        """Create a GuiManager with real WorkingDialog but mocked tray/button."""
        from queue import Queue
        import threading

        with patch("gui.pystray") as mock_pystray, \
             patch("gui.FloatingButton") as MockButton:
            mock_button = MagicMock()
            MockButton.return_value = mock_button
            mock_pystray.Icon.return_value = MagicMock()

            shutdown_event = threading.Event()
            commands_queue = Queue()
            state_queue = Queue()

            mgr = None
            try:
                from gui import GuiManager
                mgr = GuiManager(shutdown_event, commands_queue, state_queue)
                yield mgr
            finally:
                if mgr and hasattr(mgr, 'working_dialog'):
                    mgr.working_dialog.close()

    def test_show_working_message_shows_dialog(self, manager):
        manager.state_from_logic_queue.put({
            "action": "show_working",
            "message": "Loading speech recognition provider"
        })
        manager._check_queues_and_events()
        assert manager.working_dialog.isVisible()

    def test_hide_working_message_hides_dialog(self, manager):
        manager.working_dialog.show_working("Loading")
        manager.state_from_logic_queue.put({"action": "hide_working"})
        manager._check_queues_and_events()
        assert not manager.working_dialog.isVisible()

    def test_the_startup_raise_names_the_startup_placeholder(self, manager):
        """The plaque raised before anything else says who owns it.

        Without a name the plaque would be owned by nothing, and the
        first stamped dismiss to arrive -- a provider ready for a launch
        that raised no dialog of its own -- would be dropped against it
        and leave the plaque up for the session
        (wh-dialog-ownership-token).
        """
        from services.wheelhouse.shared.dialog_owner import STARTUP_OWNER

        manager.button = MagicMock()
        manager.icon_thread = MagicMock()
        manager.update_tray_menu = MagicMock()
        manager.queue_timer = MagicMock()
        manager.send_command = MagicMock()
        manager.working_dialog = MagicMock()
        manager._gui_shm_name = None

        manager.start()

        manager.working_dialog.show_working.assert_called_once_with(
            "Starting", STARTUP_OWNER
        )


# -----------------------------------------------------------------------
# GuiManager PTT command routing
# -----------------------------------------------------------------------

class TestPTTCommandRouting:
    """Test that GuiManager sends correct IPC commands for PTT."""

    @pytest.fixture
    def manager(self):
        """Create a GuiManager with mocked dependencies."""
        with patch("gui.FloatingButton") as MockBtn, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer") as MockTimer:
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            cmds_q = MagicMock()
            state_q = MagicMock()
            mgr = GuiManager(shutdown, cmds_q, state_q)
            mgr.initial_state_received = True
            mgr.speech_interaction_mode = "toggle"
            # Mock the press timer since QTimer is mocked
            mgr._press_timer = MagicMock()
            mgr._PTT_HOLD_THRESHOLD_MS = 200
            return mgr

    def test_send_ptt_start_command(self, manager):
        manager._start_ptt()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_start"
        assert cmd["source"] == "floating_button"

    def test_send_ptt_stop_command(self, manager):
        manager._ptt_held = True
        manager._stop_ptt()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"

    def test_hold_threshold_callback_starts_ptt(self, manager):
        manager._ptt_held = False
        manager.button._is_dragging = False
        # The hold timer now also refuses to start recording while an edge-drag
        # resize is in progress, so both gestures must be clear here.
        manager.button._is_resizing = False
        manager._on_hold_threshold()
        assert manager._ptt_held is True
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_start"

    def test_button_release_after_hold_stops_ptt(self, manager):
        manager._ptt_held = True
        manager._on_button_release()
        assert manager._ptt_held is False
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"

    def test_button_release_without_hold_defers_toggle(self, manager):
        """Quick click in toggle mode defers toggle for double-click detection."""
        manager._ptt_held = False
        manager._double_click_timer = MagicMock()
        manager._press_timer.isActive.return_value = True
        manager._on_button_release()
        # Toggle is deferred -- timer starts, no immediate command
        manager._double_click_timer.start.assert_called()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_ptt_mode_starts_timer_on_press(self, manager):
        """PTT mode uses hold threshold -- no immediate PTT on press."""
        manager.speech_interaction_mode = "push_to_talk"
        manager._on_button_press()
        manager._press_timer.start.assert_called_with(200)
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_ptt_mode_quick_click_does_nothing(self, manager):
        """Quick click in PTT mode does nothing (requirement 4)."""
        manager.speech_interaction_mode = "push_to_talk"
        manager._double_click_timer = MagicMock()
        manager._press_timer.isActive.return_value = True
        manager._on_button_release()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        manager._double_click_timer.start.assert_not_called()

    def test_toggle_mode_starts_timer_on_press(self, manager):
        manager.speech_interaction_mode = "toggle"
        manager._on_button_press()
        manager._press_timer.start.assert_called_with(200)

    def test_press_ignored_before_initial_state(self, manager):
        manager.initial_state_received = False
        manager._on_button_press()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        manager._press_timer.start.assert_not_called()

    def test_drag_cancels_hold_timer(self, manager):
        """Drag starting cancels the hold timer before PTT activates."""
        manager.speech_interaction_mode = "toggle"
        manager._on_button_press()  # Starts hold timer
        manager._on_drag_started()  # Drag detected
        manager._press_timer.stop.assert_called()
        assert manager._ptt_held is False

    def test_hold_threshold_skipped_during_drag(self, manager):
        """Hold timer firing during drag does not activate PTT."""
        manager.button._is_dragging = True
        manager.button._is_resizing = False
        manager._on_hold_threshold()
        assert manager._ptt_held is False
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_hold_threshold_skipped_during_edge_drag_resize(self, manager):
        """Hold timer firing during a resize does not activate PTT."""
        manager.button._is_dragging = False
        manager.button._is_resizing = True
        manager._on_hold_threshold()
        assert manager._ptt_held is False
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_drag_cancels_active_ptt(self, manager):
        """Drag after hold-activated PTT sends ptt_stop with drag_cancel."""
        manager.speech_enabled = True
        manager._ptt_held = True
        manager._on_drag_started()
        assert manager._ptt_held is False
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"
        assert cmd["reason"] == "drag_cancel"

    def test_drag_cancel_leaves_the_speech_display_to_logic(self, manager):
        """The cancellation stopped restoring a saved value.

        It restored the value saved at the press, which is the computed
        display, while StateManager.ptt_stop restores the raw setting. Those
        disagree once an explicit decision lands during the hold
        (wh-ptt-release-disables-speech.1.8). What the display shows after a
        cancellation is covered in tests/test_gui_release_does_not_guess_speech.py.
        """
        manager.speech_enabled = True
        manager._ptt_held = True
        manager.button.set_state.reset_mock()

        manager._on_drag_started()

        assert manager.speech_enabled is True
        manager.button.set_state.assert_not_called()

    def test_drag_does_not_send_ptt_stop_when_not_held(self, manager):
        """Drag with no active PTT does not send ptt_stop."""
        manager._ptt_held = False
        manager._on_drag_started()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_cancelled_press_closes_a_microphone_the_hold_opened(self, manager):
        """The release was taken by the menu or the button being hidden.

        Without this the microphone stays open with nothing held down, which is
        the worst failure the resize gesture can cause. The command is what
        closes it: StateManager.ptt_stop puts the setting back and tells the
        speech engine. This process no longer decides the display, so the
        command is what this test checks.
        """
        manager.speech_enabled = True
        manager._ptt_held = True

        manager._on_press_cancelled()

        assert manager._ptt_held is False
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"
        assert cmd["reason"] == "gesture_cancel"

    def test_cancelled_press_leaves_the_speech_display_to_logic(self, manager):
        """A cancelled press decides nothing, and this process guesses nothing.

        See test_drag_cancel_leaves_the_speech_display_to_logic above for why
        the saved value went away.
        """
        manager.speech_enabled = True
        manager._ptt_held = True
        manager.button.set_state.reset_mock()

        manager._on_press_cancelled()

        assert manager.speech_enabled is True
        manager.button.set_state.assert_not_called()

    def test_cancelled_press_drops_a_pending_click_without_toggling(self, manager):
        """A quick press that got cancelled must not toggle speech on."""
        manager.speech_interaction_mode = "toggle"
        manager._double_click_timer = MagicMock()
        manager._on_button_press()

        manager._on_press_cancelled()

        manager._press_timer.stop.assert_called()
        manager._double_click_timer.start.assert_not_called()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()


# -----------------------------------------------------------------------
# GuiManager._on_tray_left_click
# -----------------------------------------------------------------------

class TestTrayLeftClick:
    """Test system tray left-click toggle behavior."""

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            cmds_q = MagicMock()
            state_q = MagicMock()
            mgr = GuiManager(shutdown, cmds_q, state_q)
            mgr.initial_state_received = True
            mgr.speech_interaction_mode = "toggle"
            return mgr

    def test_tray_left_click_defers_toggle(self, manager):
        """Single tray click defers toggle for double-click detection."""
        manager._on_tray_left_click()
        # Should not immediately send toggle -- deferred via timer
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        # Timer should be running
        assert manager._tray_click_timer is not None

    def test_tray_left_click_ignored_before_initial_state(self, manager):
        manager.initial_state_received = False
        manager._on_tray_left_click()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_tray_deferred_click_fires_toggle(self, manager):
        """When tray double-click timer expires, the deferred toggle executes."""
        manager._on_deferred_tray_single_click()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "toggle_speech_enabled_state"

    def test_tray_single_click_does_nothing_in_ptt_mode(self, manager):
        """Single click on tray does nothing when PTT mode is on."""
        manager.speech_interaction_mode = "push_to_talk"
        manager._on_deferred_tray_single_click()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_tray_double_click_switches_mode(self, manager):
        """Two rapid tray clicks switch interaction mode and disable speech."""
        manager.speech_interaction_mode = "toggle"
        manager.speech_enabled = True
        manager._on_tray_left_click()  # First click -- starts timer
        manager._on_tray_left_click()  # Second click -- double-click
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "set_speech_interaction_mode"
        assert cmd["mode"] == "push_to_talk"
        assert manager.speech_enabled is False

    def test_tray_double_click_from_ptt_to_toggle(self, manager):
        """Double-click in PTT mode switches back to toggle and disables speech."""
        manager.speech_interaction_mode = "push_to_talk"
        manager.speech_enabled = True
        manager._on_tray_left_click()
        manager._on_tray_left_click()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "set_speech_interaction_mode"
        assert cmd["mode"] == "toggle"
        assert manager.speech_enabled is False


# -----------------------------------------------------------------------
# GuiManager._toggle_ptt_mode
# -----------------------------------------------------------------------

class TestPTTModeMenuItem:
    """Test Push-to-Talk Mode tray menu item."""

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            cmds_q = MagicMock()
            state_q = MagicMock()
            mgr = GuiManager(shutdown, cmds_q, state_q)
            mgr.initial_state_received = True
            mgr.speech_interaction_mode = "toggle"
            return mgr

    def test_toggle_ptt_mode_sends_command(self, manager):
        manager.speech_interaction_mode = "toggle"
        manager._toggle_ptt_mode()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "set_speech_interaction_mode"
        assert cmd["mode"] == "push_to_talk"

    def test_toggle_ptt_mode_back_to_toggle(self, manager):
        manager.speech_interaction_mode = "push_to_talk"
        manager._toggle_ptt_mode()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["mode"] == "toggle"

    def test_toggle_ptt_mode_disables_speech(self, manager):
        """Mode switch disables speech for clean visual transition."""
        manager.speech_enabled = True
        manager._toggle_ptt_mode()
        assert manager.speech_enabled is False


# -----------------------------------------------------------------------
# GuiManager double-click to toggle interaction mode
# -----------------------------------------------------------------------

class _StatefulTimer:
    """A stand-in for QTimer that remembers whether it is running.

    A MagicMock answers isActive() with a truthy Mock whatever has happened to
    it, so a test using one cannot tell a stopped timer from a running one --
    and "the timer was left running" is exactly the failure being guarded
    against here.
    """

    def __init__(self):
        self._active = False
        self.timeout = MagicMock()

    def start(self, _ms=None):
        self._active = True

    def stop(self):
        self._active = False

    def isActive(self):
        return self._active

    def setSingleShot(self, _value):
        pass


class TestDoubleClickModeToggle:
    """Test double-click on floating button toggles PTT/toggle mode."""

    @pytest.fixture
    def manager(self):
        """Create a GuiManager with mocked dependencies."""
        with patch("gui.FloatingButton") as MockBtn, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer") as MockTimer:
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            cmds_q = MagicMock()
            state_q = MagicMock()
            mgr = GuiManager(shutdown, cmds_q, state_q)
            mgr.initial_state_received = True
            mgr.speech_interaction_mode = "toggle"
            mgr._press_timer = MagicMock()
            mgr._double_click_timer = MagicMock()
            mgr._PTT_HOLD_THRESHOLD_MS = 200
            return mgr

    def test_double_click_in_toggle_mode_switches_to_ptt(self, manager):
        """Double-click in toggle mode should switch to push_to_talk."""
        manager.speech_interaction_mode = "toggle"
        manager._on_double_click()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "set_speech_interaction_mode"
        assert cmd["mode"] == "push_to_talk"

    def test_double_click_in_ptt_mode_switches_to_toggle(self, manager):
        """Double-click in PTT mode should switch to toggle."""
        manager.speech_interaction_mode = "push_to_talk"
        manager._on_double_click()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "set_speech_interaction_mode"
        assert cmd["mode"] == "toggle"

    def test_double_click_cancels_pending_single_click(self, manager):
        """Double-click should cancel any pending single-click timer."""
        manager.speech_interaction_mode = "toggle"
        manager._on_double_click()
        manager._double_click_timer.stop.assert_called()

    def test_double_click_cancels_active_ptt(self, manager):
        """Double-click after PTT started should stop PTT before switching."""
        manager.speech_interaction_mode = "push_to_talk"
        manager._ptt_held = True
        manager._on_double_click()
        # Should have sent ptt_stop then set_speech_interaction_mode
        calls = manager.commands_to_logic_queue.put_nowait.call_args_list
        actions = [c[0][0]["action"] for c in calls]
        assert "ptt_stop" in actions
        assert "set_speech_interaction_mode" in actions

    def test_toggle_mode_release_defers_single_click(self, manager):
        """In toggle mode, button release should defer toggle via timer."""
        manager.speech_interaction_mode = "toggle"
        manager._ptt_held = False
        manager._press_timer.isActive.return_value = True
        manager._on_button_release()
        # Should start double-click timer instead of immediate toggle
        manager._double_click_timer.start.assert_called()
        # Should NOT have sent toggle command yet
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_deferred_single_click_fires_toggle(self, manager):
        """When double-click timer expires, the deferred toggle executes."""
        manager.speech_interaction_mode = "toggle"
        manager._on_deferred_single_click()
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "toggle_speech_enabled_state"

    def test_double_click_ignored_before_initial_state(self, manager):
        """Double-click before initial state should be ignored."""
        manager.initial_state_received = False
        manager._on_double_click()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_a_double_click_switches_the_mode_exactly_once(self, manager):
        """The whole sequence a double-click really produces, start to finish.

        Qt sends press, release, then the double-click -- and no second
        release, because the widget only reports a release for a press it
        recorded, and the double-click handler records none. So the mode must
        switch once across the sequence, and the deferred single click that
        the release started must be called off.
        """
        manager.speech_interaction_mode = "toggle"
        manager._press_timer = _StatefulTimer()
        manager._double_click_timer = _StatefulTimer()

        manager._on_button_press()
        manager._on_button_release()
        manager._on_double_click()

        modes = [
            c[0][0]["mode"]
            for c in manager.commands_to_logic_queue.put_nowait.call_args_list
            if c[0][0].get("action") == "set_speech_interaction_mode"
        ]
        assert modes == ["push_to_talk"]
        assert manager._double_click_timer.isActive() is False

    def test_a_double_click_does_not_swallow_a_later_click(self, manager):
        """A click minutes later is its own click, not the double-click's tail.

        The manager used to hold a flag saying "ignore the next release",
        waiting for a second release that never comes. The flag stayed set and
        swallowed the next real release instead -- and a swallowed release
        leaves the hold timer running, so it fires with the mouse already up.
        """
        manager.speech_interaction_mode = "toggle"
        manager._press_timer = _StatefulTimer()
        manager._double_click_timer = _StatefulTimer()

        manager._on_button_press()
        manager._on_button_release()
        manager._on_double_click()

        # Some time later, an ordinary click.
        manager._on_button_press()
        assert manager._press_timer.isActive() is True
        manager._on_button_release()

        assert manager._press_timer.isActive() is False

    def test_a_swallowed_release_opens_the_microphone_with_nothing_held(self, manager):
        """The harm the stale flag caused, stated as the user would meet it."""
        manager.speech_interaction_mode = "toggle"
        manager._press_timer = _StatefulTimer()
        manager._double_click_timer = _StatefulTimer()
        # The real button is doing nothing; the mock says "dragging" to every
        # question asked of it, which would hide the failure.
        manager.button._is_dragging = False
        manager.button._is_resizing = False

        manager._on_button_press()
        manager._on_button_release()
        manager._on_double_click()

        manager._on_button_press()
        manager._on_button_release()
        manager.commands_to_logic_queue.put_nowait.reset_mock()

        # The hold timer only reaches this handler if it is still running.
        # Nothing is pressed any more, so nothing may start recording.
        if manager._press_timer.isActive():
            manager._on_hold_threshold()

        actions = [
            c[0][0].get("action")
            for c in manager.commands_to_logic_queue.put_nowait.call_args_list
        ]
        assert "ptt_start" not in actions


# -----------------------------------------------------------------------
# Help and About on the right-click menu
# -----------------------------------------------------------------------

class TestHelpAndAboutOnTheMenu:
    """Both entries belong to the shared menu builder.

    The tray icon and the floating button show the same menu through the same
    method, one branch each. An entry added to only one branch appears in only
    one place, which is the mistake these tests exist to catch.
    """

    @pytest.fixture
    def manager(self, qapp):
        import pystray
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.QTimer"), \
             patch("gui.pystray.Icon") as mock_icon_cls:
            mock_icon_cls.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            mgr.initial_state_received = True
            return mgr

    def _tray_labels(self, manager):
        menu = manager._create_menu(is_tray_menu=True)
        return [getattr(item, "text", "") for item in menu]

    def _window_labels(self, manager):
        menu = manager._create_menu(is_tray_menu=False)
        return [action.text() for action in menu.actions()]

    def test_the_tray_menu_offers_help(self, manager):
        assert "Help" in self._tray_labels(manager)

    def test_the_button_menu_offers_help(self, manager):
        assert "Help" in self._window_labels(manager)

    def test_the_tray_menu_offers_about(self, manager):
        assert "About Wheelhouse" in self._tray_labels(manager)

    def test_the_button_menu_offers_about(self, manager):
        assert "About Wheelhouse" in self._window_labels(manager)

    def test_help_asks_the_logic_process_to_open_the_help_page(self, manager):
        """The address is a setting, and settings live in the Logic process.

        The GUI process has no copy of it, so the menu sends a command rather
        than opening a browser itself. This is the same setting the spoken
        command 'wheelhouse help online' uses.
        """
        with patch.object(manager, "send_command") as send:
            manager.request_help_online()

        send.assert_called_once_with({"action": "open_help_online"})

    def test_about_shows_a_dialog_naming_the_program_and_version(self, manager):
        with patch("gui.QMessageBox") as message_box, \
             patch("gui.get_app_version", return_value="9.9.9"):
            manager.show_about_dialog()

        assert message_box.information.called
        body = message_box.information.call_args[0][2]
        assert "Wheelhouse" in body
        assert "9.9.9" in body
        assert 'say "open voice access help"' in body
        assert "x-ray" not in body
        assert "wheelhouse help online" not in body


class TestGeometryDuringAMiddlePressThatIsNotYetADrag:
    """A press is a live gesture from the moment it lands (.1.22).

    Deferral used to check _is_resizing or _is_dragging. Neither is true
    between a press in the middle of the button and the pointer travelling far
    enough to count as a drag -- and that interval is not brief: it covers a
    click that never moves, and a stationary push-to-talk hold for as long as
    the user holds it. Geometry arriving then moved or resized the button under
    the user's finger, and a press that later became a drag started from
    geometry that had changed since it began.
    """

    @pytest.fixture
    def manager(self):
        with patch("gui.FloatingButton") as mock_button_cls, \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"), \
             patch("gui.QPoint") as mock_qpoint:
            mock_button = MagicMock()
            mock_button_cls.return_value = mock_button
            mock_pystray.Icon.return_value = MagicMock()
            mock_qpoint.side_effect = lambda *args: MagicMock()
            from gui import GuiManager
            # A bare MagicMock reports the shutdown event as already set, and
            # the queue is then never read at all.
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            state_q = MagicMock()
            mgr = GuiManager(shutdown, MagicMock(), state_q)
            mgr.button = mock_button
            mock_button._is_resizing = False
            mock_button._is_dragging = False
            mock_button._gesture_running = False
            mgr.initial_state_received = True
            return mgr

    def _deliver(self, manager, size):
        msg = {
            'action': 'state_update',
            'speech_enabled': False,
            'FLOATING_BUTTON_SIZE': size,
            'FLOATING_BUTTON_POS': [100, 100],
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [msg, Empty()]
        with patch.object(manager, 'update_ui_state'):
            manager._check_queues_and_events()

    def test_a_press_that_has_not_moved_holds_geometry_back(self, manager):
        manager.button._gesture_running = True

        self._deliver(manager, 70)

        manager.button.set_size.assert_not_called()
        manager.button.move.assert_not_called()

    def test_that_geometry_arrives_when_the_press_ends(self, manager):
        manager.button._gesture_running = True
        self._deliver(manager, 70)

        manager.button._gesture_running = False
        manager._on_gesture_ended()

        manager.button.set_size.assert_called_with(70)

    def test_a_click_that_moved_the_button_first_keeps_where_it_was_put(self, manager):
        """The drag's own result is newer than what was held back."""
        manager.button._gesture_running = True
        self._deliver(manager, 70)

        manager.send_pos_change_command(MagicMock(x=lambda: 5, y=lambda: 6))
        manager.button._gesture_running = False
        manager._on_gesture_ended()

        manager.button.set_size.assert_not_called()

    def test_geometry_still_applies_when_no_gesture_is_running(self, manager):
        manager.button._gesture_running = False

        self._deliver(manager, 70)

        manager.button.set_size.assert_called_with(70)


# -----------------------------------------------------------------------
# The GUI process stays alive with no window on screen
# -----------------------------------------------------------------------

class TestApplicationOutlivesItsWindows:
    """wh-gui-about-box-quits-app.

    Wheelhouse lives in the notification area. Every window it opens is
    temporary, and the floating button -- the only one that is usually on
    screen -- is a tool window, which Qt does not count when it decides
    whether the last window has closed. Closing the About box therefore
    ended the GUI process, and the launcher shut the whole program down.
    """

    def test_closing_a_parentless_dialog_leaves_the_loop_running(self, qapp):
        """The real event loop, because only it can be ended this way.

        Qt's quit reaches the top-level loop the GUI process runs, not a
        nested one, so this test runs qapp.exec() itself. Every step is on a
        timer, and the last one stops the loop, so the test ends either way:
        early if the About box took the loop down, on its own terms if not.
        """
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QMessageBox, QWidget

        from gui import configure_gui_application

        was_set = qapp.quitOnLastWindowClosed()
        button = QWidget()
        button.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "About Wheelhouse",
            "Wheelhouse",
        )
        reached_the_end = []

        try:
            configure_gui_application(qapp)
            button.show()

            QTimer.singleShot(0, box.show)
            QTimer.singleShot(50, box.close)
            QTimer.singleShot(150, lambda: reached_the_end.append(True))
            QTimer.singleShot(250, qapp.quit)
            qapp.exec()
        finally:
            box.deleteLater()
            button.close()
            qapp.setQuitOnLastWindowClosed(was_set)

        assert reached_the_end == [True], (
            "the event loop ended when the About box closed"
        )

    def test_the_gui_process_configures_the_application(self):
        """The helper above is only worth anything if the process calls it."""
        import gui

        with patch("gui.QApplication") as mock_qapplication, \
             patch("gui.GuiManager"), \
             patch("gui.configure_gui_application") as mock_configure, \
             patch("utils.logging_setup.setup_logging"), \
             patch("services.wheelhouse.config_service.ConfigService"), \
             patch("gui.sys.exit"):
            gui.gui_process_target(MagicMock(), MagicMock(), MagicMock())

        mock_configure.assert_called_once_with(mock_qapplication.return_value)

    def _run_process_target(self, shutdown_is_set: bool, exit_code: int):
        """Run gui_process_target with the Qt loop returning exit_code."""
        import gui

        shutdown_event = MagicMock()
        shutdown_event.is_set.return_value = shutdown_is_set

        with patch("gui.QApplication") as mock_qapplication, \
             patch("gui.GuiManager"), \
             patch("gui.configure_gui_application"), \
             patch("gui.logger") as mock_logger, \
             patch("utils.logging_setup.setup_logging"), \
             patch("services.wheelhouse.config_service.ConfigService"), \
             patch("gui.sys.exit"):
            mock_qapplication.return_value.exec.return_value = exit_code
            gui.gui_process_target(shutdown_event, MagicMock(), MagicMock())

        return mock_logger

    def test_an_unrequested_exit_says_so_in_the_log(self):
        """The loop ends quietly, with a success code and no traceback."""
        mock_logger = self._run_process_target(shutdown_is_set=False, exit_code=0)

        assert mock_logger.error.called
        assert "no shutdown requested" in mock_logger.error.call_args[0][0]

    def test_a_requested_exit_is_not_reported_as_a_failure(self):
        mock_logger = self._run_process_target(shutdown_is_set=True, exit_code=0)

        assert not mock_logger.error.called


# -----------------------------------------------------------------------
# Removed menu items (wh-remove-restart-credentials-items)
# -----------------------------------------------------------------------

class TestRemovedMenuItems:
    """Neither "Restart Transcription Service" nor "Google Cloud
    Credentials" appears on the menu any more, and neither does "Audio
    Suppression".

    David removed the first two on 2026-09-04 (QUESTIONS-2026-09-04.md
    item 20). Each replaced the speech engine the same way a provider
    switch does, and neither dropped a held bare number
    (wh-provider-switch-stale-hold.1.3).

    David ruled on 2026-09-17 (wh-audio-suppression-auto) that the sound
    pause is no longer a user switch. "Audio Suppression" went from both
    menus with the hover text of the floating button's entry, and the GUI
    attribute that held the checkmark went with them.
    tests/test_audio_suppression_controls_removed.py checks the other
    removed parts: the senders, the setter, the notices and the spoken
    commands.

    The tray icon and the floating button build the same menu through
    ``_create_menu``, one branch each, so both branches are checked. The
    fixture below turns ON every condition that made the two restart and
    credentials items appear; the two guard tests prove it, so an absence
    assertion here cannot pass just because the menu came back empty. The
    "Audio Suppression" entry had no condition: both branches added it
    whenever they built the menu.
    """

    @pytest.fixture
    def manager(self, qapp):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.QTimer"), \
             patch("gui.pystray.Icon") as mock_icon_cls:
            mock_icon_cls.return_value = MagicMock()
            from gui import GuiManager
            mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
            # Both items were gated: the restart item on initial state
            # having arrived, the credentials item on google_stt being
            # installed. Both gates are open here.
            mgr.initial_state_received = True
            mgr.stt_providers_available = ["google_stt", "parakeet_tdt"]
            return mgr

    def _tray_labels(self, manager):
        menu = manager._create_menu(is_tray_menu=True)
        return [getattr(item, "text", "") for item in menu]

    def _window_labels(self, manager):
        menu = manager._create_menu(is_tray_menu=False)
        return [action.text() for action in menu.actions()]

    def test_the_tray_menu_still_builds_its_gated_items(self, manager):
        """Guard: the gates the two removed items sat behind are open."""
        labels = self._tray_labels(manager)
        assert "STT Provider" in labels
        assert "Restart Wheelhouse" in labels

    def test_the_button_menu_still_builds_its_gated_items(self, manager):
        """Guard: the gates the two removed items sat behind are open."""
        labels = self._window_labels(manager)
        assert "STT Provider" in labels
        assert "Restart Wheelhouse" in labels

    def test_the_tray_menu_has_no_restart_transcription_service(self, manager):
        assert "Restart Transcription Service" not in self._tray_labels(manager)

    def test_the_button_menu_has_no_restart_transcription_service(self, manager):
        assert "Restart Transcription Service" not in self._window_labels(manager)

    def test_the_tray_menu_has_no_google_cloud_credentials(self, manager):
        assert "Google Cloud Credentials" not in self._tray_labels(manager)

    def test_the_button_menu_has_no_google_cloud_credentials(self, manager):
        assert "Google Cloud Credentials" not in self._window_labels(manager)

    def test_the_tray_menu_has_no_audio_suppression(self, manager):
        assert "Audio Suppression" not in self._tray_labels(manager)

    def test_the_button_menu_has_no_audio_suppression(self, manager):
        assert "Audio Suppression" not in self._window_labels(manager)

    def test_the_button_menu_has_no_sound_pause_hover_text(self, manager):
        """No entry at any depth keeps the removed entry's hover text.

        ``menu`` stays referenced until every tooltip is read: the top-level
        QMenu owns the whole tree, so dropping it destroys the actions.
        """
        menu = manager._create_menu(is_tray_menu=False)

        def tooltips(qmenu):
            for action in qmenu.actions():
                yield action.toolTip()
                if action.menu() is not None:
                    yield from tooltips(action.menu())

        every_tooltip = list(tooltips(menu))
        # Guard: the walk read real hover text, so the absence below is
        # not the absence of every tooltip.
        assert any("Switch listening on or off." in tip for tip in every_tooltip)
        assert [tip for tip in every_tooltip
                if "Pause listening while this computer plays sound" in tip] == []

    def test_the_gui_keeps_no_audio_suppression_state(self, manager):
        assert not hasattr(manager, "audio_suppression_enabled")


# -----------------------------------------------------------------------
# The two menus offer the same entries (wh-audio-suppression-floating-menu)
# -----------------------------------------------------------------------

@pytest.fixture
def menu_manager(qapp):
    """A GuiManager with every menu gate open and both submenus populated.

    Both branches build a submenu only when the matching provider list is
    non-empty (``if self.stt_providers_available:`` and
    ``if self.ai_providers_available:``, once in each branch of
    ``_create_menu``), so empty lists would leave "STT Provider" and
    "AI Model" out of both label lists and let a comparison pass without
    ever having compared them. The gates are named rather than cited by
    line number, because the four line numbers this docstring used to give
    were all stale.

    ``gui.pystray.Icon`` alone is patched, not ``gui.pystray``: the tray
    branch's ``Menu``, ``MenuItem`` and ``Menu.SEPARATOR`` have to be the real
    ones for the label list below to read an item's own properties.
    """
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.QTimer"), \
         patch("gui.pystray.Icon") as mock_icon_cls:
        mock_icon_cls.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
        mgr.initial_state_received = True
        mgr.stt_providers_available = ["google_stt", "parakeet_tdt"]
        mgr.stt_provider = "google_stt"
        mgr.ai_providers_available = ["gpt-oss-20b", "qwen3-coder-30b"]
        mgr.ai_provider = "gpt-oss-20b"
        return mgr


VOICE_TEACHING_LABEL = "Teach WheelHouse your voice..."
STT_SUBMENU_TITLE = "STT Provider"
VOICE_TEACHING_TOOLTIP = (
    "Open the voice-teaching session for the Distil-Whisper engine."
)
#: Stands for a separator in the entry lists below, so a test can state
#: where a separator sits. No real entry can carry this text: both branches
#: build every label from a literal or from a provider display name.
SEPARATOR_MARK = "<separator>"


def _tray_entries(manager, submenu=None):
    """The tray branch's entries, in order, with separators marked.

    With ``submenu`` given, the entries of that submenu instead of the ones
    at the top level. ``MenuItem.submenu`` answers the ``Menu`` when the
    item's action is a menu, and ``None`` for an ordinary entry (pystray
    0.19.5, pystray/_base.py:516-520).

    ``TestTheTwoMenusOfferTheSameEntries`` has its own label helpers, and
    they drop every separator. These keep them, because the place of an
    entry relative to a separator is what the submenu tests measure.

    Iterating a ``Menu`` applies pystray's own separator rules
    (pystray/_base.py:654-682): an invisible item is dropped, consecutive
    separators collapse into one, and a separator at the head or the tail is
    removed. A separator between two entries survives, and it is the one
    shared ``Menu.SEPARATOR`` instance, so identity marks it.
    """
    import pystray
    items = list(manager._create_menu(is_tray_menu=True))
    if submenu is not None:
        parents = [item for item in items if item.text == submenu]
        assert len(parents) == 1, (
            f"expected exactly one {submenu!r} tray item, got "
            f"{[item.text for item in items]}"
        )
        holder = parents[0].submenu
        assert holder is not None, f"the tray {submenu!r} item has no submenu"
        items = list(holder)
    return [SEPARATOR_MARK if item is pystray.Menu.SEPARATOR else item.text
            for item in items]


def _window_entries(manager, submenu=None):
    """The Qt branch's entries, in order, with separators marked.

    With ``submenu`` given, the entries of that submenu instead of the ones
    at the top level. ``menu.addMenu(submenu)`` adds one action whose
    ``menu()`` is the submenu, so the parent is found by text and the
    submenu read through it.

    Only strings leave this function. The top-level ``QMenu`` owns every
    menu and action under it, and it goes out of scope on the return.
    """
    menu = manager._create_menu(is_tray_menu=False)
    actions = menu.actions()
    if submenu is not None:
        parents = [action for action in actions if action.text() == submenu]
        assert len(parents) == 1, (
            f"expected exactly one {submenu!r} menu entry, got "
            f"{[action.text() for action in actions]}"
        )
        holder = parents[0].menu()
        assert holder is not None, f"the {submenu!r} entry has no submenu"
        actions = holder.actions()
    return [SEPARATOR_MARK if action.isSeparator() else action.text()
            for action in actions]


def _tray_every_entry(manager):
    """Every tray entry a user can reach, submenu contents included."""
    import pystray

    def walk(menu):
        for item in menu:
            if item is pystray.Menu.SEPARATOR:
                continue
            yield item.text
            if item.submenu is not None:
                yield from walk(item.submenu)

    return list(walk(manager._create_menu(is_tray_menu=True)))


def _window_every_entry(manager):
    """Every entry of the button's menu, submenu contents included."""
    def walk(menu):
        for action in menu.actions():
            if action.isSeparator():
                continue
            yield action.text()
            if action.menu() is not None:
                yield from walk(action.menu())

    menu = manager._create_menu(is_tray_menu=False)
    return list(walk(menu))


def _tray_submenu_item(manager, label, submenu=STT_SUBMENU_TITLE):
    """One item inside a tray submenu, and exactly one."""
    items = [item for item in manager._create_menu(is_tray_menu=True)
             if item.text == submenu]
    assert len(items) == 1, f"expected exactly one {submenu!r} tray item"
    holder = items[0].submenu
    assert holder is not None, f"the tray {submenu!r} item has no submenu"
    matches = [item for item in holder if item.text == label]
    assert len(matches) == 1, (
        f"expected exactly one {label!r} item inside the tray {submenu!r}, "
        f"got {[item.text for item in holder]}"
    )
    return matches[0]


def _window_submenu_action(manager, label, submenu=STT_SUBMENU_TITLE):
    """One action inside a Qt submenu, with the two menus that own it.

    Both menus come back with the action because the top-level ``QMenu``
    owns the whole tree; dropping it would destroy the action itself.
    """
    menu = manager._create_menu(is_tray_menu=False)
    parents = [action for action in menu.actions()
               if action.text() == submenu]
    assert len(parents) == 1, (
        f"expected exactly one {submenu!r} menu entry, got "
        f"{[action.text() for action in menu.actions()]}"
    )
    holder = parents[0].menu()
    assert holder is not None, f"the {submenu!r} entry has no submenu"
    matches = [action for action in holder.actions()
               if action.text() == label]
    assert len(matches) == 1, (
        f"expected exactly one {label!r} entry inside {submenu!r}, got "
        f"{[action.text() for action in holder.actions()]}"
    )
    return menu, holder, matches[0]


class TestTheTwoMenusOfferTheSameEntries:
    """The tray menu and the floating button's menu list the same entries in
    the same order.

    ``_create_menu`` has two exclusive branches, and every entry has to be
    written into both by hand. ``TestHelpAndAboutOnTheMenu`` and
    ``TestRemovedMenuItems`` each check one label in both branches; this class
    checks the whole list at once, so an entry added to one branch and
    forgotten in the other is caught whatever it is called.

    Order is part of the comparison. The two menus are the same menu to the
    user, and an entry that moves in one of them makes the two disagree.
    """

    def _tray_labels(self, manager):
        """The tray branch's entry labels, in order, separators excluded.

        Two exclusions, both structural rather than by text:

        * A separator is ``pystray.Menu.SEPARATOR``, one shared instance, so
          identity finds it.
        * The hidden default item (gui.py:3787) is excluded by its own
          ``default`` and ``visible`` properties, so renaming it cannot change
          this list. pystray drops it before this filter even sees it --
          ``Menu.__iter__`` returns ``_visible_items()``, which skips every
          item whose ``visible`` is False -- and the filter states the
          exclusion anyway, because iterating ``menu.items`` instead would
          hand it over.
        """
        import pystray
        menu = manager._create_menu(is_tray_menu=True)
        return [item.text for item in menu
                if item is not pystray.Menu.SEPARATOR
                and not (item.default and not item.visible)]

    def _window_labels(self, manager):
        """The Qt branch's entry labels, in order, separators excluded.

        A Qt separator is an action that answers True to ``isSeparator()``,
        which is how it goes. Every other action stays, submenu parents
        included -- ``menu.addMenu(submenu)`` adds one action carrying the
        submenu's title.
        """
        menu = manager._create_menu(is_tray_menu=False)
        return [action.text() for action in menu.actions()
                if not action.isSeparator()]

    def test_both_branches_build_their_submenu_parents(self, menu_manager):
        """Guard: the provider gates are open in both branches.

        Without this, two short lists could match each other while both were
        missing the submenus.
        """
        for name, labels in (("tray", self._tray_labels(menu_manager)),
                             ("window", self._window_labels(menu_manager))):
            assert "STT Provider" in labels, name
            assert "AI Model" in labels, name
            # 12, not the 14 this bound started at:
            # wh-voice-teaching-stt-submenu moved the voice-teaching entry
            # off the top level and into the STT Provider submenu, and
            # wh-audio-suppression-auto removed the Audio Suppression entry,
            # so the top level holds two entries fewer. The bound stays
            # rather than going away, because its job is to refuse a short
            # list that matches another short list.
            assert len(labels) >= 12, (name, labels)

    def test_neither_label_list_keeps_a_separator_or_the_hidden_item(
        self, menu_manager
    ):
        """Guard: the two exclusions above removed something real."""
        tray = self._tray_labels(menu_manager)
        # pystray's SEPARATOR carries this text (pystray._base.Menu).
        assert "- - - -" not in tray
        assert "Toggle Speech" not in tray
        assert "" not in self._window_labels(menu_manager)

    def test_the_comparison_notices_a_reorder(self, menu_manager):
        """Guard: the comparison below is ordered.

        Sorting the two lists, or comparing them as sets, would pass on a
        menu whose entries had moved, and a moved entry is half of what this
        class is for. The same ``==`` used below has to reject a list whose
        first two entries are swapped, and sorting has to miss it.
        """
        labels = self._tray_labels(menu_manager)
        reordered = [labels[1], labels[0], *labels[2:]]
        assert reordered != labels
        assert sorted(reordered) == sorted(labels)

    def test_the_two_menus_list_the_same_entries_in_the_same_order(
        self, menu_manager
    ):
        assert self._tray_labels(menu_manager) == self._window_labels(menu_manager), (
            "the tray menu (left) and the floating button's menu (right) "
            "disagree; every entry belongs in both branches of _create_menu"
        )

    def test_the_two_menus_list_the_same_submenu_entries(self, menu_manager):
        """The comparison reaches inside both submenus as well.

        The two label helpers above read the top level only, so an entry
        that one branch puts in a submenu and the other branch leaves at the
        top level passes both of them: the entry is simply absent from one
        list and present in the other, at a position the shared-order
        comparison reads as a plain disagreement, or -- when the two
        branches happen to disagree nowhere else -- not at all.
        wh-voice-teaching-stt-submenu put an entry inside a submenu, which
        is what makes this comparison necessary.

        Separators count here, unlike in the top-level comparison. The
        voice-teaching entry sits behind one, and a separator in one branch
        only would present the same submenu differently in the two menus.
        """
        for title in (STT_SUBMENU_TITLE, "AI Model"):
            assert (_tray_entries(menu_manager, submenu=title)
                    == _window_entries(menu_manager, submenu=title)), (
                f"the two branches disagree inside the {title!r} submenu; "
                f"tray {_tray_entries(menu_manager, submenu=title)}, "
                f"window {_window_entries(menu_manager, submenu=title)}"
            )


# -----------------------------------------------------------------------
# Voice teaching sits inside the STT Provider submenu
# (wh-voice-teaching-stt-submenu)
# -----------------------------------------------------------------------

class TestVoiceTeachingSitsInTheSttProviderSubmenu:
    """"Teach WheelHouse your voice..." belongs to the STT Provider submenu.

    The entry teaches one engine, Distil-Whisper, so it belongs beside the
    engine list rather than among the entries that act on the whole
    application. David asked for the move while checking the floating
    button's menu by hand (the note at the end of
    docs/testing/2026-09-16-audio-suppression-floating-menu-merge-manual-tests.md,
    filed as wh-voice-teaching-stt-submenu).

    ``_create_menu`` has two exclusive branches -- the pystray tray menu and
    the floating button's QMenu -- and every entry is written into both by
    hand. So every expectation here is stated twice, once per branch.
    """

    def _engine_labels(self, manager):
        """The engine entries the submenu lists before the separator."""
        return [manager._get_provider_display_name(provider)
                for provider in manager.stt_providers_available]

    def test_the_tray_submenu_lists_the_engines_then_the_entry(
        self, menu_manager
    ):
        """Tray: the engines, then a separator, then voice teaching.

        The separator is what keeps the entry from reading as a third
        engine, and the order is what keeps the engine list first, where a
        user who opened the submenu to switch engines expects it.
        """
        assert _tray_entries(menu_manager, submenu=STT_SUBMENU_TITLE) == [
            *self._engine_labels(menu_manager),
            SEPARATOR_MARK,
            VOICE_TEACHING_LABEL,
        ]

    def test_the_window_submenu_lists_the_engines_then_the_entry(
        self, menu_manager
    ):
        """Floating button: the same submenu contents as the tray."""
        assert _window_entries(menu_manager, submenu=STT_SUBMENU_TITLE) == [
            *self._engine_labels(menu_manager),
            SEPARATOR_MARK,
            VOICE_TEACHING_LABEL,
        ]

    def test_neither_top_level_menu_still_offers_the_entry(self, menu_manager):
        """The move is a move, not a copy: the top level lost the entry.

        Both branches keep their own copy of every label, so one branch can
        keep the old top-level entry while the other moves it.
        """
        assert VOICE_TEACHING_LABEL not in _tray_entries(menu_manager)
        assert VOICE_TEACHING_LABEL not in _window_entries(menu_manager)

    def test_the_tray_entry_stays_greyed_until_the_first_state_arrives(
        self, menu_manager
    ):
        """Tray: the entry keeps the readiness gate it had at the top level.

        ``is_ready`` is ``bool(self.initial_state_received)``. Before the
        first state message the GUI process knows nothing about the speech
        engine, so opening a voice-teaching session would act on a state
        nobody has confirmed.
        """
        menu_manager.initial_state_received = False
        item = _tray_submenu_item(menu_manager, VOICE_TEACHING_LABEL)
        assert item.enabled is False

    def test_the_window_entry_stays_greyed_until_the_first_state_arrives(
        self, menu_manager
    ):
        """Floating button: the same readiness gate as the tray."""
        menu_manager.initial_state_received = False
        _menu, _holder, action = _window_submenu_action(
            menu_manager, VOICE_TEACHING_LABEL)
        assert action.isEnabled() is False

    def test_the_window_entry_keeps_its_hover_text(self, menu_manager):
        """The hover text moves with the entry, word for word.

        Qt answers ``toolTip()`` with the action's own text when no tooltip
        was set, so "not empty" proves nothing and this test states the
        whole sentence instead.

        The submenu has to show tooltips as well: a QMenu hides the tooltips
        of its actions unless ``setToolTipsVisible(True)`` was called on it.
        The STT Provider submenu already calls it, so that half of the test
        passes on the unmoved code. It is here because the hover text is
        worth nothing to the user if the submenu holding it hides it.
        """
        _menu, holder, action = _window_submenu_action(
            menu_manager, VOICE_TEACHING_LABEL)
        assert action.toolTip() == VOICE_TEACHING_TOOLTIP
        assert holder.toolTipsVisible(), (
            "the submenu hides its tooltips, so the hover text never shows"
        )

    def test_the_tray_entry_asks_the_qt_thread_to_open_the_session(
        self, menu_manager
    ):
        """Tray: the callback only hands the request to the Qt thread.

        pystray runs a menu callback on its own thread, and the
        voice-teaching window has to be built on the Qt thread, so the tray
        item puts the request on the state queue, which the Qt thread reads
        as ``action == "open_calibration"``. The call runs on a worker
        thread here for the same reason, and the join proves the callback
        returns instead of blocking on the queue.
        """
        import threading

        item = _tray_submenu_item(menu_manager, VOICE_TEACHING_LABEL)
        failures = []

        def invoke():
            try:
                item(menu_manager.icon)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive(), "the tray callback never returned"
        assert not failures, failures
        menu_manager.state_from_logic_queue.put.assert_called_once_with(
            {"action": "open_calibration"}
        )

    def test_the_window_entry_opens_the_session(self, menu_manager):
        """Floating button: the entry calls the handler directly.

        This branch already runs on the Qt thread, so it needs no queue.
        """
        with patch.object(menu_manager, "_open_calibration") as opened:
            _menu, _holder, action = _window_submenu_action(
                menu_manager, VOICE_TEACHING_LABEL)
            action.trigger()
        opened.assert_called_once()

    def test_with_no_engine_the_tray_menu_offers_the_entry_nowhere(
        self, menu_manager
    ):
        """No engine available: no submenu, and no entry anywhere.

        Both branches build the submenu only when
        ``self.stt_providers_available`` is non-empty, so an entry inside it
        disappears with it. At the top level the entry stayed visible in that
        state, enabled once the first state message arrived. The boss ruled
        the disappearance acceptable on 2026-09-16, and David confirmed it: a
        user with no speech engine set up has nothing to teach. This test
        states the accepted behaviour, so a reversal must be a deliberate edit.

        The search covers every submenu, not the top level alone, because
        "nowhere" is what the ruling says.
        """
        menu_manager.stt_providers_available = []
        entries = _tray_every_entry(menu_manager)
        assert STT_SUBMENU_TITLE not in entries, entries
        assert VOICE_TEACHING_LABEL not in entries, entries

    def test_with_no_engine_the_window_menu_offers_the_entry_nowhere(
        self, menu_manager
    ):
        """No engine available: the same in the floating button's menu."""
        menu_manager.stt_providers_available = []
        entries = _window_every_entry(menu_manager)
        assert STT_SUBMENU_TITLE not in entries, entries
        assert VOICE_TEACHING_LABEL not in entries, entries
