"""Tests for the soft-allow persistence layer (wh-9weum Phase 3).

Covers the loader (wh-e22yg), the runtime add_soft_allow method
(wh-wjagd), and the predicate's soft-allow accept tier
(wh-soft-allow-verdict-tier): a tuple in the soft-allow set produces
verdict=True with reason ``accept_soft_allow_tuple`` so the router
routes to ClipboardOnlyStrategy without the override toast. Unknown
tuples keep the soft-reject reason ``default_reject_paste_capable_class``
so the router emits the rejection toast that fronts the override flow.

The loader hardens against missing and malformed files: missing file
returns an empty set silently, malformed file returns an empty set and
logs a WARNING. Failure modes never crash the predicate.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import uiautomation as auto

from ui.text_target import TextTargetPredicate


def _ctrl(*, control_type=auto.ControlType.PaneControl,
          control_type_name="PaneControl",
          class_name="Zed::Window",
          has_text_pattern=False,
          has_value_pattern=False,
          is_focusable=True):
    """Build a mock UIA control for soft-allow predicate tests.

    Defaults match the soft-reject path: no TextPattern, ClassName
    populated, ControlType outside the denylist.
    """
    ctrl = MagicMock()
    ctrl.ControlType = int(control_type)
    ctrl.ControlTypeName = control_type_name
    ctrl.ClassName = class_name
    ctrl.IsKeyboardFocusable = is_focusable

    def get_pattern(pid):
        if pid == auto.PatternId.TextPattern and has_text_pattern:
            return MagicMock(name="TextPattern")
        if pid == auto.PatternId.ValuePattern and has_value_pattern:
            return MagicMock(name="ValuePattern")
        return None

    ctrl.GetPattern.side_effect = get_pattern
    return ctrl


# --- Loader ----------------------------------------------------------------


class TestLoader:
    def test_missing_file_yields_empty_set(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        # Path does not exist on disk.
        predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset()

    def test_empty_file_yields_empty_set(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text("", encoding="utf-8")
        predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset()

    def test_file_without_entries_section_yields_empty_set(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "# leading comments only\n# no entries yet\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset()

    def test_malformed_toml_yields_empty_set_and_warns(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "soft_allow_tuples.toml"
        # Trailing equals with no value and unbalanced quotes is invalid.
        path.write_text(
            "[[entries]]\nprocess_name = 'zed.exe\nclass_name = =\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset()
        assert any(
            "soft_allow" in record.message.lower()
            for record in caplog.records
        )

    def test_populated_file_loads_into_frozenset(self, tmp_path):
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-04-30T15:00:00Z'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'sublime_text.exe'\n"
            "class_name = 'PX_WINDOW_CLASS'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-01T10:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
            ("sublime_text.exe", "PX_WINDOW_CLASS", "WindowControl"),
        })

    def test_entries_missing_required_keys_are_skipped(self, tmp_path):
        # An entry missing a required key is dropped without aborting the
        # whole file. The valid entry still loads.
        path = tmp_path / "soft_allow_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'incomplete.exe'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-04-30T15:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(soft_allow_path=path)
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })


# --- Soft-allow predicate behaviour ----------------------------------------


class TestSoftAllowVerdict:
    def test_known_tuple_accepts_with_soft_allow_reason(self):
        # wh-soft-allow-verdict-tier: a tuple in the soft-allow set is
        # now an accept verdict with the dedicated reason
        # accept_soft_allow_tuple. The router branches on this reason to
        # route to ClipboardOnlyStrategy (silent paste). The predicate
        # records the focused control's class, control type, and process
        # so accept-side telemetry stays meaningful.
        predicate = TextTargetPredicate(
            soft_allow_tuples=[
                ("zed.exe", "Zed::Window", "WindowControl"),
            ],
        )
        ctrl = _ctrl(
            control_type=auto.ControlType.WindowControl,
            control_type_name="WindowControl",
            class_name="Zed::Window",
        )
        v = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert v.verdict is True
        assert v.reason == "accept_soft_allow_tuple"
        assert v.control_type == "WindowControl"
        assert v.class_name == "Zed::Window"
        assert v.process_name == "zed.exe"

    def test_unknown_tuple_keeps_soft_reject_reason(self):
        # Without the soft-allow entry, evaluate still emits the soft
        # reject reason so the router can route to the rejection toast
        # (which fronts the Try-it-anyway override flow). The split
        # between known-tuple accept and unknown-tuple soft-reject is
        # the wh-soft-allow-verdict-tier contract.
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        ctrl = _ctrl(
            control_type=auto.ControlType.WindowControl,
            control_type_name="WindowControl",
            class_name="Zed::Window",
        )
        v = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_partial_tuple_match_does_not_accept(self):
        # The soft-allow lookup keys on the full (process, class,
        # control_type) triple. A focus that matches only two of the
        # three fields must NOT accept.
        predicate = TextTargetPredicate(
            soft_allow_tuples=[
                ("zed.exe", "Zed::Window", "WindowControl"),
            ],
        )
        # Same process and class, different control type.
        ctrl = _ctrl(
            control_type=auto.ControlType.PaneControl,
            control_type_name="PaneControl",
            class_name="Zed::Window",
        )
        v = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_add_soft_allow_promotes_next_evaluate_to_accept(self):
        # Runtime add_soft_allow (the wh-9weum Phase 4 grant prompt
        # invokes this via the input-process IPC handler) must change
        # the next evaluate from soft-reject to accept_soft_allow_tuple
        # immediately, no restart required.
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        ctrl = _ctrl(
            control_type=auto.ControlType.WindowControl,
            control_type_name="WindowControl",
            class_name="Zed::Window",
        )
        before = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert before.verdict is False
        assert before.reason == "default_reject_paste_capable_class"

        predicate.add_soft_allow(("zed.exe", "Zed::Window", "WindowControl"))

        after = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert after.verdict is True
        assert after.reason == "accept_soft_allow_tuple"


# --- Runtime add_soft_allow (wh-wjagd) ------------------------------------


class TestAddSoftAllow:
    def test_add_soft_allow_appends_to_set(self):
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        assert predicate.soft_allow_tuples == frozenset()
        predicate.add_soft_allow(("zed.exe", "Zed::Window", "WindowControl"))
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })

    def test_add_soft_allow_is_idempotent(self):
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        predicate.add_soft_allow(("zed.exe", "Zed::Window", "WindowControl"))
        predicate.add_soft_allow(("zed.exe", "Zed::Window", "WindowControl"))
        assert len(predicate.soft_allow_tuples) == 1

    def test_add_soft_allow_makes_evaluate_visible_immediately(self):
        # The runtime add must affect the next evaluate call -- the
        # predicate keeps state across calls, so the in-memory set has
        # to update without a restart.
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        predicate.add_soft_allow(("zed.exe", "Zed::Window", "WindowControl"))
        assert ("zed.exe", "Zed::Window", "WindowControl") in (
            predicate.soft_allow_tuples
        )


# --- IPC end-to-end (wh-01t75) ---------------------------------------------


class TestLogicAddSoftAllow:
    """Tests for LogicController.add_soft_allow.

    The method writes the file first via append_soft_allow_tuple, then
    sends the IPC command to the input process. On a write failure the
    IPC is NOT sent and a soft_allow_write_failed event is enqueued on
    the GUI state queue (Phase 4 will surface a 'couldn't save' toast).
    """

    @pytest.fixture
    def stub_controller(self, tmp_path, monkeypatch):
        """Build a minimal stub with the attributes add_soft_allow uses."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        # Stub state_manager -> state_to_gui_queue with put_nowait.
        gui_queue = SimpleNamespace(
            messages=[],
        )

        def _put_nowait(msg):
            gui_queue.messages.append(msg)

        gui_queue.put_nowait = _put_nowait
        state_manager = SimpleNamespace(state_to_gui_queue=gui_queue)

        # Stub app with an awaitable send_request (the grant travels as an
        # acknowledged request since wh-overlay-slow-uia-stale-badges.14.14;
        # send_command stays for tests that assert it is NOT used).
        app = SimpleNamespace(
            send_command=AsyncMock(),
            send_request=AsyncMock(return_value={"status": "ok"}),
        )

        # Build a LogicController-like object that uses the real
        # add_soft_allow method but has the attributes it touches.
        from main import LogicController

        controller = LogicController.__new__(LogicController)
        controller.app = app
        controller.state_manager = state_manager
        controller._soft_allow_path = tmp_path / "soft_allow_tuples.toml"

        return controller, app, gui_queue

    @pytest.mark.asyncio
    async def test_writes_disk_then_sends_ipc_on_success(
        self, stub_controller,
    ):
        controller, app, gui_queue = stub_controller
        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.SUCCESS
        assert outcome.is_durable is True

        # Disk file exists and contains the entry.
        path = controller._soft_allow_path
        assert path.exists()
        contents = path.read_text(encoding="utf-8")
        assert "zed.exe" in contents
        assert "Zed::Window" in contents
        assert "WindowControl" in contents

        # IPC was sent as an acknowledged request with the expected payload.
        app.send_request.assert_awaited_once()
        call_args = app.send_request.await_args
        assert call_args.args[0] == "add_soft_allow_tuple"
        params = call_args.args[1]
        assert params["process_name"] == "zed.exe"
        assert params["class_name"] == "Zed::Window"
        assert params["control_type"] == "WindowControl"

        # No failure event on the GUI queue.
        assert not any(
            m.get("action") == "soft_allow_write_failed"
            for m in gui_queue.messages
        )

    @pytest.mark.asyncio
    async def test_disk_failure_skips_ipc_and_emits_failure_event(
        self, stub_controller, monkeypatch,
    ):
        controller, app, gui_queue = stub_controller

        # Force the writer to report failure.
        import main as main_module

        def fake_append(_tuple, _path):
            return False

        monkeypatch.setattr(
            main_module, "append_soft_allow_tuple", fake_append,
        )
        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.DISK_FAILED
        assert outcome.is_durable is False

        # IPC was NOT sent.
        app.send_request.assert_not_awaited()
        app.send_command.assert_not_awaited()

        # The GUI state queue carries a soft_allow_write_failed event.
        failure_events = [
            m for m in gui_queue.messages
            if m.get("action") == "soft_allow_write_failed"
        ]
        assert len(failure_events) == 1
        evt = failure_events[0]
        assert evt["process_name"] == "zed.exe"
        assert evt["class_name"] == "Zed::Window"
        assert evt["control_type"] == "WindowControl"

    @pytest.mark.asyncio
    async def test_ipc_failure_after_disk_success_returns_ipc_failed(
        self, stub_controller,
    ):
        """Deepseek wh-ipc-failed-untested: cover the IPC_FAILED branch
        end-to-end at the method level. Disk write succeeds; the IPC
        send raises. add_soft_allow returns IPC_FAILED, the disk file
        carries the entry (durable grant), and no
        soft_allow_write_failed event is enqueued (that event is
        DISK_FAILED only)."""

        controller, app, gui_queue = stub_controller
        app.send_request.side_effect = RuntimeError("simulated IPC failure")
        # wh-grant-ipc-failed-ux: no sleeping in tests; two instant retries.
        controller._soft_allow_ipc_retry_delays = (0, 0)

        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.IPC_FAILED
        assert outcome.is_durable is True

        # Disk file exists with the entry -- the grant is durable.
        path = controller._soft_allow_path
        assert path.exists()
        contents = path.read_text(encoding="utf-8")
        assert "zed.exe" in contents
        assert "Zed::Window" in contents

        # IPC was attempted on every configured try before IPC_FAILED
        # (wh-grant-ipc-failed-ux: 1 initial send + one per retry delay).
        assert app.send_request.await_count == 3

        # No soft_allow_write_failed event -- that's DISK_FAILED only.
        assert not any(
            m.get("action") == "soft_allow_write_failed"
            for m in gui_queue.messages
        )

    @pytest.mark.asyncio
    async def test_success_requires_acknowledged_response(self, stub_controller):
        """SUCCESS is reported only after the Input process acknowledges.

        wh-overlay-slow-uia-stale-badges.14.14: send_command returns on
        queue ACCEPTANCE, and the sender can drop the payload afterward
        (expired, unread_frame, event_error, write_error,
        event_set_failed) -- so acceptance-based SUCCESS reset the click
        counter while the running session never received the grant. The
        grant must travel as a request the Input process answers.
        """
        controller, app, gui_queue = stub_controller

        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.SUCCESS
        app.send_request.assert_awaited_once()
        call_args = app.send_request.await_args
        assert call_args.args[0] == "add_soft_allow_tuple"
        assert call_args.args[1]["process_name"] == "zed.exe"

    @pytest.mark.asyncio
    async def test_error_response_is_retried_then_ipc_failed(self, stub_controller):
        """An Input-process error response is a failed attempt, not SUCCESS."""
        controller, app, gui_queue = stub_controller
        app.send_request.return_value = {
            "error": True, "message": "predicate rejected the tuple",
        }
        controller._soft_allow_ipc_retry_delays = (0, 0)

        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.IPC_FAILED
        assert app.send_request.await_count == 3

    @pytest.mark.asyncio
    async def test_dropped_send_is_retried_before_success(self, stub_controller):
        """A sender-side drop (request never answered) is retried.

        wh-overlay-slow-uia-stale-badges.14.10 introduced the retry;
        .14.14 moved the failure signal from a False return to the
        request timing out or IpcDeliveryError, both of which cover the
        post-acceptance drop class the boolean never could.
        """
        controller, app, gui_queue = stub_controller
        app.send_request.side_effect = [
            asyncio.TimeoutError(), {"status": "ok"},
        ]
        controller._soft_allow_ipc_retry_delays = (0, 0)

        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.SUCCESS
        assert app.send_request.await_count == 2

    @pytest.mark.asyncio
    async def test_all_sends_dropped_returns_ipc_failed(self, stub_controller):
        """Every attempt unanswered ends in IPC_FAILED, not SUCCESS."""
        controller, app, gui_queue = stub_controller
        app.send_request.return_value = None
        app.send_request.side_effect = asyncio.TimeoutError()
        controller._soft_allow_ipc_retry_delays = (0, 0)

        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.IPC_FAILED
        assert app.send_request.await_count == 3

    @pytest.mark.asyncio
    async def test_writer_raises_surfaces_soft_allow_write_failed(
        self, stub_controller, monkeypatch,
    ):
        """wh-27gvv.2.1 (deepseek review): a non-OSError exception from
        the writer must still surface the "couldn't save" notice. The
        writer normally catches OSError and returns False, but a
        future writer change, a path override mistake, or a
        serialisation failure could raise something else. Without the
        inner try/except in add_soft_allow the failure feedback never
        reaches the GUI: the handler's outer wrapper in
        _handle_grant_prompt_yes_clicked logs but does not enqueue
        soft_allow_write_failed.

        Mirror of test_writer_raises_surfaces_declined_write_failed in
        tests/test_logic_declined_persistence.py (added in wh-27gvv.1.1
        for the symmetric No-path bug).
        """
        controller, app, gui_queue = stub_controller
        import main as main_module

        def raise_runtime(_tuple, _path):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            main_module, "append_soft_allow_tuple", raise_runtime,
        )
        from main import AddSoftAllowOutcome
        outcome = await controller.add_soft_allow(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
        )
        assert outcome is AddSoftAllowOutcome.DISK_FAILED
        assert outcome.is_durable is False

        # IPC was NOT sent.
        app.send_request.assert_not_awaited()
        app.send_command.assert_not_awaited()

        # The GUI state queue carries a soft_allow_write_failed event.
        failure_events = [
            m for m in gui_queue.messages
            if m.get("action") == "soft_allow_write_failed"
        ]
        assert len(failure_events) == 1
        evt = failure_events[0]
        assert evt["process_name"] == "zed.exe"
        assert evt["class_name"] == "Zed::Window"
        assert evt["control_type"] == "WindowControl"


class TestInputProcHandler:
    """Tests for the add_soft_allow_tuple dispatch in input_proc.

    The input-process handler is a small piece of dispatch logic; the
    interesting behaviour is that it calls
    ui_handler.text_target_predicate.add_soft_allow with the right
    tuple. We don't run the whole input_proc loop here; instead we
    assert that the predicate's add_soft_allow method updates the set
    when called with the documented arguments. The wiring inside
    input_proc.py is exercised by the manual smoke test path.
    """

    def test_predicate_add_soft_allow_with_ipc_payload_shape(self):
        # Mirror the dispatch in input_proc.py: pull three fields from
        # params and pass them as a tuple. This guards the contract
        # between the IPC payload shape and the predicate API.
        predicate = TextTargetPredicate(soft_allow_tuples=[])
        params = {
            "process_name": "zed.exe",
            "class_name": "Zed::Window",
            "control_type": "WindowControl",
        }
        predicate.add_soft_allow((
            params["process_name"],
            params["class_name"],
            params["control_type"],
        ))
        assert ("zed.exe", "Zed::Window", "WindowControl") in (
            predicate.soft_allow_tuples
        )


class TestStarterList:
    """Starter approved-control list slice (wh-k535r).

    The predicate accepts a second file path -- the starter list shipped
    with the codebase -- and merges its entries with the user's
    soft_allow_tuples.toml. The starter file is read-only; the writer
    (utils/soft_allow_writer.py) only rewrites the user file, so starter
    entries cannot be clobbered by a user grant.

    A triple appearing in both files appears once in the merged set;
    behaviour is identical to either file declaring it alone.
    """

    def test_starter_path_only_loads_starter_entries(self, tmp_path):
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_starter_path=starter,
        )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })

    def test_starter_and_user_files_merge(self, tmp_path):
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        user = tmp_path / "soft_allow_tuples.toml"
        user.write_text(
            "[[entries]]\n"
            "process_name = 'sublime_text.exe'\n"
            "class_name = 'PX_WINDOW_CLASS'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-14T00:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_path=user,
            soft_allow_starter_path=starter,
        )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
            ("sublime_text.exe", "PX_WINDOW_CLASS", "WindowControl"),
        })

    def test_duplicate_triple_in_both_files_collapses_to_one(self, tmp_path):
        # The same (process, class, control_type) triple in both files
        # appears once in the merged set. The user's added_at is the
        # authoritative one on disk because the writer's read-modify-write
        # only touches the user file; the predicate does not key on
        # added_at, so the in-memory set is a clean union.
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        user = tmp_path / "soft_allow_tuples.toml"
        user.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-15T11:22:33Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_path=user,
            soft_allow_starter_path=starter,
        )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })

    def test_missing_starter_file_silent_with_populated_user_file(
        self, tmp_path, caplog,
    ):
        # Starter file missing is the documented initial state (the
        # repo ships an empty starter file but a custom deploy could
        # delete it). User entries still load; no WARNING logged.
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        # File deliberately not created.
        user = tmp_path / "soft_allow_tuples.toml"
        user.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-14T00:00:00Z'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            predicate = TextTargetPredicate(
                soft_allow_path=user,
                soft_allow_starter_path=starter,
            )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })
        # No WARNING about the missing starter file.
        assert not any(
            "starter" in record.message.lower()
            for record in caplog.records
        )

    def test_missing_both_files_yields_empty_set(self, tmp_path):
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        user = tmp_path / "soft_allow_tuples.toml"
        # Neither file exists.
        predicate = TextTargetPredicate(
            soft_allow_path=user,
            soft_allow_starter_path=starter,
        )
        assert predicate.soft_allow_tuples == frozenset()

    def test_malformed_user_does_not_disable_starter_entries(
        self, tmp_path, caplog,
    ):
        # Symmetric to test_malformed_starter_does_not_disable_user_entries
        # (wh-k535r.1.2 / codex round 1): a malformed user file falls back
        # to empty user entries and logs a WARNING, but the starter file
        # still loads. A future refactor that couples the two loader
        # branches -- or returns early after the user-file parse failure --
        # would silently drop all shipped starter entries and still pass
        # the other tests, so this regression fence exercises the opposite
        # direction.
        user = tmp_path / "soft_allow_tuples.toml"
        user.write_text(
            "[[entries]]\nprocess_name = 'broken.exe\nclass_name = =\n",
            encoding="utf-8",
        )
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            predicate = TextTargetPredicate(
                soft_allow_path=user,
                soft_allow_starter_path=starter,
            )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })
        # The WARNING must identify the user loader, not the starter
        # loader. Mirror of the matching assertion in the
        # malformed-starter test.
        assert any(
            "soft_allow loader" in record.message
            and "starter" not in record.message.lower()
            for record in caplog.records
        )

    def test_malformed_starter_does_not_disable_user_entries(
        self, tmp_path, caplog,
    ):
        # A malformed starter file falls back to empty starter entries
        # and logs a WARNING, but the user file still loads. The two
        # files have independent failure modes so a starter regression
        # cannot wipe the user's approved controls.
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\nprocess_name = 'zed.exe\nclass_name = =\n",
            encoding="utf-8",
        )
        user = tmp_path / "soft_allow_tuples.toml"
        user.write_text(
            "[[entries]]\n"
            "process_name = 'sublime_text.exe'\n"
            "class_name = 'PX_WINDOW_CLASS'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-14T00:00:00Z'\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            predicate = TextTargetPredicate(
                soft_allow_path=user,
                soft_allow_starter_path=starter,
            )
        assert predicate.soft_allow_tuples == frozenset({
            ("sublime_text.exe", "PX_WINDOW_CLASS", "WindowControl"),
        })
        # The WARNING must identify the starter caller specifically.
        # The user loader and the starter loader both emit messages
        # containing "soft_allow", so a substring check on that token
        # alone would pass even if the starter warning never fired.
        assert any(
            "starter" in record.message.lower()
            for record in caplog.records
        )

    def test_starter_entry_produces_accept_soft_allow_tuple_verdict(
        self, tmp_path,
    ):
        # End-to-end: a triple loaded from the starter file produces the
        # accept_soft_allow_tuple verdict, exactly as a user-granted
        # triple does. The router maps this reason to ClipboardOnlyStrategy
        # so dictation into the starter-listed control silently pastes.
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_starter_path=starter,
        )
        ctrl = _ctrl(
            control_type=auto.ControlType.WindowControl,
            control_type_name="WindowControl",
            class_name="Zed::Window",
        )
        v = predicate.evaluate(
            ctrl, class_name="Zed::Window", process_name="zed.exe",
        )
        assert v.verdict is True
        assert v.reason == "accept_soft_allow_tuple"

    def test_add_soft_allow_does_not_remove_starter_entries(self, tmp_path):
        # A runtime grant via add_soft_allow rebinds the set to the union
        # of the existing set and the new triple. Starter entries already
        # in the set must survive the rebind.
        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_starter_path=starter,
        )
        predicate.add_soft_allow(
            ("notepad.exe", "Notepad", "EditControl"),
        )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
            ("notepad.exe", "Notepad", "EditControl"),
        })

    def test_explicit_starter_tuples_override_path(self, tmp_path):
        # Tests construct the predicate with explicit tuples to avoid
        # touching the on-disk file. Mirror that path for the starter
        # list: explicit ``soft_allow_starter_tuples`` takes precedence
        # over ``soft_allow_starter_path``.
        starter_path = tmp_path / "soft_allow_starter_tuples.toml"
        starter_path.write_text(
            "[[entries]]\n"
            "process_name = 'should_not_load.exe'\n"
            "class_name = 'IgnoreMe'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        predicate = TextTargetPredicate(
            soft_allow_starter_path=starter_path,
            soft_allow_starter_tuples=[
                ("zed.exe", "Zed::Window", "WindowControl"),
            ],
        )
        assert predicate.soft_allow_tuples == frozenset({
            ("zed.exe", "Zed::Window", "WindowControl"),
        })


class TestStarterFileShips:
    """The starter file exists at the documented path inside the repo.

    Acceptance criterion (wh-k535r): A fresh install merges starter
    entries with the user's file at startup. The build_predicate_from_config
    helper reaches that path via _DEFAULT_SOFT_ALLOW_STARTER_PATH, so the
    constant must resolve to a real file under services/wheelhouse/data/
    in the repo. The file may be empty (no entries) initially; what
    matters is that the path is loadable.
    """

    def test_starter_path_resolves_under_services_wheelhouse_data(self):
        from ui.text_target import _DEFAULT_SOFT_ALLOW_STARTER_PATH

        parts = _DEFAULT_SOFT_ALLOW_STARTER_PATH.parts
        assert parts[-3] == "wheelhouse"
        assert parts[-2] == "data"
        assert parts[-1] == "soft_allow_starter_tuples.toml"

    def test_starter_path_shares_parent_with_user_path(self):
        # Regression fence for the wh-9weum.4.4 mistake class: a typo
        # in parents[] math on one path constant but not the other
        # (e.g. parents[2] on the starter side) would silently drift
        # the starter file outside the data/ directory. Source-tree
        # tests pass because the typo'd path can still resolve to a
        # real directory; production installs lose the starter list.
        # Asserting the two paths share a parent forecloses that drift.
        from ui.text_target import (
            _DEFAULT_SOFT_ALLOW_PATH,
            _DEFAULT_SOFT_ALLOW_STARTER_PATH,
        )

        assert (
            _DEFAULT_SOFT_ALLOW_STARTER_PATH.parent
            == _DEFAULT_SOFT_ALLOW_PATH.parent
        ), (
            "starter and user files must live in the same data/ folder; "
            f"starter resolves to {_DEFAULT_SOFT_ALLOW_STARTER_PATH} but "
            f"user resolves to {_DEFAULT_SOFT_ALLOW_PATH}"
        )

    def test_starter_file_is_present_in_repo(self):
        from ui.text_target import _DEFAULT_SOFT_ALLOW_STARTER_PATH

        # The starter file ships with the repo so the predicate's loader
        # finds it on a fresh checkout. An empty entries list is fine;
        # the data capture step seeds entries over time.
        assert _DEFAULT_SOFT_ALLOW_STARTER_PATH.exists(), (
            f"starter file missing from repo at "
            f"{_DEFAULT_SOFT_ALLOW_STARTER_PATH}"
        )

    def test_default_predicate_loads_starter_path(self):
        # The module-level default_predicate is wired to load both the
        # user file and the starter file. Replicate the wiring check at
        # the build_predicate_from_config helper too in a separate test.
        from ui.text_target import (
            _DEFAULT_SOFT_ALLOW_PATH,
            _DEFAULT_SOFT_ALLOW_STARTER_PATH,
            default_predicate,
        )

        # Both paths point inside the repo. The set may be empty if both
        # files are empty; the contract is the wiring, not the contents.
        assert _DEFAULT_SOFT_ALLOW_PATH.exists() or True  # may be empty
        assert _DEFAULT_SOFT_ALLOW_STARTER_PATH.exists()
        # The default predicate did not raise during module init; that
        # is what this test mostly proves. The attribute access is the
        # smoke check.
        _ = default_predicate.soft_allow_tuples

    def test_build_predicate_from_config_loads_starter_path(self):
        # The config-driven builder used by main.py must also wire the
        # starter path. Without this, production runs would load only
        # the user file and the starter list would be dead code on the
        # default code path.
        from ui.text_target import build_predicate_from_config

        predicate = build_predicate_from_config({})
        # Accessing the property does not raise -- the constructor ran
        # the loader against the production starter path.
        _ = predicate.soft_allow_tuples

    def test_make_default_predicate_actually_reads_starter_entries(
        self, tmp_path, monkeypatch,
    ):
        # wh-k535r.1.1 (codex round 1): the smoke test on
        # default_predicate above only proves that module import did
        # not raise. _make_default_predicate is the helper the
        # module-level default_predicate uses, so a test that
        # monkeypatches the two production constants and re-invokes
        # the helper exercises the same construction path with a
        # populated starter file. A regression that strips the
        # soft_allow_starter_path keyword from the helper would fail
        # this test even though the smoke test would still pass.
        import ui.text_target as text_target_module

        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        empty_user = tmp_path / "soft_allow_tuples.toml"
        monkeypatch.setattr(
            text_target_module,
            "_DEFAULT_SOFT_ALLOW_PATH",
            empty_user,
        )
        monkeypatch.setattr(
            text_target_module,
            "_DEFAULT_SOFT_ALLOW_STARTER_PATH",
            starter,
        )

        predicate = text_target_module._make_default_predicate()
        assert ("zed.exe", "Zed::Window", "WindowControl") in (
            predicate.soft_allow_tuples
        )

    def test_build_predicate_from_config_actually_reads_starter_entries(
        self, tmp_path, monkeypatch,
    ):
        # wh-k535r.1.1 (codex round 1): the smoke test above only proves
        # the builder does not raise. With the production starter file
        # shipped empty, that test would still pass if someone removed
        # the soft_allow_starter_path argument from
        # build_predicate_from_config. Verify the entries actually flow
        # through by pointing the production constant at a temp starter
        # file with a known entry, then asserting the entry is in the
        # merged set.
        import ui.text_target as text_target_module

        starter = tmp_path / "soft_allow_starter_tuples.toml"
        starter.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'Zed::Window'\n"
            "control_type = 'WindowControl'\n"
            "added_at = '2026-05-13T00:00:00Z'\n",
            encoding="utf-8",
        )
        # Also point the user constant at an empty path so the test
        # isolates the starter-side wiring from any state in the
        # production user file at test time.
        empty_user = tmp_path / "soft_allow_tuples.toml"
        monkeypatch.setattr(
            text_target_module,
            "_DEFAULT_SOFT_ALLOW_PATH",
            empty_user,
        )
        monkeypatch.setattr(
            text_target_module,
            "_DEFAULT_SOFT_ALLOW_STARTER_PATH",
            starter,
        )

        predicate = text_target_module.build_predicate_from_config({})
        assert ("zed.exe", "Zed::Window", "WindowControl") in (
            predicate.soft_allow_tuples
        )


class TestPathConsistency:
    """Regression tests for wh-9weum.4.4 path mismatch.

    Logic writes to ``services/wheelhouse/data/soft_allow_tuples.toml``
    and Input must read from the same path. A wrong number of
    ``parents[]`` levels in one of the resolution callsites silently
    breaks restart persistence. The mismatch is invisible inside a
    single run because the in-memory IPC update keeps the two halves
    in sync; the regression only surfaces on restart.
    """

    def test_predicate_default_path_matches_logic_writer_path(self):
        from pathlib import Path
        from main import LogicController
        from ui.text_target import _DEFAULT_SOFT_ALLOW_PATH

        # Build the path the LogicController uses without instantiating
        # the controller (the resolver is small and pure).
        ctrl = LogicController.__new__(LogicController)
        logic_path: Path = ctrl._resolve_soft_allow_path()  # type: ignore[attr-defined]

        assert _DEFAULT_SOFT_ALLOW_PATH == logic_path, (
            f"text_target loader resolves to {_DEFAULT_SOFT_ALLOW_PATH} "
            f"but Logic writer resolves to {logic_path}; soft-allow "
            "entries would not survive restart"
        )

    def test_all_user_state_paths_derive_from_shared_helper(self):
        # wh-k8ef: every user-state file (soft-allow grants, declines,
        # pending counters) and the loader constant in the Input-side
        # predicate must resolve through utils.system.get_user_data_dir,
        # and the read-only starter list through get_bundled_data_dir.
        # Deriving all sites from the two helpers is what makes the
        # frozen-build relocation apply everywhere at once; a site that
        # hand-builds its path would silently write into the wiped
        # _MEIxxxxxx dir under PyInstaller.
        from main import LogicController
        from ui.text_target import (
            _DEFAULT_SOFT_ALLOW_PATH,
            _DEFAULT_SOFT_ALLOW_STARTER_PATH,
        )
        from utils.system import get_bundled_data_dir, get_user_data_dir

        user_dir = get_user_data_dir()
        ctrl = LogicController.__new__(LogicController)

        assert ctrl._resolve_soft_allow_path() == (
            user_dir / "soft_allow_tuples.toml"
        )
        assert ctrl._resolve_declined_path() == (
            user_dir / "soft_allow_declined_tuples.toml"
        )
        assert ctrl._resolve_pending_counters_path() == (
            user_dir / "soft_allow_pending_counters.toml"
        )
        assert _DEFAULT_SOFT_ALLOW_PATH == (
            user_dir / "soft_allow_tuples.toml"
        )
        assert _DEFAULT_SOFT_ALLOW_STARTER_PATH == (
            get_bundled_data_dir() / "soft_allow_starter_tuples.toml"
        )

    def test_predicate_default_path_lives_under_services_wheelhouse(self):
        from ui.text_target import _DEFAULT_SOFT_ALLOW_PATH

        parts = _DEFAULT_SOFT_ALLOW_PATH.parts
        # The parent directory's name must be 'wheelhouse' (with 'data'
        # as the leaf). A regression to parents[2] would resolve to the
        # 'services' directory, putting the file outside the wheelhouse
        # service boundary.
        assert parts[-3] == "wheelhouse", (
            f"unexpected default path: {_DEFAULT_SOFT_ALLOW_PATH}"
        )
        assert parts[-2] == "data"
        assert parts[-1] == "soft_allow_tuples.toml"


# ===========================================================================
# Input-process acknowledgment for add_soft_allow_tuple
# (wh-overlay-slow-uia-stale-badges.14.14)
# ===========================================================================

class TestInputProcSoftAllowAck:
    """The Input-process handler acknowledges an add_soft_allow_tuple
    request so Logic can report SUCCESS only after the live predicate
    actually changed (wh-overlay-slow-uia-stale-badges.14.14). Without
    the ack, a sender-side drop after queue acceptance left the running
    session without the grant while the UI reported success and reset
    the click counter.
    """

    def _make(self):
        from queue import Queue
        from types import SimpleNamespace

        ui_handler = SimpleNamespace(
            text_target_predicate=SimpleNamespace(add_soft_allow=MagicMock())
        )
        return ui_handler, Queue()

    def _params(self, **overrides):
        params = {
            "process_name": "zed.exe",
            "class_name": "Zed::Window",
            "control_type": "WindowControl",
        }
        params.update(overrides)
        return params

    def test_success_acks_request(self):
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler, q = self._make()
        _handle_add_soft_allow_tuple(self._params(), "rq-ack-1", q, ui_handler)

        ui_handler.text_target_predicate.add_soft_allow.assert_called_once_with(
            ("zed.exe", "Zed::Window", "WindowControl")
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-ack-1"
        assert resp.get("status") == "ok"
        assert not resp.get("error")
        assert q.empty()

    def test_missing_field_reports_error(self):
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler, q = self._make()
        _handle_add_soft_allow_tuple(
            self._params(control_type=""), "rq-ack-2", q, ui_handler
        )

        ui_handler.text_target_predicate.add_soft_allow.assert_not_called()
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-ack-2"
        assert resp.get("error") is True
        assert resp.get("message")

    def test_predicate_failure_reports_error(self):
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler, q = self._make()
        ui_handler.text_target_predicate.add_soft_allow = MagicMock(
            side_effect=RuntimeError("predicate exploded")
        )
        _handle_add_soft_allow_tuple(self._params(), "rq-ack-3", q, ui_handler)

        resp = q.get_nowait()
        assert resp["request_id"] == "rq-ack-3"
        assert resp.get("error") is True
        assert "predicate exploded" in resp.get("message", "")

    def test_fire_and_forget_sends_no_response(self):
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler, q = self._make()
        _handle_add_soft_allow_tuple(self._params(), None, q, ui_handler)

        ui_handler.text_target_predicate.add_soft_allow.assert_called_once()
        assert q.empty()


class UnhashableStrAction(str):
    """str subclass with a disabled hash (wh-overlay-slow-uia-stale-badges.14.38).

    isinstance(x, str) admits it, but any dict lookup or frozenset
    membership that hashes it raises TypeError -- including
    hasattr(handler, x) when the handler has an instance __dict__.
    """

    __hash__ = None  # type: ignore[assignment]


class HashRaisesStrAction(str):
    """str subclass whose hash raises its own exception type (.14.38)."""

    def __hash__(self):
        raise RuntimeError("hash is not supported by this value")


class UnrenderableError(Exception):
    """Exception whose own text cannot be produced.

    wh-overlay-slow-uia-stale-badges.14.54: every handler that catches
    an exception raised by untrusted data then renders it with str(e)
    or an f-string, and that render raises a second time -- escaping
    the handler that had just contained the first failure.
    """

    def __str__(self):
        raise RuntimeError("exception rendering exploded")


class UnrenderableRaisingKey(str):
    """Picklable str subclass whose equality raises UnrenderableError."""

    __hash__ = str.__hash__

    def __eq__(self, other):
        raise UnrenderableError()


class EqRaisesStrAction(str):
    """str subclass whose equality raises (.14.38).

    A comparison like action == "activate_window" calls the subclass
    __eq__ first, so a special-route comparison propagates this.
    """

    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq is not supported by this value")


class RaisingGetDict(dict):
    """dict subclass whose get raises (.14.38).

    isinstance(x, dict) admits it, but the params.get calls at the
    special routes outside any per-action try then raise.
    """

    def get(self, *args, **kwargs):
        raise RuntimeError("get is not supported by this mapping")


class RaisingReprValue:
    """Value whose repr raises (.14.38).

    A reject path that renders the untrusted value with %r or !r
    raises while BUILDING its own error message.
    """

    def __repr__(self):
        raise RuntimeError("repr is not supported by this value")


class RaisingBoolValue:
    """Value whose truth test raises (wh-overlay-slow-uia-stale-badges.14.40).

    A special route that truth-tests a params VALUE (``x or default``,
    ``if not x``) before its per-action try propagates this.
    """

    def __bool__(self):
        raise RuntimeError("bool is not supported by this value")


class TestInputProcParamsValidation:
    """wh-overlay-slow-uia-stale-badges.14.20: a truthy non-mapping
    params value (list, str) used to raise AttributeError at the
    dispatch sites that call params.get outside any per-action try,
    which escaped to the outer Input-loop handler and shut the process
    down. _coerce_params validates once at the extraction point: a
    mapping passes through; anything else is rejected with the standard
    error response (when a request_id is present) and the loop
    continues.

    wh-overlay-slow-uia-stale-badges.14.38: only an EXACT dict passes.
    A dict subclass can override get() to raise at the special routes,
    so it is rejected the same way, and the reject path must not render
    the untrusted value while building its message.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    def test_mapping_passes_through(self):
        from input_proc import _coerce_params
        q = self._q()
        params = {"a": 1}
        assert _coerce_params(params, "rq-1", q, "any_action") is params
        assert q.empty()

    def test_list_params_rejected_with_error_response(self):
        from input_proc import _coerce_params
        q = self._q()
        assert _coerce_params(["bad"], "rq-2", q, "add_soft_allow_tuple") is None
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-2"
        assert resp.get("error") is True
        assert resp["action"] == "add_soft_allow_tuple"
        assert q.empty()

    def test_str_params_rejected_fire_and_forget_no_response(self):
        from input_proc import _coerce_params
        q = self._q()
        assert _coerce_params("bad", None, q, "terminal_editor_cancelled") is None
        assert q.empty()

    def test_dict_subclass_params_rejected_with_error_response(self):
        """wh-overlay-slow-uia-stale-badges.14.38: a dict subclass
        passes isinstance but its overridden get() raises at the
        special routes outside any per-action try."""
        from input_proc import _coerce_params
        q = self._q()
        assert _coerce_params(
            RaisingGetDict(), "rq-4", q, "set_log_level",
        ) is None
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-4"
        assert resp.get("error") is True
        assert q.empty()

    def test_params_with_raising_repr_rejected_safely(self):
        """wh-overlay-slow-uia-stale-badges.14.38: the reject path must
        not raise while building its own message from a value whose
        repr raises."""
        from input_proc import _coerce_params
        q = self._q()
        assert _coerce_params(
            RaisingReprValue(), "rq-5", q, "any_action",
        ) is None
        resp = q.get_nowait()
        assert resp.get("error") is True
        assert q.empty()


class TestInputProcActionValidation:
    """wh-overlay-slow-uia-stale-badges.14.33: the reader loop calls
    hasattr(ui_handler, action) outside the action-execution try block,
    and hasattr raises TypeError for a None or non-string attribute
    name -- the outer handler then shuts the Input process down over one
    malformed envelope. _validate_action validates once at the reader
    boundary: a string action passes (unknown strings keep the existing
    warning-and-continue behavior); a missing or non-string action is
    rejected with the standard error response (when a request_id is
    present) and the loop continues.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    def test_string_action_passes(self):
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action("click_element", "rq-1", q) is True
        assert q.empty()

    def test_unknown_string_action_still_passes(self):
        """String-ness is the only check here; the hasattr branch keeps
        its warning-and-continue / unanswered-request-timeout behavior
        for unknown string actions."""
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action("not_a_real_action", "rq-2", q) is True
        assert q.empty()

    @pytest.mark.parametrize(
        "bad", [None, [], 0, False, 1.5, b"click", {}, ("a",)],
    )
    def test_non_string_action_rejected_with_error_response(self, bad):
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action(bad, "rq-3", q) is False
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-3"
        assert resp.get("error") is True
        assert q.empty()

    @pytest.mark.parametrize("bad", [None, [], 0])
    def test_non_string_action_fire_and_forget_no_response(self, bad):
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action(bad, None, q) is False
        assert q.empty()

    @pytest.mark.parametrize("bad", [
        UnhashableStrAction("click_element"),
        HashRaisesStrAction("click_element"),
        EqRaisesStrAction("click_element"),
    ], ids=["hash-disabled", "hash-raises", "eq-raises"])
    def test_str_subclass_action_rejected_with_error_response(self, bad):
        """wh-overlay-slow-uia-stale-badges.14.38: a str subclass
        passes isinstance, then its broken hash raises at
        hasattr(ui_handler, action) (the handler has an instance
        __dict__) or at a frozenset membership, and a raising __eq__
        propagates from a special-route comparison. Only an exact str
        passes the boundary."""
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action(bad, "rq-6", q) is False
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-6"
        assert resp.get("error") is True
        assert q.empty()

    def test_action_with_raising_repr_rejected_safely(self):
        """wh-overlay-slow-uia-stale-badges.14.38: the reject path must
        not raise while building its own message from a value whose
        repr raises."""
        from input_proc import _validate_action
        q = self._q()
        assert _validate_action(RaisingReprValue(), "rq-7", q) is False
        resp = q.get_nowait()
        assert resp.get("error") is True
        assert q.empty()


class TestInputProcEnvelopeBoundary:
    """wh-overlay-slow-uia-stale-badges.14.38: _read_envelope is the
    single extraction chokepoint for an untrusted unpickled envelope.
    It requires an EXACT dict (a subclass's .get can raise), it
    canonicalizes request_id and trace_id to trustworthy values, and
    every extraction runs under one protective except -- a poisoned
    dict key raises from the rich comparison inside .get, which
    previously escaped to the outer Input-loop handler and shut the
    process down.
    """

    def test_plain_dict_envelope_extracts_fields(self):
        from input_proc import _read_envelope
        out = _read_envelope({
            "action": "press", "params": {"a": 1},
            "request_id": "rq-1", "trace_id": "tr-1",
        })
        assert out == ("press", {"a": 1}, True, "rq-1", "tr-1")

    def test_absent_params_defaults_empty_with_flag(self):
        from input_proc import _read_envelope
        assert _read_envelope({"action": "press"}) == (
            "press", {}, False, None, "",
        )

    @pytest.mark.parametrize("bad", [[], "x", 42, ("a",)])
    def test_non_dict_envelope_rejected(self, bad):
        from input_proc import _read_envelope
        assert _read_envelope(bad) is None

    def test_dict_subclass_envelope_rejected(self):
        """A reducer-controlled envelope can unpickle as a dict
        subclass whose .get raises; the exact-type gate rejects it
        before any lookup runs."""
        from input_proc import _read_envelope
        assert _read_envelope(RaisingGetDict(action="press")) is None

    def test_poisoned_key_envelope_rejected(self):
        """An exact dict whose stored key raises from __eq__ makes
        .get("action") raise inside the lookup's rich comparison; the
        protective except turns that into a drop."""
        from input_proc import _read_envelope
        env = {EqRaisesStrAction("action"): "press"}
        assert _read_envelope(env) is None

    def test_non_str_request_id_becomes_none(self):
        from input_proc import _read_envelope
        out = _read_envelope({"action": "a", "request_id": 42})
        assert out is not None and out[3] is None

    def test_str_subclass_request_id_becomes_none(self):
        from input_proc import _read_envelope
        out = _read_envelope({
            "action": "a",
            "request_id": UnhashableStrAction("rq-x"),
        })
        assert out is not None and out[3] is None

    def test_non_str_trace_id_becomes_empty(self):
        from input_proc import _read_envelope
        out = _read_envelope({"action": "a", "trace_id": 5})
        assert out is not None and out[4] == ""


class TestInputProcParamsExtraction:
    """wh-overlay-slow-uia-stale-badges.14.22: the reader wiring used to
    apply ``or {}`` BEFORE _coerce_params, so every falsy non-mapping
    (None, [], '', 0, False) became an accepted empty dict and the
    validation never saw it -- a malformed terminal_editor_cancelled
    with params=[] then dispatched the unconditional empty-rid recovery
    and force-cleaned a live editor session. The extraction defaults to
    {} ONLY for an absent params key; every supplied value is validated.

    wh-overlay-slow-uia-stale-badges.14.38: the extraction now lives in
    the _read_envelope chokepoint (has_params flag) composed with
    _coerce_params, exactly as the reader loop wires them.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    def _extract(self, msg, request_id, q, action):
        from input_proc import _read_envelope, _coerce_params
        out = _read_envelope(msg)
        assert out is not None
        _action, raw_params, has_params, _rid, _tid = out
        if not has_params:
            return {}
        return _coerce_params(raw_params, request_id, q, action)

    def test_absent_params_key_defaults_to_empty_dict(self):
        q = self._q()
        result = self._extract({"action": "press"}, "rq-a", q, "press")
        assert result == {}
        assert q.empty()

    def test_supplied_mapping_passes_through(self):
        q = self._q()
        params = {"key": "enter"}
        msg = {"action": "press", "params": params}
        assert self._extract(msg, "rq-b", q, "press") is params
        assert q.empty()

    @pytest.mark.parametrize("bad", [None, [], "", 0, False])
    def test_supplied_falsy_non_mapping_rejected_with_error_response(self, bad):
        q = self._q()
        msg = {"action": "terminal_editor_cancelled", "params": bad}
        assert self._extract(msg, "rq-c", q, "terminal_editor_cancelled") is None
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-c"
        assert resp.get("error") is True
        assert resp["action"] == "terminal_editor_cancelled"
        assert q.empty()

    @pytest.mark.parametrize("bad", [None, [], 0])
    def test_supplied_falsy_non_mapping_fire_and_forget_no_response(self, bad):
        q = self._q()
        msg = {"action": "terminal_editor_cancelled", "params": bad}
        assert self._extract(msg, None, q, "terminal_editor_cancelled") is None
        assert q.empty()


class TestInputProcSpecialRouteValueShapes:
    """wh-overlay-slow-uia-stale-badges.14.40: _coerce_params proves the
    params CONTAINER is an exact dict, but the special routes consume
    params VALUES before their per-action try starts -- a truth test
    (``x or default``, ``if not x``), a .lower() call, or a {!r} render
    of an untrusted value. Any of those raising escapes to the outer
    Input-loop handler and shuts the process down. Every special route
    must consume untrusted values only inside its protective try, and
    set_log_level (previously inline in the loop with getattr outside
    any try) gets its own protected handler.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    # --- set_log_level (new module-level handler) ---

    def test_set_log_level_valid_level_sets_root_no_ack(self):
        import logging
        from input_proc import _handle_set_log_level

        q = self._q()
        root = logging.getLogger()
        before = root.level
        try:
            _handle_set_log_level({"level": "DEBUG"}, "rq-sll-1", q)
            assert root.level == logging.DEBUG
            # The route has always been fire-and-forget: no ack even
            # when the envelope carries a request_id.
            assert q.empty()
        finally:
            root.setLevel(before)

    def test_set_log_level_non_str_level_contained_with_error_response(self):
        import logging
        from input_proc import _handle_set_log_level

        q = self._q()
        root = logging.getLogger()
        before = root.level
        try:
            _handle_set_log_level({"level": []}, "rq-sll-2", q)
            assert root.level == before
            resp = q.get_nowait()
            assert resp["request_id"] == "rq-sll-2"
            assert resp.get("error") is True
            assert q.empty()
        finally:
            root.setLevel(before)

    def test_set_log_level_non_str_level_fire_and_forget_no_response(self):
        import logging
        from input_proc import _handle_set_log_level

        q = self._q()
        root = logging.getLogger()
        before = root.level
        try:
            _handle_set_log_level({"level": []}, None, q)
            assert root.level == before
            assert q.empty()
        finally:
            root.setLevel(before)

    def test_set_log_level_poisoned_params_key_contained_with_error_response(self):
        """wh-overlay-slow-uia-stale-badges.14.42: the exception
        reporter must never read params again -- the first lookup
        raised from the poisoned key's __eq__, and a second lookup in
        the except escapes the handler and kills the Input process."""
        import logging
        from input_proc import _handle_set_log_level

        q = self._q()
        root = logging.getLogger()
        before = root.level
        try:
            _handle_set_log_level(
                {EqRaisesStrAction("level"): "DEBUG"}, "rq-sll-4", q,
            )
            assert root.level == before
            resp = q.get_nowait()
            assert resp["request_id"] == "rq-sll-4"
            assert resp.get("error") is True
            assert q.empty()
        finally:
            root.setLevel(before)

    def test_set_log_level_poisoned_params_key_fire_and_forget_no_response(self):
        import logging
        from input_proc import _handle_set_log_level

        q = self._q()
        root = logging.getLogger()
        before = root.level
        try:
            _handle_set_log_level(
                {EqRaisesStrAction("level"): "DEBUG"}, None, q,
            )
            assert root.level == before
            assert q.empty()
        finally:
            root.setLevel(before)

    # --- activate_window ---

    @pytest.mark.parametrize("bad_target", [
        ["not-a-string"],
        RaisingBoolValue(),
    ], ids=["truthy-list-no-lower", "raising-truthiness"])
    def test_activate_window_bad_target_value_contained(self, bad_target):
        import threading
        from input_proc import _handle_activate_window

        q = self._q()
        is_internal = threading.Event()
        _handle_activate_window(
            {"target": bad_target}, "rq-aw-1", q, "activate_window",
            is_internal, 10, 5, {}, MagicMock(),
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-aw-1"
        assert resp.get("error") is True
        assert q.empty()
        assert not is_internal.is_set()

    # --- add_soft_allow_tuple ---

    def _soft_allow_handler(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            text_target_predicate=SimpleNamespace(add_soft_allow=MagicMock())
        )

    def test_add_soft_allow_raising_truthiness_contained(self):
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler = self._soft_allow_handler()
        q = self._q()
        _handle_add_soft_allow_tuple(
            {
                "process_name": RaisingBoolValue(),
                "class_name": "x",
                "control_type": "y",
            },
            "rq-sa-1", q, ui_handler,
        )
        ui_handler.text_target_predicate.add_soft_allow.assert_not_called()
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-sa-1"
        assert resp.get("error") is True
        assert q.empty()

    def test_add_soft_allow_raising_repr_beside_missing_field_contained(self):
        """The missing-field message renders the other fields with {!r};
        a raising __repr__ must not escape while BUILDING that message."""
        from input_proc import _handle_add_soft_allow_tuple

        ui_handler = self._soft_allow_handler()
        q = self._q()
        _handle_add_soft_allow_tuple(
            {
                "process_name": "",
                "class_name": RaisingReprValue(),
                "control_type": "y",
            },
            "rq-sa-2", q, ui_handler,
        )
        ui_handler.text_target_predicate.add_soft_allow.assert_not_called()
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-sa-2"
        assert resp.get("error") is True
        assert q.empty()

    # --- _te_event_ack ---

    def test_te_event_ack_raising_truthiness_hwnd_contained(self):
        from types import SimpleNamespace
        from input_proc import _handle_te_event_ack_command

        ui_handler = SimpleNamespace(
            terminal_editor=SimpleNamespace(on_event_ack=MagicMock())
        )
        q = self._q()
        _handle_te_event_ack_command(
            {"request_id": "sess-1", "op": "show",
             "editor_hwnd": RaisingBoolValue()},
            "rq-te-1", q, ui_handler,
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-te-1"
        assert resp.get("error") is True
        assert q.empty()


class TestInputProcSelfOwningBindGuard:
    """wh-overlay-slow-uia-stale-badges.14.45: an exact dict passes
    _coerce_params yet can still fail Python's argument binding at the
    dispatch call -- a non-string key raises at ** expansion, a params
    key can duplicate the envelope-injected request_id, and a required
    argument can be missing. For _HANDLES_OWN_RESPONSE actions the
    dispatch except suppresses the standard error response (the handler
    owns its response), so the awaited Future timed out instead of
    erroring. _reject_unbindable_self_owning probes the binding first;
    a failure there is answered with exactly one standard error
    response, and the handler is never called (no double-ack risk).
    """

    def _q(self):
        from queue import Queue
        return Queue()

    @staticmethod
    def _handler(request_id, text):
        raise AssertionError(
            "handler must not be called when binding fails"
        )

    @pytest.mark.parametrize("bad_params", [
        {1: "v"},
        {"text": "hi", "request_id": "dup"},
        {},
    ], ids=["non-string-key", "duplicate-request-id", "missing-required"])
    def test_unbindable_params_rejected_with_error_response(self, bad_params):
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-1",
            bad_params, q,
        ) is True
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-bind-1"
        assert resp.get("error") is True
        assert resp["action"] == "intelligent_insert_text"
        assert q.empty()

    def test_unbindable_params_fire_and_forget_no_response(self):
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", None, {1: "v"}, q,
        ) is True
        assert q.empty()

    def test_bindable_params_pass(self):
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-2",
            {"text": "hi"}, q,
        ) is False
        assert q.empty()

    def test_mock_method_accepts_extra_argument(self):
        """A MagicMock's signature is (*args, **kwargs), so an extra
        keyword binds and mock-driven tests keep their existing
        dispatch behavior."""
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            MagicMock(), "intelligent_insert_text", "rq-bind-3",
            {"unexpected_kw": 1}, q,
        ) is False
        assert q.empty()

    def test_extra_argument_rejected_for_real_signature(self):
        """The same extra keyword fails binding against a real handler
        signature and is answered with the standard error response."""
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-5",
            {"text": "hi", "unexpected_kw": 1}, q,
        ) is True
        resp = q.get_nowait()
        assert resp.get("error") is True
        assert q.empty()

    def test_uninspectable_method_passes(self):
        """When inspect.signature cannot introspect the callable (min
        has no text signature), the guard must not reject -- the call
        proceeds with the existing behavior."""
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            min, "intelligent_insert_text", "rq-bind-4", {"text": "x"}, q,
        ) is False
        assert q.empty()

    def test_poisoned_param_name_key_rejected_with_error_response(self):
        """wh-overlay-slow-uia-stale-badges.14.47: an eq-raising str
        subclass key whose VALUE equals a declared parameter name
        survives pickle and passes _coerce_params, then Signature.bind's
        internal lookup of that parameter calls the stored key's __eq__
        and raises RuntimeError -- not TypeError. The guard must contain
        it with the same single error response, or the dispatch except
        suppresses the response and the request times out."""
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-6",
            {EqRaisesStrAction("text"): "dictation"}, q,
        ) is True
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-bind-6"
        assert resp.get("error") is True
        assert resp["action"] == "intelligent_insert_text"
        assert q.empty()

    def test_poisoned_param_name_key_fire_and_forget_no_response(self):
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", None,
            {EqRaisesStrAction("text"): "dictation"}, q,
        ) is True
        assert q.empty()

    def test_poisoned_request_id_key_rejected_with_error_response(self):
        """A poisoned params key whose value is request_id collides
        with the injected keyword at call construction. The reflected
        comparison calls the subclass __eq__ first, so this raises
        RuntimeError -- not the TypeError the exact-key duplicate
        raises -- and must be contained the same way."""
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-7",
            {"text": "hi", EqRaisesStrAction("request_id"): "dup"}, q,
        ) is True
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-bind-7"
        assert resp.get("error") is True
        assert q.empty()


class TestInputProcWalkTimestampEnrichment:
    """wh-overlay-slow-uia-stale-badges.14.48: the reader loop threads a
    dequeue-anchored timestamp into the two UIA-walk handlers with
    {**params, "command_dequeue_monotonic": ...}. That expansion ran
    after _coerce_params but BEFORE the per-action try, so a params key
    whose value equals "command_dequeue_monotonic" and whose equality
    raises killed the whole Input process at the insert -- there is no
    Input respawn path, so voice control stopped. The enrichment must be
    contained like every other pre-try params use.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    @pytest.mark.parametrize("action", ["click_element", "start_overlay_walk"])
    def test_valid_params_receive_the_timestamp(self, action):
        from input_proc import _enrich_walk_params
        q = self._q()
        out = _enrich_walk_params({"target": "OK"}, 123.5, action, "rq-walk-1", q)
        assert out == {"target": "OK", "command_dequeue_monotonic": 123.5}
        assert q.empty()

    def test_poisoned_key_rejected_with_error_response(self):
        from input_proc import _enrich_walk_params
        q = self._q()
        params = {EqRaisesStrAction("command_dequeue_monotonic"): 1.0}
        assert _enrich_walk_params(
            params, 123.5, "click_element", "rq-walk-2", q,
        ) is None
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-walk-2"
        assert resp.get("error") is True
        assert resp["action"] == "click_element"
        assert q.empty()

    def test_poisoned_key_fire_and_forget_no_response(self):
        from input_proc import _enrich_walk_params
        q = self._q()
        params = {EqRaisesStrAction("command_dequeue_monotonic"): 1.0}
        assert _enrich_walk_params(
            params, 123.5, "start_overlay_walk", None, q,
        ) is None
        assert q.empty()

    def test_unrenderable_exception_is_contained(self):
        """wh-overlay-slow-uia-stale-badges.14.54: the helper caught
        the poisoned-key collision and then built its message with an
        f-string, so an exception whose own __str__ raises escaped the
        handler that had just caught it. The helper runs before the
        per-action try, so that second raise stopped the whole Input
        process."""
        from input_proc import _enrich_walk_params
        q = self._q()
        params = {UnrenderableRaisingKey("command_dequeue_monotonic"): 1.0}
        assert _enrich_walk_params(
            params, 123.5, "click_element", "rq-walk-3", q,
        ) is None
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-walk-3"
        assert resp.get("error") is True
        assert resp["action"] == "click_element"
        assert q.empty()

    def test_unrenderable_exception_fire_and_forget_no_response(self):
        from input_proc import _enrich_walk_params
        q = self._q()
        params = {UnrenderableRaisingKey("command_dequeue_monotonic"): 1.0}
        assert _enrich_walk_params(
            params, 123.5, "start_overlay_walk", None, q,
        ) is None
        assert q.empty()


class TestInputProcSelfOwningBindGuardUnrenderable:
    """wh-overlay-slow-uia-stale-badges.14.54, the self-owning half:
    the bind guard renders str(e) into its warning and its error
    response, so an exception whose text cannot be produced escaped
    into the dispatch except -- which suppresses the response for these
    actions, leaving the request unanswered until it timed out.
    """

    def _q(self):
        from queue import Queue
        return Queue()

    @staticmethod
    def _handler(request_id, text):
        raise AssertionError("handler must not be called when binding fails")

    def test_unrenderable_bind_exception_still_answers(self):
        from input_proc import _reject_unbindable_self_owning
        q = self._q()
        params = {UnrenderableRaisingKey("text"): "dictation"}
        assert _reject_unbindable_self_owning(
            self._handler, "intelligent_insert_text", "rq-bind-8", params, q,
        ) is True
        resp = q.get_nowait()
        assert resp["request_id"] == "rq-bind-8"
        assert resp.get("error") is True
        assert q.empty()


class NameBombMeta(type):
    """Metaclass whose class-name access raises."""

    @property
    def __name__(cls):
        raise RuntimeError("type name access exploded")


class NameBombError(Exception, metaclass=NameBombMeta):
    """Exception whose type name and text both refuse to render."""

    def __str__(self):
        raise RuntimeError("exception rendering exploded")


class NameBombValue(metaclass=NameBombMeta):
    """A plain command value whose class name refuses to render.

    wh-overlay-slow-uia-stale-badges.14.61: the malformed-envelope
    containment code named a rejected action or params by its type
    while building the reject text, outside any protective try. This
    shape is globally importable and picklable, so it survives the
    Logic-to-Input round trip and reaches those renderers.
    """


class TestInputProcSafeTypeName:
    """wh-overlay-slow-uia-stale-badges.14.59: the DISPATCH_TIMING error
    branch passed type(e).__name__ as a logging ARGUMENT, and arguments
    are evaluated at the call even though the formatting is lazy. An
    exception class whose metaclass raises on __name__ therefore escaped
    two lines before _safe_error_text ran, and that except sits inside
    the reader loop's outer try -- which ends the only Input consumer.
    """

    def test_safe_type_name_contains_a_raising_type_name(self):
        from input_proc import _safe_type_name
        assert isinstance(_safe_type_name(NameBombError()), str)

    def test_safe_error_text_contains_a_doubly_hostile_exception(self):
        from input_proc import _safe_error_text
        assert isinstance(_safe_error_text(NameBombError()), str)

    def test_dispatch_timing_log_does_not_read_the_type_name_directly(self):
        """The reader loop cannot be driven from a unit test, so the
        guard is on its source -- the same technique
        tests/test_input_proc_response_gate.py uses for this loop."""
        import inspect
        import input_proc

        src = inspect.getsource(input_proc.input_process_main)
        assert "DISPATCH_TIMING action=%s status=error" in src
        assert "type(e).__name__" not in src


class TestInputProcHostileTypeNameBoundaries:
    """wh-overlay-slow-uia-stale-badges.14.61: _coerce_params and
    _validate_action exist so a malformed envelope warns and continues
    instead of ending the only Input consumer. Both built their reject
    text from type(value).__name__ outside any protective try, so a
    value whose metaclass raises on __name__ defeated the containment
    the two functions were added for.
    """

    def test_coerce_params_survives_a_raising_type_name(self):
        from input_proc import _coerce_params
        response_queue = MagicMock()

        assert _coerce_params(
            NameBombValue(), "rid-1", response_queue, "start_overlay_walk",
        ) is None
        response_queue.put.assert_called_once()
        assert response_queue.put.call_args[0][0]["error"] is True

    def test_coerce_params_survives_a_raising_type_name_without_a_request(self):
        from input_proc import _coerce_params
        response_queue = MagicMock()

        assert _coerce_params(
            NameBombValue(), None, response_queue, "start_overlay_walk",
        ) is None
        response_queue.put.assert_not_called()

    def test_validate_action_survives_a_raising_type_name(self):
        from input_proc import _validate_action
        response_queue = MagicMock()

        assert _validate_action(NameBombValue(), "rid-1", response_queue) is False
        response_queue.put.assert_called_once()
        sent = response_queue.put.call_args[0][0]
        assert sent["error"] is True
        assert isinstance(sent["action"], str)

    def test_validate_action_survives_a_raising_type_name_without_a_request(self):
        from input_proc import _validate_action
        response_queue = MagicMock()

        assert _validate_action(NameBombValue(), None, response_queue) is False
        response_queue.put.assert_not_called()
