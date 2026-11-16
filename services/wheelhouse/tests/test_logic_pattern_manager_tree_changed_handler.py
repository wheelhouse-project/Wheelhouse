"""Logic-side consumer tests for ``pattern_manager_tree_changed``.

Bead wh-overlay-rewalk-after-filter. The Pattern Manager dialog emits
``pattern_manager_tree_changed {hwnd, sequence}`` on the
``commands_to_logic_queue`` whenever its tree changes what a UI Automation walk
would return. ``main.py``'s ``_build_gui_handler_map`` routes the action to
``LogicController._handle_pattern_manager_tree_changed``, which ``safe_parse``s
the payload (wh-uf54: a malformed payload is logged and dropped, never raised
into the command listener) and hands the two validated fields to
``_on_pattern_manager_tree_change``.

The decision itself is tested in ``test_overlay_focus_hooks.py``; these tests
cover only the handler's own two jobs -- validate, then delegate -- and the
routing trap that makes the action name what it is: the listener sends every
action starting with ``"pm_"`` to ``_handle_pattern_manager_action`` before it
consults the handler table, so a ``pm_``-prefixed name would never arrive here.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from services.wheelhouse.click_overlay_state import OverlayState
from services.wheelhouse.main import LogicController
from services.wheelhouse.numbered_overlay.controller import (
    NumberedOverlayController,
)
from services.wheelhouse.shared.pattern_manager_tree_changed import (
    ACTION_NAME,
)
from services.wheelhouse.ui.click_config import ClickConfig


def _controller():
    """A bare LogicController carrying only what the handler touches."""

    controller = object.__new__(LogicController)

    controller.click_config = ClickConfig.from_raw(
        {"enabled": True, "overlay_enabled": True}
    )
    # The outputs an overlay effect would leave by: requests to the Input
    # process (app), GUI messages (state_manager), and scheduled tasks.
    controller.app = MagicMock()
    controller.state_manager = MagicMock()
    controller.create_task_with_error_handling = MagicMock()
    # The public constructor, as main.py builds it.
    controller.overlay = NumberedOverlayController(
        controller, clock=time.monotonic
    )
    controller.overlay.on_pattern_manager_tree_change = MagicMock()  # type: ignore[method-assign]
    return controller


def test_malformed_payload_dropped_without_raising_or_touching_the_machine():
    controller = _controller()
    before = controller.overlay.click_overlay_state.state

    # Missing 'sequence': safe_parse logs and returns None, the handler returns.
    controller._handle_pattern_manager_tree_changed(
        {"action": ACTION_NAME, "hwnd": 4242}
    )

    controller.overlay.on_pattern_manager_tree_change.assert_not_called()
    # No overlay effect was performed: nothing went to the Input process or
    # the GUI, and no task was scheduled.
    assert controller.app.mock_calls == []
    assert controller.state_manager.mock_calls == []
    controller.create_task_with_error_handling.assert_not_called()
    assert controller.overlay.click_overlay_state.state is before
    assert controller.overlay.click_overlay_state.state is OverlayState.CLOSED


def test_wrong_action_payload_dropped_without_raising():
    controller = _controller()

    controller._handle_pattern_manager_tree_changed(
        {"action": "overlay_state_changed", "hwnd": 4242, "sequence": 1}
    )

    controller.overlay.on_pattern_manager_tree_change.assert_not_called()


def test_zero_hwnd_payload_dropped_without_raising():
    # 0 is the Logic side's "no window" value; the schema refuses it on the
    # wire, so the handler must drop it rather than treat it as a window.
    controller = _controller()

    controller._handle_pattern_manager_tree_changed(
        {"action": ACTION_NAME, "hwnd": 0, "sequence": 1}
    )

    controller.overlay.on_pattern_manager_tree_change.assert_not_called()


def test_the_action_name_escapes_the_pm_prefix_route():
    # main.py's listener routes every "pm_*" action to
    # _handle_pattern_manager_action BEFORE the handler table, so a pm_-
    # prefixed name would never reach the table entry asserted above.
    assert not ACTION_NAME.startswith("pm_")
