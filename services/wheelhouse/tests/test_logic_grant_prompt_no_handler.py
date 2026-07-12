"""Tests for the Logic-side grant_prompt_no_clicked handler and the
suppression hook in _on_retry_threshold_reached (wh-vdt1t).

When the GUI emits ``grant_prompt_no_clicked`` carrying the identity
tuple, the Logic handler records the tuple in
``_grant_prompt_no_suppressed`` (a per-run set). The forwarder
``_on_retry_threshold_reached`` consults this set BEFORE forwarding
the text_target_grant_prompt action; suppressed tuples drop the
forward silently so the follow-up toast does not re-fire during the
current run, even across a GUI restart (the resolution to
wh-vbvgf.7.1 deferred from the wh-bqv9c codex review).

The counter is intentionally NOT reset on No (per bead spec wh-vdt1t).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock


def _payload(**overrides) -> dict:
    base = {
        "action": "grant_prompt_no_clicked",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
    }
    base.update(overrides)
    return base


def _make_controller(declined_path=None):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._handle_grant_prompt_no_clicked = (
        LogicController._handle_grant_prompt_no_clicked.__get__(controller)
    )
    controller._on_retry_threshold_reached = (
        LogicController._on_retry_threshold_reached.__get__(controller)
    )
    # wh-27gvv: the No handler now calls self.add_declined which writes
    # to disk and on success updates the in-memory set. Bind the real
    # method so the persistence path runs and the existing assertions
    # on _grant_prompt_no_suppressed still hold.
    controller.add_declined = (
        LogicController.add_declined.__get__(controller)
    )
    controller._resolve_declined_path = (
        LogicController._resolve_declined_path.__get__(controller)
    )
    controller._declined_path = declined_path
    controller._grant_prompt_no_suppressed = set()
    controller.click_counter = MagicMock()
    controller.click_counter.reset_tuple = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    return controller


class TestNoHandler:
    async def test_no_click_records_tuple_in_suppression_set(self, tmp_path):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )

        await controller._handle_grant_prompt_no_clicked(_payload())

        assert ("zed.exe", "zed::Workspace", "Pane") in (
            controller._grant_prompt_no_suppressed
        )

    async def test_no_click_does_not_reset_counter(self, tmp_path):
        """Per bead spec wh-vdt1t: 'On No click, logic process leaves
        the counter alone. It does NOT reset to 0...'."""

        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )

        await controller._handle_grant_prompt_no_clicked(_payload())

        controller.click_counter.reset_tuple.assert_not_called()

    async def test_malformed_payload_drops_without_recording(
        self, tmp_path, caplog,
    ):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        bad = {"action": "grant_prompt_no_clicked"}  # missing fields

        with caplog.at_level(logging.WARNING):
            await controller._handle_grant_prompt_no_clicked(bad)

        assert controller._grant_prompt_no_suppressed == set()


class TestSuppressionHook:
    """The forwarder _on_retry_threshold_reached must consult the
    suppression set and drop the GUI forward when the tuple is in it."""

    async def test_suppressed_tuple_does_not_forward(self):
        from services.wheelhouse.events import RetryThresholdReached

        controller = _make_controller()
        controller._grant_prompt_no_suppressed.add(
            ("zed.exe", "zed::Workspace", "Pane"),
        )

        await controller._on_retry_threshold_reached(RetryThresholdReached(
            process_name="zed.exe",
            class_name="zed::Workspace",
            control_type="Pane",
            app_friendly_name="Zed",
            count=4,
        ))

        controller.state_manager.state_to_gui_queue.put_nowait.assert_not_called()

    async def test_unsuppressed_tuple_forwards_normally(self):
        from services.wheelhouse.events import RetryThresholdReached

        controller = _make_controller()
        # Different tuple is in the suppression set; the incoming
        # event must still forward.
        controller._grant_prompt_no_suppressed.add(
            ("notepad.exe", "Edit", "Document"),
        )

        await controller._on_retry_threshold_reached(RetryThresholdReached(
            process_name="zed.exe",
            class_name="zed::Workspace",
            control_type="Pane",
            app_friendly_name="Zed",
            count=3,
        ))

        controller.state_manager.state_to_gui_queue.put_nowait.assert_called_once()


class TestDispatchBinding:
    """wh-vbvgf.13.3 (deepseek review): real dispatch test. Exercises
    the action -> handler binding via _build_gui_handler_map. A
    copy-paste error that wires the wrong handler under the right
    key fails this test instead of stranding the click in production.
    The source-inspection regression net (TestHandlerMapRouting
    below) is kept as cheap belt-and-braces."""

    def _build_dispatch_controller(self):
        from main import LogicController

        controller = MagicMock(spec=LogicController)
        controller._build_gui_handler_map = (
            LogicController._build_gui_handler_map.__get__(controller)
        )
        # _build_gui_handler_map evaluates several
        # self.state_manager.X expressions at dict-build time; supply
        # a mock so the dict construction doesn't fail.
        controller.state_manager = MagicMock()
        controller.create_task_with_error_handling = MagicMock()
        return controller

    def test_no_action_routes_to_no_handler(self):
        from unittest.mock import patch

        controller = self._build_dispatch_controller()
        command = {
            "action": "grant_prompt_no_clicked",
            "process_name": "zed.exe",
            "class_name": "zed::Workspace",
            "control_type": "Pane",
        }

        with patch.object(
            controller, "_handle_grant_prompt_no_clicked"
        ) as mock_no, patch.object(
            controller, "_handle_grant_prompt_yes_clicked"
        ) as mock_yes:
            handler_map = controller._build_gui_handler_map(command)
            handler_map["grant_prompt_no_clicked"]()

        mock_no.assert_called_once()
        mock_yes.assert_not_called()

    def test_yes_action_routes_to_yes_handler(self):
        from unittest.mock import patch

        controller = self._build_dispatch_controller()
        command = {
            "action": "grant_prompt_yes_clicked",
            "process_name": "zed.exe",
            "class_name": "zed::Workspace",
            "control_type": "Pane",
        }

        with patch.object(
            controller, "_handle_grant_prompt_yes_clicked"
        ) as mock_yes, patch.object(
            controller, "_handle_grant_prompt_no_clicked"
        ) as mock_no:
            handler_map = controller._build_gui_handler_map(command)
            handler_map["grant_prompt_yes_clicked"]()

        mock_yes.assert_called_once()
        mock_no.assert_not_called()


class TestHandlerMapRouting:
    def test_listener_source_routes_grant_prompt_no_clicked(self):
        """A typo in the action key or handler method name must fail
        this test instead of stranding the No click in production."""

        import inspect
        from main import LogicController

        source = inspect.getsource(LogicController._build_gui_handler_map)
        assert '"grant_prompt_no_clicked"' in source, (
            "handler_map is missing the 'grant_prompt_no_clicked' action key"
        )
        assert "_handle_grant_prompt_no_clicked" in source, (
            "handler_map is not wired to _handle_grant_prompt_no_clicked"
        )

    def test_action_name_matches_schema_constant(self):
        import inspect
        from main import LogicController
        from shared.grant_prompt_no_clicked import ACTION_NAME

        source = inspect.getsource(LogicController._build_gui_handler_map)
        assert f'"{ACTION_NAME}"' in source
