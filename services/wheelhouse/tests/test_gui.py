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

# wh-pytest-flaky-segfault: many classes here construct GuiManager,
# which builds real Qt widgets; without a QApplication Qt aborts the
# whole interpreter (no traceback, output lost). The session-scoped
# qapp fixture (higher scope, so instantiated before every class or
# function fixture) guarantees one exists even in isolation runs.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


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
        assert manager._get_provider_display_name("azure") == "Azure Speech"

    def test_falls_back_to_title_case(self, manager):
        manager.stt_provider_display_names = {}
        result = manager._get_provider_display_name("sherpa_zipformer")
        assert result == "Sherpa Zipformer"

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
        assert manager._is_provider_checked("google", "azure") is False


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
            'stt_providers_available': ['google', 'azure'],
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
        assert manager.stt_providers_available == ['google', 'azure']
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
        with patch("gui.notification") as mock_notification:
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
        with patch("gui.notification") as mock_notification:
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
        with patch("gui.notification") as mock_notification:
            mock_notification.notify = MagicMock()
            manager._show_first_use_hint({'action': 'click_first_use_hint'})
            mock_notification.notify.assert_not_called()

    def test_bad_payload_does_not_raise(self):
        manager = self._bare_manager()
        with patch("gui.notification") as mock_notification:
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
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'toggle_button_visibility'}
        )

    def test_toggle_interim_results_sends_command(self, manager):
        manager.toggle_interim_results()
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'toggle_interim_results'}
        )

    def test_switch_stt_provider_sends_command(self, manager):
        manager.switch_stt_provider("azure")
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'switch_stt_provider', 'provider': 'azure'}
        )

    def test_send_size_change_command(self, manager):
        manager.send_size_change_command(75)
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'set_config_value', 'key': 'FLOATING_BUTTON_SIZE', 'value': 75}
        )


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

    def test_drag_cancels_active_ptt_and_restores_speech(self, manager):
        """Drag after hold-activated PTT sends ptt_stop with drag_cancel and restores speech."""
        manager.speech_enabled = True
        manager._speech_before_hold = True
        manager._ptt_held = True
        manager._on_drag_started()
        assert manager._ptt_held is False
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"
        assert cmd["reason"] == "drag_cancel"
        # Speech should be restored to pre-hold state
        assert manager.speech_enabled is True

    def test_drag_cancel_restores_speech_off(self, manager):
        """Drag cancel with speech previously off keeps it off."""
        manager.speech_enabled = False
        manager._speech_before_hold = False
        manager._ptt_held = True
        manager._on_drag_started()
        assert manager.speech_enabled is False

    def test_drag_does_not_send_ptt_stop_when_not_held(self, manager):
        """Drag with no active PTT does not send ptt_stop."""
        manager._ptt_held = False
        manager._on_drag_started()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_cancelled_press_closes_a_microphone_the_hold_opened(self, manager):
        """The release was taken by the menu or the button being hidden.

        Without this the microphone stays open with nothing held down, which is
        the worst failure the resize gesture can cause.
        """
        manager.speech_enabled = True
        manager._speech_before_hold = False
        manager._ptt_held = True

        manager._on_press_cancelled()

        assert manager._ptt_held is False
        cmd = manager.commands_to_logic_queue.put_nowait.call_args[0][0]
        assert cmd["action"] == "ptt_stop"
        assert cmd["reason"] == "gesture_cancel"
        assert manager.speech_enabled is False

    def test_cancelled_press_restores_speech_that_was_already_on(self, manager):
        """A cancelled press decides nothing, so it must not turn speech off."""
        manager.speech_enabled = True
        manager._speech_before_hold = True
        manager._ptt_held = True

        manager._on_press_cancelled()

        assert manager.speech_enabled is True

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
# Google Cloud credentials file picker (wh-google-creds-file-picker)
# -----------------------------------------------------------------------

class TestGoogleCredentialsPicker:
    """The tray menu's Google Cloud Credentials item opens a file dialog
    and sends the chosen service-account key path to the Logic process."""

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

    def test_set_google_credentials_file_sends_command(self, manager):
        manager.set_google_credentials_file("C:/keys/sa.json")
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'set_google_credentials_file', 'path': 'C:/keys/sa.json'}
        )

    def test_picker_sends_selected_path(self, manager):
        with patch(
            "gui.QFileDialog.getOpenFileName",
            return_value=("C:/keys/sa.json", "JSON key files (*.json)"),
        ):
            manager._pick_google_credentials_file()
        manager.commands_to_logic_queue.put_nowait.assert_called_with(
            {'action': 'set_google_credentials_file', 'path': 'C:/keys/sa.json'}
        )

    def test_picker_cancel_sends_nothing(self, manager):
        with patch(
            "gui.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ):
            manager._pick_google_credentials_file()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_menu_offers_item_only_with_google_provider(self):
        """Both menu builders (pystray and Qt) show the credentials item,
        gated on the google_stt provider being installed."""
        import inspect

        import gui as gui_module

        source = inspect.getsource(gui_module.GuiManager)
        assert source.count("Google Cloud Credentials") >= 2
        assert 'google_stt' in source
        assert "open_google_credentials_picker" in source
