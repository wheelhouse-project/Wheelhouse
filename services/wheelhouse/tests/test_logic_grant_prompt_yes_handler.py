"""Tests for the Logic-side grant_prompt_yes_clicked handler (wh-8d81z).

When the GUI emits ``grant_prompt_yes_clicked`` carrying the identity
tuple, the Logic handler:

  1. Validates the payload via
     ``GrantPromptYesClickedEvent.from_dict`` -- malformed payloads
     are logged and dropped (wh-uf54).
  2. Calls ``self.add_soft_allow(process_name, class_name, control_type)``
     -- this writes the soft-allow file and on success sends
     ``add_soft_allow_tuple`` IPC to the input process. On disk-write
     failure ``add_soft_allow`` already enqueues a
     ``soft_allow_write_failed`` event on the GUI state queue.
  3. On ``add_soft_allow`` success, resets the click counter for the
     tuple via ``self.click_counter.reset_tuple(...)``.
  4. On ``add_soft_allow`` failure, the counter is NOT reset (per the
     bead spec) so the user can click Yes again later.

Coverage:
  * Success path: add_soft_allow + reset_tuple both fire.
  * Disk-write failure: add_soft_allow fires, reset_tuple does NOT.
  * IPC failure (after disk success): same -- reset_tuple does NOT
    fire because add_soft_allow returned False.
  * Malformed payload: handler logs and drops without calling
    add_soft_allow.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock


def _payload(**overrides) -> dict:
    base = {
        "action": "grant_prompt_yes_clicked",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
    }
    base.update(overrides)
    return base


def _make_controller():
    from main import LogicController, AddSoftAllowOutcome

    controller = MagicMock(spec=LogicController)
    controller._handle_grant_prompt_yes_clicked = (
        LogicController._handle_grant_prompt_yes_clicked.__get__(controller)
    )
    controller.add_soft_allow = AsyncMock(
        return_value=AddSoftAllowOutcome.SUCCESS
    )
    controller.click_counter = MagicMock()
    controller.click_counter.reset_tuple = AsyncMock()
    # By default the counter is fully zeroed after reset; tests that
    # exercise the race-guard set get_count.side_effect to a list so
    # the first check returns non-zero and the second returns zero.
    controller.click_counter.get_count = MagicMock(return_value=0)
    return controller


class TestSuccessPath:
    async def test_success_calls_add_soft_allow_and_reset(self):
        from main import AddSoftAllowOutcome

        controller = _make_controller()
        controller.add_soft_allow.return_value = AddSoftAllowOutcome.SUCCESS

        await controller._handle_grant_prompt_yes_clicked(_payload())

        controller.add_soft_allow.assert_awaited_once_with(
            "zed.exe", "zed::Workspace", "Pane",
        )
        controller.click_counter.reset_tuple.assert_awaited_once_with(
            "zed.exe", "zed::Workspace", "Pane",
        )

    async def test_success_call_order(self):
        """add_soft_allow must complete BEFORE reset_tuple. Resetting
        before the disk write succeeded would lose the count if the
        write later failed."""

        from main import AddSoftAllowOutcome

        controller = _make_controller()
        order: list[str] = []

        async def add_first(*args, **kwargs):
            order.append("add_soft_allow")
            return AddSoftAllowOutcome.SUCCESS

        async def reset_after(*args, **kwargs):
            order.append("reset_tuple")

        controller.add_soft_allow.side_effect = add_first
        controller.click_counter.reset_tuple.side_effect = reset_after

        await controller._handle_grant_prompt_yes_clicked(_payload())

        assert order == ["add_soft_allow", "reset_tuple"]

    async def test_ipc_failure_still_resets_counter(self):
        """wh-vbvgf.9.2 (codex review): when the disk write succeeds
        but the IPC send fails, the soft-allow tuple is durable on
        disk and the input process picks it up on the next launcher
        run. The counter must be reset because the soft-allow file
        owns the grant going forward."""

        from main import AddSoftAllowOutcome

        controller = _make_controller()
        controller.add_soft_allow.return_value = AddSoftAllowOutcome.IPC_FAILED

        await controller._handle_grant_prompt_yes_clicked(_payload())

        controller.click_counter.reset_tuple.assert_awaited_once_with(
            "zed.exe", "zed::Workspace", "Pane",
        )


class TestFailurePath:
    async def test_disk_failure_does_not_reset_counter(self):
        from main import AddSoftAllowOutcome

        controller = _make_controller()
        controller.add_soft_allow.return_value = AddSoftAllowOutcome.DISK_FAILED

        await controller._handle_grant_prompt_yes_clicked(_payload())

        controller.add_soft_allow.assert_awaited_once()
        controller.click_counter.reset_tuple.assert_not_called()

    async def test_add_soft_allow_exception_does_not_reset(self, caplog):
        controller = _make_controller()
        controller.add_soft_allow.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.WARNING):
            # Handler must not propagate the exception -- a bug in
            # add_soft_allow should not crash the GUI command listener.
            await controller._handle_grant_prompt_yes_clicked(_payload())

        controller.click_counter.reset_tuple.assert_not_called()


class TestSchemaValidation:
    async def test_malformed_payload_drops_without_add_soft_allow(self, caplog):
        controller = _make_controller()
        bad = {"action": "grant_prompt_yes_clicked"}  # missing fields

        with caplog.at_level(logging.WARNING):
            await controller._handle_grant_prompt_yes_clicked(bad)

        controller.add_soft_allow.assert_not_called()
        controller.click_counter.reset_tuple.assert_not_called()

    async def test_wrong_action_drops_without_add_soft_allow(self, caplog):
        controller = _make_controller()
        bad = {
            "action": "try_anyway_clicked",  # wrong
            "process_name": "zed.exe",
            "class_name": "Z",
            "control_type": "Pane",
        }

        with caplog.at_level(logging.WARNING):
            await controller._handle_grant_prompt_yes_clicked(bad)

        controller.add_soft_allow.assert_not_called()
        controller.click_counter.reset_tuple.assert_not_called()


class TestResetRaceGuard:
    """wh-reset-race-concurrent-verified (deepseek review): if a
    RetryVerified for the same tuple acquires the per-tuple
    asyncio.Lock between the Yes handler's reset_tuple call and the
    next grant-prompt cycle, the counter ends at one instead of zero.
    The Yes handler now checks get_count after reset and re-resets if
    a concurrent increment slipped in."""

    async def test_reset_again_when_count_nonzero_after_first_reset(self):
        controller = _make_controller()
        # Simulate the race: first get_count returns 1 (a verify
        # raced with the reset), second returns 0 (after the
        # second reset).
        controller.click_counter.get_count = MagicMock(side_effect=[1, 0])

        await controller._handle_grant_prompt_yes_clicked(_payload())

        # reset_tuple was awaited twice -- once initial, once for
        # the race guard.
        assert controller.click_counter.reset_tuple.await_count == 2

    async def test_no_extra_reset_when_count_zero_after_first_reset(self):
        controller = _make_controller()
        controller.click_counter.get_count = MagicMock(return_value=0)

        await controller._handle_grant_prompt_yes_clicked(_payload())

        # reset_tuple was awaited exactly once (the happy path).
        controller.click_counter.reset_tuple.assert_awaited_once()


class TestHandlerMapRouting:
    """wh-vbvgf.9.3 (codex review): regression net for the action
    string in handler_map. A typo or deletion would let the schema
    tests, the GUI emit tests, and the direct handler tests all pass
    while the Yes click never reaches Logic in production."""

    def test_listener_source_routes_grant_prompt_yes_clicked(self):
        """The dispatch action and handler name both appear inline in
        the handler_map builder. Inspect the source so a future typo
        in either the action key or the handler method name fails
        this test instead of stranding the click in production."""

        import inspect
        from main import LogicController

        source = inspect.getsource(LogicController._build_gui_handler_map)
        assert '"grant_prompt_yes_clicked"' in source, (
            "handler_map is missing the 'grant_prompt_yes_clicked' action key"
        )
        assert "_handle_grant_prompt_yes_clicked" in source, (
            "handler_map is not wired to _handle_grant_prompt_yes_clicked"
        )

    def test_action_name_matches_schema_constant(self):
        """The action string in the handler_map must match the
        ACTION_NAME constant in the schema module so a future rename
        in the schema cannot silently desynchronise from the
        listener."""

        import inspect
        from main import LogicController
        from shared.grant_prompt_yes_clicked import ACTION_NAME

        source = inspect.getsource(LogicController._build_gui_handler_map)
        assert f'"{ACTION_NAME}"' in source, (
            f"handler_map is missing the action string '{ACTION_NAME}' "
            "from the schema module"
        )


class TestPrivacy:
    async def test_handler_does_not_attempt_to_read_text_fields(self):
        """A future malicious or buggy GUI sender must not be able to
        coax the handler into reading dictation text. The schema
        validation pass already strips unknown fields, but assert that
        the handler does not consult any forbidden keys directly.
        """

        controller = _make_controller()
        payload = _payload()
        payload["text"] = "this should be ignored"
        payload["dictation"] = "this too"
        payload["correlation_token"] = "11111111-1111-4111-8111-111111111111"

        await controller._handle_grant_prompt_yes_clicked(payload)

        # add_soft_allow received exactly the three identity fields.
        controller.add_soft_allow.assert_awaited_once_with(
            "zed.exe", "zed::Workspace", "Pane",
        )
