"""End-to-end integration tests for Phase 4 interactive override flow (wh-gu4gh).

Phase 2 added the rejection toast (wh-lscre). Phase 4 adds the
interactive override flow on top:

  Input rejects -> Logic forwards (populates rejection_token_cache,
  sends to GUI queue) -> GUI shows toast with Try-it-anyway button ->
  user clicks Try-it-anyway -> GUI emits try_anyway_clicked with
  correlation_token -> Logic resolves token (HIT/EXPIRED/MISS) ->
  on HIT: dispatches retry_dictation_by_token to Input,
  on EXPIRED/MISS: emits click_too_late log + canonical follow-up
  toast.

  On verified retry: ClickCounter increments per tuple. At threshold
  (default 3): publishes RetryThresholdReached. Logic forwarder
  consults _grant_prompt_no_suppressed and forwards
  text_target_grant_prompt to GUI. GUI renders the three-strikes
  follow-up toast with Yes/No buttons. Yes click adds the tuple to
  soft_allow_tuples.toml and updates the input-side predicate. Disk
  failure on Yes emits soft_allow_write_failed; the GUI shows the
  acknowledgment toast and clears the per-tuple dedup so the user
  can retry. No click registers the tuple in
  _grant_prompt_no_suppressed so the prompt does not re-fire this
  run.

Each hop is unit-tested individually:

  - test_logic_rejection_forwarding.py
  - test_logic_try_anyway_handler.py
  - test_logic_retry_forwarding.py
  - test_logic_retry_verified_signal.py
  - test_click_counter.py
  - test_logic_grant_prompt_forwarding.py
  - test_logic_grant_prompt_yes_handler.py
  - test_logic_grant_prompt_no_handler.py
  - test_gui_grant_prompt.py
  - test_gui_soft_allow_write_failed.py

This file glues the hops together so a field-level regression in any
hop fails an integration test. Models the pattern in
test_phase2_integration.py.

End-to-end smoke testing against real apps (item 11 in the wh-gu4gh
description) is manual and out of scope for the automated suite.
"""

from __future__ import annotations

import asyncio
import logging
from queue import Queue
from unittest.mock import AsyncMock, MagicMock

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

from rejection_toast_wording import (
    CATEGORY_UNCERTAIN,
    compose_rejection_wording,
    should_show_try_anyway,
)
from shared.rejection_token_cache import RejectionTokenCache
from shared.try_anyway_clicked import ACTION_NAME as TRY_ANYWAY_ACTION
from ui.context import UIContext
from ui.rejection_text_cache import RejectionTextCache
from ui.strategies.specific import RejectedInsertionStrategy
from ui.text_target import TextTargetVerdict


EXPECTED_CLICK_TOO_LATE_WORDING = (
    "Wheelhouse couldn't try again. Say the words again, "
    "then click Try it anyway."
)


def _make_context(
    process_name: str = "zed.exe",
    class_name: str = "Zed::Window",
) -> UIContext:
    ctrl = MagicMock()
    ctrl.ControlTypeName = "WindowControl"
    return UIContext(
        focused_control=ctrl,
        is_flutter=False, is_terminal=False,
        process_name=process_name, class_name=class_name,
        process_id=12345,
    )


def _make_verdict(
    reason: str = "default_reject_paste_capable_class",
    control_type: str = "WindowControl",
    process_name: str = "zed.exe",
    class_name: str = "Zed::Window",
) -> TextTargetVerdict:
    return TextTargetVerdict(
        verdict=False, reason=reason,
        supported_patterns=("Invoke",),
        control_type=control_type, class_name=class_name,
        process_name=process_name,
    )


def _make_controller(rejection_token_cache=None):
    """Build a MagicMock controller with the methods under test bound.

    Mirrors `_make_controller` in test_logic_try_anyway_handler.py
    plus _forward_rejection_event_to_gui from test_phase2_integration.py
    so the same fixture covers both hops.
    """
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.app = MagicMock()
    controller.app.send_request = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.rejection_token_cache = (
        rejection_token_cache or RejectionTokenCache()
    )

    # Bind the real methods under test.
    controller._handle_input_event = (
        LogicController._handle_input_event.__get__(controller)
    )
    controller._forward_rejection_event_to_gui = (
        LogicController._forward_rejection_event_to_gui.__get__(controller)
    )
    controller._handle_try_anyway_clicked = (
        LogicController._handle_try_anyway_clicked.__get__(controller)
    )
    controller._send_retry_followup_toast = (
        LogicController._send_retry_followup_toast.__get__(controller)
    )
    # forward_retry_dictation_by_token is mocked so we can assert it
    # was called without exercising the input-side IPC pipeline.
    controller.forward_retry_dictation_by_token = AsyncMock()
    return controller


class TestTryAnywayHitFullFlow:
    """The happy path: input rejects with a correlation_token, logic
    populates the rejection_token_cache and forwards to the GUI, the
    user clicks Try-it-anyway, logic resolves the token to HIT, and
    the input-side retry forwarder is awaited.

    Catches contract drift in: text_target_rejected schema,
    rejection_token_cache.put / resolve, try_anyway_clicked schema,
    the cache HIT branch in _handle_try_anyway_clicked.
    """

    def test_uncertain_rejection_then_try_anyway_routes_to_forwarder(self):
        # Hop 1: Input strategy emits text_target_rejected.
        response_queue: Queue = Queue()
        text_cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=text_cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello world", _make_context())

        emitted = response_queue.get_nowait()
        assert emitted["type"] == "text_target_rejected"
        token = emitted["correlation_token"]

        # Hop 2: Logic _handle_input_event dispatches to forwarder,
        # which validates the schema, populates the cache, and sends
        # show_rejection_toast onto the GUI queue.
        controller = _make_controller()
        controller._handle_input_event(emitted)

        # Cache holds the tuple under the same correlation token.
        from shared.rejection_token_cache import CacheStatus
        cache_result = controller.rejection_token_cache.resolve(token)
        assert cache_result.status is CacheStatus.HIT
        assert cache_result.tuple_.process_name == "zed.exe"
        # resolve removes the entry from the cache; put it back so the
        # try-anyway HIT path below can find it. This mirrors the
        # real flow where the GUI dispatches try-anyway BEFORE Logic
        # has called resolve on the cache.
        controller.rejection_token_cache.put(token, cache_result.tuple_)

        # GUI received the rejection toast action.
        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait
            .call_args[0][0]
        )
        assert gui_msg["action"] == "show_rejection_toast"
        assert gui_msg["correlation_token"] == token

        # Hop 3: GUI emits try_anyway_clicked carrying the token.
        click_command = {
            "action": TRY_ANYWAY_ACTION,
            "correlation_token": token,
        }
        asyncio.run(controller._handle_try_anyway_clicked(click_command))

        # The forwarder is awaited with the token AND the resolved
        # tuple (wh-vbvgf.3.2: pass-through guards against TTL race).
        controller.forward_retry_dictation_by_token.assert_awaited_once()
        call_args = (
            controller.forward_retry_dictation_by_token.await_args
        )
        assert call_args.args[0] == token
        passed_rejection = call_args.kwargs.get("rejection")
        assert passed_rejection is not None
        assert passed_rejection.process_name == "zed.exe"


class TestStaleClickFullFlow:
    """A click that arrives after the cache TTL elapses is dropped
    with a click_too_late log and a canonical follow-up toast. The
    input-side retry forwarder is NOT invoked.

    Catches contract drift in: cache TTL semantics, the EXPIRED
    branch in _handle_try_anyway_clicked, the canonical wording.
    """

    def test_expired_token_logs_click_too_late_and_shows_followup(
        self, caplog,
    ):
        # Use a deterministic clock so the test is fast and stable
        # on Windows where time.monotonic resolution can be 15ms.
        clock = [1000.0]
        cache = RejectionTokenCache(
            ttl_seconds=10.0, time_source=lambda: clock[0],
        )
        controller = _make_controller(rejection_token_cache=cache)

        # Hop 1+2: input rejects, logic forwards (populates cache).
        response_queue: Queue = Queue()
        text_cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=text_cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello world", _make_context())
        emitted = response_queue.get_nowait()
        token = emitted["correlation_token"]
        controller._handle_input_event(emitted)

        # Advance the clock past the TTL (cache holds 10s; jump 100s).
        clock[0] += 100.0

        # Hop 3: GUI emits try_anyway_clicked. The cache resolves to
        # EXPIRED, the handler logs click_too_late and emits the
        # canonical follow-up toast.
        click_command = {
            "action": TRY_ANYWAY_ACTION,
            "correlation_token": token,
        }
        with caplog.at_level(logging.INFO, logger="main"):
            asyncio.run(
                controller._handle_try_anyway_clicked(click_command)
            )

        # No IPC to Input.
        controller.forward_retry_dictation_by_token.assert_not_awaited()

        # click_too_late line carries the token.
        click_too_late_records = [
            rec for rec in caplog.records
            if "click_too_late" in rec.getMessage()
        ]
        assert len(click_too_late_records) >= 1
        assert any(
            token in rec.getMessage()
            for rec in click_too_late_records
        )

        # GUI queue carries exactly one show_notification with the
        # canonical wording. The earlier put_nowait in the forwarder
        # carried show_rejection_toast; the show_notification is
        # the click_too_late follow-up.
        toasts = [
            call.args[0]
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
            if call.args[0].get("action") == "show_notification"
        ]
        assert len(toasts) == 1
        assert toasts[0]["message"] == EXPECTED_CLICK_TOO_LATE_WORDING


class TestBrowserTrapFullFlow:
    """A browser-trap rejection (rejection toast category =
    browser_trap) must not surface the Try-it-anyway button. The
    button visibility is gated on the wording category by
    `should_show_try_anyway`.

    Catches contract drift in: rejection wording categorization,
    should_show_try_anyway return values, the rejection toast's
    category-based button gate.
    """

    def test_browser_trap_is_silenced_in_input_process(self):
        """wh-1r2b3: browser-trap rejections never leave the Input process.

        Before wh-1r2b3 the rejection event flowed all the way to the
        GUI and the Try-it-anyway button was hidden by the category
        gate. After wh-1r2b3 the Input process drops the rejection
        before it reaches the response queue. There is no event to
        flow, so the button-gate question is moot.

        Catches a regression where the silencing check is removed or
        moved so the rejection event escapes the Input process for
        a browser-trap category.
        """

        response_queue: Queue = Queue()
        text_cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=text_cache,
        )
        strategy.set_pending_verdict(_make_verdict(
            reason="default_reject",
            control_type="DocumentControl",
            process_name="brave.exe",
            class_name="",
        ))
        strategy.insert("hello", _make_context(
            process_name="brave.exe", class_name="",
        ))

        assert response_queue.qsize() == 0
        assert len(text_cache.keys()) == 0

    def test_uncertain_category_enables_try_anyway(self):
        # Counterpoint: the uncertain category does show the button.
        # This protects against a regression that flips the gate's
        # default value (e.g. someone changes the category check
        # and the test in test_rejection_toast.py only covers the
        # category-name level, not the post-forward IPC level).
        response_queue: Queue = Queue()
        text_cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=text_cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        emitted = response_queue.get_nowait()

        controller = _make_controller()
        controller._handle_input_event(emitted)

        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait
            .call_args[0][0]
        )

        wording = compose_rejection_wording(
            reason=gui_msg["reason"],
            control_type=gui_msg["control_type"],
            process_name=gui_msg["process_name"],
            class_name=gui_msg["class_name"],
            app_friendly_name=gui_msg.get("app_friendly_name"),
        )
        assert wording.category == CATEGORY_UNCERTAIN
        assert should_show_try_anyway(wording.category) is True


def _make_retry_verified(
    process_name="zed.exe",
    class_name="zed::Workspace",
    control_type="Pane",
    app_friendly_name="Zed",
):
    # Use the absolute import path so the RetryVerified class
    # object matches the one ClickCounter subscribes against. The
    # CWD-relative `from events import RetryVerified` would create
    # a parallel module object on Windows and the bus's
    # type(event)-keyed dispatch would not find the subscriber.
    from services.wheelhouse.events import RetryVerified
    return RetryVerified(
        process_name=process_name,
        class_name=class_name,
        control_type=control_type,
        app_friendly_name=app_friendly_name,
    )


def _wire_yes_click_controller(tmp_path, threshold=3):
    """Shared fixture for the Yes-click test classes.

    wh-vbvgf.21.2 (deepseek review): TestYesClickWriteAndCounterReset
    and TestYesClickDiskFailureFullFlow had nearly-identical _wire
    methods. Both bind the same set of LogicController methods on a
    MagicMock controller and subscribe the threshold forwarder to a
    real EventBus. Folded into one helper so the two classes only
    diverge on what they exercise after the wiring is done.
    """
    from main import LogicController
    from event_bus import EventBus
    from click_counter import ClickCounter
    from services.wheelhouse.events import RetryThresholdReached

    bus = EventBus()
    counter = ClickCounter(
        event_bus=bus,
        persistence_path=tmp_path / "counters.toml",
        threshold=threshold,
    )
    counter.subscribe()

    controller = MagicMock(spec=LogicController)
    controller.event_bus = bus
    controller.click_counter = counter
    controller.app = MagicMock()
    controller.app.send_command = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller._grant_prompt_no_suppressed = set()
    controller._soft_allow_path = tmp_path / "soft_allow_tuples.toml"
    controller._resolve_soft_allow_path = (
        LogicController._resolve_soft_allow_path.__get__(controller)
    )
    controller._on_retry_threshold_reached = (
        LogicController._on_retry_threshold_reached.__get__(controller)
    )
    controller._handle_grant_prompt_yes_clicked = (
        LogicController._handle_grant_prompt_yes_clicked.__get__(
            controller
        )
    )
    controller.add_soft_allow = (
        LogicController.add_soft_allow.__get__(controller)
    )
    bus.subscribe(
        RetryThresholdReached,
        controller._on_retry_threshold_reached,
    )
    return bus, counter, controller


async def _drain_persist_task(counter):
    """Await the ClickCounter global single-flight persist task if
    one is in flight (wh-vbvgf.21.3 deepseek review).

    The counter triggers an asyncio.create_task on each verified
    retry to write the file. Without awaiting it, asyncio.run()
    returns while the task is still running, and the next test's
    asyncio.run() can see leftover task state or file-system races.
    """
    if counter._persist_task is not None:
        try:
            await counter._persist_task
        except asyncio.CancelledError:
            pass


class TestVerifiedRetryCounterFullFlow:
    """Three verified retries against the same tuple cross the
    threshold and produce a text_target_grant_prompt action on the
    GUI queue. The fourth retry (after a No click) is suppressed.

    Catches contract drift in: RetryVerified -> ClickCounter
    increment, RetryThresholdReached publish at threshold,
    _on_retry_threshold_reached forwarder, _grant_prompt_no_suppressed
    consultation in the forwarder, _handle_grant_prompt_no_clicked
    add-to-suppressed-set.
    """

    def _wire(self, tmp_path):
        from main import LogicController
        from event_bus import EventBus
        from click_counter import ClickCounter
        from services.wheelhouse.events import RetryThresholdReached

        bus = EventBus()
        counter = ClickCounter(
            event_bus=bus,
            persistence_path=tmp_path / "counters.toml",
            threshold=3,
        )
        counter.subscribe()

        controller = MagicMock(spec=LogicController)
        controller.event_bus = bus
        controller.click_counter = counter
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()
        controller._grant_prompt_no_suppressed = set()

        controller._on_retry_threshold_reached = (
            LogicController._on_retry_threshold_reached.__get__(controller)
        )
        controller._handle_grant_prompt_no_clicked = (
            LogicController._handle_grant_prompt_no_clicked.__get__(controller)
        )
        # wh-27gvv: the No handler now writes the declined entry to
        # disk via add_declined before updating the suppression set.
        # Bind the real method and point _declined_path at a temp file
        # so the persistence runs and the existing suppression
        # assertions still hold.
        controller.add_declined = (
            LogicController.add_declined.__get__(controller)
        )
        controller._resolve_declined_path = (
            LogicController._resolve_declined_path.__get__(controller)
        )
        controller._declined_path = (
            tmp_path / "soft_allow_declined_tuples.toml"
        )
        bus.subscribe(
            RetryThresholdReached,
            controller._on_retry_threshold_reached,
        )
        return bus, counter, controller

    def test_three_verified_retries_publish_grant_prompt(self, tmp_path):
        bus, counter, controller = self._wire(tmp_path)
        event = _make_retry_verified()

        async def run():
            for _ in range(3):
                await bus.publish(event)
            # wh-vbvgf.21.3: drain the global single-flight write
            # so file-system state is settled before assertions.
            await _drain_persist_task(counter)

        asyncio.run(run())

        # Counter sat at 3 for the same tuple.
        assert counter.get_count("zed.exe", "zed::Workspace", "Pane") == 3

        # GUI queue received exactly one text_target_grant_prompt
        # (the publish at count=3). The earlier increments were
        # below threshold and did not publish.
        prompts = [
            call.args[0]
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
            if call.args[0].get("action") == "text_target_grant_prompt"
        ]
        assert len(prompts) == 1
        assert prompts[0]["count"] == 3
        assert prompts[0]["process_name"] == "zed.exe"

    def test_no_click_suppresses_next_threshold_publish(self, tmp_path):
        bus, counter, controller = self._wire(tmp_path)
        event = _make_retry_verified()

        async def run():
            # Three retries cross the threshold and publish once.
            for _ in range(3):
                await bus.publish(event)
            # User clicks No on the grant prompt. Logic adds the
            # tuple to _grant_prompt_no_suppressed.
            await controller._handle_grant_prompt_no_clicked({
                "action": "grant_prompt_no_clicked",
                "process_name": "zed.exe",
                "class_name": "zed::Workspace",
                "control_type": "Pane",
            })
            # A fourth retry would normally publish again
            # (count=4 >= threshold). The suppression set blocks
            # the forward.
            await bus.publish(event)
            await _drain_persist_task(counter)

        asyncio.run(run())

        # Only ONE text_target_grant_prompt landed on the GUI
        # queue: the first publish at count=3. The fourth retry
        # was published on the bus but the forwarder dropped it
        # because the No-click registered the tuple in
        # _grant_prompt_no_suppressed.
        prompts = [
            call.args[0]
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
            if call.args[0].get("action") == "text_target_grant_prompt"
        ]
        assert len(prompts) == 1


class TestYesClickWriteAndCounterReset:
    """The Yes click on the grant prompt writes the tuple to
    soft_allow_tuples.toml, sends add_soft_allow_tuple IPC to the
    input process, and resets the per-tuple counter to zero.

    Catches contract drift in: GrantPromptYesClickedEvent schema,
    add_soft_allow's write-then-IPC sequence, ClickCounter.reset_tuple,
    AddSoftAllowOutcome.SUCCESS branch in
    _handle_grant_prompt_yes_clicked.
    """

    def test_yes_click_writes_file_sends_ipc_resets_counter(self, tmp_path):
        bus, counter, controller = _wire_yes_click_controller(tmp_path)
        retry_event = _make_retry_verified()
        soft_allow_path = controller._soft_allow_path

        async def run():
            for _ in range(3):
                await bus.publish(retry_event)
            # Counter sat at 3, grant prompt fired. User clicks Yes.
            await controller._handle_grant_prompt_yes_clicked({
                "action": "grant_prompt_yes_clicked",
                "process_name": "zed.exe",
                "class_name": "zed::Workspace",
                "control_type": "Pane",
            })
            await _drain_persist_task(counter)

        asyncio.run(run())

        # File was written and contains the tuple.
        assert soft_allow_path.exists()
        body = soft_allow_path.read_text(encoding="utf-8")
        assert "zed.exe" in body
        assert "zed::Workspace" in body
        assert "Pane" in body

        # IPC was sent to the input process.
        controller.app.send_command.assert_awaited_once()
        call = controller.app.send_command.await_args
        assert call.args[0] == "add_soft_allow_tuple"
        ipc_params = call.args[1]
        assert ipc_params["process_name"] == "zed.exe"

        # Counter for the tuple was reset to zero.
        assert counter.get_count(
            "zed.exe", "zed::Workspace", "Pane",
        ) == 0


class TestYesClickIpcRetry:
    """wh-grant-ipc-failed-ux: after a successful disk write,
    add_soft_allow retries a failed add_soft_allow_tuple IPC send
    before reporting IPC_FAILED. A transient queue hiccup no longer
    leaves the running input process without the grant (which showed
    the user the same rejection notice they just clicked Yes on,
    until the next restart).
    """

    def _wire(self, tmp_path):
        bus, counter, controller = _wire_yes_click_controller(tmp_path)
        # No sleeping in tests: two immediate retries.
        controller._soft_allow_ipc_retry_delays = (0, 0)
        return controller

    def test_transient_ipc_failure_recovers_on_retry(self, tmp_path):
        controller = self._wire(tmp_path)
        controller.app.send_command = AsyncMock(
            side_effect=[RuntimeError("queue full"), RuntimeError("again"), None]
        )

        outcome = asyncio.run(
            controller.add_soft_allow("zed.exe", "zed::Workspace", "Pane")
        )

        from main import AddSoftAllowOutcome
        assert outcome is AddSoftAllowOutcome.SUCCESS
        assert controller.app.send_command.await_count == 3

    def test_ipc_failed_only_after_all_attempts_exhausted(self, tmp_path):
        controller = self._wire(tmp_path)
        controller.app.send_command = AsyncMock(
            side_effect=RuntimeError("input process gone")
        )

        outcome = asyncio.run(
            controller.add_soft_allow("zed.exe", "zed::Workspace", "Pane")
        )

        from main import AddSoftAllowOutcome
        assert outcome is AddSoftAllowOutcome.IPC_FAILED
        # 1 initial attempt + one per configured retry delay.
        assert controller.app.send_command.await_count == 3

    def test_single_attempt_success_sends_once(self, tmp_path):
        controller = self._wire(tmp_path)

        outcome = asyncio.run(
            controller.add_soft_allow("zed.exe", "zed::Workspace", "Pane")
        )

        from main import AddSoftAllowOutcome
        assert outcome is AddSoftAllowOutcome.SUCCESS
        assert controller.app.send_command.await_count == 1


class TestYesClickDiskFailureFullFlow:
    """When the soft-allow disk write fails on Yes, add_soft_allow
    enqueues a soft_allow_write_failed event on the GUI queue, the
    add_soft_allow_tuple IPC is NOT sent to the input process, and
    the counter is NOT reset.

    The user can later say the words again, the verified-retry
    counter increments past 3 again, and the grant prompt re-fires.
    The wh-vbvgf.18.1 fix in _show_soft_allow_write_failed_toast
    clears the GUI dedup so the prompt actually re-appears.
    """

    def test_disk_fail_emits_event_skips_ipc_keeps_counter(
        self, tmp_path, monkeypatch,
    ):
        bus, counter, controller = _wire_yes_click_controller(tmp_path)
        retry_event = _make_retry_verified()

        # Force the disk write to fail. add_soft_allow uses
        # asyncio.to_thread on append_soft_allow_tuple; patching the
        # symbol main.py imports captures both call sites.
        import main as logic_main
        monkeypatch.setattr(
            logic_main, "append_soft_allow_tuple",
            lambda new_tuple, path: False,
        )

        async def run():
            for _ in range(3):
                await bus.publish(retry_event)
            await controller._handle_grant_prompt_yes_clicked({
                "action": "grant_prompt_yes_clicked",
                "process_name": "zed.exe",
                "class_name": "zed::Workspace",
                "control_type": "Pane",
            })
            await _drain_persist_task(counter)

        asyncio.run(run())

        # GUI queue carries one text_target_grant_prompt (from the
        # threshold publish) AND one soft_allow_write_failed (from
        # the disk failure).
        actions = [
            call.args[0].get("action")
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
        ]
        assert "soft_allow_write_failed" in actions
        assert actions.count("soft_allow_write_failed") == 1

        write_failed = [
            call.args[0]
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
            if call.args[0].get("action") == "soft_allow_write_failed"
        ][0]
        assert write_failed["process_name"] == "zed.exe"

        # IPC to input was NOT sent because the disk write failed
        # before reaching the send_command call.
        controller.app.send_command.assert_not_awaited()

        # Counter is still at 3 -- per the bead spec, DISK_FAILED
        # leaves the counter alone so the user can click Yes again
        # later.
        assert counter.get_count(
            "zed.exe", "zed::Workspace", "Pane",
        ) == 3

    def test_retry_after_disk_failure_publishes_second_grant_prompt(
        self, tmp_path, monkeypatch,
    ):
        """wh-vbvgf.20.1 (codex review): the previous test asserts the
        immediate symptoms of a disk-write failure but does not
        exercise the loop the bead spec actually protects: the user
        retries, the verified-retry counter increments past the
        threshold again, and the grant prompt re-fires.

        A regression that left the counter at 3 (the previous test
        passes) but accidentally added the tuple to
        _grant_prompt_no_suppressed on DISK_FAILED, broke the
        at-or-above-threshold publish path after a failed Yes, or
        dropped the second prompt in the forwarder would slip past
        the previous test. This test catches that.
        """
        bus, counter, controller = _wire_yes_click_controller(tmp_path)
        retry_event = _make_retry_verified()

        import main as logic_main
        monkeypatch.setattr(
            logic_main, "append_soft_allow_tuple",
            lambda new_tuple, path: False,
        )

        async def run():
            for _ in range(3):
                await bus.publish(retry_event)
            await controller._handle_grant_prompt_yes_clicked({
                "action": "grant_prompt_yes_clicked",
                "process_name": "zed.exe",
                "class_name": "zed::Workspace",
                "control_type": "Pane",
            })
            # User says the dictation words again. Counter
            # increments to 4 (>= threshold) and republishes
            # RetryThresholdReached. The forwarder must not have
            # added the tuple to the No-suppression set on
            # DISK_FAILED.
            await bus.publish(retry_event)
            await _drain_persist_task(counter)

        asyncio.run(run())

        # The Logic side enqueued two text_target_grant_prompt
        # actions: count=3 from the first threshold and count=4
        # from the post-failure retry.
        prompts = [
            call.args[0]
            for call in (
                controller.state_manager.state_to_gui_queue
                .put_nowait.call_args_list
            )
            if call.args[0].get("action") == "text_target_grant_prompt"
        ]
        assert len(prompts) == 2
        assert [p["count"] for p in prompts] == [3, 4]

        # Tuple was NOT added to the No-suppression set by the
        # disk-failure path. (The No path adds via
        # _handle_grant_prompt_no_clicked; the Yes-with-disk-fail
        # path must not.)
        assert ("zed.exe", "zed::Workspace", "Pane") not in (
            controller._grant_prompt_no_suppressed
        )

        # Counter is at 4 after the post-failure retry.
        assert counter.get_count(
            "zed.exe", "zed::Workspace", "Pane",
        ) == 4


@pytest.mark.usefixtures("qapp")
class TestGuiDedupClearAfterDiskFailure:
    """wh-vbvgf.20.1 (codex review): a sibling integration test on
    the GUI side. The Logic-side test above asserts the second
    threshold publish reaches the GUI queue. This test asserts the
    GUI rendering path actually shows the prompt: the soft_allow_
    write_failed handler clears the per-tuple dedup so the next
    text_target_grant_prompt for the same tuple is not silently
    suppressed by _grant_prompt_acted_on.

    The dedup-clear logic is the wh-vbvgf.18.1 fix in
    _show_soft_allow_write_failed_toast. This integration test
    glues that to the grant-prompt rendering path so a regression
    that re-broke the dedup clear (or one that broke the
    grant-prompt acted-on consultation) fails here.
    """

    def test_grant_prompt_re_renders_after_disk_failure_clear(self):
        from unittest.mock import MagicMock, patch

        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager
            shutdown = MagicMock()
            shutdown.is_set.return_value = False
            mgr = GuiManager(shutdown, MagicMock(), MagicMock())

        tuple_key = ("zed.exe", "zed::Workspace", "Pane")
        # Pre-populate the dedup set as if the user had clicked
        # Yes on a previous grant prompt.
        mgr._grant_prompt_acted_on.add(tuple_key)

        # Disk failure event arrives. Handler clears the dedup.
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ):
            mgr._show_soft_allow_write_failed_toast({
                "action": "soft_allow_write_failed",
                "process_name": tuple_key[0],
                "class_name": tuple_key[1],
                "control_type": tuple_key[2],
            })

        assert tuple_key not in mgr._grant_prompt_acted_on

        # Next grant prompt for the same tuple now reaches the
        # rendering path -- show_prompt is called on the widget,
        # not silently suppressed by the acted-on set.
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_instance = MagicMock()
            mock_toast_instance.isVisible.return_value = False
            mock_toast_cls.return_value = mock_toast_instance

            mgr._show_grant_prompt_toast({
                "action": "text_target_grant_prompt",
                "process_name": tuple_key[0],
                "class_name": tuple_key[1],
                "control_type": tuple_key[2],
                "app_friendly_name": "Zed",
                "count": 4,
            })

        mock_toast_instance.show_prompt.assert_called_once()


class TestCounterPersistenceAcrossRestart:
    """The click counter writes per-tuple counts to disk so a logic
    process restart picks up where it left off. wh-82lnx is the
    bead that introduced this; the integration test guards the
    file format and the load_from_disk path together.
    """

    def test_count_at_2_survives_logic_restart(self, tmp_path):
        from event_bus import EventBus
        from click_counter import ClickCounter

        path = tmp_path / "counters.toml"

        # Lifetime A: drive 2 verified retries, force a flush, then
        # tear down.
        bus_a = EventBus()
        counter_a = ClickCounter(
            event_bus=bus_a, persistence_path=path, threshold=3,
        )
        counter_a.subscribe()
        event = _make_retry_verified()

        async def run_a():
            for _ in range(2):
                await bus_a.publish(event)
            # Wait for the global single-flight writer to finish.
            if counter_a._persist_task is not None:
                await counter_a._persist_task

        asyncio.run(run_a())
        assert counter_a.get_count(
            "zed.exe", "zed::Workspace", "Pane",
        ) == 2

        # Lifetime B: a fresh ClickCounter loads the file and reads
        # the count back. Mirrors the logic-process restart path
        # in main.py at startup.
        bus_b = EventBus()
        counter_b = ClickCounter(
            event_bus=bus_b, persistence_path=path, threshold=3,
        )
        counter_b.load_from_disk()
        assert counter_b.get_count(
            "zed.exe", "zed::Workspace", "Pane",
        ) == 2
