"""Tests for the Logic-process forwarding of text_target_rejected events
to the GUI process (wh-xxko1).

The Input process emits a ``text_target_rejected`` event when the
text-target predicate hard-rejects the focused control. The Logic
process's ``_handle_input_event`` validates the payload via
``TextTargetRejectedEvent.from_dict`` and forwards it to the GUI as a
``show_rejection_toast`` action. The forwarded message carries every
rendering field plus the correlation_token so the GUI can branch
wording and the optional Phase 4 retry click can match the toast back
to the rejection.

A malformed payload must not crash the logic loop. The handler logs
a warning and drops the event (wh-uf54).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from shared.text_target_rejection import (
    TextTargetRejectedEvent,
    new_correlation_token,
)


def _make_event_dict(**overrides) -> dict:
    base = TextTargetRejectedEvent(
        process_name="zed.exe",
        class_name="Zed::Window",
        control_type="WindowControl",
        reason="default_reject_paste_capable_class",
        supported_patterns=("Invoke",),
        app_friendly_name="Zed Editor",
        correlation_token=new_correlation_token(),
    ).to_dict()
    base.update(overrides)
    return base


class TestRejectionEventForwarding:
    def test_well_formed_event_is_forwarded_to_gui(self):
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller._handle_input_event = (
            LogicController._handle_input_event.__get__(controller)
        )
        controller._forward_rejection_event_to_gui = (
            LogicController._forward_rejection_event_to_gui.__get__(controller)
        )
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        event = _make_event_dict()
        controller._handle_input_event(event)

        controller.state_manager.state_to_gui_queue.put_nowait.assert_called_once()
        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        )
        assert gui_msg["action"] == "show_rejection_toast"
        assert gui_msg["process_name"] == "zed.exe"
        assert gui_msg["class_name"] == "Zed::Window"
        assert gui_msg["control_type"] == "WindowControl"
        assert gui_msg["reason"] == "default_reject_paste_capable_class"
        assert gui_msg["supported_patterns"] == ("Invoke",)
        assert gui_msg["app_friendly_name"] == "Zed Editor"
        assert gui_msg["correlation_token"] == event["correlation_token"]

    def test_malformed_event_does_not_crash(self, caplog):
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller._handle_input_event = (
            LogicController._handle_input_event.__get__(controller)
        )
        controller._forward_rejection_event_to_gui = (
            LogicController._forward_rejection_event_to_gui.__get__(controller)
        )
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        bad = {
            "type": "text_target_rejected",
            "process_name": "zed.exe",
            # missing class_name, control_type, reason, supported_patterns,
            # app_friendly_name, correlation_token
        }
        with caplog.at_level(logging.WARNING):
            controller._handle_input_event(bad)
        # No forward.
        controller.state_manager.state_to_gui_queue.put_nowait.assert_not_called()
        # Warning logged.
        assert any(
            "text_target_rejected" in rec.message.lower()
            or "rejection" in rec.message.lower()
            for rec in caplog.records
        )

    def test_forward_works_when_state_manager_missing(self, caplog):
        # Defensive: if the state_manager is somehow unavailable when the
        # event arrives (process startup ordering), the handler logs and
        # drops the event rather than raising AttributeError.
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller._handle_input_event = (
            LogicController._handle_input_event.__get__(controller)
        )
        controller._forward_rejection_event_to_gui = (
            LogicController._forward_rejection_event_to_gui.__get__(controller)
        )
        controller.state_manager = None

        with caplog.at_level(logging.WARNING):
            controller._handle_input_event(_make_event_dict())

    def test_other_event_types_unaffected(self):
        # Adding the rejection branch must not break the existing
        # te_event path.
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller._handle_input_event = (
            LogicController._handle_input_event.__get__(controller)
        )
        controller._forward_te_event_to_gui = MagicMock()
        controller.state_manager = MagicMock()
        controller.state_manager.speech_notifier = MagicMock()

        controller._handle_input_event(
            {"type": "te_event", "event": "submit"}
        )
        controller._forward_te_event_to_gui.assert_called_once()
