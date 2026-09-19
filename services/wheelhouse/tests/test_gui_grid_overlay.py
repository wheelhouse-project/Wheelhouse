"""GUI-process routing for the three mouse-grid paint actions
(bead wh-grid-paint-mode under the ``wh-mouse-grid`` molecule).

Logic puts ``paint_grid`` / ``paint_grid_pin`` / ``clear_grid`` dicts on the
Logic-to-GUI state queue; ``GuiManager._check_queues_and_events`` routes each
to a handler that validates the payload with ``safe_parse`` (wh-uf54) and
drives a DEDICATED ``OverlayPaintWindowManager`` -- separate from the
numbered overlay's and the working badge's, so the three features never tear
down each other's windows.

This file mirrors ``tests/test_gui_walk_cue.py`` (dispatch branches) and
``tests/test_working_badge_gui.py`` (dedicated-manager construction). The
``TestQueueDispatch`` class is the guard against the exact bug this slice
risks: a handler that exists but whose ``elif`` branch is missing, which
would silently drop every grid message.
"""

from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

# Imported by the SAME path gui.py uses (``shared.X``, not
# ``services.wheelhouse.shared.X``), which is the convention already split
# between gui.py and main.py for the numbered overlay's schemas. Both paths
# resolve, but they produce two distinct module objects and therefore two
# distinct dataclasses, so an event built through one path never compares
# equal to one built through the other. Production is unaffected (only
# ``to_dict`` output crosses the process boundary), but the handler
# assertions below compare instances, so they must use gui.py's path.
from shared.clear_grid import ClearGridEvent
from shared.paint_grid import PaintGridEvent
from shared.paint_grid_pin import PaintGridPinEvent


def _grid_event() -> PaintGridEvent:
    return PaintGridEvent(
        monitor_left=0,
        monitor_top=0,
        monitor_width=1920,
        monitor_height=1080,
        left=640,
        top=360,
        width=640,
        height=360,
    )


@pytest.fixture
def manager(qapp):
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, MagicMock(), MagicMock())
        return mgr


class TestGridOverlayConstruction:
    def test_dedicated_grid_manager_is_built(self, manager):
        assert manager._grid_overlay is not None
        # Distinct from the numbered overlay's manager: a numbered-overlay
        # clear must never destroy the grid's windows, and vice versa.
        assert manager._grid_overlay is not manager._overlay_manager

    def test_construction_failure_leaves_the_grid_unavailable(self, qapp):
        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"), \
             patch(
                 "overlay_paint_window.OverlayPaintWindowManager",
                 side_effect=OSError("no win32"),
             ):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            mgr = GuiManager(shutdown, MagicMock(), MagicMock())
        assert mgr._grid_overlay is None
        # Handling a message with no manager is a harmless no-op.
        mgr._handle_paint_grid(_grid_event().to_dict())


class TestQueueDispatch:
    def test_paint_grid_action_routes_to_handler(self, manager):
        payload = _grid_event().to_dict()
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager, "_handle_paint_grid") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)

    def test_paint_grid_pin_action_routes_to_handler(self, manager):
        payload = PaintGridPinEvent(x=10, y=20).to_dict()
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager, "_handle_paint_grid_pin") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)

    def test_clear_grid_action_routes_to_handler(self, manager):
        payload = ClearGridEvent().to_dict()
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager, "_handle_clear_grid") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)


class TestGridHandlers:
    def test_paint_grid_passes_the_parsed_event(self, manager):
        manager._grid_overlay = MagicMock()
        event = _grid_event()

        manager._handle_paint_grid(event.to_dict())

        manager._grid_overlay.paint_grid.assert_called_once_with(event)

    def test_paint_grid_pin_passes_the_parsed_event(self, manager):
        manager._grid_overlay = MagicMock()
        event = PaintGridPinEvent(x=-40, y=15)

        manager._handle_paint_grid_pin(event.to_dict())

        manager._grid_overlay.paint_grid_pin.assert_called_once_with(event)

    def test_clear_grid_calls_clear(self, manager):
        manager._grid_overlay = MagicMock()

        manager._handle_clear_grid(ClearGridEvent().to_dict())

        manager._grid_overlay.clear_grid.assert_called_once_with()

    def test_malformed_paint_grid_is_dropped(self, manager):
        manager._grid_overlay = MagicMock()

        manager._handle_paint_grid({"action": "paint_grid", "left": "x"})

        manager._grid_overlay.paint_grid.assert_not_called()

    def test_malformed_pin_is_dropped(self, manager):
        manager._grid_overlay = MagicMock()

        manager._handle_paint_grid_pin({"action": "paint_grid_pin", "x": 1})

        manager._grid_overlay.paint_grid_pin.assert_not_called()

    def test_manager_exception_does_not_escape(self, manager):
        manager._grid_overlay = MagicMock()
        manager._grid_overlay.paint_grid.side_effect = RuntimeError("boom")

        manager._handle_paint_grid(_grid_event().to_dict())
