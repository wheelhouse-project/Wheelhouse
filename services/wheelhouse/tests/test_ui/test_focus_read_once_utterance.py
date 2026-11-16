"""One focus read for each utterance (wh-focus-read-once-utterance).

The Input process used to call ``capture_context`` -- the UI Automation
focus read, ``GetFocusedControl`` plus the identity probes -- for EVERY
dictated word. This bead keeps the read of the first word of an
utterance and lets the later words of the same utterance use that
stored read, while a Win32 check (``current_foreground_root`` and
``TargetIdentity.is_current``, no UI Automation) shows the same window
in the foreground.

The design is the bead comment "STAGE DESIGN DONE", with the conditions
C1 to C6 of "[boss] RULING on STAGE DESIGN (13:36 on 2026-11-14)" and
"[claude] BUILD DECISIONS 13:50". The discard events are:

  D1 start_utterance; D2 end_utterance (after its letter flush);
  D3 the Win32 check shows another window; D4 the retry pass;
  D5 a failed or empty read is not stored; D6 any other Input command;
  D7 a physical click or key press (invalidate_buffer).

Each test drives the real ``UIActionHandler`` the way the command loop
does: ``note_input_command(action)`` first, then the action itself. The
Win32 calls and ``capture_context`` are replaced, so no test reads or
touches the real desktop. The router is the real ``InsertionRouter``
with a stub text-target check, so the strategy choice is the shipped
decision code.
"""
from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock, patch

import pytest

from ui.context import UIContext
from ui.router import InsertionRouter
from ui.strategies.base import InsertionResult
from ui.target_identity import TargetIdentity
from ui.text_target import TextTargetVerdict

_MOD = "ui.ui_action_handler"

_CONFIG = {
    "ui_actions": {
        "timing": {"utterance_clipboard_timeout_seconds": 1.0},
        "verified_unicode": {"max_chars": 50},
        "foreground_check": {"same_process_browser_names_extend": []},
        "text_target": {},
    }
}

# The short-text limit of the router in these tests. "hello" is under it
# and goes to UnicodeFirstStrategy; a longer word goes to StandardStrategy.
_MAX_CHARS = 10

_WINDOW_A = 0x5000
_WINDOW_B = 0x6000

_DELIVERED = InsertionResult(success=True, clipboard_dirty=False)

_LOGGER = "ui.ui_action_handler"


def _identity(root: int) -> TargetIdentity:
    """A complete identity for one top-level window."""
    return TargetIdentity(
        hwnd=root + 1, root=root, process_id=42, tag=7, root_tag=8,
        foreground_root=root, foreground_tag=8,
    )


def _context(root: int = _WINDOW_A, *, control=None, **fields) -> UIContext:
    """What capture_context returns for a live control in one window."""
    values = dict(
        focused_control=control if control is not None else MagicMock(),
        is_flutter=False,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
        process_id=42,
        target_identity=_identity(root),
    )
    values.update(fields)
    return UIContext(**values)


class _Desktop:
    """The replaced Win32 state and the replaced focus read.

    ``foreground`` is the root that ``current_foreground_root`` answers.
    ``is_current`` answers True while the identity names the foreground.
    ``capture`` counts every focus read; it answers the next queued
    context, or a fresh live context in the foreground window.
    """

    def __init__(self):
        self.foreground = _WINDOW_A
        self.reads: list[UIContext] = []
        self.queued: list[UIContext] = []

    def capture(self) -> UIContext:
        context = self.queued.pop(0) if self.queued else _context(self.foreground)
        self.reads.append(context)
        return context

    def foreground_root(self) -> int:
        return self.foreground

    def is_current(self, identity, **_kwargs) -> bool:
        return identity.foreground_root == self.foreground


def _accept_everything():
    """A text-target check that accepts every control."""
    predicate = MagicMock()
    predicate.evaluate.return_value = TextTargetVerdict(
        verdict=True, reason="edit_control",
    )
    return predicate


@pytest.fixture
def handler():
    """A UIActionHandler with the collaborators mocked and a real router.

    The strategy objects are real so the router hands back the shipped
    types; each strategy's ``insert`` is replaced by a recorder that
    reports a delivered word.
    """
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        built = UIActionHandler(response_queue=MagicMock(), config=_CONFIG)

    built.response = MagicMock()
    built.terminal_editor.is_active = False
    built.terminal_editor._submit_in_progress.is_set.return_value = False
    built.utterance_manager.is_in_utterance.return_value = True
    for strategy in (
        built.unicode_first_strategy, built.standard_strategy,
        built.clipboard_only_strategy, built.flutter_strategy,
        built.simple_paste_strategy,
    ):
        strategy.insert = MagicMock(return_value=_DELIVERED)
    built.router = InsertionRouter(
        built.standard_strategy,
        built.flutter_strategy,
        built.simple_paste_strategy,
        rejected_strategy=built.rejected_strategy,
        text_target_predicate=_accept_everything(),
        verified_unicode_strategy=built.unicode_first_strategy,
        verified_unicode_max_chars=_MAX_CHARS,
        clipboard_only_strategy=built.clipboard_only_strategy,
    )
    return built


@pytest.fixture
def desktop():
    """Replace every Win32 and UI Automation read the insert path makes.

    ``create=True`` on ``current_foreground_root``: the handler module
    names it only once this bead imports it there.
    """
    state = _Desktop()
    with patch(f"{_MOD}.capture_context", side_effect=state.capture), \
         patch(f"{_MOD}.current_foreground_root",
               side_effect=state.foreground_root, create=True), \
         patch.object(TargetIdentity, "is_current",
                      lambda identity, **kw: state.is_current(identity, **kw)):
        yield state


def _command(handler, action: str) -> None:
    """What the command loop does for each command before it dispatches.

    Looked up with getattr so that, before the method exists, a test fails
    on the behavior it measures (the number of reads) rather than on the
    missing name. Without the method the handler keeps the base behavior,
    one read for each word, so every count below still fails.
    """
    note = getattr(handler, "note_input_command", None)
    if note is not None:
        note(action)


def _start(handler, utterance_id: int = 1) -> None:
    _command(handler, "start_utterance")
    handler.start_utterance(utterance_id)


def _end(handler, utterance_id: int = 1) -> None:
    _command(handler, "end_utterance")
    handler.end_utterance(utterance_id)


def _word(handler, text: str, request_id: str = "r") -> bool:
    _command(handler, "intelligent_insert_text")
    return handler.intelligent_insert_text(text, request_id=request_id)


def _contexts_used(strategy) -> list[UIContext]:
    """The context each insert of one strategy received."""
    return [c.args[1] for c in strategy.insert.call_args_list]


def _focus_lines(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("FOCUS_READ")
    ]


# --- Item 2: one read for the words of one utterance ------------------------


class TestOneReadForEachUtterance:

    def test_three_words_in_one_window_make_one_focus_read(
        self, handler, desktop,
    ):
        _start(handler)
        for index, word in enumerate(("one", "two", "three")):
            assert _word(handler, word, request_id=f"r{index}") is True

        assert len(desktop.reads) == 1, (
            f"capture_context called {len(desktop.reads)} times for an "
            f"utterance of 3 words, expected 1"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert len(used) == 3
        assert all(context is desktop.reads[0] for context in used)


# --- Item 3: a new read on a window change ----------------------------------


class TestANewReadOnAWindowChange:

    def test_another_foreground_window_discards_the_stored_read(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "one")
        desktop.foreground = _WINDOW_B
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2, (
            "expected a read for word 1 and a new read for word 2 after "
            f"the window change, got {len(desktop.reads)} reads"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert len(used) == 3, "a word was dropped because of a stored read"
        assert used[0].target_identity.root == _WINDOW_A
        assert used[1].target_identity.root == _WINDOW_B
        assert used[2] is used[1]

    def test_a_window_that_is_no_longer_current_discards_the_stored_read(
        self, handler, desktop,
    ):
        """Same root number, but the identity check fails (a recycled handle)."""
        _start(handler)
        _word(handler, "one")
        with patch.object(TargetIdentity, "is_current",
                          lambda identity, **kw: False):
            _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2, (
            "expected word 1 and a new read for word 2; word 3 uses the "
            f"read of word 2; got {len(desktop.reads)} reads"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[2] is used[1] is desktop.reads[1]

    def test_the_foreground_root_check_stands_beside_is_current(
        self, handler, desktop,
    ):
        """is_current also admits the target's own root as the foreground.

        TargetIdentity.is_current accepts a foreground that is either
        foreground_root or root, so it alone does not prove that the
        foreground is the window of the stored read. The handler's own
        current_foreground_root compare must refuse the stored read here.
        """
        _start(handler)
        _word(handler, "one")
        desktop.foreground = _WINDOW_B
        with patch.object(TargetIdentity, "is_current",
                          lambda identity, **kw: True):
            _word(handler, "two")

        assert len(desktop.reads) == 2, (
            "the stored read was used while another window held the "
            "foreground"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1].target_identity.root == _WINDOW_B

    def test_a_failing_foreground_check_reads_again(self, handler, desktop):
        """Any doubt in the Win32 check answers False: the word reads again."""
        _start(handler)
        _word(handler, "one")
        with patch(f"{_MOD}.current_foreground_root",
                   side_effect=RuntimeError("probe failed"), create=True):
            delivered = _word(handler, "two")

        assert delivered is True
        assert len(desktop.reads) == 2, (
            "the stored read was used although the foreground check failed"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1] is desktop.reads[1]


# --- Item 4: a new read for each utterance ----------------------------------


class TestANewReadForEachUtterance:

    def test_two_utterances_in_sequence_make_two_reads(self, handler, desktop):
        _start(handler, 1)
        _word(handler, "one")
        _word(handler, "two")
        _end(handler, 1)
        _start(handler, 2)
        _word(handler, "three")
        _word(handler, "four")
        _end(handler, 2)

        assert len(desktop.reads) == 2, (
            f"expected one read for each utterance, got {len(desktop.reads)}"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[0] is used[1] is desktop.reads[0]
        assert used[2] is used[3] is desktop.reads[1]

    def test_a_word_after_the_next_start_utterance_gets_a_new_read(
        self, handler, desktop,
    ):
        """C2: no end_utterance between the two; the start alone discards."""
        _start(handler, 1)
        _word(handler, "one")
        _start(handler, 2)
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2

    def test_the_final_letter_flush_uses_the_stored_read_then_end_discards(
        self, handler, desktop,
    ):
        """D2 runs after the end_utterance flush, so the flush uses the read."""
        _start(handler, 1)
        _word(handler, "hello")
        _word(handler, "a")          # buffered single letter
        _end(handler, 1)

        assert len(desktop.reads) == 1, (
            "the end_utterance letter flush made its own focus read"
        )
        assert handler._utterance_focus_read is None


# --- Item 5: a failed or empty read is not stored ---------------------------


class TestAFailedReadIsNotStored:

    def test_a_failed_read_is_read_again_on_the_next_word(
        self, handler, desktop,
    ):
        desktop.queued.append(
            _context(focused_control=None, focus_read_failed=True,
                     read_error=RuntimeError("stalled")),
        )
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2, (
            "expected the failed read, a new read for word 2, and the "
            f"stored read for word 3; got {len(desktop.reads)} reads"
        )

    def test_an_empty_identity_is_read_again_on_the_next_word(
        self, handler, desktop,
    ):
        desktop.queued.append(_context(target_identity=TargetIdentity()))
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2

    def test_a_flutter_read_is_not_stored(self, handler, desktop):
        """BUILD DECISIONS: the router skips the text check for Flutter."""
        desktop.queued.extend([
            _context(is_flutter=True), _context(is_flutter=True),
        ])
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")

        assert len(desktop.reads) == 2
        assert handler._utterance_focus_read is None


# --- Item 6: the retry pass makes a new read that replaces the stored one ---


class TestTheRetryPassReadsAgain:

    def test_the_retry_read_replaces_the_stored_read(self, handler, desktop):
        refusal = InsertionResult(
            success=False, clipboard_dirty=False,
            target_window_gone=True, delivered_nothing=True,
        )
        handler.unicode_first_strategy.insert.side_effect = [
            _DELIVERED, refusal, _DELIVERED, _DELIVERED,
        ]
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")       # stored read refused, retry reads again
        _word(handler, "three")

        assert len(desktop.reads) == 2, (
            "expected the first-word read and the retry read only; got "
            f"{len(desktop.reads)}"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1] is desktop.reads[0]
        assert used[2] is desktop.reads[1]
        assert used[3] is desktop.reads[1], (
            "the word after the retry did not use the retry's read"
        )

    def test_a_failed_retry_read_discards_the_stored_read(
        self, handler, desktop,
    ):
        refusal = InsertionResult(
            success=False, clipboard_dirty=False,
            target_window_gone=True, delivered_nothing=True,
        )
        handler.unicode_first_strategy.insert.side_effect = [
            _DELIVERED, refusal, _DELIVERED, _DELIVERED,
        ]
        desktop.queued.extend([
            _context(),
            _context(focused_control=None, focus_read_failed=True,
                     read_error=RuntimeError("stalled")),
        ])
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 3

    def test_a_flutter_retry_read_discards_the_stored_read(
        self, handler, desktop,
    ):
        """A Flutter read is not stored, and it discards the record it meets."""
        refusal = InsertionResult(
            success=False, clipboard_dirty=False,
            target_window_gone=True, delivered_nothing=True,
        )
        handler.unicode_first_strategy.insert.side_effect = [
            _DELIVERED, refusal,
        ]
        desktop.queued.extend([_context(), _context(is_flutter=True)])
        _start(handler)
        _word(handler, "one")
        assert handler._utterance_focus_read is desktop.reads[0]
        _word(handler, "two")       # stored read refused, retry reads Flutter

        assert len(desktop.reads) == 2
        handler.flutter_strategy.insert.assert_called_once()
        assert handler._utterance_focus_read is None, (
            "the stored read survived a Flutter retry read"
        )


# --- Item 7: decisions on the word itself stay per word ---------------------


class TestPerWordDecisions:

    def test_two_lengths_get_two_strategies_with_one_stored_read(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "hi")
        _word(handler, "a considerably longer phrase")
        _word(handler, "ok")

        assert len(desktop.reads) == 1
        assert handler.unicode_first_strategy.insert.call_count == 2
        assert handler.standard_strategy.insert.call_count == 1
        assert _contexts_used(handler.standard_strategy)[0] is desktop.reads[0]


# --- Item 8: the other callers keep their own reads -------------------------


class TestOtherPathsUnchanged:

    def test_press_key_action_reads_for_itself_and_discards(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")
        _command(handler, "press_key_action")
        with patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2)):
            handler.press_key_action("tab")
        _word(handler, "three")
        _word(handler, "four")

        assert len(desktop.reads) == 3, (
            "expected word 1, press_key_action's own read, and word 3; got "
            f"{len(desktop.reads)}"
        )
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[2] is desktop.reads[2]

    def test_remember_target_is_set_for_every_word_of_each_utterance(
        self, handler, desktop,
    ):
        _start(handler, 1)
        _word(handler, "one")
        _word(handler, "two")
        _end(handler, 1)
        _start(handler, 2)
        _word(handler, "three")
        _end(handler, 2)

        assert len(desktop.reads) == 2
        remembered = [
            c.args[0]
            for c in handler.window_manager.remember_target.call_args_list
        ]
        assert remembered == [
            desktop.reads[0].focused_control,
            desktop.reads[0].focused_control,
            desktop.reads[1].focused_control,
        ]

    def test_wrap_or_insert_is_not_a_dictation_command(self, handler, desktop):
        """Its own action name sets the flag False and discards (D6)."""
        _start(handler)
        _word(handler, "one")
        _command(handler, "wrap_or_insert")
        assert handler._utterance_focus_read is None
        assert handler._command_reuses_focus_read is False


# --- Item 9: a focus move inside the same window ----------------------------


class TestAFocusMoveInsideTheSameWindow:

    def test_later_words_use_the_stored_control(self, handler, desktop):
        """Accepted behavior change (David, 2026-11-14 13:02, Option 1).

        The application moves the focus to another field of the SAME
        window with no command and no listener event. The later words of
        the utterance go to the field of the stored read, as the strategy
        sets the focus on the stored control.
        """
        first_field = MagicMock(name="first_field")
        second_field = MagicMock(name="second_field")
        desktop.queued.extend([
            _context(control=first_field), _context(control=second_field),
        ])
        _start(handler)
        _word(handler, "one")
        # The focus moves to second_field here; nothing tells WheelHouse.
        _word(handler, "two")

        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1].focused_control is first_field
        assert len(desktop.reads) == 1


# --- Item 10: one log line for each insert pass -----------------------------


class TestTheFocusReadLogLine:

    def test_a_new_read_then_a_stored_read(self, handler, desktop, caplog):
        caplog.set_level(logging.INFO, logger=_LOGGER)
        _start(handler)
        _word(handler, "secretword")
        _word(handler, "another")

        lines = _focus_lines(caplog)
        assert len(lines) == 2, lines
        assert re.fullmatch(
            r"FOCUS_READ source=new cause=first_word duration_ms=\d+",
            lines[0],
        ), lines[0]
        assert lines[1] == "FOCUS_READ source=stored"
        for line in lines:
            assert "secretword" not in line
            assert "another" not in line
            assert "notepad" not in line

    def test_a_retried_word_has_two_lines(self, handler, desktop, caplog):
        refusal = InsertionResult(
            success=False, clipboard_dirty=False,
            target_window_gone=True, delivered_nothing=True,
        )
        handler.unicode_first_strategy.insert.side_effect = [
            _DELIVERED, refusal, _DELIVERED,
        ]
        _start(handler)
        _word(handler, "one")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        caplog.clear()
        _word(handler, "two")

        lines = _focus_lines(caplog)
        assert len(lines) == 2, lines
        assert lines[0] == "FOCUS_READ source=stored"
        assert re.fullmatch(
            r"FOCUS_READ source=new cause=retry duration_ms=\d+", lines[1],
        ), lines[1]

    @pytest.mark.parametrize("event, cause", [
        ("window", "window_changed"),
        ("failed", "failed_read"),
        ("command", "command"),
        ("user_input", "user_input"),
        ("empty_identity", "failed_read"),
        ("no_control", "failed_read"),
        ("flutter", "flutter"),
    ])
    def test_the_cause_of_a_new_read(
        self, handler, desktop, caplog, event, cause,
    ):
        if event == "failed":
            desktop.queued.append(
                _context(focused_control=None, focus_read_failed=True,
                         read_error=RuntimeError("stalled")),
            )
        elif event == "empty_identity":
            desktop.queued.append(_context(target_identity=TargetIdentity()))
        elif event == "no_control":
            desktop.queued.append(_context(focused_control=None))
        elif event == "flutter":
            desktop.queued.append(_context(is_flutter=True))
        _start(handler)
        _word(handler, "one")
        if event == "window":
            desktop.foreground = _WINDOW_B
        elif event == "command":
            _command(handler, "press_key_action")
        elif event == "user_input":
            handler.invalidate_buffer(source="mouse:left")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        caplog.clear()
        _word(handler, "two")

        lines = _focus_lines(caplog)
        assert len(lines) == 1, lines
        assert re.fullmatch(
            rf"FOCUS_READ source=new cause={cause} duration_ms=\d+", lines[0],
        ), lines[0]

    def test_a_direct_call_is_not_dictation(self, handler, desktop, caplog):
        """No note_input_command call: the base behavior, one read each."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        handler._execute_insert_with_ack("one", "r1")
        handler._execute_insert_with_ack("two", "r2")

        assert len(desktop.reads) == 2
        lines = _focus_lines(caplog)
        assert len(lines) == 2
        assert all(
            re.fullmatch(
                r"FOCUS_READ source=new cause=not_dictation duration_ms=\d+",
                line,
            ) for line in lines
        ), lines

    def test_outside_an_utterance_every_word_reads(
        self, handler, desktop, caplog,
    ):
        handler.utterance_manager.is_in_utterance.return_value = False
        caplog.set_level(logging.INFO, logger=_LOGGER)
        _word(handler, "one")
        _word(handler, "two")

        assert len(desktop.reads) == 2
        lines = _focus_lines(caplog)
        assert len(lines) == 2, lines
        assert all("cause=no_utterance" in line for line in lines), lines
        assert handler._utterance_focus_read is None


# --- C1: the stored record is read once into a local ------------------------


class TestTheStoredRecordIsReadOnce:

    def test_a_discard_during_the_check_does_not_break_the_word(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "one")
        stored = handler._utterance_focus_read
        assert stored is desktop.reads[0]

        def discard_then_answer():
            # A listener thread discards the record in the middle of the
            # check (D7 between the check and the use).
            handler._utterance_focus_read = None
            return desktop.foreground

        with patch(f"{_MOD}.current_foreground_root",
                   side_effect=discard_then_answer, create=True):
            delivered = _word(handler, "two")

        assert delivered is True
        handler.response.send_error.assert_not_called()
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1] is stored
        assert len(desktop.reads) == 1


# --- C4: a dead stored control ----------------------------------------------


class TestADeadStoredControl:

    def test_one_new_read_and_a_second_evaluation(
        self, handler, desktop, caplog,
    ):
        dead = MagicMock(name="dead_control")
        alive = MagicMock(name="alive_control")
        desktop.queued.extend([_context(control=dead), _context(control=alive)])

        def evaluate(control, **_kwargs):
            if control is dead and dead.gone:
                return TextTargetVerdict(verdict=False, reason="stale_com")
            return TextTargetVerdict(verdict=True, reason="edit_control")

        dead.gone = False
        handler.router.text_target = MagicMock()
        handler.router.text_target.evaluate.side_effect = evaluate
        router_calls = MagicMock(wraps=handler.router.get_strategy)
        handler.router.get_strategy = router_calls

        _start(handler)
        _word(handler, "one")
        dead.gone = True
        caplog.set_level(logging.INFO, logger=_LOGGER)
        caplog.clear()
        router_calls.reset_mock()
        _word(handler, "two")

        assert router_calls.call_count == 2, (
            "the word was not evaluated again after the stored control "
            "was rejected"
        )
        handler.clipboard_only_strategy.insert.assert_not_called()
        used = _contexts_used(handler.unicode_first_strategy)
        assert used[1].focused_control is alive
        assert len(desktop.reads) == 2
        lines = _focus_lines(caplog)
        assert len(lines) == 1, lines
        assert re.fullmatch(
            r"FOCUS_READ source=new cause=stored_control_rejected "
            r"duration_ms=\d+",
            lines[0],
        ), lines[0]

        _word(handler, "three")
        assert len(desktop.reads) == 2, "the new read was not stored"
        assert _contexts_used(handler.unicode_first_strategy)[2].focused_control is alive

    def test_a_new_read_routed_to_clipboard_only_is_not_read_again(
        self, handler, desktop,
    ):
        """C4 acts on a STORED read only.

        A new read whose control the router sends to ClipboardOnlyStrategy
        is the base behavior: the word goes to that strategy with one read.
        """
        dead = MagicMock(name="dead_control")
        desktop.queued.append(_context(control=dead))
        handler.router.text_target = MagicMock()
        handler.router.text_target.evaluate.return_value = TextTargetVerdict(
            verdict=False, reason="stale_com",
        )

        _start(handler)
        _word(handler, "one")

        assert len(desktop.reads) == 1, (
            "a new read routed to ClipboardOnlyStrategy was read again"
        )
        handler.clipboard_only_strategy.insert.assert_called_once()


# --- C5: the discard events D6 and D7, and WheelHouse's own typing -----------


class TestDiscardEvents:

    def test_another_command_makes_the_next_word_read_again(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "one")
        _command(handler, "hotkey_action")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2

    def test_a_click_or_key_makes_the_next_word_read_again(
        self, handler, desktop,
    ):
        _start(handler)
        _word(handler, "one")
        handler.invalidate_buffer(source="keyboard:tab")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 2

    def test_wheelhouse_own_typing_does_not_discard(self, handler, desktop):
        """The listener path ignores keys sent while is_internal_action is set.

        Each insert replays what on_user_press does for a key that
        WheelHouse itself sends: the command loop sets is_internal_action
        around the handler, so the listener asks
        _should_emit_keyboard_invalidation with True and does not call
        invalidate_buffer.
        """
        import input_proc

        def type_own_keys(*_args, **_kwargs):
            for key in ("h", "space", "enter"):
                if input_proc._should_emit_keyboard_invalidation(key, True):
                    handler.invalidate_buffer(source=f"keyboard:{key}")
            return _DELIVERED

        handler.unicode_first_strategy.insert.side_effect = type_own_keys
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")
        _word(handler, "three")

        assert len(desktop.reads) == 1
        assert handler._utterance_focus_read is desktop.reads[0]


# --- The command loop tells the handler about every command -----------------


class TestTheCommandLoopNotesEveryCommand:

    @pytest.fixture
    def loop(self):
        from tests.test_input_proc_stale_command_expiry import (
            _FakeHandler, _Harness,
        )

        class _NotingHandler(_FakeHandler):
            def __init__(self):
                super().__init__()
                self.noted = []
                self.fail_next_note = False

            def note_input_command(self, action):
                with self._lock:
                    self.noted.append(action)
                if self.fail_next_note:
                    self.fail_next_note = False
                    raise RuntimeError("note failed")

            def retract(self):
                self._record("retract")
                return {"status": "not_retracted", "reason": "test"}

            def intelligent_insert_text(
                self, insertion_string, request_id=None, **kwargs,
            ):
                # Guard (e): what the loop's flag says while the word runs,
                # and what the real key listener does with a key that
                # WheelHouse sends during the word.
                self.internal_during_insert.append(self.internal_flag.is_set())
                from pynput.keyboard import KeyCode
                self.on_press(KeyCode.from_char("h"))
                self._record("intelligent_insert_text")
                return True

            def invalidate_buffer(self, **kwargs):
                with self._lock:
                    self.invalidations.append(kwargs.get("source"))

        harness = _Harness()
        harness.handler = _NotingHandler()
        harness.handler.internal_during_insert = []
        harness.handler.invalidations = []
        harness.start()
        # The loop's is_internal_action Event is a local of
        # input_process_main. The key listener it builds is a closure over
        # that Event, so the Event is read from the closure of the
        # on_press callback the loop handed to the (replaced) Listener.
        import input_proc
        on_press = input_proc.keyboard.Listener.call_args.kwargs["on_press"]
        cells = dict(zip(
            on_press.__code__.co_freevars,
            (cell.cell_contents for cell in on_press.__closure__),
        ))
        harness.handler.internal_flag = cells["is_internal_action"]
        harness.handler.on_press = on_press
        try:
            yield harness
        finally:
            harness.stop()

    def test_every_dispatched_command_is_noted_before_it_runs(self, loop):
        from tests.test_input_proc_stale_command_expiry import _wait_until
        import time

        far = time.monotonic() + 3600.0
        loop.send("start_utterance", deadline=far)
        loop.send("retract", deadline=far)
        loop.send("end_utterance", deadline=far)

        assert _wait_until(lambda: loop.handler.ran("end_utterance"), 10.0)
        assert loop.handler.noted == [
            "start_utterance", "retract", "end_utterance",
        ]

    def test_a_failing_note_does_not_end_the_loop(self, loop):
        from tests.test_input_proc_stale_command_expiry import _wait_until
        import time

        far = time.monotonic() + 3600.0
        loop.handler.fail_next_note = True
        loop.send("start_utterance", deadline=far)
        loop.send("end_utterance", deadline=far)

        assert _wait_until(lambda: loop.handler.ran("end_utterance"), 10.0)
        assert loop.handler.noted == ["start_utterance", "end_utterance"]
        assert loop.handler.ran("start_utterance")
        assert loop.loop_alive()

    def test_a_failing_note_is_logged_at_warning_not_error(self, loop, caplog):
        """Guard (b): an ERROR record is a Windows notice
        (ErrorNotificationHandler sits on the root logger)."""
        from tests.test_input_proc_stale_command_expiry import _wait_until
        import time

        caplog.set_level(logging.DEBUG, logger="input_proc")
        far = time.monotonic() + 3600.0
        loop.handler.fail_next_note = True
        loop.send("start_utterance", deadline=far)
        loop.send("end_utterance", deadline=far)

        assert _wait_until(lambda: loop.handler.ran("end_utterance"), 10.0)
        records = [
            r for r in caplog.records
            if r.getMessage().startswith("note_input_command failed")
        ]
        assert len(records) == 1, [r.getMessage() for r in records]
        assert records[0].levelno == logging.WARNING, records[0].levelname

    def test_the_loop_marks_a_dictated_word_as_internal_action(self, loop):
        """Guard (e): WheelHouse's own keys during a word must not discard.

        The listener ignores a key only while is_internal_action is set,
        so the generic dispatch must set it around intelligent_insert_text.
        """
        from tests.test_input_proc_stale_command_expiry import _wait_until
        import time

        far = time.monotonic() + 3600.0
        loop.send(
            "intelligent_insert_text", deadline=far, request_id="word-1",
            params={"insertion_string": "hello"},
        )

        assert _wait_until(
            lambda: loop.handler.ran("intelligent_insert_text"), 10.0,
        )
        assert loop.handler.internal_during_insert == [True], (
            "is_internal_action was not set while the loop ran the word"
        )
        assert loop.handler.invalidations == [], (
            "a key sent during the word reached invalidate_buffer and "
            "would discard the stored focus read"
        )


# --- Guards found by the read-only verifier ---------------------------------


class TestGuards:

    def test_the_focus_read_line_is_info(self, handler, desktop, caplog):
        """Guard (a): an ERROR record is a Windows notice."""
        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        _start(handler)
        _word(handler, "one")
        _word(handler, "two")

        records = [
            r for r in caplog.records
            if r.getMessage().startswith("FOCUS_READ")
        ]
        assert len(records) == 2
        assert [r.levelno for r in records] == [logging.INFO, logging.INFO]

    @pytest.mark.parametrize("action", [
        None, "wrap_or_insert", "verbatim_insert_text",
    ])
    def test_an_insert_that_is_not_dictation_stores_nothing(
        self, handler, desktop, action,
    ):
        """Guard (c): only a dictation command stores a read.

        Inside an utterance, with a storable read, a direct call (no
        note_input_command) and the inserts of wrap_or_insert and
        verbatim_insert_text keep the base behavior: nothing is stored.
        """
        _start(handler)
        if action is not None:
            _command(handler, action)
        handler._execute_insert_with_ack(
            "one", "r1", action_name=action or "intelligent_insert_text",
        )
        handler._execute_insert_with_ack(
            "two", "r2", action_name=action or "intelligent_insert_text",
        )

        assert handler._utterance_focus_read is None
        assert len(desktop.reads) == 2

    def test_a_reject_of_the_stored_read_is_not_evaluated_again(
        self, handler, desktop,
    ):
        """Guard (d): C4 acts on ClipboardOnlyStrategy only.

        The elevated refusal returns RejectedInsertionStrategy and parks a
        pending answer on it, so a second evaluation there could hand that
        answer to a later insert. One read, one router call, and the stored
        read stays.
        """
        elevated = {"now": False}
        handler.router.elevation_checker = (
            lambda control: "elevated" if elevated["now"] else "not_elevated"
        )
        handler.rejected_strategy.insert = MagicMock(
            return_value=InsertionResult(
                success=True, clipboard_dirty=False,
                rejected_reason="elevated_process_window",
            ),
        )
        router_calls = MagicMock(wraps=handler.router.get_strategy)
        handler.router.get_strategy = router_calls

        _start(handler)
        _word(handler, "one")
        stored = handler._utterance_focus_read
        assert stored is desktop.reads[0]
        elevated["now"] = True
        router_calls.reset_mock()
        _word(handler, "two")

        assert router_calls.call_count == 1, (
            "the word was evaluated again after a RejectedInsertionStrategy "
            "answer"
        )
        assert len(desktop.reads) == 1
        handler.rejected_strategy.insert.assert_called_once()
        assert handler._utterance_focus_read is stored
