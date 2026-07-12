"""End-to-end integration tests for Phase 2 rejection-toast pipeline (wh-lscre).

The Phase 2 pipeline has three hops:

  1. Input process: RejectedInsertionStrategy emits a
     ``text_target_rejected`` event onto the response queue.
  2. Logic process: ``_handle_input_event`` dispatches to
     ``_forward_rejection_event_to_gui`` which schema-validates the
     payload and forwards a ``show_rejection_toast`` action onto the
     state-to-GUI queue.
  3. GUI process: ``_show_rejection_toast`` runs the suppression map
     and the wording helper to produce the toast.

Each hop is unit-tested individually (see test_rejected_strategy_emit.py,
test_logic_rejection_forwarding.py, test_rejection_toast_wording.py,
test_rejection_rate_limit.py). This file glues them together so a
field-level regression in any hop fails an integration test.
"""

from __future__ import annotations

from queue import Queue
from unittest.mock import MagicMock

from rejection_rate_limit import ToastSuppressionMap
from rejection_toast_wording import (
    CATEGORY_UNCERTAIN,
    compose_rejection_wording,
)
from shared.text_target_rejection import TextTargetRejectedEvent
from ui.context import UIContext
from ui.rejection_text_cache import RejectionTextCache
from ui.strategies.specific import RejectedInsertionStrategy
from ui.text_target import TextTargetVerdict


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


class TestEndToEnd:
    def test_zed_uncertain_path_full_flow(self):
        # Step 1: input strategy emits.
        response_queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello world", _make_context())

        emitted = response_queue.get_nowait()
        assert emitted["type"] == "text_target_rejected"

        # Step 2: logic forwards.
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
        controller._handle_input_event(emitted)

        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        )
        assert gui_msg["action"] == "show_rejection_toast"

        # Step 3: GUI composes wording + suppression.
        suppression = ToastSuppressionMap()
        decision = suppression.decide((
            gui_msg["process_name"], gui_msg["class_name"],
            gui_msg["control_type"], gui_msg["reason"],
        ))
        assert decision.show is True
        assert decision.is_first is True
        assert decision.lifetime_ms == 8000

        wording = compose_rejection_wording(
            reason=gui_msg["reason"],
            control_type=gui_msg["control_type"],
            process_name=gui_msg["process_name"],
            class_name=gui_msg["class_name"],
            app_friendly_name=gui_msg["app_friendly_name"],
        )
        assert wording.category == CATEGORY_UNCERTAIN

    def test_browser_trap_full_flow_is_silenced(self):
        """wh-1r2b3: browser-trap rejections are dropped silently.

        Before wh-1r2b3 this test verified the rejection event flowed
        through all three hops and the GUI categorized it as
        browser_trap so the Try-it-anyway button would be hidden.
        After wh-1r2b3 the Input process drops the rejection before
        the event leaves the strategy, because a rejection notice
        with no actionable button is pure noise. The end-to-end
        assertion is now negative: no event reaches the response
        queue.
        """

        response_queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=cache,
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
        # The cache must also be empty -- no Try-it-anyway means no
        # need to remember the dictation text.
        assert len(cache.keys()) == 0

    def test_button_definitely_not_text_full_flow_is_silenced(self):
        """wh-1r2b3: denylist-control-type rejections are dropped silently.

        Before wh-1r2b3 the rejection event flowed through all three
        hops and the GUI categorized it as definitely_not_text so
        the body would name the focused control (e.g. "button").
        After wh-1r2b3 the Input process drops the rejection before
        the event leaves the strategy.
        """

        response_queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict(
            reason="denylist_control_type",
            control_type="ButtonControl",
            process_name="explorer.exe",
            class_name="Button",
        ))
        strategy.insert("hello", _make_context(
            process_name="explorer.exe", class_name="Button",
        ))

        assert response_queue.qsize() == 0
        assert len(cache.keys()) == 0

    def test_full_flow_supports_repeated_rejections_across_keys(self):
        # Two different keys should each produce a first-time decision
        # at the GUI; the same key twice should be suppressed second
        # time.
        suppression = ToastSuppressionMap()

        a = ("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class")
        b = ("brave.exe", "", "DocumentControl", "default_reject")

        d1 = suppression.decide(a)
        d2 = suppression.decide(b)
        d3 = suppression.decide(a)
        assert d1.show and d2.show
        assert d3.show is False  # cooldown


class TestNonUncertainNeverReachesGuiQueue:
    """wh-1r2b3.2.2 (deepseek finding 2): the documented contract is
    that the GUI never shows a rejection notice for any non-uncertain
    category. The silencing happens in
    ``RejectedInsertionStrategy.insert`` before
    ``_emit_rejection_event`` is called, so no event is placed on the
    response queue and the Logic forwarder never runs for these
    categories.

    Earlier tests in this file confirm each silenced category leaves
    the response queue empty. This class extends the assertion to the
    next hop: build a LogicController fixture with a real
    state-to-GUI queue, drive the Input strategy with a non-uncertain
    verdict, drain whatever the response queue holds (always empty)
    into ``_handle_input_event``, then assert nothing was placed on
    the GUI-bound queue. A regression that re-routes a non-uncertain
    rejection through the Logic forwarder fails here.

    The three category fixtures cover browser_trap, definitely_not_text,
    and other.
    """

    @staticmethod
    def _make_controller():
        from main import LogicController
        from shared.rejection_token_cache import RejectionTokenCache

        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()
        # _forward_rejection_event_to_gui writes to this cache before
        # posting to the GUI queue on the uncertain branch, so the
        # fixture must provide a real one for the uncertain
        # counterpoint test.
        controller.rejection_token_cache = RejectionTokenCache()
        controller._handle_input_event = (
            LogicController._handle_input_event.__get__(controller)
        )
        controller._forward_rejection_event_to_gui = (
            LogicController._forward_rejection_event_to_gui.__get__(controller)
        )
        return controller

    @staticmethod
    def _drive_full_chain(verdict: TextTargetVerdict, context: UIContext):
        """Run the Input strategy, drain any emitted events into Logic.

        Returns the controller so the caller can assert on the
        GUI-bound queue. The response queue is intentionally not
        asserted empty here -- the silenced-category tests in this
        file already cover the response-queue contract, and the
        uncertain counterpoint legitimately produces one event that
        this helper drains into Logic.
        """
        response_queue: Queue = Queue()
        text_cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=response_queue, text_cache=text_cache,
        )
        strategy.set_pending_verdict(verdict)
        strategy.insert("hello world", context)

        controller = (
            TestNonUncertainNeverReachesGuiQueue._make_controller()
        )
        # Drain whatever the strategy emitted into Logic. For silenced
        # categories the loop is a no-op; for uncertain it runs once.
        # A regression that re-enables the emit for a silenced
        # category surfaces here: the forwarder runs and the silenced
        # tests' assert_not_called assertion fails.
        while not response_queue.empty():
            controller._handle_input_event(response_queue.get_nowait())
        return controller

    def test_browser_trap_does_not_reach_gui_queue(self):
        controller = self._drive_full_chain(
            verdict=_make_verdict(
                reason="default_reject",
                control_type="DocumentControl",
                process_name="brave.exe",
                class_name="",
            ),
            context=_make_context(
                process_name="brave.exe", class_name="",
            ),
        )
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_not_called()

    def test_definitely_not_text_does_not_reach_gui_queue(self):
        controller = self._drive_full_chain(
            verdict=_make_verdict(
                reason="denylist_control_type",
                control_type="ButtonControl",
                process_name="explorer.exe",
                class_name="Button",
            ),
            context=_make_context(
                process_name="explorer.exe", class_name="Button",
            ),
        )
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_not_called()

    def test_other_category_does_not_reach_gui_queue(self):
        # default_reject + non-browser + non-empty class -> category "other".
        controller = self._drive_full_chain(
            verdict=_make_verdict(
                reason="default_reject",
                control_type="WindowControl",
                process_name="zed.exe",
                class_name="Zed::Window",
            ),
            context=_make_context(
                process_name="zed.exe", class_name="Zed::Window",
            ),
        )
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_not_called()

    def test_uncertain_does_reach_gui_queue(self):
        # Counterpoint: the uncertain category must still flow all the
        # way to the GUI queue. A regression that over-silences and
        # drops the uncertain case too would slip past the three tests
        # above; this test catches that.
        controller = self._drive_full_chain(
            verdict=_make_verdict(
                reason="default_reject_paste_capable_class",
            ),
            context=_make_context(),
        )
        # The uncertain branch did emit, so Logic forwarded a
        # show_rejection_toast onto the GUI queue.
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_called_once()
        gui_msg = (
            controller.state_manager.state_to_gui_queue.put_nowait
            .call_args[0][0]
        )
        assert gui_msg["action"] == "show_rejection_toast"


class TestSchemaRoundTrip:
    def test_to_dict_from_dict_roundtrips(self):
        # Defensive: the strategy's emit and the logic-side parse must
        # agree on the schema. A regression in either side breaks the
        # round trip.
        original = TextTargetRejectedEvent(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            app_friendly_name="Zed Editor",
            correlation_token="11111111-1111-4111-8111-111111111111",
        )
        round_tripped = TextTargetRejectedEvent.from_dict(original.to_dict())
        assert round_tripped == original
