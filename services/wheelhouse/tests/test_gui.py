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

    def test_indeterminate_color_before_initial_state(self, manager):
        manager.initial_state_received = False
        with patch("gui.create_icon_image") as mock_create:
            mock_create.return_value = MagicMock()
            manager.update_tray_menu()
            mock_create.assert_called_with((100, 100, 100))

    def test_enabled_color_when_speech_active(self, manager):
        manager.initial_state_received = True
        manager.speech_enabled = True
        with patch("gui.create_icon_image") as mock_create:
            mock_create.return_value = MagicMock()
            manager.update_tray_menu()
            mock_create.assert_called_with((200, 0, 0))

    def test_disabled_color_when_speech_inactive(self, manager):
        manager.initial_state_received = True
        manager.speech_enabled = False
        with patch("gui.create_icon_image") as mock_create:
            mock_create.return_value = MagicMock()
            manager.update_tray_menu()
            mock_create.assert_called_with((160, 160, 160))


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

    def test_second_release_after_double_click_is_ignored(self, manager):
        """The mouseReleaseEvent after a double-click should not trigger action."""
        manager.speech_interaction_mode = "toggle"
        manager._on_double_click()
        manager.commands_to_logic_queue.put_nowait.reset_mock()
        # Second release after double-click
        manager._on_button_release()
        # Should not send any additional command
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
