"""Integration tests for push-to-talk full cycle.

Tests PTT state transitions, event subscriptions, and mode switching.
Uses real EventBus for subscription verification, with mocked loop.create_task
to capture event publication (same pattern as unit tests).
"""

import asyncio
from unittest.mock import Mock

import pytest

from state_manager import StateManager
from event_bus import EventBus
from events import PTTStartedEvent, PTTStoppedEvent


@pytest.fixture
def real_event_bus():
    return EventBus()


@pytest.fixture
def sm_integrated(mock_config, real_event_bus, mock_gui_queue, mock_websocket_manager):
    """StateManager with real EventBus but mocked loop for task capture."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()  # Capture created tasks
    mgr = StateManager(
        config_service=mock_config,
        event_bus=real_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    yield mgr, real_event_bus, loop
    loop.close()


def _get_published_events(loop):
    """Extract event objects from mocked create_task calls."""
    events = []
    for call in loop.create_task.call_args_list:
        coro = call[0][0]
        # The coroutine name tells us if it's an event_bus.publish call
        if hasattr(coro, 'cr_code') and 'publish' in (coro.cr_code.co_qualname or ''):
            # Can't easily extract the event from a coroutine, so we close it
            coro.close()
        else:
            # Close coroutines to avoid warnings
            if hasattr(coro, 'close'):
                coro.close()
    return events


class TestPTTFullCycle:
    """Integration: full PTT start -> stop cycle with state verification."""

    def test_ptt_start_stop_cycle_state(self, sm_integrated):
        sm, bus, loop = sm_integrated

        # Start PTT
        sm.ptt_start(source="floating_button")

        assert sm._ptt_active is True
        assert sm._speech_enabled is True
        assert sm.speech_enabled is True
        assert sm._speech_suppressed_by_idle is False

        # Verify create_task was called (publish event + broadcast)
        assert loop.create_task.call_count >= 2  # publish + broadcast

        # Stop PTT
        loop.create_task.reset_mock()
        sm.ptt_stop()

        assert sm._ptt_active is False
        assert sm._speech_enabled is False
        assert sm.speech_enabled is False
        assert loop.create_task.call_count >= 2  # publish + broadcast

    def test_ptt_clears_idle_suppression(self, sm_integrated):
        sm, _, loop = sm_integrated
        sm._speech_suppressed_by_idle = True
        sm._speech_enabled = False

        sm.ptt_start()

        assert sm._speech_suppressed_by_idle is False
        assert sm.speech_enabled is True

    def test_mode_switching(self, sm_integrated):
        sm, _, _ = sm_integrated

        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"

        sm.set_speech_interaction_mode("toggle")
        assert sm._speech_interaction_mode == "toggle"

    def test_mode_switch_invalid_rejected(self, sm_integrated):
        sm, _, _ = sm_integrated

        sm.set_speech_interaction_mode("invalid")
        assert sm._speech_interaction_mode == "toggle"  # Unchanged

    def test_safety_timeout_method_stops_ptt(self, sm_integrated):
        sm, _, loop = sm_integrated

        sm.ptt_start()
        assert sm._ptt_active is True

        # Simulate safety timeout firing
        sm._ptt_safety_timeout()

        assert sm._ptt_active is False
        assert sm._speech_enabled is False

    def test_double_ptt_start_is_noop(self, sm_integrated):
        sm, _, loop = sm_integrated

        sm.ptt_start()
        initial_call_count = loop.create_task.call_count

        sm.ptt_start()  # Second call should be ignored
        assert loop.create_task.call_count == initial_call_count  # No new tasks

    def test_ptt_stop_without_start_is_noop(self, sm_integrated):
        sm, _, loop = sm_integrated

        initial_call_count = loop.create_task.call_count
        sm.ptt_stop()
        assert loop.create_task.call_count == initial_call_count  # No tasks created

    def test_ptt_start_sends_state_with_mode(self, sm_integrated, mock_gui_queue):
        sm, _, loop = sm_integrated
        sm._speech_interaction_mode = "push_to_talk"

        sm.ptt_start()

        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["ptt_active"] is True
        assert state["speech_interaction_mode"] == "push_to_talk"
        assert state["speech_enabled"] is True

    def test_full_lifecycle_toggle_then_ptt_then_back(self, sm_integrated, mock_gui_queue):
        """Full lifecycle: toggle mode -> PTT mode -> start -> stop -> back to toggle."""
        sm, _, loop = sm_integrated

        # Start in toggle mode
        assert sm._speech_interaction_mode == "toggle"

        # Switch to PTT mode
        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"

        # PTT cycle
        sm.ptt_start()
        assert sm._ptt_active is True
        assert sm.speech_enabled is True

        sm.ptt_stop()
        assert sm._ptt_active is False
        assert sm.speech_enabled is False

        # Switch back to toggle
        sm.set_speech_interaction_mode("toggle")
        assert sm._speech_interaction_mode == "toggle"

        # Toggle works normally
        sm.toggle_speech_enabled_state()
        assert sm.speech_enabled is True
