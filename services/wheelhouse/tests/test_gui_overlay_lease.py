"""GUI queue routing of the lease-renew and reset-overlay actions
(bead wh-overlay-slow-uia-stale-badges.9).

The queue pump of ``GuiManager`` routes an ``overlay_lease_renew`` action
to ``overlay_messages.handle_overlay_lease_renew``, and a ``reset_overlay``
action to ``overlay_messages.handle_reset_overlay``. A reset also ends the
walk cue. The lease, teardown retry, and reset behavior itself is tested
by the boundary tests ``tests/test_overlay_gui_requirements_*.py``.

Fixture style mirrors ``tests/test_gui_grid_overlay.py``: GuiManager with
FloatingButton / WorkingDialog / pystray / QTimer patched, collaborators
replaced with MagicMocks.
"""

from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")


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


class TestQueueDispatch:
    def test_overlay_lease_renew_routes_to_handler(self, manager):
        payload = {
            "action": "overlay_lease_renew",
            "overlay_session_id": 5,
            "paint_generation": 4,
            "lease_ms": 60000,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager.overlay_messages, "handle_overlay_lease_renew") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)

    def test_reset_overlay_routes_to_handler_and_clears_walk_cue(self, manager):
        payload = {"action": "reset_overlay"}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager.overlay_messages, "handle_reset_overlay") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)
        # A reset ends any in-flight walk from the dead Logic.
        manager.button.set_walk_cue.assert_called_with(False)
