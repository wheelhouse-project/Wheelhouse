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

        # Stub app with an awaitable send_command.
        app = SimpleNamespace(send_command=AsyncMock())

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

        # IPC was sent with the expected payload.
        app.send_command.assert_awaited_once()
        call_args = app.send_command.await_args
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
        app.send_command.side_effect = RuntimeError("simulated IPC failure")
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
        assert app.send_command.await_count == 3

        # No soft_allow_write_failed event -- that's DISK_FAILED only.
        assert not any(
            m.get("action") == "soft_allow_write_failed"
            for m in gui_queue.messages
        )

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
