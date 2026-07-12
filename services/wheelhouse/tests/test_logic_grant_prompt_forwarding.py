"""Tests for the Logic-process forwarding of RetryThresholdReached
events to the GUI process (wh-bqv9c).

The click counter (wh-82lnx) publishes ``RetryThresholdReached`` on the
EventBus when a tuple's verified-retry counter reaches the soft-allow
threshold. The Logic process subscribes through
``LogicController._on_retry_threshold_reached``, validates the
event, and forwards a structured payload to the GUI queue with
``action == "text_target_grant_prompt"``. The GUI's queue listener
routes the action to ``_show_grant_prompt_toast``, which renders the
follow-up toast.

Coverage:
  * A well-formed event is forwarded with all five fields populated.
  * The forwarded action key matches the GUI dispatch table value.
  * The forward gracefully degrades when ``state_manager`` or its
    ``state_to_gui_queue`` is missing (logs warning, no crash).
  * A queue ``put_nowait`` failure is handled (logs warning, no
    crash).
  * The forwarder carries no dictation text or correlation_token
    (privacy contract).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from services.wheelhouse.events import RetryThresholdReached
from services.wheelhouse.shared.text_target_grant_prompt import MSG_TYPE


def _make_event(**overrides) -> RetryThresholdReached:
    fields: dict = dict(
        process_name="zed.exe",
        class_name="zed::Workspace",
        control_type="Pane",
        app_friendly_name="Zed",
        count=3,
    )
    fields.update(overrides)
    return RetryThresholdReached(**fields)


def _make_controller():
    """Build a MagicMock(spec=LogicController) bound to the real
    forwarder method, with a mock state_manager.state_to_gui_queue."""

    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._on_retry_threshold_reached = (
        LogicController._on_retry_threshold_reached.__get__(controller)
    )
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    # wh-vdt1t: forwarder consults this set before forwarding. An
    # empty set means no suppression -- the default for these tests.
    controller._grant_prompt_no_suppressed = set()
    return controller


class TestRetryThresholdForwarding:
    async def test_well_formed_event_is_forwarded(self):
        controller = _make_controller()
        event = _make_event()

        await controller._on_retry_threshold_reached(event)

        controller.state_manager.state_to_gui_queue.put_nowait.assert_called_once()
        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        )
        # The IPC schema's MSG_TYPE is rebranded to ``action`` on the
        # GUI queue (the GUI listener dispatches by ``action``).
        assert gui_msg["action"] == MSG_TYPE
        assert gui_msg["action"] == "text_target_grant_prompt"
        assert gui_msg["process_name"] == "zed.exe"
        assert gui_msg["class_name"] == "zed::Workspace"
        assert gui_msg["control_type"] == "Pane"
        assert gui_msg["app_friendly_name"] == "Zed"
        assert gui_msg["count"] == 3

    async def test_forward_works_when_state_manager_missing(self, caplog):
        from main import LogicController

        controller = MagicMock(spec=LogicController)
        controller._on_retry_threshold_reached = (
            LogicController._on_retry_threshold_reached.__get__(controller)
        )
        controller.state_manager = None

        with caplog.at_level(logging.WARNING):
            # Should not raise.
            await controller._on_retry_threshold_reached(_make_event())

        # Some warning was logged.
        assert any(
            "grant_prompt" in rec.message.lower()
            or "state_manager" in rec.message.lower()
            for rec in caplog.records
        )

    async def test_forward_works_when_queue_put_raises(self, caplog):
        controller = _make_controller()
        controller.state_manager.state_to_gui_queue.put_nowait.side_effect = (
            RuntimeError("queue full")
        )

        with caplog.at_level(logging.WARNING):
            # Should not raise.
            await controller._on_retry_threshold_reached(_make_event())

        # Some warning was logged.
        assert any(
            "grant_prompt" in rec.message.lower()
            or "queue" in rec.message.lower()
            for rec in caplog.records
        )

    async def test_forwarded_payload_carries_no_text(self):
        """Privacy property: the forwarded message has no field that
        smells like user-typed content, and no correlation_token."""

        controller = _make_controller()

        await controller._on_retry_threshold_reached(_make_event())

        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        )
        forbidden = {
            "text", "dictation", "transcript", "utterance", "content",
            "correlation_token",
        }
        assert set(gui_msg.keys()).isdisjoint(forbidden)


class TestSubscriptionRegistration:
    """The Logic controller must register its forwarder on EventBus.

    A regression that drops the subscription would leave the click
    counter publishing into the void; the test pins the wiring at
    construction time so it cannot silently break.
    """

    def test_subscribe_registers_threshold_handler(self):
        from main import LogicController
        from event_bus import EventBus

        bus = EventBus()
        controller = MagicMock(spec=LogicController)
        controller.event_bus = bus
        controller._on_retry_threshold_reached = (
            LogicController._on_retry_threshold_reached.__get__(controller)
        )
        controller._subscribe_grant_prompt_forwarder = (
            LogicController._subscribe_grant_prompt_forwarder.__get__(controller)
        )

        controller._subscribe_grant_prompt_forwarder()

        # The subscription must register against RetryThresholdReached.
        callbacks = bus._subscribers.get(RetryThresholdReached, [])
        assert any(
            cb == controller._on_retry_threshold_reached for cb in callbacks
        ), "RetryThresholdReached was not subscribed to the forwarder"
