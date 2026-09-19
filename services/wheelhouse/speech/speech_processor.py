"""Speech processor with truth table-based decision logic for word processing.

GLOSSARY:
---------
WordEvent: Object containing a word and utterance boundary flags (start/end)
Utterance: A continuous phrase of speech, bounded by silence, from STT (speech-to-text)
Fresh: First word of an utterance (start_of_utterance=True)
Mid-utterance: Words after the first word (start_of_utterance=False)
Catalog: Dictionary of known pattern first-words for O(1) lookup
PatternType: Classification of a word (COMMAND, REPLACEMENT, or NONE)
Command: Action pattern like "delete line", "press enter"
Replacement: Text substitution handling homophones (e.g., "taylor/tailor" → "Taylor")
Hotword: Safety prefix word (e.g., "x-ray") required for commands with requires_hotword=True
Passthrough: Send word immediately to application (0ms latency)
Buffering: Hold words temporarily to detect multi-word patterns
IDLE: Processing mode where words are evaluated individually
Processing Mode: State machine state (IDLE, COMMAND_BUFFERING, REPLACEMENT_BUFFERING, HOTWORD_BUFFERING)

OVERVIEW:
---------
This module implements the main processing loop that consumes WordEvent objects
from a queue and routes them through a truth table based on:
- Utterance position (fresh vs mid-utterance)
- Pattern catalog membership (in catalog vs not)
- Pattern type (COMMAND vs REPLACEMENT)
- Current processing mode (IDLE vs BUFFERING)

The processor implements 6 distinct cases when in IDLE mode:
- FRESH_PASSTHROUGH: Non-catalog words at utterance start → send immediately
- FRESH_COMMAND: Command words at utterance start → buffer (1000ms timeout)
- FRESH_REPLACEMENT: Replacement words at utterance start → buffer (400ms timeout)
- MID_PASSTHROUGH: Non-catalog words mid-utterance → send immediately
- MID_COMMAND_PASSTHROUGH: Command words mid-utterance → send immediately (treat as dictation)
- MID_REPLACEMENT_BUFFER: Replacement words mid-utterance → buffer (400ms timeout)

KEY INSIGHT - Why PatternType Matters:
--------------------------------------
Pattern type discrimination is essential for mid-utterance processing:
- Commands mid-utterance are dictation text, not actions
- Replacements mid-utterance must buffer for pattern matching

Example 1: "I want to delete text"
  → "delete" is COMMAND type
  → Mid-utterance position
  → MID_COMMAND_PASSTHROUGH case
  → Send as dictation text (user wants to type the word "delete")

Example 2: "my name is mary smith"
  → "mary" is REPLACEMENT type (first word of replacement pattern)
  → Mid-utterance position
  → MID_REPLACEMENT_BUFFER case
  → Buffer for pattern matching (user wants text replacement)

HOTWORD HANDLING:
-----------------
Hotword support for commands with requires_hotword=True provides a safety gate.

The hotword must precede commands that require it:
- "x-ray close window" → Execute "close window" (requires_hotword=True, hotword unlocks)
- "close window" alone → Passthrough as dictation (requires_hotword=True, no hotword)
- "delete five" → Execute (requires_hotword=False, no hotword needed)
- "x-ray delete five" → Also execute (hotword works with any command)

Mid-utterance hotword is treated as normal text:
- "I said x-ray" → Passthrough "x-ray" as dictation (not special mid-utterance)

Edge cases:
- "x-ray" alone (utterance ends) → Passthrough as dictation
- "x-ray hello world" (not a command) → Passthrough as dictation
- "x-ray mary smith" (replacement, not command) → Passthrough as dictation

:flow: Speech Processing
:description: Main word processing loop with truth table-based state machine. Steps 2-3 of
the Speech Processing flow that dequeues and evaluates WordEvents from step 1 (STT intake).
:consumes_from: Speech Processing
:produces_for: Command and Dictation Routing
"""
import asyncio
import time
import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from utils.redact import redact_transcript

from .word_event import WordEvent
from .pattern_catalog import (
    PatternCatalog,
    PatternType,
    _normalize_lookup_word,
)
from services.wheelhouse.shared.grapheme import (
    count_grapheme_clusters,
    normalize_line_endings,
)
from .domain import ProcessingMode, Action, Decision
from .number_word_parser import parse_number_word
from .click_parser import _NUMBER_FILLERS, _TRAILING_PUNCT
from .router import SpeechRouter, _word_matches_hotword
from utils.trace_context import set_trace, elapsed_ms

logger = logging.getLogger(__name__)
pipeline_logger = logging.getLogger("wheelhouse.pipeline")

# wh-overlay-slow-uia-stale-badges.7: the longest a screen read may
# refuse dictation. The read-in-flight marker
# (LogicController.screen_read_in_flight_since) is cleared in a finally
# around the Input round trip, but a destroyed Logic-side awaiter must
# not leave voice refused forever, so the gate ignores a marker older
# than this cap.
#
# wh-overlay-slow-uia-stale-badges.3 made the cap FOLLOW the configured
# read limit instead of standing on its own. The read now has its own
# key, [click] screen_read_timeout_ms, which an operator may raise to
# 60 s; a fixed 10 s cap would then stop refusing dictation while the
# single Input command loop was still held by the read, and the late-typed
# word this gate exists to prevent would come back. The effective cap is
# max(this floor, the configured limit in seconds + the slack below).
# This constant stays as the floor and as the fallback when no config is
# readable.
_READ_GATE_MAX_REFUSAL_S = 10.0

# Added to the configured read limit when deriving the cap above. Covers
# the IPC send and the reply hop that sit outside the Input-side read
# bound, so a read that uses its whole window is still refusing dictation
# when its answer arrives.
_READ_GATE_REFUSAL_SLACK_S = 2.0


def _build_default_focused_hwnd_provider():
    """Return a zero-arg callable that reports the current foreground HWND.

    Lazy-imports ``win32gui`` so headless test contexts that import this
    module without Win32 available do not crash. Returns a callable that
    always reports HWND 0 when ``win32gui`` cannot be imported; the
    focus-redirect policy treats HWND 0 as ``cannot_resolve_focused_process``
    so the dictation falls through to the standard path.
    """
    try:
        import win32gui
    except ImportError:
        return lambda: 0
    return win32gui.GetForegroundWindow


# ============================================================================
# THE HELD TAIL (wh-whole-utterance-command-matching, Stage 2)
# ============================================================================
#
# One utterance can end with words held back from dictation while the
# processor waits to learn what the utterance's end proves about them.
# Three narrow holds grew up side by side (a replacement prefix awaiting
# its completing word, a trailing-position command candidate, a bare
# number that may be a badge click), each with its own attribute and its
# own flush obligations -- and every NEW event type had to remember to
# flush all of them, or revive the leak bug class. Stage 2 keeps the
# three kinds and their exact behaviour, but stores whichever one is
# active in ONE slot, so an event type added later has ONE flush call
# (_flush_held_tail_as_dictation) and ONE advance point
# (_advance_held_tail). The kinds are mutually exclusive in every live
# sequence: a trailing candidate flushes on any following non-marker
# event, a bare number arms only on the utterance-opening word, and the
# replacement-prefix pass runs (and on failure flushes) at the top of
# the loop before anything later could arm.

class _HeldTailKind(enum.Enum):
    """Which hold the single tail slot is carrying."""

    REPLACEMENT_PREFIX = "replacement_prefix"
    TRAILING_COMMAND = "trailing_command"
    BARE_NUMBER = "bare_number"


@dataclass
class _HeldTail:
    """The words held back at the tail of the current utterance."""

    kind: _HeldTailKind
    words: list[str] = field(default_factory=list)


# ============================================================================
# CANCEL-ONLY LANE (wh-cancel-fix-running-rewrite)
# ============================================================================

# The action a pattern must name for the cancel-only lane to treat it as the
# command that stops a running AI call. The trigger WORDS stay in
# speech/config/patterns.toml; only the action name lives here, because the
# lane has to call something and a pattern file cannot name a Python object.
_CANCEL_ACTION_NAME = "cancel_fix"


def _cancel_command_patterns(catalog) -> list[dict]:
    """Every catalog pattern whose actions name the cancel action.

    Read from the catalog on each open of the lane rather than cached, so a
    pattern reload -- a user editing user_patterns.toml, a catalog rebuild --
    cannot leave the lane matching a trigger the file no longer has.
    """
    found: list[dict] = []
    for pattern in catalog.get_all_patterns():
        steps = pattern.get("actions") or []
        for step in steps:
            if isinstance(step, dict) and step.get("function") == _CANCEL_ACTION_NAME:
                found.append(pattern)
                break
    return found


class _CancelCommandRecognizer:
    """Decides when the deferred words have just spelled the cancel command.

    This is deliberately NOT the truth table. The lane defers every event and
    acts on exactly one thing, so it needs to recognise one thing: the hotword
    followed by words that match a cancel pattern. Everything else -- greedy
    timers, replacement prefixes, remainders, trailing words -- is left to the
    real routing, which sees these same events again when the lane closes and
    _processing_loop replays them.

    What it recognises is held to what the shipped routing would recognise,
    because every difference is either a cancel the user gets no answer to or
    a cancel the user never asked for. Three parts of that, each measured by
    codex round 2 (findings .1.2, .1.3, .1.4):

      * The hotword arms only at ``start_of_utterance``, which is the same
        gate the router applies (``SpeechRouter.decide`` arms a fresh hotword
        only in IDLE at the start of an utterance). That gate is what makes
        the shipped text escapes -- "type X", "dictate X", "literal X" --
        still escape while an AI call runs.
      * Matching goes through ``PatternMatcher.match_single_pattern``, the
        engine's own single-pattern path, so the trailing- and interior-
        punctuation retry applies here exactly as it does to a command. A
        Google transcript with automatic punctuation on says "fix." and the
        engine matches it; the lane now does too.
      * A retraction marker carries the corrected final transcript in
        ``retraction_full_text`` and the corrected WORDS never arrive as
        events at all (``WebSocketManager._handle_mode3_retract``), so the
        marker's text is read as one whole utterance rather than discarded.

    The hotword test is ``router._word_matches_hotword``, the same function the
    router uses, so a fused "xray" counts here exactly as it counts there.
    """

    def __init__(self, patterns: list[dict], matcher):
        self._patterns = patterns
        # The engine's own matcher, taken from the TextParser rather than
        # built again, so the lane cannot drift from the routing it stands in
        # for. match_single_pattern only reads: it compiles nothing, stores
        # nothing, and is safe to call from inside a word-event turn.
        self._matcher = matcher
        self._words: list[str] = []
        self._hotword_seen = False

    def reset(self) -> None:
        self._words.clear()
        self._hotword_seen = False

    def _is_a_cancel(self, words: list[str]) -> bool:
        """True when these words, after the hotword, are a cancel command.

        ``authorized_command=True`` is the caller's statement that the hotword
        gate has already been passed, which is what this class checks before
        it collects a single word. Without it the shipped cancel pattern --
        ``requires_hotword = true`` -- would be refused here, as it is refused
        for a remainder that never had a hotword.
        """
        if not words:
            return False
        text = " ".join(words)
        return any(
            self._matcher.match_single_pattern(
                text, pattern, authorized_command=True
            ) is not None
            for pattern in self._patterns
        )

    def _corrected_final_is_a_cancel(self, text: str, *, hotword: str) -> bool:
        """True when a corrected final transcript is the cancel command.

        The correction replaces the whole utterance, so it is read as one:
        the first word must be the hotword, and the rest must be the command.
        That is what the replay of the same text would decide, because
        ``_handle_retraction`` replays it with ``start_of_utterance`` on the
        first word.

        crewcut: the replay clears ``start_of_utterance`` on the first word
        when earlier held words were restored in front of the correction, and
        this check cannot see that -- the restore happens after the retract
        round trip, which has not started while the lane is open. So a cancel
        phrase opening a correction that CONTINUES an earlier sentence is
        honoured here and would not be by the replay. Removing the limit
        means giving the lane the hold state the restore reads
        (``_restore_earlier_utterance_words``) rather than the marker alone.
        """
        words = text.split()
        if not words:
            return False
        if not _word_matches_hotword(words[0].strip().lower(), hotword):
            return False
        return self._is_a_cancel([word.strip().lower() for word in words[1:]])

    def observe(self, word_event: WordEvent, *, hotword: str) -> bool:
        """True when this event completed the cancel command.

        Forgets what it had collected on every marker: an end marker, a
        retraction, a timeout finalization and a lifecycle reset all end the
        run of words the user was in the middle of. The retraction is the one
        marker that can still ANSWER true, because it carries the corrected
        text of the utterance it retracts.
        """
        if word_event.is_retraction_marker:
            corrected = word_event.retraction_full_text or ""
            self.reset()
            return self._corrected_final_is_a_cancel(corrected, hotword=hotword)

        if (
            word_event.is_utterance_end_marker
            or word_event.is_timeout_finalize_marker
            or word_event.is_lifecycle_reset_marker
        ):
            self.reset()
            return False

        if word_event.start_of_utterance:
            self.reset()

        word = (word_event.word or "").strip().lower()
        if not word:
            return False

        if not self._hotword_seen:
            # The hotword arms a command only at the start of an utterance,
            # which is the router's gate as well. Words before it cannot
            # begin a hotword-gated command, so they are not collected at
            # all -- and a hotword in the middle of one arms nothing, which
            # is what leaves "type x-ray cancel fix" as text.
            if word_event.start_of_utterance and _word_matches_hotword(
                word, hotword
            ):
                self._hotword_seen = True
                self._words.clear()
            return False

        self._words.append(word)
        matched = self._is_a_cancel(self._words)
        if matched or word_event.end_of_utterance:
            self.reset()
        return matched


# ============================================================================
# MAIN PROCESSOR
# ============================================================================

class SpeechProcessor:
    """Processes WordEvent objects through truth table decision logic.
    
    Bridges the WebSocket intake queue and the legacy command pipeline. Maintains
    buffering state, evaluates hotword requirements, and decides whether to emit
    dictation immediately or assemble command/replacement candidates for
    downstream parsing.

    State machine routes words based on:
    - Utterance position (fresh vs mid-utterance)
    - Pattern catalog membership
    - Pattern type (COMMAND vs REPLACEMENT)
    - Hotword detection (for commands with requires_hotword=True)
    
    Truth Table (7 cases):
    
    Fresh Utterance (start_of_utterance=True):
    
    | Case Name         | In Catalog? | Pattern Type | Action             |
    |:------------------|:------------|:-------------|:-------------------|
    | FRESH_HOTWORD     | N/A         | N/A          | Hotword buffer     |
    | FRESH_PASSTHROUGH | False       | NONE         | Passthrough (0 ms) |
    | FRESH_COMMAND     | True        | COMMAND      | Buffer (1000 ms)   |
    | FRESH_REPLACEMENT | True        | REPLACEMENT  | Buffer (400 ms)    |
    
    Mid-Utterance (start_of_utterance=False):
    
    | Case Name               | In Catalog? | Pattern Type | Action             |
    |:------------------------|:------------|:-------------|:-------------------|
    | MID_PASSTHROUGH         | False       | NONE         | Passthrough (0 ms) |
    | MID_COMMAND_PASSTHROUGH | True        | COMMAND      | Passthrough (0 ms) |
    | MID_REPLACEMENT_BUFFER  | True        | REPLACEMENT  | Buffer (400 ms)    |
    
    Pattern type discrimination is essential for mid-utterance processing:
    - Commands mid-utterance are treated as dictation text
    - Replacements mid-utterance must be buffered for pattern matching
    
    Hotword handling:
    - Hotword at utterance start → Enter HOTWORD_BUFFERING mode
    - Subsequent words buffered and matched against commands
    - Commands execute only if: requires_hotword=False OR hotword_active=True
    
    Examples:
    - "I want to delete text" → "delete" passthroughs as dictation (MID_COMMAND_PASSTHROUGH)
    - "my name is mary smith" → "mary" buffers for replacement matching (MID_REPLACEMENT_BUFFER)
    - "x-ray close window" → Hotword enables command execution (requires_hotword=True)
    - "snake case this" → Buffers "snake", "case", validates as pattern prefix, waits for "this"
    - "quotes now is the time" → Buffers "quotes", command pattern fails after "now",
      switches to REPLACEMENT_BUFFERING, continues collecting, produces "now is the time"
    - "quote" or "quotes" alone → Both catalog lookups work (optional 's' expanded in first-word extraction)
    - "undo 3" or "undo three" → Numeric parameters work (optional group with validation)
    """
    
    def __init__(
        self,
        word_queue: asyncio.Queue,
        catalog: PatternCatalog,
        text_parser,  # TextParser instance for command execution
        app,
        replacement_timeout_ms: int = 700,
        command_timeout_ms: int = 1000,
        greedy_timeout_ms: int = 5000,
        hotword: str = "x-ray",
        logic_controller=None,
        focus_redirect_policy=None,
        focused_hwnd_provider=None,
    ):
        """Initialize speech processor.

        Args:
            word_queue: Queue containing WordEvent objects to process
            catalog: Pattern catalog for O(1) first-word lookup
            text_parser: TextParser instance for command pattern matching and execution
            app: WheelHouse app instance for sending to dictation
            replacement_timeout_ms: Timeout for replacement patterns (e.g., "new paragraph")
            command_timeout_ms: Timeout for command patterns (e.g., "backspace 2")
            hotword: Command hotword for commands with requires_hotword=True
            logic_controller: Optional LogicController for routing dictation
                through the persistent hidden editor IPC
                (``insert_editor_word`` / ``retract_editor_text``). When
                None, dictation always flows through the standard
                ``intelligent_insert_text`` IPC.
            focus_redirect_policy: Optional FocusRedirectPolicy that
                decides per-utterance whether DICTATION should route to
                the persistent editor (terminal-at-prompt) or to the
                standard intelligent_insert_text path. When None, the
                policy is never consulted and dictation flows through
                the standard path unconditionally.
            focused_hwnd_provider: Optional zero-arg callable returning
                the current foreground HWND. Used to feed
                ``should_redirect``. Defaults to a Win32
                ``GetForegroundWindow`` callable when the policy is
                wired; tests can inject a deterministic provider.

        wh-g2-refactor.18: the legacy focus_redirect_path parameter was
        removed when the persistent hidden dictation editor replaced the
        focus-redirect state machine. Slice 18 of the G2 refactor re-wires
        a smaller policy-only surface so DICTATE words can route into
        the persistent editor via the editor IPC. When neither the
        policy nor the logic_controller is wired (legacy fixtures), the
        old behaviour (direct ``intelligent_insert_text``) is preserved.
        """
        self.word_queue = word_queue
        self.catalog = catalog
        self.text_parser = text_parser
        self.app = app
        self.logic_controller = logic_controller
        self.focus_redirect_policy = focus_redirect_policy
        # Default focused-HWND provider: lazy-loaded win32gui call so
        # headless test contexts that import this module without Win32
        # do not crash at construction time.
        if focused_hwnd_provider is None and focus_redirect_policy is not None:
            focused_hwnd_provider = _build_default_focused_hwnd_provider()
        self._focused_hwnd_provider = focused_hwnd_provider
        self.replacement_timeout_ms = replacement_timeout_ms
        self.command_timeout_ms = command_timeout_ms
        # wh-greedy-buffer-race: longer timer used while the current buffer
        # already matches a greedy "swallow the rest" pattern, so end-of-
        # utterance wins the race against the buffer timer when STT delivers
        # words one at a time.
        self.greedy_timeout_ms = greedy_timeout_ms
        self.hotword = hotword.lower()  # Normalize to lowercase for case-insensitive matching

        # Router
        self.router = SpeechRouter(catalog, hotword)
        
        # State machine
        self.mode = ProcessingMode.IDLE
        self.buffer: list[str] = []
        self.timeout_task: Optional[asyncio.Task] = None
        self.hotword_active = False  # Track if current buffer was triggered by hotword
        # wh-cancel-fix-running-rewrite: the cancel-only lane. While an AI
        # command awaits the model, _processing_loop is blocked inside that
        # command and cannot read word_queue, so "x-ray cancel fix" used to
        # sit in the queue until the replacement had already been pasted.
        # The lane task below is the only reader for that window: it defers
        # every event it takes into _deferred_word_events and acts on none
        # of them, EXCEPT the cancel command, which it runs at once. When
        # the lane closes, _processing_loop drains the deferred list before
        # it reads the queue again, so every other event keeps the arrival
        # order the queue would have given it.
        self._deferred_word_events: list[WordEvent] = []
        self._ai_cancel_lane_task: Optional[asyncio.Task] = None
        # True only between the start and the end of one process_word_event
        # call in _processing_loop. The lane may open only while this is
        # true: outside that window the main loop is itself reading
        # word_queue, and a second reader would take events out of order.
        self._in_word_event_turn = False
        self._pending_utterance_end: Optional[int] = None  # Deferred end_utterance until buffer finalizes
        # wh-overlay-slow-uia-stale-badges.7: set means no dictation
        # sentence is open. Cleared when a word actually reaches the
        # intelligent_insert_text send (refused and editor-routed words
        # do not open a sentence); set again at every end_utterance
        # send, at the start of a new utterance, and in stop(). The
        # overlay build dispatch waits on this event so a screen read
        # requested mid-sentence starts after the sentence ends.
        self.sentence_closed_event: asyncio.Event = asyncio.Event()
        self.sentence_closed_event.set()
        # True while EITHER marker-driven branch runs its buffer
        # finalization: the end-marker branch, and the Mode 1
        # lifecycle-reset branch, which sets it deliberately because
        # wh-whole-utterance-command-matching.3.1.6 and .3.1.8 (boss
        # rulings, option (a)) declare that marker a confirmed
        # utterance end. tests/mutation_gate_whole_utterance.py pins
        # the lifecycle half with lifecycle-marker-active-never-set.
        # The R1 trailing split reads this instead of
        # _pending_utterance_end. The standing reason is the lifecycle
        # branch named above: it sets this flag and never sets that
        # slot, whose only assignment is in the end-marker branch, so a
        # reader of the slot would miss a lifecycle finalization
        # entirely. A second reason has since gone away: the slot used
        # to survive a raise as stale state, and a stale reader would
        # have split a later TIMEOUT finalization whose utterance end
        # was never confirmed (wh-whole-utterance-command-matching
        # .3.1.3 instance 4). The per-word handler now clears it
        # (wh-pending-utterance-end-stale-slot).
        self._marker_finalization_active = False

        # Retraction support: track if any command was executed in current utterance
        self._command_executed_in_utterance: bool = False

        # wh-whole-utterance-command-matching (Stage 2): the ONE slot
        # for words held back at the tail of the current utterance. The
        # three hold kinds it can carry, each with the exact lifecycle
        # it had as a separate attribute:
        #
        # TRAILING_COMMAND (wh-2vz): a trailing-position command
        #   candidate held until we know whether it is the actual last
        #   word of the utterance. See the trailing-commands section
        #   near the bottom of this file for the lifecycle.
        # BARE_NUMBER (wh-click-number-dictation): a bare-number
        #   candidate held while the overlay is showing badges, until
        #   we know whether the number is the WHOLE utterance.
        #   Distil-Whisper drops the word "click" from short "click N"
        #   finals, so the final arrives as a bare number; a
        #   whole-utterance number with badges on screen is consumed at
        #   the utterance-end marker as the click command "click N".
        #   See the bare-number section next to the trailing-commands
        #   section for the lifecycle.
        # REPLACEMENT_PREFIX (wh-trailing-question-mark-words): a
        #   replacement buffer that finalized as plain dictation, held
        #   back so a late completing word still gets one more
        #   replacement pass. Stores the WORD LIST, not the joined
        #   text, because the second pass has to hand a buffer to the
        #   router. See the held-replacement-prefix section for the
        #   lifecycle.
        #
        # The old per-kind attribute names survive as properties over
        # this slot (reads AND writes), so kind-specific code and the
        # white-box tests keep their exact shape while the storage,
        # the top-of-loop advance (_advance_held_tail), and the flush
        # obligation for any new event type
        # (_flush_held_tail_as_dictation) are one thing, not three.
        self._held_tail: Optional[_HeldTail] = None
        # wh-click-number-dictation: the word whose flush was deferred
        # because it may extend the held number ("twenty" then "three").
        # Set at the flush guard, decided in _execute_decision: the
        # DICTATE branch extends the hold with it, any other outcome
        # flushes the hold so the held words land before that outcome.
        self._bare_number_deferred_word: Optional[str] = None
        # True while the word event being processed opened its
        # utterance; the bare-number hold requires it so a number
        # mid-utterance ("I ate 6") keeps dictating.
        self._current_word_starts_utterance: bool = False

        # wh-trailing-question-mark-words: the REPLACEMENT_PREFIX hold
        # exists because the STT server holds back the last confirmed
        # word, and when sherpa's own endpoint wins the race that word
        # can arrive more than ``replacement_timeout_ms`` after the one
        # in front of it -- long enough for the timer to finalize
        # "question" on its own and type it as text before "mark" ever
        # shows up. Stored in the single tail slot above via the
        # _pending_replacement_prefix property.
        # wh-spaced-punctuation-names-unresolved.3: True when the prefix
        # hold standing right now was armed ACROSS a confirmed utterance
        # end -- the only shape whose completing word comes from the
        # NEXT utterance, and so the only shape an utterance-end marker
        # must not close. Meaningful only while a REPLACEMENT_PREFIX
        # hold stands; every arming assigns it through
        # _arm_replacement_prefix_release, so a stale value from an
        # earlier hold can never be read.
        self._prefix_hold_crossed_utterance_end: bool = False
        # wh-spaced-punctuation-names-unresolved.3.1: True when the
        # words held right now were completed or extended by a word
        # that OPENED its own utterance. Such a join is tentative:
        # the utterance may keep going, and its later words are then
        # the proof that it is ordinary dictation. On the shipped
        # remote path that proof arrives one event after the join,
        # because no real word carries end_of_utterance. Meaningful
        # only while a REPLACEMENT_PREFIX hold stands; every arming
        # clears it in _arm_replacement_prefix_release, so a stale
        # value from an earlier hold can never be read.
        self._prefix_hold_joined_new_utterance: bool = False
        # wh-spaced-punctuation-names-unresolved.3.1.2, codex round 2.
        # A hold that survives its own utterance's end_utterance is
        # typed while the NEXT utterance's paste accounting is open,
        # and the Input side credits every insert to one running
        # counter with no utterance concept
        # (ui/clipboard_operations.py:341-348 and the three
        # reset_paste_counter callers at ui/ui_action_handler.py:1126,
        # 1170, 1417). So a correction of the next utterance retracts
        # the earlier words too. This field carries the ownership the
        # IPC does not.
        #
        # Two fields, because the boundary and the delivery answer
        # different questions and codex round 3 proved that one field
        # cannot answer both.
        #
        # WHICH words are earlier is known only at the boundary: by
        # delivery the hold may have grown with words from the new
        # utterance ("open" held across the end, then "single quote"
        # completing it in the next one -- only "open" is earlier).
        # That candidate is what _words_held_across_utterance_end
        # carries, from the end_utterance send until the hold resolves.
        self._words_held_across_utterance_end: list[str] = []
        #
        # WHETHER those words are really inside the next retractable
        # span is known only at delivery, and only then does the record
        # below get written. An earlier version of this fix wrote it at
        # the boundary and claimed "every hold that stands at a
        # boundary is delivered before any retraction can act on it".
        # That claim is false:
        # _flush_pending_replacement_prefix_as_dictation clears the
        # slot BEFORE _send_to_dictation, and _send_to_dictation
        # returns without typing while a screen read is in flight, so
        # a hold can resolve having displayed nothing at all.
        #
        # The record clears at BOTH span-ending boundaries, not just
        # the end. ui/ui_action_handler.py resets the one paste counter
        # at 1126 (start_utterance) and 1170 (end_utterance), and
        # anything delivered before either reset has left the span for
        # good. The third reset at 1417 is different and deliberately
        # does NOT clear this: it is inside a successful retract, and
        # the replay immediately refills the emptied span with the very
        # text this record names, so a chained second correction of the
        # same utterance still has to restore it.
        self._earlier_utterance_words_in_retract_span: list[str] = []
        # wh-spaced-punctuation-names-unresolved.3.1.5. WHICH span the
        # record above was delivered into. The processor never sees the
        # start_utterance command, and the two paste-counter resets it
        # has to tell apart look identical from here: a hold that
        # expires just BEFORE that command has left the span, and one
        # that expires just after it is inside the new span. The app
        # counts the command (app.py:1132) and the producer copies the
        # count onto the utterance's first word, so these two numbers
        # are what separate the two cases.
        self._earlier_utterance_words_generation: Optional[int] = None
        # wh-spaced-punctuation-names-unresolved.3.1.10. WHICH SURFACE
        # typed the record above: "editor" or "legacy". The two
        # corrections remove text from two different places -- the
        # editor retract peels the credit ledger's own runs, the legacy
        # retract undoes the Input paste -- so a correction can restore
        # these words only when it is the correction on the surface
        # that delivered them. The count above answers a narrower
        # question (was the Input span reset) and cannot answer this
        # one at all: a session or a count can match while the words
        # sit on the other screen entirely.
        self._earlier_utterance_words_surface: Optional[str] = None
        # The count read immediately before the most recent delivery
        # enqueue. Promoted into the field above when that delivery
        # turns out to have carried the boundary words.
        self._last_delivery_generation: Optional[int] = None
        # What the most recent replacement insertion step reported it
        # put on screen, or None. Set by
        # ``note_replacement_text_delivered`` and consumed by
        # ``_record_replacement_delivery``.
        self._last_replacement_surface: Optional[str] = None

        # Processing loop task
        self.processor_task: Optional[asyncio.Task] = None
        # Set true by stop(); read by _timeout_handler so a wakeup that races
        # cancellation cannot run a late dictation or command (wh-3pvsu).
        self._stopped: bool = False

        # Generation token for timeout-finalize sentinels (wh-oe7u.4).
        # Bumped by _start_timeout, _cancel_timeout, _reset_to_idle, and
        # stop(). Sentinels carry the token they were created with;
        # process_word_event ignores any sentinel whose token does not
        # match. Without this, a sentinel left in the queue by a
        # cancelled timeout could finalize a NEWER buffer.
        self.timeout_token: int = 0

        # wh-g2-refactor.18: the wh-n8bu holdback/drain machinery was
        # removed with the focus-redirect path. The persistent hidden
        # dictation editor exists at GUI startup, so there is no
        # editor-show drain to wait for and no held-back words to
        # release. Word events flow directly to the standard dispatch.

        # wh-g2-refactor.18 (slice 18.32.1): per-utterance editor-path
        # tracking. The DICTATE branch consults
        # ``focus_redirect_policy.should_redirect`` and, on a positive
        # decision, routes the word to the persistent editor via
        # ``logic_controller.insert_editor_word``. We track:
        #   * ``_current_utterance_id`` -- the last real-word utterance
        #     id seen by ``process_word_event``. Used to label the
        #     editor IPCs since ``Decision`` doesn't carry it through.
        #   * ``_used_editor_this_utterance`` -- True once any word in
        #     the current utterance has been written into the editor.
        #     Drives the retract path: if the prior writes went to the
        #     editor, retract via ``retract_editor_text``; otherwise
        #     use the legacy ``retract`` IPC.
        #   * ``_editor_chars_this_utterance`` -- running total of
        #     chars sent to the editor for the current utterance, so
        #     the retract IPC can pass the right ``chars_requested``.
        self._current_utterance_id: Optional[int] = None
        self._used_editor_this_utterance: bool = False
        self._editor_chars_this_utterance: int = 0

        # wh-whole-utterance-command-matching (Stage 1): the words of
        # the current utterance, in spoken order, appended at WordEvent
        # arrival in process_word_event -- BEFORE the router resolves
        # each word to an Action. Appending there rather than in a
        # decision branch is what keeps replacement words in the list:
        # they resolve through Action.EXECUTE, not Action.DICTATE.
        # Reset when a word opens a new utterance; a retraction replay
        # rebuilds it with the corrected words (the editor-path retract
        # mirrors them in directly, since its replay happens inline in
        # the GUI). Stage 3 (wh-whole-utterance-command-matching.3)
        # reads it at every buffer finalization so whole_utterance_only
        # patterns are judged against the utterance, not the buffer.
        self._words_this_utterance: list[str] = []
        # wh-whole-utterance-command-matching.3: the previous
        # utterance's complete list, snapshotted at the reset above.
        # The auto-finalize of a cut-short utterance's buffer runs
        # AFTER the reset, so it reads this snapshot instead of the
        # live list.
        self._previous_utterance_words: list[str] = []

        logger.info(
            f"SpeechProcessor initialized: replacement_timeout={replacement_timeout_ms}ms, "
            f"command_timeout={command_timeout_ms}ms, "
            f"greedy_timeout={greedy_timeout_ms}ms, hotword='{self.hotword}', "
            f"focus_redirect_policy={'wired' if focus_redirect_policy else 'none'}"
        )

    def apply_hotword(self, hotword: str) -> None:
        """Update the active command hotword on this processor and its router.

        The hotword is copied into two places at construction: this
        processor's ``self.hotword`` and its ``self.router.hotword``. A
        catalog reload updates only the catalog's copy, so a hotword change
        would not take effect until restart without this refresh. Called from
        SpeechHandler.apply_hotword after each PatternCatalog reload
        (wh-user-patterns-split.4).

        This runs in the same event loop as the word-processing loop, but the
        loop awaits the word queue between words, so this CAN interleave
        mid-utterance (bulletproof.5.2). That is safe: the only per-utterance
        use of the wake-word STRING is the dictation-fallback prefix, and the
        router reconstructs that from a snapshot captured when the buffer
        started, not from this live value. Every other in-flight check keys off
        the ``hotword_active`` bool, which this does not touch.
        """
        # Strip before lowercasing: the router compares an STT token to this
        # value with exact equality, so surrounding whitespace from a
        # hand-edited hotword would silently stop every hotword-gated command
        # from firing (wh-user-patterns-split.8.1).
        normalized = hotword.strip().lower()
        self.hotword = normalized
        self.router.hotword = normalized
        logger.info("Command hotword updated to '%s'", normalized)

    # ========================================================================
    # HELD-TAIL COMPATIBILITY PROPERTIES
    # (wh-whole-utterance-command-matching, Stage 2)
    #
    # Each old hold-slot attribute name maps onto the single
    # ``self._held_tail`` slot. Reads return None unless the tail
    # carries that kind. A None-assignment clears only a SAME-KIND tail:
    # the old attributes were independent, and every kind flush method
    # ends by assigning None to its own name, so a kind-blind clear
    # would let one kind's flush drop another kind's held words. Arming
    # a kind while a DIFFERENT kind is held happens in no live sequence
    # (see the held-tail section comment above _HeldTailKind), so the
    # setter logs an error before evicting rather than dropping the
    # held words silently.
    # ========================================================================

    def _tail_words_if(
        self, kind: _HeldTailKind
    ) -> Optional[list[str]]:
        """The held word LIST when the tail carries ``kind``, else None.

        Returns the live list, not a copy: the bare-number extend path
        appends to it in place.
        """
        tail = self._held_tail
        if tail is not None and tail.kind is kind:
            return tail.words
        return None

    def _set_tail(
        self, kind: _HeldTailKind, words: Optional[list[str]]
    ) -> None:
        """Arm ``kind`` with ``words``, or clear a same-kind tail on None."""
        tail = self._held_tail
        if words is None:
            if tail is not None and tail.kind is kind:
                self._held_tail = None
            return
        if tail is not None and tail.kind is not kind:
            pipeline_logger.error(
                "held tail collision: arming %s while %s holds %s -- "
                "evicting the held words. No live arming sequence does "
                "this; a new event type is missing its "
                "_flush_held_tail_as_dictation call.",
                kind.name,
                tail.kind.name,
                redact_transcript(" ".join(tail.words)),
            )
        self._held_tail = _HeldTail(kind=kind, words=words)

    @property
    def _pending_replacement_prefix(self) -> Optional[list[str]]:
        return self._tail_words_if(_HeldTailKind.REPLACEMENT_PREFIX)

    @_pending_replacement_prefix.setter
    def _pending_replacement_prefix(
        self, words: Optional[list[str]]
    ) -> None:
        self._set_tail(_HeldTailKind.REPLACEMENT_PREFIX, words)

    @property
    def _pending_trailing_word(self) -> Optional[str]:
        words = self._tail_words_if(_HeldTailKind.TRAILING_COMMAND)
        return words[0] if words else None

    @_pending_trailing_word.setter
    def _pending_trailing_word(self, word: Optional[str]) -> None:
        self._set_tail(
            _HeldTailKind.TRAILING_COMMAND,
            None if word is None else [word],
        )

    @property
    def _pending_bare_number_words(self) -> Optional[list[str]]:
        return self._tail_words_if(_HeldTailKind.BARE_NUMBER)

    @_pending_bare_number_words.setter
    def _pending_bare_number_words(
        self, words: Optional[list[str]]
    ) -> None:
        self._set_tail(_HeldTailKind.BARE_NUMBER, words)

    # ========================================================================
    # HELD-TAIL OPERATIONS (the one advance, the one flush, the one
    # end-of-utterance consume)
    # ========================================================================

    async def _advance_held_tail(self, word_event) -> bool:
        """Advance the held tail with an arriving non-marker event.

        Called from the top of process_word_event for every event
        except the utterance-end marker and the timeout sentinel, which
        have their own branches. Returns True when the tail consumed
        the event and nothing further should look at it.

        REPLACEMENT_PREFIX (wh-trailing-question-mark-words): the held
        buffer gets one more replacement pass using the arriving word.
        "question" held plus a late "mark" resolves to "?" instead of
        typing two words. This kind is the only one that can consume
        the event: the pass can use up BOTH the held words and the
        arriving word. The remaining marker events (retraction,
        lifecycle reset) carry word="" -- integrations/
        websocket_manager.py builds both that way -- so they cannot
        complete anything and flush the held words as text instead. A
        word that OPENS a new utterance also flushes rather than
        completing: the held words belong to the utterance that spoke
        them.

        TRAILING_COMMAND (wh-2vz): any event that reaches here proves
        the held trailing candidate is NOT the last word of the
        utterance. Flush it as plain dictation before the new event is
        processed. The utterance_end_marker branch consumes the
        candidate instead and must NOT flush -- the caller's
        end-marker exclusion is what keeps it away from here.
        wh-2vz.1.2 (codex round 1): the sentinel exclusion is also
        load-bearing. Queue sentinels are not spoken input; a stale or
        fresh sentinel arriving between a held candidate and the
        utterance_end_marker must not dictate the held word. The
        sentinel branch either drops it as stale (token mismatch) or
        returns early in IDLE mode (which is the mode the processor is
        in whenever a trailing candidate is held, because the DICTATE
        branch sets IDLE before holding the candidate).

        BARE_NUMBER (wh-click-number-dictation): the same rule -- any
        regular event after the held number proves it was not the
        whole utterance, so it is ordinary dictation. A retraction
        marker also flushes (typing the number first and then
        retracting is exactly what happened before the hold existed,
        so the baseline behaviour is preserved). One exception
        (multi-word): a user reading badge 23 says "twenty three", and
        parse_number_word reads that as 23, so the next word may be
        CONTINUING the number rather than proving it was not the whole
        utterance. When it can, the flush is deferred and the word
        still goes to the router -- the router stays the authority on
        whether the word is dictation or a command. _execute_decision
        then either extends the hold (its DICTATE branch) or flushes
        it (every other outcome), so a deferred word can never leave
        the hold standing on a word the router used for something
        else.

        A word that OPENS a new utterance is never a continuation, no
        matter what it parses to: the previous utterance ended, and
        its lost end marker is a real failure mode rather than a
        theoretical one. Without this test the user's "twenty" and
        their next utterance's "three" fused into a click on badge 23
        (deepseek round 1, wh-click-number-dictation.1.1). The
        replacement-prefix hold refuses the same fusion the same way,
        in _resolve_pending_replacement_prefix. The retraction replay
        is unaffected: it marks only its FIRST word as opening the
        utterance, and the retraction marker has already flushed any
        standing hold before the replay begins.

        All six producers of the start_of_utterance flag were
        enumerated when the bare-number guard was written, and none
        delivers a genuinely NEW utterance's first word with the flag
        false, so no two utterances can fuse into one wrong click. The
        in-process STT bridge used to break the other direction: it
        re-queued the FULL cumulative text of every interim and set
        the flag on word 0 each time, so a genuinely continuing word
        arrived with the flag true and this guard flushed a hold that
        was still growing -- one spoken "twenty three" over painted
        badges was typed AND clicked. The bridge now queues only the
        words an event adds (main.py _handle_stt_transcript,
        wh-inprocess-interim-first-flag), so the flag means the same
        thing on both paths. Delta counting on its own would have
        broken THIS direction after a provider switch, because every
        provider counts its own utterances from 1: the restarted
        provider's first utterance reused a spent id, the bridge read
        it as a continuation, and the new utterance reached this guard
        with no opening word at all. The bridge matches on the
        provider instance as well as the id
        (wh-inprocess-interim-first-flag.1.1), which is what keeps the
        enumeration above true.
        """
        tail = self._held_tail
        assert tail is not None
        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            return await self._resolve_pending_replacement_prefix(
                word_event
            )
        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
            return False
        # BARE_NUMBER
        if (
            not word_event.start_of_utterance
            and self._bare_number_extends(word_event.word)
        ):
            self._bare_number_deferred_word = word_event.word
        else:
            await self._flush_pending_bare_number_as_dictation()
        return False

    async def _flush_held_tail_as_dictation(self) -> None:
        """Flush the held tail, whatever its kind, as plain dictation.

        THE one flush obligation for any code path that must not leave
        words held (today: the lifecycle-reset backstop). A future
        event type that ends an utterance early calls this once
        instead of remembering three kind-specific flushes. Each kind
        keeps its exact IPC shape: the prefix flush sends one joined
        dictation, the trailing flush sends the single word, the
        bare-number flush sends one dictation per held word in spoken
        order and clears the deferred-word slot.
        """
        tail = self._held_tail
        if tail is None:
            return
        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            await self._flush_pending_replacement_prefix_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
        else:
            await self._flush_pending_bare_number_as_dictation()

    async def _consume_held_tail_at_utterance_end(
        self, *, allow_continued_hold: bool = False,
    ) -> None:
        """Consume the held tail at the utterance-end marker.

        Replaces the three sequential per-kind calls the end-marker
        branch used to make. The trailing kind fires its command action
        (the candidate really was the last word). The bare-number kind
        clicks the badge (the number really was the whole utterance).

        The prefix kind used to have no end-of-utterance action: the end
        marker proved no completing word was coming, so the held words
        flushed as dictation. wh-spaced-punctuation-names-unresolved.3
        splits that into three, because a word in the NEXT utterance can
        now complete a held name:

          * held words that already spell a complete replacement fire it
            (the ruling's "the utterance ends and the mark fires");
          * held words that are still an exact opening of a name stay
            held, but ONLY when the hold was armed across a confirmed
            utterance end AND ``allow_continued_hold`` says this caller
            can carry a hold past its own point;
          * anything else flushes as dictation, as before.

        The ``_prefix_hold_crossed_utterance_end`` half is what keeps
        ordinary text prompt. A hold the timeout sentinel armed
        mid-utterance is still waiting for a word in the SAME utterance,
        and this marker proves that word is not coming, so it flushes
        now rather than sitting out the rest of its deadline: "question"
        alone, finalized by the timer and then closed by its own end
        marker, is dictated on the marker exactly as it was before this
        stage (tests/e2e/test_e2e_utterance_end_replacement.py
        ::test_lone_question_after_the_timeout_is_still_dictated).

        ``allow_continued_hold`` is False by default, and the
        lifecycle-reset caller keeps the default deliberately: that
        branch emits an end_utterance / start_utterance pair right after
        this call, and held words must never survive it and land in
        phrase 2 (wh-trailing-question-mark-words). The end-marker branch
        passes True -- the whole point of the stage is that a hold armed
        at an utterance end survives the next one, still bounded by the
        one release deadline its arming site set.
        """
        tail = self._held_tail
        if tail is None:
            return
        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            if await self._fire_held_replacement_if_complete():
                return
            if (
                allow_continued_hold
                and self._prefix_hold_crossed_utterance_end
                and self.router.is_incomplete_replacement_name(
                    list(tail.words)
                )
            ):
                return
            await self._flush_pending_replacement_prefix_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
        else:
            await self._consume_pending_bare_number()

    async def _consume_finalization_armed_tail(
        self, drain_replacement_prefix: bool = False
    ) -> None:
        """Consume a tail the end-marker finalization itself armed.

        wh-whole-utterance-command-matching.3 (boss 19(a)
        regression-guard ruling + David's item-20 R1, 2026-08-29):
        the deferral moves all buffer finalization inside the
        end-marker branch, AFTER _consume_held_tail_at_utterance_end
        has run, so a tail armed by that finalization -- a bare
        number, or a trailing command word split off the
        dictation-bound payload -- would otherwise survive into the
        next utterance. Its end marker is THIS event, so consume it
        now.

        The prefix kind stays held by default: a replacement prefix
        waits for a completing word or its release deadline, and that
        wait across an utterance boundary is the whole of
        wh-spaced-punctuation-names-unresolved.3 -- it is what lets
        "open", said as its own utterance, finish as a name in the
        next one.

        ``drain_replacement_prefix`` is for the caller that cannot
        allow that wait. wh-spaced-punctuation-names-unresolved.3.1.4,
        codex round 2: a lifecycle reset pairs an end_utterance with a
        start_utterance for the SAME utterance id, so phrase 1 must be
        delivered before the pair whatever armed the hold. Its entry
        drain runs before this finalization and so cannot see a hold
        the finalization itself creates.

        Until .3 that could not happen, and an earlier version of this
        docstring said so: the marker-driven finalization passed no
        word_event, and the arming site in _execute_decision required
        one. .3 added ``or self._marker_finalization_active`` to that
        site -- which the utterance-end marker path needs -- and the
        lifecycle branch sets the same flag, so the site became
        reachable from both. The claim is corrected here rather than
        removed, because the reason the end-marker path may still
        retain a prefix is unchanged.
        """
        tail = self._held_tail
        if tail is None:
            return
        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            if drain_replacement_prefix:
                # Reuses the ordinary end-of-utterance consume, which
                # fires a complete name and otherwise types the held
                # words. allow_continued_hold stays at its default
                # False, so nothing survives the pair.
                await self._consume_held_tail_at_utterance_end()
            return
        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
        else:
            await self._consume_pending_bare_number()

    # ========================================================================
    # LIFECYCLE METHODS
    # ========================================================================
    
    async def start(self):
        """Start the word processing loop.

        Creates an async task that continuously processes WordEvent objects
        from the word queue until stopped.
        """
        if self.processor_task and not self.processor_task.done():
            logger.warning("SpeechProcessor already running")
            return

        self.processor_task = asyncio.create_task(self._processing_loop())
        logger.info("SpeechProcessor started")


    async def stop(self):
        """Stop the word processing loop.

        Cancels the processing task, cancels any pending timeout, and clears
        deferred utterance-end state so nothing fires after shutdown
        (wh-3pvsu, wh-oe7u.4).
        """
        # Order matters: set _stopped first so any wakeup-after-cancel sees
        # the flag, then bump timeout_token so any in-flight timeout
        # sentinel becomes stale, then cancel the timer task itself.
        self._stopped = True
        self.timeout_token += 1
        self._cancel_timeout()
        # wh-overlay-slow-uia-stale-badges.7: a sentence cannot outlive
        # the processor; leaving the event clear would park an overlay
        # build's sentence wait until its bound for no reason.
        self.sentence_closed_event.set()
        # Drop any deferred end_utterance so it cannot be flushed across
        # shutdown.
        self._pending_utterance_end = None
        # Drop the held tail, whatever its kind, on shutdown (wh-2vz,
        # wh-click-number-dictation, wh-trailing-question-mark-words).
        # Sending an IPC during stop() would race the shutdown sequence.
        # crewcut: the held words are typed nowhere -- they are
        # sacrificed so shutdown stays deterministic. To remove the
        # limit, stop() would have to await a dictation IPC during
        # teardown; that needs a shutdown ordering in which the app
        # connection is guaranteed alive until after the speech
        # processor drains, which it is not today.
        self._held_tail = None
        self._bare_number_deferred_word = None
        # wh-g2-refactor.18: the wh-n8bu holdback/drain machinery and
        # the focus-redirect-path teardown were removed with the
        # focus-redirect path. Nothing to tear down here anymore.

        if self.processor_task and not self.processor_task.done():
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                pass
            logger.info("SpeechProcessor stopped")
    
    async def _processing_loop(self):
        """Main processing loop that consumes WordEvents from queue.
        
        Continuously retrieves WordEvent objects and routes them through
        the truth table decision logic until cancelled.
        
        :flow: Speech Processing
        :step: 2
        :consumes_from: Speech Processing
        :description: Dequeues WordEvents from the intake queue and forwards them to the truth-table router in arrival order.
        :data_in: WordEvent instances produced by step 1 and buffered within `word_queue`.
        :data_out: WordEvent instances passed to `SpeechProcessor.process_word_event` for evaluation (step 3).
        :notes: Long-running background task consuming from word_queue. Runs until cancelled by
        `stop()`. Backpressure handled implicitly by awaiting `word_queue.get()`. Each WordEvent
        forwarded to truth-table evaluation in step 3.
        """
        logger.info("Processing loop started")

        try:
            while True:
                # Get next word event from queue
                # wh-cancel-fix-running-rewrite: events the cancel-only lane
                # took while an AI command awaited the model come first, in
                # the order the lane took them, so they reach the truth table
                # exactly as the queue would have delivered them.
                if self._deferred_word_events:
                    word_event = self._deferred_word_events.pop(0)
                else:
                    word_event = await self.word_queue.get()
                set_trace(word_event.trace_id or "")
                # Redact only the word; the ids/flags stay verbatim so
                # utterance correlation survives redaction (wh-797.17.3).
                logger.debug(
                    f"Processing: word='{redact_transcript(word_event.word)}' "
                    f"utt={word_event.utterance_id} "
                    f"start={word_event.start_of_utterance} "
                    f"end={word_event.end_of_utterance} "
                    f"end_marker={word_event.is_utterance_end_marker} "
                    f"retraction={word_event.is_retraction_marker} "
                    f"timeout_finalize={word_event.is_timeout_finalize_marker}"
                )

                # Process through decision tree
                # Catch errors per-word to prevent single failures from killing the loop
                try:
                    # wh-cancel-fix-running-rewrite: the cancel-only lane may
                    # open only from inside this call. The flag stays set
                    # through the nested process_word_event calls the
                    # retraction replay makes, which is what the lane needs:
                    # an AI command reached from a replayed event is still
                    # this turn.
                    self._in_word_event_turn = True
                    await self.process_word_event(word_event)
                except asyncio.CancelledError:
                    # CancelledError must propagate to allow graceful shutdown
                    raise
                except Exception as e:
                    # Log and continue - don't let one word failure kill the loop
                    logger.error(
                        f"Error processing word '{redact_transcript(word_event.word)}' "
                        f"(utterance {word_event.utterance_id}): {e}",
                        exc_info=True
                    )
                    # Reset to IDLE mode to prevent stuck state
                    if self.mode != ProcessingMode.IDLE:
                        logger.warning(f"Resetting from {self.mode} to IDLE after error")
                        self.mode = ProcessingMode.IDLE
                        self.buffer.clear()
                        self.hotword_active = False
                    # wh-click-number-dictation.1.6: the bare-number defer and
                    # the decision it waits for are ONE event. Every normal
                    # path out of _execute_decision clears the deferred slot,
                    # so it is never set at the start of the next event -- but
                    # a raise between the flush guard's defer and
                    # _execute_decision skips all of them and lands here. Left
                    # standing, the slot makes the NEXT word overwrite it (the
                    # lost word leaves no trace) and the hold then synthesises
                    # a click for a number nobody spoke: "twenty three" with a
                    # raise on "three", then "five", clicks badge 25. The other
                    # two holds do not need this. An exception leaves their
                    # words standing too, but the next event types them, and
                    # neither synthesises a command; the bare-number hold is
                    # the only one whose consume turns a lost word into an
                    # irreversible wrong click.
                    #
                    # crewcut: the held words are sacrificed rather than typed,
                    # exactly as stop() sacrifices them. Flushing them here
                    # would type what the user said, but it means sending an
                    # IPC from the handler for an error whose cause may BE the
                    # IPC path. To remove the limit, flush instead of clearing
                    # once the handler can tell a decision failure apart from a
                    # transport failure. The drop itself lives in
                    # drop_bare_number_hold, which a provider switch calls
                    # for its own reason (wh-provider-switch-stale-hold);
                    # this is the same sacrifice, so it is the same code.
                    self.drop_bare_number_hold("word-processing error")
                    # wh-pending-utterance-end-stale-slot: the THIRD
                    # slot with this defer-then-decide shape. It is set
                    # one line above the decide_timeout call in the
                    # utterance-end branch, and it has three clears: the
                    # line below, stop(), and _send_pending_utterance_end
                    # after the send returns. Until the clear below
                    # existed the other two were the only ones, so a
                    # raise here left the slot standing and the NEXT
                    # utterance sent end_utterance carrying the PREVIOUS
                    # utterance's id. The clipboard manager does refuse
                    # the mismatched id
                    # (utterance_clipboard_manager._end_utterance_locked),
                    # but ui_action_handler.end_utterance flushes the LIVE
                    # letter buffer before it calls the manager, and resets
                    # the LIVE retraction tracking after that call returns.
                    # The refusal is a return inside the manager, so neither
                    # effect is inside the id guard and the stale send
                    # landed both of them on the open utterance.
                    #
                    # crewcut: the failed utterance's own end_utterance is
                    # never sent from here. It is sacrificed rather than
                    # sent, the way the two slots above sacrifice their
                    # held words, for two reasons: sending an IPC from
                    # this handler means sending one for an error whose
                    # cause may BE the IPC path, and one of the raise
                    # sites IS _send_pending_utterance_end mid-send, where
                    # a retry can duplicate the send and repeat those same
                    # un-guarded effects. Little is stranded -- the
                    # clipboard manager's safety timeout forces the end
                    # when the signal never arrives, its start_utterance
                    # ends the previous utterance, and
                    # ui_action_handler.start_utterance resets the same
                    # three retraction-tracking fields end_utterance
                    # resets. The one real cost is that utterance's letter
                    # buffer, which now flushes at the NEXT end_utterance
                    # instead of this one. To remove the limit, send from
                    # here instead of clearing, once the handler can tell
                    # a decision failure apart from a transport failure --
                    # the same condition the bare-number crewcut above
                    # names.
                    self._pending_utterance_end = None
                    # Continue processing next word
                finally:
                    # wh-cancel-fix-running-rewrite: the turn is over, so
                    # no lane may open until the next one starts. Closing a
                    # lane the AI action left open is the backstop for an
                    # action that raised between its begin and its own end;
                    # a second reader of word_queue must never outlive the
                    # turn that opened it.
                    self._in_word_event_turn = False
                    self.end_ai_cancel_lane()

        except asyncio.CancelledError:
            logger.info("Processing loop cancelled")
            raise
    
    # ========================================================================
    # CANCEL-ONLY LANE (wh-cancel-fix-running-rewrite)
    # ========================================================================

    def begin_ai_cancel_lane(self) -> None:
        """Open the cancel-only lane for the AI call that is about to start.

        Called by the AI actions immediately before they await the model.
        Opens nothing at all in two cases, and both are deliberate:

          * outside a word-event turn. ``_processing_loop`` is then reading
            word_queue itself, and a lane would be a second reader taking
            events out of order. An AI action reached from anywhere but the
            word loop therefore behaves exactly as it did before this change.
          * a lane is already open. One AI call cannot start inside another
            (the service's processing lock is held), so a second open would
            mean a leak, not a nested call.
        """
        if not self._in_word_event_turn:
            return
        if self._ai_cancel_lane_task is not None:
            return
        self._ai_cancel_lane_task = asyncio.create_task(
            self._ai_cancel_lane_loop()
        )

    def end_ai_cancel_lane(self) -> None:
        """Close the lane. Safe to call when none is open.

        The deferred events are NOT dropped: they stay in
        ``_deferred_word_events`` and ``_processing_loop`` takes them before
        its next read of word_queue.

        Cancelling a task blocked in ``asyncio.Queue.get()`` cannot lose an
        item. ``put_nowait`` appends to the queue's own deque and then wakes a
        getter; the value never lives in the getter's future, so a getter
        cancelled after the wake leaves the item in the deque for the next
        reader.
        """
        task = self._ai_cancel_lane_task
        self._ai_cancel_lane_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _ai_cancel_lane_loop(self) -> None:
        """Defer every event while an AI call runs; act on the cancel at once.

        The loop ends by cancellation from :meth:`end_ai_cancel_lane`. An
        unexpected error ends it too, and the consequence is exactly the
        behaviour that existed before this change: the events stay in
        word_queue and are processed when the AI command returns.
        """
        recognizer = _CancelCommandRecognizer(
            _cancel_command_patterns(self.catalog),
            self.text_parser.matcher,
        )
        try:
            while True:
                word_event = await self.word_queue.get()
                self._deferred_word_events.append(word_event)
                if recognizer.observe(word_event, hotword=self.hotword):
                    pipeline_logger.info(
                        "AI-CANCEL recognized during a model call "
                        "elapsed_ms=%.1f", elapsed_ms(),
                    )
                    await self._run_cancel_action()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Cancel-only lane stopped on an error; the remaining events "
                "stay in word_queue and are processed when the AI command "
                "returns.",
                exc_info=True,
            )

    async def _run_cancel_action(self) -> None:
        """Call the cancel action the recognised pattern names.

        The action is looked up in the same registry the pattern engine uses,
        by the name in ``_CANCEL_ACTION_NAME``. It is called directly rather
        than through ``parse_and_execute`` on purpose: re-entering the parser
        from the lane would run the whole command path -- remainders,
        pattern-type bookkeeping, dictation fallback -- on a second task while
        the main loop sits inside another command.
        """
        functions = getattr(
            getattr(self.text_parser, "action_functions", None),
            "_functions", None,
        )
        action = (functions or {}).get(_CANCEL_ACTION_NAME)
        if action is None:
            logger.warning(
                "Cancel-only lane: no '%s' action is registered; the running "
                "AI call cannot be cancelled.", _CANCEL_ACTION_NAME,
            )
            return
        await action()

    # ========================================================================
    # MAIN DECISION LOGIC - TRUTH TABLE
    # ========================================================================

    async def process_word_event(self, word_event: WordEvent):
        """Process word through truth table-based state machine.

        :flow: Speech Processing
        :step: 3
        :produces_for: Command and Dictation Routing
        :description: Evaluates each WordEvent with the truth-table state machine via SpeechRouter.
        :data_in: WordEvent from step 2.
        :data_out: Decisions executed by _execute_decision.
        """
        # wh-g2-refactor.18: the wh-n8bu _current_word_event stash was
        # removed with the focus-redirect holdback. No code now consumes
        # the stash.

        # wh-whole-utterance-command-matching (Stage 1): record every
        # real word at arrival, before any hold can consume the event.
        # The held-replacement-prefix pass just below can consume a
        # completing word and return without reaching the main routing,
        # so an append placed after it would miss that word. Marker
        # events all carry word="" and are excluded. The reset runs
        # before the auto-finalize of the previous utterance's buffer
        # further down; a consumer that wants the previous utterance's
        # complete list must read it before this point.
        if word_event.word and not (
            word_event.is_utterance_end_marker
            or word_event.is_retraction_marker
            or word_event.is_timeout_finalize_marker
            or word_event.is_lifecycle_reset_marker
        ):
            if word_event.start_of_utterance:
                # wh-whole-utterance-command-matching.3: snapshot the
                # outgoing utterance's complete list before the reset.
                # This reset runs BEFORE the auto-finalize of the
                # previous utterance's buffer further down (the Stage-1
                # ordering caveat above), so the auto-finalize must
                # read the snapshot -- the live list already holds the
                # NEW utterance's first word by then.
                self._previous_utterance_words = self._words_this_utterance
                self._words_this_utterance = []
            self._words_this_utterance.append(word_event.word)

        # wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3
        # case A. WebSocketManager sends the start_utterance IPC
        # immediately before it queues this word
        # (integrations/websocket_manager.py:1292 from stable text,
        # 1527 from a final-only utterance), and that IPC resets the
        # paste counter at ui/ui_action_handler.py:1126. So by the time
        # this word arrives, anything delivered earlier -- including a
        # hold that expired in the gap after the previous
        # end_utterance -- has left the retractable span. The processor
        # never sees that IPC, so this word is its signal for the same
        # boundary.
        #
        # This sits ABOVE the advance below, not in the per-utterance
        # reset block further down, and the order is load-bearing: the
        # advance can flush a standing hold, and that delivery lands
        # AFTER the reset, inside the new span. Clearing afterwards
        # would erase the record the flush had just earned.
        #
        # The candidate is deliberately left alone. A hold still
        # standing here crosses into this new utterance's span and is
        # recorded when it is delivered.
        if word_event.start_of_utterance:
            self._clear_record_delivered_before(word_event)

        # wh-whole-utterance-command-matching (Stage 2): the ONE advance
        # point for the held tail. Any event other than the
        # utterance-end marker, the timeout sentinel, and the
        # lifecycle-reset marker (each has its own branch below)
        # advances whatever kind is held; the per-kind logic and the
        # reasoning behind the first two exclusions live in
        # _advance_held_tail. A True return means the tail consumed
        # the event and nothing further should look at it.
        # wh-whole-utterance-command-matching.3.1.4 (codex round 2):
        # the lifecycle exclusion exists so the flush a lifecycle
        # marker triggers runs INSIDE that branch's recovery guard --
        # a flush IPC failure out here skipped the end_utterance/
        # start_utterance pair. The flush itself is unchanged: the
        # branch's own _flush_held_tail_as_dictation does identical
        # work for all three hold kinds on an empty-word marker.
        if (
            self._held_tail is not None
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_lifecycle_reset_marker
        ):
            if await self._advance_held_tail(word_event):
                return

        # ====================================================================
        # CHECK FOR TIMEOUT-FINALIZE SENTINEL
        # ====================================================================
        # wh-oe7u.4: timeout finalization runs through the same word_queue
        # as normal events so the processing loop is the single writer for
        # state mutation. Stale sentinels (older generation token, stopped
        # processor, IDLE mode, or empty buffer) are no-ops.
        if word_event.is_timeout_finalize_marker:
            if self._stopped:
                logger.debug("Timeout sentinel ignored: processor stopped")
                return
            if word_event.timeout_token != self.timeout_token:
                logger.debug(
                    "Timeout sentinel ignored: stale token %d (current=%d)",
                    word_event.timeout_token, self.timeout_token,
                )
                return
            # wh-trailing-question-mark-words.2.1: the release deadline
            # the hold branch below arms. This runs BEFORE the IDLE guard
            # on purpose -- the hold resets to IDLE deliberately, so the
            # guard would otherwise discard the one sentinel that keeps
            # the held words from being lost forever. A held prefix plus a
            # matching token identifies this sentinel: nothing else can
            # arm a timer while words are held, because every word event
            # clears the slot before a new buffer could start one. A
            # release that arrives after some other path already emptied
            # the slot falls through to the IDLE guard below and is
            # discarded there.
            if self._pending_replacement_prefix is not None:
                # wh-spaced-punctuation-names-unresolved.3: the deadline
                # has two outcomes now, because the held words can be a
                # COMPLETE name by the time it fires -- a completing word
                # that did not end its utterance extends the hold instead
                # of firing (see _resolve_pending_replacement_prefix).
                # A complete name inserts its mark; an unfinished opening
                # dictates its words, exactly as before.
                if await self._fire_held_replacement_if_complete():
                    logger.info(
                        "Release deadline reached with a complete "
                        "replacement held; inserting it",
                    )
                    return
                logger.info(
                    "Release deadline reached with words still held; "
                    "dictating them",
                )
                await self._flush_pending_replacement_prefix_as_dictation()
                return
            # wh-number-badge-problems.1.6: the release deadline
            # _arm_spoken_click_release sets, for exactly the same reason
            # and with the same placement -- the hold reset to IDLE, so
            # the IDLE guard below would discard the one sentinel that
            # keeps the spoken click from vanishing. The held tail is a
            # single slot holding one kind at a time, so this release and
            # the replacement release above can never both fire.
            if self._is_spoken_click_hold(self._pending_bare_number_words):
                logger.info(
                    "Release deadline reached with a spoken click still "
                    "held; dictating it",
                )
                await self._flush_pending_bare_number_as_dictation()
                return
            if self.mode == ProcessingMode.IDLE:
                # Buffer was already finalized by other means (utterance_end,
                # new utterance auto-finalize, retraction reset). The sentinel
                # is redundant -- skip without running decide_timeout to avoid
                # double-finalization. Empty buffer in a buffering mode (e.g.
                # HOTWORD_BUFFERING with hotword alone) is NOT a skip case;
                # decide_timeout returns IGNORE for it and _execute_decision
                # resets back to IDLE, which is the correct behavior.
                logger.debug(
                    "Timeout sentinel ignored: mode=IDLE (already finalized)",
                )
                return
            logger.info("Processing timeout-finalize sentinel")
            decision = self.router.decide_timeout(
                self.buffer, self.hotword_active, mode=self.mode,
                utterance_words=list(self._words_this_utterance),
            )
            # wh-trailing-question-mark-words: a replacement buffer the
            # timer resolved to plain dictation is HELD instead of typed,
            # so a completing word that arrives after the timer expired
            # can still form the replacement. Without this the router
            # falls through to "Finalized as dictation", the DICTATE
            # branch types "question", and the held-back "mark" then
            # arrives alone in IDLE and types literally --
            # _find_earliest_replacement only runs inside
            # _process_remainder, which only the EXECUTE branch reaches.
            #
            # Hotword buffers keep the existing path: the router rebuilds
            # their DICTATE payload with the wake word in front, and
            # re-deriving the text from the raw buffer here would drop it.
            #
            # wh-okay-prefix-splits-replacement: the hotword condition moved
            # into _should_hold_replacement_prefix, which is the one gate all
            # three arming sites share. The mode condition stays here because
            # it is this site's own: only this site finalizes a buffer the
            # router was already treating as a replacement.
            #
            # wh-spaced-punctuation-names-unresolved.3: COMMAND_BUFFERING
            # joins the two replacement modes when the buffer is an exact
            # opening of a replacement name. A first word that is BOTH a
            # command prefix and a name's opening buffers as a command --
            # measured on the shipped catalog, "open" reaches this site in
            # COMMAND_BUFFERING while "question" reaches it in
            # REPLACEMENT_BUFFERING -- so without this two of the three
            # names the bead names ("open bracket", "open single quote")
            # could never reach the hold at any pause length. The extra
            # condition is the name test, not the mode alone, so an
            # ordinary command buffer that times out still dictates at
            # once.
            if (
                decision.action == Action.DICTATE
                and (
                    self.mode in (
                        ProcessingMode.REPLACEMENT_BUFFERING,
                        ProcessingMode.MID_REPLACEMENT_BUFFERING,
                    )
                    or (
                        self.mode == ProcessingMode.COMMAND_BUFFERING
                        and self.router.is_incomplete_replacement_name(
                            list(self.buffer)
                        )
                    )
                )
                and self._should_hold_replacement_prefix(
                    list(self.buffer),
                    hotword_active=self.hotword_active,
                    end_of_utterance=word_event.end_of_utterance,
                )
            ):
                held = list(self.buffer)
                self._pending_replacement_prefix = held
                pipeline_logger.info(
                    "REPLACEMENT-PREFIX held text=%r elapsed_ms=%.1f",
                    redact_transcript(" ".join(held)), elapsed_ms(),
                )
                self._reset_to_idle()
                # wh-trailing-question-mark-words.2.1: arm the release
                # deadline for the held words. Nothing else releases them
                # if the terminal FINAL message is dropped and the user
                # then stops speaking: integrations/websocket_manager.py
                # takes the utterance-end marker down with that message,
                # and its idle watchdog (_fire_idle_watchdog) only writes
                # the GUI activity state. Arm AFTER _reset_to_idle, which
                # bumps timeout_token and would otherwise make this
                # sentinel stale before it is ever dequeued.
                self._arm_replacement_prefix_release(
                    across_utterance_end=word_event.end_of_utterance,
                )
                await self._send_pending_utterance_end()
                return
            await self._execute_decision(decision)
            return

        # ====================================================================
        # CHECK FOR LIFECYCLE RESET MARKER (wh-x4fwo Mode 1)
        # ====================================================================
        # When the STT server fired a fallback final whose text disagreed with
        # the prior stable, WebSocketManager treats it as a SECOND phrase and
        # queues this marker between phrase 1 and phrase 2. The marker pairs
        # an end_utterance and a start_utterance IPC for the same utterance_id
        # so phrase 1 closes (clipboard, dictation flushes) before phrase 2
        # opens. The marker rides the same word_queue as the phrase 1 words,
        # so ordering is preserved against the async dictation pipeline.
        if word_event.is_lifecycle_reset_marker:
            logger.debug(
                f"Processing lifecycle_reset marker for utterance {word_event.utterance_id}"
            )
            # A raise during ANY of the pre-pair IPC below -- the held
            # flush, the deferred-buffer finalization, or the armed-tail
            # consume -- must not skip the end/start pair: the Input
            # process would keep phrase 1's window open and phrase 2's
            # text would land inside it (wh-whole-utterance-command-
            # matching.3.1.3 instance 2; the flush moved inside this
            # guard for .3.1.4, codex round 2). The words are lost
            # either way (the same loss the _processing_loop handler
            # accepts, per its crewcut: no retry IPC from an error
            # path); reset here so the loop handler's own reset is not
            # load-bearing. Exception, not BaseException:
            # CancelledError must keep propagating on stop().
            try:
                # The ONE dispatch for holds standing when this marker
                # arrives (the top-of-loop advance excludes this
                # marker). wh-whole-utterance-command-matching.3.1.8
                # (boss ruling, option (a)): this marker is a confirmed
                # utterance end, so a pre-held tail takes the same
                # end-of-utterance dispatch an ordinary end marker
                # gives it -- the trailing command fires, the bare
                # number clicks (each before the pair below), and the
                # replacement prefix still flushes as dictation. Hold
                # timing (before vs during the close) must not change
                # what a confirmed utterance end means. Either way the
                # slot clears, so held words can never survive the
                # end_utterance / start_utterance pair and land in
                # phrase 2 (wh-trailing-question-mark-words).
                await self._consume_held_tail_at_utterance_end()
                # wh-whole-utterance-command-matching.3: the deferral
                # keeps an impossible buffer alive until the utterance
                # closes, and this marker IS phrase 1 closing. Finalize
                # the buffer before the end_utterance/start_utterance
                # pair so phrase 1's text cannot land inside phrase 2's
                # window. Before the deferral this buffer was always
                # empty here (an impossible buffer had already
                # finalized at word speed), so this call changes
                # nothing for the pre-deferral shapes.
                if self.mode != ProcessingMode.IDLE:
                    finalize_decision = self.router.decide_timeout(
                        self.buffer, self.hotword_active, mode=self.mode,
                        utterance_words=list(self._words_this_utterance),
                    )
                    # wh-whole-utterance-command-matching.3.1.6 (boss
                    # ruling, option (a)): this marker is a confirmed
                    # utterance end (the Mode-1 contract in
                    # integrations/websocket_manager.py), so the
                    # item-20 R1 split applies to this finalization
                    # exactly as it does in the end-marker branch --
                    # the same spoken words must not behave
                    # differently by internal close path. The consume
                    # fires the armed action BEFORE the pair, the
                    # same ordering the end-marker path keeps
                    # (.3.1.5): the action lands while phrase 1 is
                    # still open.
                    self._marker_finalization_active = True
                    try:
                        await self._execute_decision(finalize_decision)
                    finally:
                        self._marker_finalization_active = False
                    # wh-spaced-punctuation-names-unresolved.3.1.4: the
                    # finalization above can arm a replacement prefix
                    # of its own, and the entry drain at the top of
                    # this branch ran before it. A reset is a boundary
                    # that must deliver phrase 1, so drain that kind
                    # too -- unlike the end-marker branch, which
                    # retains it on purpose.
                    await self._consume_finalization_armed_tail(
                        drain_replacement_prefix=True,
                    )
            except Exception:
                logger.exception(
                    "Lifecycle-reset close-out failed; held/deferred "
                    "words lost, closing the utterance pair anyway"
                )
                self.buffer.clear()
                self.mode = ProcessingMode.IDLE
                self.hotword_active = False
                # A tail the failed finalization armed (or a hold whose
                # flush raised before clearing) must not leak into
                # phrase 2.
                self._held_tail = None
            self.sentence_closed_event.set()
            await self._send_end_utterance(word_event.utterance_id)
            await self.app.send_command({
                'action': 'start_utterance',
                'params': {'utterance_id': word_event.utterance_id},
            })
            return

        # ====================================================================
        # CHECK FOR UTTERANCE_END MARKER
        # ====================================================================
        # Special marker indicating all words from utterance have been processed
        # Clipboard restoration timing depends on whether we're buffering:
        # - IDLE: Safe to restore immediately
        # - Buffering: Defer until buffer finalizes (prevents clipboard race condition)
        if word_event.is_utterance_end_marker:
            logger.debug(f"Processing utterance_end marker for utterance {word_event.utterance_id}")
            # wh-pkhrp / wh-g2-refactor.18 (slice 18.32.1): invalidate
            # the focus-redirect policy's per-utterance cache so the
            # next utterance starts with a fresh detector decision.
            # Cheap synchronous call -- safe before the
            # trailing-action / end_utterance handling below. No-op
            # when the policy is not wired (legacy fixtures).
            if self.focus_redirect_policy is not None:
                try:
                    self.focus_redirect_policy.on_utterance_end()
                except Exception:
                    logger.exception(
                        "focus_redirect_policy.on_utterance_end raised; "
                        "continuing"
                    )
            # wh-g2-refactor.18: the focus-redirect drain/defer
            # machinery is gone. Consume the held tail now and proceed
            # with end-marker handling: the prefix kind types its held
            # words (no further word can complete the replacement), the
            # trailing kind fires its action (the candidate really was
            # the last word), the bare-number kind clicks the badge
            # (the number really was the whole utterance). Runs before
            # end_utterance below so the action or dictation lands
            # while the utterance is still open.
            await self._consume_held_tail_at_utterance_end(
                allow_continued_hold=True,
            )
            if self.mode == ProcessingMode.IDLE:
                # wh-oe7u.4: timeout finalization now runs inside the
                # processing loop via the queue sentinel, so the loop is
                # blocked during the IPC await and the next utterance_end
                # cannot be dequeued until finalization is done.
                self.sentence_closed_event.set()
                await self._send_end_utterance(word_event.utterance_id)
            else:
                # Buffer pending. Set _pending_utterance_end first so the post-execute
                # call inside _execute_decision picks it up, then finalize the buffer
                # right now via decide_timeout. This is the fix for wh-jkjkh: without
                # it, multi-word patterns split into separate dictations whenever STT
                # gaps exceed the per-pattern timeout, and partial-match buffers (e.g.
                # "back space" with an unfilled optional count) wait the full safety
                # timeout before firing.
                logger.debug(f"Finalizing buffer on utterance_end for {word_event.utterance_id}")
                self._pending_utterance_end = word_event.utterance_id
                finalize_decision = self.router.decide_timeout(
                    self.buffer, self.hotword_active, mode=self.mode,
                    utterance_words=list(self._words_this_utterance),
                )
                self._marker_finalization_active = True
                try:
                    await self._execute_decision(finalize_decision)
                finally:
                    self._marker_finalization_active = False
                # wh-click-number-dictation: the finalization above can
                # itself hold a bare number (a single-word buffer whose
                # word opened the utterance). Its end marker is THIS
                # event, so consume it now rather than leak it into the
                # next utterance.
                # wh-whole-utterance-command-matching.3: the same
                # finalization can now also arm a trailing command
                # word (the item-20 R1 split), so the consume covers
                # both kinds.
                await self._consume_finalization_armed_tail()
                # wh-whole-utterance-command-matching.3.1.5 (codex
                # round 2): when the finalization armed a trailing
                # tail, its split site left _pending_utterance_end set
                # so the consume above could fire the action while the
                # utterance was still open (end_utterance restores the
                # user's clipboard -- tests/test_ui/
                # test_utterance_clipboard_race.py). Send it now.
                # No-op on every other path: _execute_decision already
                # sent and cleared it.
                await self._send_pending_utterance_end()
            return

        # ====================================================================
        # CHECK FOR RETRACTION MARKER
        # ====================================================================
        if word_event.is_retraction_marker:
            logger.info(
                f"Processing retraction marker for utterance {word_event.utterance_id}: "
                f"full_text='{redact_transcript(word_event.retraction_full_text)}'"
            )
            # wh-2vz: any held trailing candidate was already flushed by
            # the top-of-loop guard before this branch is reached.
            await self._handle_retraction(word_event)
            return

        # Reset command execution flag at start of new utterance.
        if word_event.start_of_utterance:
            self._command_executed_in_utterance = False
            # wh-overlay-slow-uia-stale-badges.7: an utterance whose end
            # marker was lost must not hold the dictation sentence open
            # forever -- a fresh utterance starting is proof the old
            # sentence is over. A word this utterance actually types
            # will clear the event again in _send_to_dictation.
            self.sentence_closed_event.set()
            # wh-g2-refactor.18 (slice 18.32.1): reset per-utterance
            # editor-path tracking. The previous utterance's editor
            # writes belong to that utterance's retract; the new
            # utterance starts with a clean slate.
            self._used_editor_this_utterance = False
            self._editor_chars_this_utterance = 0

        # wh-g2-refactor.18 (slice 18.32.1): capture the current
        # utterance id so the DICTATE branch can label editor IPCs
        # without having to thread the WordEvent through Decision.
        if word_event.utterance_id is not None:
            self._current_utterance_id = word_event.utterance_id

        # wh-click-number-dictation: remember whether the word being
        # processed opened its utterance; the bare-number hold in the
        # DICTATE branch reads this (Decision does not carry the event).
        self._current_word_starts_utterance = bool(
            word_event.start_of_utterance
        )

        # Auto-finalize previous utterance if new one starts while buffering
        if self.mode != ProcessingMode.IDLE and word_event.start_of_utterance:
            logger.info(f"New utterance {word_event.utterance_id} started while buffering. Finalizing previous buffer.")
            # wh-whole-utterance-command-matching.3: this buffer belongs
            # to the PREVIOUS utterance, and the Stage-1 reset at the
            # top of this method has already replaced the live word
            # list with the new utterance's first word. Judge the
            # whole_utterance_only span against the snapshot taken at
            # that reset -- the naive live-list read wrongly suppressed
            # a cut-short "save" utterance's command (red-first test:
            # test_cut_short_save_still_fires_on_auto_finalize).
            finalize_decision = self.router.decide_timeout(
                self.buffer, self.hotword_active, mode=self.mode,
                utterance_words=list(self._previous_utterance_words),
            )
            await self._execute_decision(finalize_decision)
            # Note: _execute_decision resets mode to IDLE
            # wh-click-number-dictation: the finalization belongs to the
            # PREVIOUS utterance, whose end marker never arrived -- a
            # bare number it held can never legitimately be consumed as
            # a click. Flush it as dictation now so it lands before the
            # new utterance's words instead of leaking past them.
            await self._flush_pending_bare_number_as_dictation()

        # Delegate decision to Router
        decision = self.router.decide(
            word_event,
            self.mode,
            self.buffer,
            hotword_active=self.hotword_active,
            command_timeout_ms=self.command_timeout_ms,
            replacement_timeout_ms=self.replacement_timeout_ms,
            greedy_timeout_ms=self.greedy_timeout_ms,
            # Stage-1 list, already including this event's word (the
            # append above runs first). Only the flagged-last-word
            # finalization (router step 1) reads it
            # (wh-whole-utterance-command-matching.3.1.1).
            utterance_words=list(self._words_this_utterance),
        )

        pipeline_logger.info(
            "ROUTED action=%s mode=%s reason=%r word=%r elapsed_ms=%.1f",
            decision.action.name, self.mode.name, decision.reason,
            redact_transcript(word_event.word), elapsed_ms(),
        )

        await self._execute_decision(decision, word_event=word_event)

    async def _execute_decision(
        self, decision: Decision, word_event: Optional[WordEvent] = None
    ):
        """Execute the routing decision.

        Handles side effects like buffering, executing commands, or sending dictation.

        Args:
            decision: The routing decision to carry out.
            word_event: The word event that produced this decision, when one
                did. wh-okay-prefix-splits-replacement: only the word-driven
                path passes it, and only that path can hold a trailing
                replacement prefix. Every other caller leaves it None --
                the timeout sentinel arms its own hold before calling here,
                and the auto-finalize at the start of a new utterance is
                closing the PREVIOUS utterance, which no later word can
                complete.
        """
        if decision.reason:
            logger.debug(f"Decision: {decision.action.name} ({decision.reason})")

        # wh-click-number-dictation (multi-word): the flush guard
        # deferred one word because it could extend the held number.
        # Only the DICTATE branch below can extend the hold, and only
        # with that exact word. Any other decision -- a command, a
        # buffer, an ignore, or a dictation of different text after a
        # buffer finalization -- means the word was not a continuation,
        # so the held words dictate now, before this decision runs, and
        # the spoken order is preserved.
        if self._bare_number_deferred_word is not None and not (
            decision.action == Action.DICTATE
            and decision.payload == self._bare_number_deferred_word
        ):
            self._bare_number_deferred_word = None
            await self._flush_pending_bare_number_as_dictation()

        if decision.action == Action.IGNORE:
            # Still need to clean up state and send any deferred end_utterance
            # (e.g., when hotword alone times out with no follow-up command)
            self._reset_to_idle()
            await self._send_pending_utterance_end()
            return

        elif decision.action == Action.DICTATE:
            # wh-oe7u.4: the wh-bvl6d _inflight_finalization guard is gone.
            # Timeout finalization now runs through the queue sentinel
            # path inside _processing_loop, so this whole branch executes
            # serially with all other word events. A concurrent
            # utterance_end_marker cannot be dequeued during the IPC
            # await; it waits in the queue and runs after _execute_decision
            # returns.
            #
            # wh-okay-prefix-splits-replacement: capture the buffer's
            # hotword authorization BEFORE the reset clears it, the same
            # way the EXECUTE branch below does.
            hotword_at_finalization = self.hotword_active
            self._reset_to_idle()
            # wh-okay-prefix-splits-replacement (site 2, trace
            # T-17877987532): a command buffer that a word disproved
            # finalizes as dictation at word speed, with no timer involved.
            # When its last word could still grow into a replacement, hold
            # the words instead of typing them, so "okay question" plus a
            # later "mark" types "okay?" rather than three spoken words.
            # Arm the release deadline AFTER _reset_to_idle, which bumps
            # timeout_token and would otherwise make the sentinel stale
            # before it is dequeued.
            #
            # wh-spaced-punctuation-names-unresolved.3: a marker-driven
            # finalization reaches this site too. It passes no word_event
            # (the marker is not a spoken word), so the site used to skip
            # it entirely -- and that is where "open" said as its own
            # utterance was typed, measured before this change. The
            # confirmed-utterance-end signal for those calls is
            # _marker_finalization_active, which is exactly what
            # _utterance_end_is_confirmed reads, so ask that helper for
            # the flag instead of reading a word_event that may not
            # exist. Timeout and auto-finalize calls still pass no
            # word_event and set no flag, so they still skip the site.
            confirmed_end = self._utterance_end_is_confirmed(word_event)
            if decision.payload and (
                word_event is not None or self._marker_finalization_active
            ):
                held_words = decision.payload.split()
                if self._should_hold_replacement_prefix(
                    held_words,
                    hotword_active=hotword_at_finalization,
                    end_of_utterance=confirmed_end,
                ):
                    self._pending_replacement_prefix = held_words
                    pipeline_logger.info(
                        "REPLACEMENT-PREFIX held text=%r site=%s "
                        "elapsed_ms=%.1f",
                        redact_transcript(" ".join(held_words)),
                        "marker-finalization" if word_event is None
                        else "word-driven-finalization",
                        elapsed_ms(),
                    )
                    self._arm_replacement_prefix_release(
                        across_utterance_end=confirmed_end,
                    )
                    await self._send_pending_utterance_end()
                    return
            # wh-click-number-dictation: a bare number that opened its
            # utterance, with the overlay showing badges, may be a
            # "click N" whose verb the STT final dropped. Hold it; the
            # utterance-end marker consumes it as the click, and any
            # other event flushes it back to dictation.
            if self._bare_number_deferred_word == decision.payload:
                # wh-click-number-dictation (multi-word): this word
                # continues the held number. Re-checked rather than
                # trusted, so a state change between the guard and here
                # cannot force a click the overlay no longer supports.
                self._bare_number_deferred_word = None
                if self._extend_bare_number_hold(decision.payload):
                    await self._send_pending_utterance_end()
                    return
                await self._flush_pending_bare_number_as_dictation()
            if self._maybe_hold_bare_number(decision.payload):
                # wh-number-badge-problems.1.6 (codex round 6): nothing
                # releases the hold if the utterance-end marker never
                # arrives. Arm a release deadline, the way
                # wh-trailing-question-mark-words.2.1 arms one for a held
                # replacement prefix. Safe here because _reset_to_idle ran
                # at the top of this branch and bumped timeout_token;
                # arming before it would make the sentinel stale before it
                # was ever dequeued. An end marker that does arrive
                # consumes the hold and bumps the token again, so the
                # deadline's own sentinel then changes nothing.
                self._arm_spoken_click_release()
                await self._send_pending_utterance_end()
                return
            # wh-overlay-count-homophones.1.3: the multi-word half of the
            # shadowed class. A grid pattern claims the FIRST word of
            # "one twelve" / "number seventy four" / "for two", so the
            # router buffers that word as a COMMAND; the pair then
            # matches no pattern and the buffer finalizes here as
            # dictation. The badge number typed. Because every spoken
            # form of 100..999 begins with a unit word, no verb-less form
            # of ANY badge 100..999 reached the hold above.
            #
            # Placement, and why it is here rather than anywhere else:
            # AFTER _maybe_hold_bare_number, so the single-word hold and
            # the spoken-click hold both keep their existing paths and
            # this is reached only once both have declined; and BEFORE
            # the trailing-word split and the dictation send below, which
            # are what typed the words. It runs on the FINALIZED payload,
            # so no partial utterance can trigger it.
            #
            # The multi-word test is what keeps a mid-utterance number
            # dictating: a lone number reaching this point failed
            # _maybe_hold_bare_number's start-of-utterance check, and
            # clicking it would swallow a number the user dictated.
            # try_bare_number_badge_click owns the badges-showing gate
            # and the single click sender, and returns False when the
            # words name no number -- "one twelve is my number" parses to
            # None and dictates unchanged.
            #
            # The whole-utterance test is the rule the spoken-click hold
            # already applies, now in one shared helper: a badge pick is
            # a badge pick only when the user said nothing else. It is
            # what keeps "hello click 74" typing -- that payload is the
            # tail of a longer utterance, and the spoken-click hold
            # declined it for this same reason a few lines above. A
            # verb-led payload that IS the whole utterance never reaches
            # here, because that hold consumed it.
            #
            # The confirmed-end test is wh-overlay-count-homophones.1.4
            # (codex round 3), and it is the reason a click may not fire
            # on the buffer timer. This branch also serves the 1000 ms
            # no-end-marker fallback, which finalizes MID-utterance: a
            # user who says "one twelve", pauses past the timer, then
            # says "is my number" had badge 112 clicked under them and
            # could not take it back, because a click cannot be undone
            # and the command flag disables retraction. Measured, before
            # this gate: painted and refresh_in_flight both clicked at
            # the timer, for every shape in the class. The single-word
            # hold above never had this exposure -- it HOLDS and a later
            # word flushes it back to dictation -- so the gate is what
            # gives this check the same safety by a different means.
            # _utterance_end_is_confirmed is the rule
            # _split_trailing_word_at_marker_finalization already
            # applied; it moved into a helper so both read one
            # definition.
            if (
                decision.payload
                and " " in decision.payload.strip()
                and self._utterance_end_is_confirmed(word_event)
                and self._payload_is_the_whole_utterance(decision.payload)
            ):
                if await self.try_bare_number_badge_click(decision.payload):
                    await self._send_pending_utterance_end()
                    return
            # wh-whole-utterance-command-matching.3 (item-20 R1): a
            # marker-driven finalization's dictation payload ending
            # in a trailing command word splits -- the head dictates,
            # the trailing word is held, and
            # _consume_finalization_armed_tail in the end-marker
            # branch fires it in this same utterance.
            if decision.payload:
                head, trailing = (
                    self._split_trailing_word_at_marker_finalization(
                        decision.payload, hotword_at_finalization,
                        word_event=word_event,
                    )
                )
                if trailing is not None:
                    if head:
                        await self._send_to_dictation(head)
                    self._pending_trailing_word = trailing
                    pipeline_logger.info(
                        "TRAILING candidate held word=%r site=%s "
                        "elapsed_ms=%.1f",
                        redact_transcript(trailing),
                        "marker-finalization-dictate",
                        elapsed_ms(),
                    )
                    # wh-whole-utterance-command-matching.3.1.5 (codex
                    # round 2): deliberately NOT sending the pending
                    # end_utterance here. The caller that armed this
                    # tail consumes it next and fires the action, and
                    # the action must land while the utterance is
                    # still open (end_utterance restores the user's
                    # clipboard -- the invariant in tests/test_ui/
                    # test_utterance_clipboard_race.py). The end-marker
                    # branch sends the deferred end AFTER its consume;
                    # the flagged-last-word path never set the slot.
                    return
            # wh-2vz: if the payload is a single word matching the
            # trailing-command map, hold it as a pending candidate
            # instead of dispatching. The next regular event will
            # either flush it as text (proving it was not the last
            # word) or the utterance_end_marker branch will consume
            # it as the trailing action.
            if await self._maybe_hold_trailing_candidate(decision.payload):
                # wh-2vz.2.2 (deepseek round 1): if the focus-redirect
                # path is buffering, register a discard callable on
                # the path so a fail-closed event (FOCUS_PENDING
                # timeout, focus_lost, te_cancelled, mirror reject)
                # arriving BEFORE the utterance_end marker clears the
                # held word. Without this, the held word leaks past
                # the failed redirect cycle and the next utterance's
                # first word trips the top-of-loop flush guard,
                # dictating "submit" as text in the wrong utterance.
                # Only the buffering case needs the discard; outside
                # of buffering, no fail-closed event will fire.
                # wh-g2-refactor.18: the focus-redirect discard hook
                # registration was removed with the redirect path.
                # Pending end_utterance still has to flow even though
                # we did not dispatch the word here, because the
                # surrounding processing loop relies on the same
                # ordering as the regular DICTATE path.
                await self._send_pending_utterance_end()
                return
            # wh-g2-refactor.18 (slice 18.32.1): _send_to_dictation
            # now consults the focus-redirect policy and routes to the
            # persistent editor when the policy says terminal-at-prompt.
            # The legacy ``intelligent_insert_text`` path is the
            # fallback for everything else and for fixtures that do not
            # wire a policy.
            await self._send_to_dictation(decision.payload)
            # Send deferred end_utterance AFTER dictation completes.
            await self._send_pending_utterance_end()

        elif decision.action == Action.EXECUTE:
            # See DICTATE branch for serialization rationale (wh-oe7u.4).
            # Capture the buffer's hotword authorization BEFORE the reset
            # clears it: the engine re-match must apply the same hotword
            # gate the router applied, or a hotword-required pattern that
            # sits earlier in the file hijacks the buffer the router
            # resolved to a later ungated pattern
            # (wh-voice-access-parity.3.5).
            hotword_authorized = self.hotword_active
            self._reset_to_idle()
            # Execute BEFORE remainder first (buffered content that arrived earlier)
            if decision.before_remainder:
                await self._process_remainder(decision.before_remainder)
            # Execute the matched pattern
            await self._execute_command(
                decision.payload, hotword_authorized=hotword_authorized
            )
            # Execute AFTER remainder last (content that arrived after the match)
            armed_trailing = None
            if decision.remainder:
                # wh-whole-utterance-command-matching.3 (item-20 R1):
                # split a trailing command word off the AFTER
                # remainder at marker-driven finalization before the
                # replacement-only remainder pass, so 'backspace
                # hello submit' at the end marker fires submit
                # instead of typing it. The head still gets the full
                # replacement pass.
                remainder_text = decision.remainder
                head, trailing = (
                    self._split_trailing_word_at_marker_finalization(
                        remainder_text, hotword_authorized,
                        word_event=word_event,
                    )
                )
                if trailing is not None:
                    remainder_text = head
                # wh-okay-prefix-splits-replacement (site 3, trace
                # T-17877991226): only the AFTER remainder can hold. Text
                # before the matched pattern arrived earlier in spoken
                # order, so no word arriving next belongs to it.
                if remainder_text:
                    await self._process_remainder(
                        remainder_text,
                        word_event=word_event,
                        hotword_active=hotword_authorized,
                    )
                if trailing is not None:
                    self._pending_trailing_word = trailing
                    armed_trailing = trailing
                    pipeline_logger.info(
                        "TRAILING candidate held word=%r site=%s "
                        "elapsed_ms=%.1f",
                        redact_transcript(trailing),
                        "marker-finalization-remainder",
                        elapsed_ms(),
                    )
            # wh-g2-refactor.18: the focus-redirect transfer for
            # replacement inserts is gone with the redirect path.
            # Send deferred end_utterance AFTER command execution completes.
            # wh-whole-utterance-command-matching.3.1.5 (codex round
            # 2): unless this finalization armed a trailing tail -- the
            # caller consumes it next, and its action must fire while
            # the utterance is still open (the clipboard invariant in
            # tests/test_ui/test_utterance_clipboard_race.py), so the
            # caller sends the deferred end after its consume.
            if armed_trailing is None:
                await self._send_pending_utterance_end()

        elif decision.action == Action.BUFFER:
            # Payload is the word to add
            self.buffer.append(decision.payload)
            if decision.target_mode:
                self.mode = decision.target_mode
            if decision.timeout_ms:
                self._start_timeout(decision.timeout_ms)

        elif decision.action == Action.TRANSITION:
            if decision.target_mode:
                self.mode = decision.target_mode
            if decision.target_mode == ProcessingMode.HOTWORD_BUFFERING:
                self.hotword_active = True
                self.buffer.clear() # Hotword itself is not buffered
            if decision.timeout_ms:
                self._start_timeout(decision.timeout_ms)

    # ========================================================================
    # TRAILING-POSITION COMMANDS (wh-2vz)
    # ========================================================================
    #
    # A trailing-position command word fires its action AFTER the dictated
    # prefix is inserted. The word itself is stripped from the transcription.
    #
    # The WebSocketManager reliably sends ``is_utterance_end_marker`` at
    # the end of every utterance, so the trailing decision is anchored on
    # that marker. Each real word
    # that the router would dictate is first checked against the trailing-
    # commands map; matches are held as a pending candidate instead of
    # dispatched. The candidate is then either:
    #   - flushed as plain dictation when ANY subsequent regular event
    #     arrives (another word, a buffer mutation, a retraction), proving
    #     the trailing word was not actually the last word of the utterance;
    #   - consumed as the trailing action when ``is_utterance_end_marker``
    #     arrives, proving the held word WAS the last word.
    #
    # The remote-path field ``WordEvent.end_of_utterance=True`` is NOT used
    # as the trigger because remote STT only sets that flag on the
    # ``is_utterance_end_marker`` (empty payload) event, never on the
    # accompanying real words. The in-process path does set the flag on real
    # words, but the design relies only on the marker so both paths share
    # one code path.

    def _split_trailing_word_at_marker_finalization(
        self, text: str, hotword_authorized: bool,
        word_event: Optional[WordEvent] = None,
    ) -> tuple[str, Optional[str]]:
        """R1 split (wh-whole-utterance-command-matching.3, David's
        item-20 ruling 2026-08-29): at end-of-utterance finalization a
        dictation-bound payload whose LAST word is a trailing command
        splits -- the head dictates, the trailing word is held so
        _consume_finalization_armed_tail (marker path) or the
        end-marker first-consume (flagged-word path) fires it in its
        own utterance. Returns (head, trailing_word); trailing_word
        is None when no split applies.

        Only a CONFIRMED utterance end splits, via two signals:
        _marker_finalization_active covers BOTH marker-driven
        finalizations -- the end-marker branch's and the Mode 1
        lifecycle-reset branch's, each setting it around exactly its
        own _execute_decision call -- and a word_event whose
        end_of_utterance flag sits on a real
        word covers the in-process bridge, which flags the utterance's
        last real word and finalizes through router step 1 before the
        queued marker arrives (wh-whole-utterance-command-matching
        .3.1.1). Marker events themselves are excluded from the flag
        path: the harness end marker and websocket_manager's
        finalize/retraction markers carry flags of their own and are
        not the utterance's last spoken word. Timeout and
        auto-finalize payloads keep dictating whole -- their calls
        pass no word_event and run outside both marker branches, so
        with no confirmed end the trailing word was never proven to be
        the utterance's last word, which preserves today's no-marker
        outcomes. A lifecycle reset DOES split, deliberately:
        wh-whole-utterance-command-matching.3.1.6 (boss ruling, option
        (a)) requires the same spoken words to behave the same by
        either internal close path, and
        tests/mutation_gate_whole_utterance.py pins that with
        lifecycle-marker-active-never-set. Hotword dictation keeps
        typing every word ('x-ray
        foo submit' stays literal text).
        """
        if hotword_authorized:
            return text, None
        if not self._utterance_end_is_confirmed(word_event):
            return text, None
        words = text.split()
        if not words:
            return text, None
        if self.catalog.get_trailing_command(words[-1]) is None:
            return text, None
        return " ".join(words[:-1]), words[-1]

    async def _maybe_hold_trailing_candidate(self, text: str) -> bool:
        """If ``text`` is a single word matching the trailing-command map,
        hold it as a pending candidate instead of dispatching it.

        Returns True if the text was captured (caller MUST NOT dispatch it
        to dictation); False otherwise. Captures only single-word DICTATE
        decisions -- multi-word payloads (from buffer finalization) and
        remainder text always flush through to dictation as today, so
        utterances like "comma submit" continue to insert ", submit" as
        text rather than fire Enter.
        """
        if not text:
            return False
        # Multi-word DICTATE payloads come from buffer finalization. Those
        # are dictation of a phrase that already failed to match a leading
        # pattern; do not retro-classify them as trailing.
        if " " in text.strip():
            return False
        entry = self.catalog.get_trailing_command(text)
        if entry is None:
            return False
        # The top-of-loop guard in process_word_event has already
        # flushed any prior pending candidate by the time we reach
        # this branch. Belt-and-braces: assign unconditionally.
        self._pending_trailing_word = text
        pipeline_logger.info(
            "TRAILING candidate held word=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        return True

    async def _flush_pending_trailing_word_as_dictation(self) -> None:
        """Dispatch the held trailing candidate as ordinary dictation.

        Called on every event that proves the held word was not actually
        the last word of the utterance (a follow-up word, a buffer
        mutation, retraction, lifecycle reset, a new utterance, processor
        stop, etc.). Clears the pending slot before the IPC so a failure
        cannot leak the word into the next consume path.
        """
        pending = self._pending_trailing_word
        if pending is None:
            return
        self._pending_trailing_word = None
        pipeline_logger.info(
            "TRAILING candidate flushed-as-text word=%r elapsed_ms=%.1f",
            redact_transcript(pending), elapsed_ms(),
        )
        # Use the same dictation routing the regular DICTATE branch
        # uses so the trailing word follows the focus-redirect policy
        # (wh-g2-refactor.18 slice 18.32.1) instead of going straight
        # to intelligent_insert_text.
        await self._send_to_dictation(pending)

    def _clear_held_trailing_word(self) -> None:
        """Synchronously clear the held trailing candidate.

        wh-2vz.2.2 (deepseek round 1): registered on the
        focus-redirect path via ``register_held_trailing_discard``
        when a trailing candidate is held while the path is
        buffering. The path's fail-closed paths call this so the
        slot is cleared whether or not ``defer_trailing_action`` was
        later called.

        Logs at INFO so the audit trail records the drop. Safe to
        call when the slot is already empty.
        """
        pending = self._pending_trailing_word
        if pending is None:
            return
        self._pending_trailing_word = None
        pipeline_logger.info(
            "TRAILING candidate dropped word=%r elapsed_ms=%.1f",
            redact_transcript(pending), elapsed_ms(),
        )

    async def _consume_pending_trailing_word_at_utterance_end(self) -> None:
        """Fire the trailing action for the held word, then clear it.

        Called from the ``is_utterance_end_marker`` handler so the
        action runs while the utterance is still open. (Historical
        note: this used to have a separate buffering branch driven by
        the focus-redirect path; that path was removed in
        wh-g2-refactor.18 and the trailing action now always fires
        synchronously here.)

        Reads the slot, clears it, then delegates the action-firing
        body to :meth:`_fire_trailing_action_for_word` so both the
        slot-based path here and the captured-word deferred path share
        the same firing logic.
        """
        pending = self._pending_trailing_word
        if pending is None:
            return
        self._pending_trailing_word = None
        await self._fire_trailing_action_for_word(pending)

    async def _fire_trailing_action_for_word(self, word: str) -> None:
        """Fire the trailing-position action for ``word``.

        Shared body for both the slot-based path
        (:meth:`_consume_pending_trailing_word_at_utterance_end`) and
        the captured-word deferred path used by the focus-redirect
        drain chain. The caller is responsible for sourcing the word
        -- this helper does not touch ``self._pending_trailing_word``.

        On success, flips ``_command_executed_in_utterance`` so a
        later STT revision cannot retract the irreversible side
        effect. On any failure path (catalog reload dropped the
        entry, registry-vs-pattern mismatch, action execution raised),
        the word is dictated as text as a conservative fallback
        rather than silently dropped.
        """
        entry = self.catalog.get_trailing_command(word)
        if entry is None:
            # Catalog reloaded between hold and consume and dropped the
            # word. Fail closed: dictate the held word as text rather
            # than silently drop it. Route through _send_to_dictation
            # so the focus-redirect policy still applies
            # (wh-g2-refactor.18 slice 18.32.1).
            logger.warning(
                "Trailing candidate %r no longer in catalog at "
                "fire time; dictating as text instead",
                redact_transcript(word),
            )
            await self._send_to_dictation(word)
            return

        compiled = entry["compiled_pattern"]
        actions = entry["actions"]
        # wh-whole-utterance-command-matching.3.1.7 (codex round 3):
        # every producer stores the RAW token ("submit."), while
        # get_trailing_command resolved it through
        # _normalize_lookup_word -- re-match the same normalized form,
        # or STT terminal punctuation turns the fired command into
        # dictation. The raw token stays in the fallback dictations
        # and the logs, so dictated text keeps its punctuation.
        match = compiled.match(_normalize_lookup_word(word))
        if match is None:
            logger.warning(
                "Trailing command registry mismatch for word=%r; "
                "dictating as text instead",
                redact_transcript(word),
            )
            await self._send_to_dictation(word)
            return

        pipeline_logger.info(
            "TRAILING command fired word=%r elapsed_ms=%.1f",
            redact_transcript(word), elapsed_ms(),
        )
        logger.info(f"Executing trailing command: '{redact_transcript(word)}'")
        try:
            executed = await self.text_parser._execute_rule(
                match, actions, validation_group=None, pattern_type="command",
            )
        except Exception:
            logger.exception(
                "Trailing command execution raised for word=%r; "
                "suppressing dictation of the word",
                redact_transcript(word),
            )
            executed = False

        if executed:
            # Trailing commands are irreversible side effects. Block
            # retraction for the rest of the utterance.
            self._command_executed_in_utterance = True

    # ========================================================================
    # BARE-NUMBER CLICKS (wh-click-number-dictation)
    # ========================================================================
    #
    # Distil-Whisper drops the word "click" from short "click N" finals
    # (live evidence 2026-08-08: segments oscillate ' click six.' / ' 6.'
    # and the FINAL keeps only the number). The bare number matches no
    # command pattern and would dictate. While the overlay is showing
    # badges, a FINAL utterance that is JUST a number 1..999 is almost
    # certainly a badge pick, so it is held back from dictation and, at
    # the utterance-end marker, executed as the click command "click N".
    #
    # The lifecycle mirrors the trailing-command hold above:
    #   - held only when the word opened its utterance, parses as a
    #     number 1..999, and the overlay state machine is in a
    #     badge-showing state (painted / refresh_in_flight);
    #   - extended by a following word when the words so far still
    #     parse as a number 1..999, because a user reading badge 23
    #     says "twenty three". The extension runs after the router has
    #     classified the word, so a word the router wanted for a
    #     command is never swallowed. This hold reaches only the
    #     numbers whose FIRST word no whole-utterance pattern claims:
    #     "twenty three" opens it, "one twelve" does not, because a
    #     grid pattern takes "one" as a command and the router buffers
    #     it (wh-overlay-count-homophones.1.3, measured). Badge 112
    #     said as "one twelve" is clicked by the finalized-payload
    #     check in the DICTATE branch instead, which is why that check
    #     exists;
    #   - flushed back to dictation by any regular event (the number was
    #     not the whole utterance), by a new-utterance auto-finalize, or
    #     conservatively at shutdown;
    #   - consumed at the utterance-end marker. The retraction-replay
    #     path is covered by the same marker: the websocket manager
    #     queues the end marker directly behind every retraction marker,
    #     so a replayed bare-number final is held during the replay and
    #     consumed one event later.

    def _overlay_accepts_bare_number(self) -> bool:
        """True when the overlay state machine is showing badges.

        Reads ``logic_controller.click_overlay_state.state`` defensively
        (legacy fixtures wire no controller; early startup may not have
        built the machine yet) and compares the state's VALUE string
        rather than the OverlayState enum member. This module can be
        imported under two roots (``speech.`` and
        ``services.wheelhouse.speech.``); enum members from the two
        module copies never compare equal, so an identity comparison
        here could silently disable the feature. The value strings are
        the stable, IPC-visible names of ``OverlayState.PAINTED`` and
        ``OverlayState.REFRESH_IN_FLIGHT`` -- the derived "is the
        overlay usable" rule from the state machine's contract.
        """
        machine = getattr(self.logic_controller, "click_overlay_state", None)
        if machine is None:
            return False
        state_value = getattr(getattr(machine, "state", None), "value", None)
        return state_value in ("painted", "refresh_in_flight")

    def _maybe_hold_bare_number(self, text: str) -> bool:
        """Hold ``text`` as a bare-number click candidate if it qualifies.

        Returns True if the text was captured (caller MUST NOT dispatch
        it to dictation). Qualifies only when the text is a single word
        that opened its utterance, the overlay is currently showing
        badges, and the word -- ignoring terminal punctuation on it
        (_spoken_number_text) -- either parses as a number 1..999 or is
        the "number"/"numbers" filler that can only precede one.
        Mid-utterance numbers always dictate. A multi-word payload (from buffer
        finalization) goes to _maybe_hold_spoken_click, which holds only
        the spoken "click N" form; every other multi-word payload
        dictates.

        The filler is here because the recogniser drops the leading
        "click": the user says "click number three" and the final is
        "number three". parse_number_word reads that as 3 -- it supports
        the form for exactly this reason -- but "number" ALONE parses to
        None, so the hold never opened and both words dictated. David
        ruled on 2026-08-27 that "number n should click bubble n"
        (wh-click-number-dictation.1.2).

        What that filler branch actually reaches, measured under
        wh-overlay-count-homophones.1.3: nothing that ships. "number"
        and "numbers" are the first word of the grid-number-prefixed
        pattern, so the shipped router classifies them as COMMAND and
        buffers them; at the marker finalization
        _current_word_starts_utterance is False, so the single-word
        branch below cannot open the hold either. The branch is kept
        because it is the correct reading of a bare "number N" payload
        and costs nothing, not because a shipped path delivers one.
        David's ruling is honoured by the finalized-payload check in the
        DICTATE branch, which sends "number seventy four" and
        "numbers 75" to the badge click. Same shape as the homophone
        note further down: the guard is right, its reachability is not
        what the surrounding prose once implied.

        A hold opened this way is the first that does not parse as a
        number on its own, which is what
        _consume_pending_bare_number's parse check is for.
        """
        if not text:
            return False
        if " " in text.strip():
            return self._maybe_hold_spoken_click(text)
        if not self._current_word_starts_utterance:
            return False
        # wh-number-badge-problems.1.4 (boss ruling 2026-09-02): the same
        # terminal punctuation the spoken-click hold ignores reaches the
        # bare hold too, where "74." parsed to None and the badge was
        # never selected. The held word stays raw; only these guards read
        # the stripped form, and the command text keeps its punctuation
        # because ClickCommandParser strips it again (click_parser.py:170).
        bare = self._spoken_number_text([text])
        if (
            # wh-overlay-count-homophones: aliases so a badge number the
            # engine returned as "to", "too" or "for" opens the hold. The
            # badges-showing gate below is what keeps the words typable in
            # ordinary dictation, where no hold ever opens.
            #
            # Measured, wh-overlay-count-homophones.1.1: for the three
            # homophones this branch is unreachable today, and the alias
            # here changes nothing that ships. The router classifies a
            # lone "to", "too" or "for" as a COMMAND -- the grid patterns
            # match the whole utterance -- so it buffers and never
            # reaches this DICTATE branch. Those words take the grid
            # actions' badge fallback instead
            # (ActionFunctions._click_badge_instead_of_dictating). The
            # alias stays for the numbers the grid patterns do NOT claim,
            # and so this guard reads a spoken number the same way every
            # other site does; a mutation of it is caught only by the
            # unit tests that call this helper directly.
            parse_number_word(bare, aliases=True) is None
            # .strip() because _spoken_number_text removes punctuation and
            # not whitespace, and this check compares the whole string
            # against the filler set. parse_number_word strips its own
            # input, so only this side needs it.
            and bare.strip().casefold() not in _NUMBER_FILLERS
        ):
            return False
        if not self._overlay_accepts_bare_number():
            return False
        self._pending_bare_number_words = [text]
        pipeline_logger.info(
            "BARE-NUMBER candidate held word=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        return True

    # crewcut: these verbs mirror click_element's trigger "^(?:click|tap)"
    # in action_catalog.py; a user-customized trigger is not consulted.
    # Read them from the catalog entry once the catalog exposes its verbs.
    _SPOKEN_CLICK_VERBS = ("click", "tap")

    @staticmethod
    def _spoken_number_text(tokens: list[str]) -> str:
        """Held or spoken tokens, ready for ``parse_number_word``.

        wh-number-badge-problems.1.4 (codex round 4): local STT appends
        terminal punctuation to the last word of an utterance, so a
        spoken "click 74" arrives as "click 74." and
        ``parse_number_word("74.")`` is None. click_parser strips
        ``_TRAILING_PUNCT`` from its own final token for the same
        reason, and the trailing-command hold re-matches a normalized
        form (wh-whole-utterance-command-matching.3.1.7); this is that
        rule for both holds, reusing click_parser's own constant
        because the command text they build is what click_parser
        parses. Only the LAST token is stripped, so a
        payload whose other words carry punctuation ("click 74, please")
        still fails to parse and dictates. The caller keeps the raw
        text: every dictation fallback must type the punctuation the
        user's dictation would have carried.
        """
        if not tokens:
            return ""
        stripped = list(tokens)
        stripped[-1] = stripped[-1].rstrip(_TRAILING_PUNCT)
        return " ".join(token for token in stripped if token)

    def _utterance_end_is_confirmed(
        self, word_event: Optional[WordEvent],
    ) -> bool:
        """True when this finalization stands on a CONFIRMED utterance end.

        Two signals, and only these two.
        ``_marker_finalization_active`` covers BOTH marker-driven
        finalizations -- the end-marker branch's and the Mode 1
        lifecycle-reset branch's -- each setting the flag around
        exactly its own ``_execute_decision`` call. A ``word_event``
        whose ``end_of_utterance`` flag sits on a
        REAL word covers the in-process bridge, which flags the
        utterance's last real word and finalizes through router step 1
        before the queued marker arrives
        (wh-whole-utterance-command-matching.3.1.1). Marker events
        themselves are excluded: the harness end marker and
        websocket_manager's finalize and retraction markers carry flags
        of their own and are not the utterance's last spoken word.

        Timeout and auto-finalize payloads answer False -- their calls
        pass no ``word_event`` and run outside both marker branches, so
        nothing has yet proven that the words so far were the whole
        utterance.

        The lifecycle reset answers True, deliberately.
        wh-whole-utterance-command-matching.3.1.6 and .3.1.8 (boss
        rulings, option (a)) declare that marker a confirmed utterance
        end, and the branch at the ``is_lifecycle_reset_marker`` close
        has clicked a HELD bare number there since before this file
        carried this helper. The marker is not the evidence-free
        client-side timer it resembles:
        ``integrations/websocket_manager.py`` reaches Mode 1 only when
        the server's final does not extend the stable, there was no
        stable disagreement, no end-of-speech arrived, and the server's
        own ``final_reason`` is GOOGLE_SILENCE_2S, EOS_FALLBACK or
        NO_TEXT_TIMEOUT -- all server-side silence or end-of-speech
        finalizations. wh-overlay-count-homophones.1.6 (codex round 6)
        read the earlier wording here as describing a defect; the
        wording was wrong, not the code, and
        tests/test_grid_number_badge_click.py now pins the behaviour.
        Extracted from
        _split_trailing_word_at_marker_finalization for
        wh-overlay-count-homophones.1.4, which needs the same rule.
        """
        return self._marker_finalization_active or (
            word_event is not None
            and word_event.end_of_utterance
            and bool(word_event.word)
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_retraction_marker
            and not word_event.is_lifecycle_reset_marker
        )

    def _payload_is_the_whole_utterance(self, text: str) -> bool:
        """True when ``text`` is every word of the utterance, in order.

        The rule two badge-click paths share, in one place. A click is a
        click only when the user said nothing else: "hello click 74"
        types, and so does "hello one twelve". ``_words_this_utterance``
        is the utterance as spoken, so any word in it that ``text`` does
        not account for proves the payload was a tail, not the whole.
        Casefolded on both sides because the recogniser capitalises the
        first word of an utterance.
        """
        spoken = [word.casefold() for word in self._words_this_utterance]
        return [token.casefold() for token in text.split()] == spoken

    def _maybe_hold_spoken_click(self, text: str) -> bool:
        """Hold a finalized "click N" buffer as a badge click if it qualifies.

        wh-number-badge-problems.2: the shipped recogniser keeps the word
        "click", so "click 74" opens command buffering; click_element
        needs the hotword, so router._cannot_match defers the buffer as
        impossible and the end marker finalizes it as dictation (David's
        evidence log of 2026-09-02, lines 21777-21806). While badges show,
        that buffer is the badge click David ruled for on 2026-08-27
        (C7), exactly like the bare number whose verb the recogniser
        dropped. Qualifies only when the text is the whole utterance,
        opens with "click" or "tap", the rest -- optionally after one
        "number"/"numbers" filler, and ignoring terminal punctuation on
        the last token (_spoken_number_text) -- parses as a number
        1..999, and the overlay is showing badges. "click 0",
        "click 1000", "click submit button" and "click 74 please" all
        dictate as before.

        The one held word is the spoken payload itself ("click 74",
        "tap seventy four"), the same shape the bare path holds, so every
        dictation fallback -- the flush when a later word or the next
        utterance proves the payload was not the whole utterance, the
        overlay re-check at consume time, a click pattern that does not
        match -- types exactly the string the pre-fix finalization typed,
        in one insertion (wh-number-badge-problems.1.3).
        _held_bare_number_command derives "click <N>" in digits from it
        at consume time, whatever form was spoken. The whole-utterance
        check replaces the bare number's _current_word_starts_utterance
        check, which is False at marker finalization.
        """
        tokens = text.split()
        if len(tokens) < 2:
            return False
        if tokens[0].casefold() not in self._SPOKEN_CLICK_VERBS:
            return False
        # wh-overlay-count-homophones: aliases, so "click for" is held as a
        # spoken badge click rather than finalized as dictation.
        number = parse_number_word(
            self._spoken_number_text(tokens[1:]), aliases=True,
        )
        if number is None:
            return False
        if not self._payload_is_the_whole_utterance(text):
            return False
        if not self._overlay_accepts_bare_number():
            return False
        self._pending_bare_number_words = [text]
        pipeline_logger.info(
            "BARE-NUMBER candidate held from spoken click text=%r "
            "number=%d elapsed_ms=%.1f",
            redact_transcript(text), number, elapsed_ms(),
        )
        return True

    def _is_spoken_click_hold(self, held: Optional[list[str]]) -> bool:
        """True when the held words are a spoken click, not a bare number.

        A bare hold can never open with "click" or "tap" -- neither word
        parses as a number nor is a "number"/"numbers" filler -- so the
        verb alone tells the two holds apart. That is the same
        discriminator _held_bare_number_command uses to decide which
        command text to build.
        """
        if not held:
            return False
        tokens = " ".join(held).split()
        return (
            len(tokens) >= 2
            and tokens[0].casefold() in self._SPOKEN_CLICK_VERBS
        )

    def _arm_spoken_click_release(self) -> None:
        """Arm the deadline that releases a held spoken click.

        wh-number-badge-problems.1.6 (codex round 6). The hold waits for
        the utterance-end marker, and integrations/websocket_manager.py
        builds that marker from the terminal FINAL message, so a dropped
        message takes the marker with it; its idle watchdog
        (_fire_idle_watchdog) only writes the GUI activity state. A user
        who then stops speaking sends no further event, so without this
        deadline the spoken click is lost in silence -- no click, and not
        even the text the pre-fix finalization typed.

        Only the spoken-click hold is armed. The bare-number hold waits
        the same way, which codex recorded as a separate, pre-existing
        question outside this branch's scope. ``command_timeout_ms`` is
        the duration because the held words are a command, and it is the
        timeout the router already gives a command buffer.
        """
        if not self._is_spoken_click_hold(self._pending_bare_number_words):
            return
        self._start_timeout(self.command_timeout_ms)

    def _held_bare_number_command(self, held: list[str]) -> Optional[str]:
        """The click command the held words name, or None to dictate them.

        A bare-number hold keeps the spoken words and runs
        "click <words>", the command text a spoken "click N" produces. A
        spoken-click hold keeps the whole spoken payload -- "click 74",
        "tap seventy four", "click number 74", "click 74." -- and runs
        "click <N>" with the number in digits, so the click_parser sees
        one form whatever the user said and whatever punctuation the
        recogniser appended. Held words that name no number (the lone
        "number"/"numbers" filler the bare path can hold) return None.
        A bare hold can never open with "click" or "tap" (neither parses
        as a number nor is a filler), so the verb alone tells the two
        holds apart.
        """
        tokens = " ".join(held).split()
        if len(tokens) >= 2 and tokens[0].casefold() in self._SPOKEN_CLICK_VERBS:
            # wh-overlay-count-homophones: aliases at both branches, so the
            # command text this builds names the same badge the hold opened
            # on. The spoken-click branch resolves to digits ("click for"
            # -> "click 4"); the bare branch keeps the spoken words and
            # main.py resolves them.
            number = parse_number_word(
                self._spoken_number_text(tokens[1:]), aliases=True,
            )
            return None if number is None else f"click {number}"
        if parse_number_word(
            self._spoken_number_text(held), aliases=True,
        ) is None:
            return None
        return f"click {' '.join(held)}"

    def _bare_number_extends(self, text: Optional[str]) -> bool:
        """True when ``text`` would keep the held words a valid number.

        Answers the flush guard's question: does this next word
        continue the held bare number ("twenty" + "three" = 23), or
        does it prove the number was not the whole utterance ("6" +
        "pack")? A single token only, and the overlay must still be
        showing badges, so a number whose badges vanished mid-utterance
        stops growing and dictates. Terminal punctuation on the new word
        is ignored (_spoken_number_text), so "seventy" + "four?" is
        still 74.
        """
        held = self._pending_bare_number_words
        if not held or not text or " " in text.strip():
            return False
        if not self._overlay_accepts_bare_number():
            return False
        # wh-overlay-count-homophones: aliases, so "twenty" + "for" stays a
        # growing number (24) instead of flushing the hold to dictation.
        return parse_number_word(
            self._spoken_number_text(held + [text]), aliases=True,
        ) is not None

    def _extend_bare_number_hold(self, text: str) -> bool:
        """Append ``text`` to the held bare number if it still parses.

        Returns True if the word was captured (caller MUST NOT dispatch
        it to dictation).
        """
        held = self._pending_bare_number_words
        if held is None or not self._bare_number_extends(text):
            return False
        held.append(text)
        pipeline_logger.info(
            "BARE-NUMBER candidate extended words=%r elapsed_ms=%.1f",
            redact_transcript(" ".join(held)), elapsed_ms(),
        )
        return True

    def drop_bare_number_hold(self, reason: str) -> None:
        """Drop a held bare number without typing it or clicking it.

        The third way the hold can end, beside the flush below that types
        the words and the consume after it that clicks them. This one is
        for the cases where the held words have stopped belonging to
        anything the user is still saying. Two callers:
        the per-word exception handler in _processing_loop, which has
        made this drop since wh-click-number-dictation.1.6, and
        LogicController._switch_stt_provider, from the two places in its
        remote branch where the outgoing engine is known to be unable to
        finish the hold -- either arm, once the old process has been
        seen to exit (wh-provider-switch-stale-hold). A third place used
        to be the in-process branch, where STTManager swapped the
        provider without raising; wh-in-process-capture-removal deleted
        that branch with the engine it drove.

        Two weaker triggers are deliberately refused, and each was used
        first. Merely having SENT the stop is not enough: that send can
        fail and the old engine can survive the whole switch
        (wh-provider-switch-stale-hold.1.1). A successful SPAWN of the
        replacement is not enough either: start_provider returns on
        subprocess.Popen, the replacement can fail its own startup
        afterwards, and the engine it replaced keeps transcribing
        (wh-provider-switch-stale-hold.2.1). Both mistake an attempt for
        an outcome, and both throw away a number the user is still in
        the middle of saying to an engine they still have. stop() is
        deliberately
        NOT a caller: it drops the held tail whatever kind it carries,
        and routing it here would narrow that to BARE_NUMBER and strand a
        trailing-command or replacement-prefix tail across shutdown.

        A hold survives a provider switch because nothing in the switch
        reaches the processor: the word queue and its consumer outlive
        both engines. The first word the NEW engine delivers opens a new
        utterance, and the new-utterance guard in _maybe_hold_bare_number
        flushes the standing hold as dictation -- so a number spoken to
        the engine that is gone is typed into whatever the user is
        working in, with no badge click and nothing on screen to connect
        it to. That guard is right for two utterances from one engine; it
        cannot see that the engine was replaced between them.

        Three limits, all deliberate, all left for later work.

        The two other tray paths that replaced the engine without
        dropping the hold are gone. LogicController's credentials handler
        and its hard STT restart, reached by the tray items "Google Cloud
        Credentials" and "Restart Transcription Service", were removed on
        David's order (wh-remove-restart-credentials-items,
        QUESTIONS-2026-09-04.md item 20), with the hold they stranded as
        one of his reasons (wh-provider-switch-stale-hold.1.3). The
        switch is now the only path here that replaces the engine.

        crewcut: this is a point-in-time clear, so it says nothing about
        words the outgoing engine has already sent
        (wh-provider-switch-stale-hold.1.2). The switch drops the hold as
        soon as the new engine is launched, but the old process keeps its
        socket until it exits, and outside a calibration session
        WebSocketManager does not filter transcript frames by client, so
        an old-engine word can still reach the queue afterwards and open
        a fresh hold or click a badge of its own. Removing the limit
        means giving those frames an identity the processor can reject --
        extending the active-client gate in websocket_manager.py beyond
        calibration, or draining the queue at the switch -- neither of
        which belongs to a change scoped to the hold.

        crewcut: the switch's proof of the exit is a bounded wait, so
        an outgoing process that outlives it is treated as still running
        and the hold is kept. If that process then exits, nothing drops
        the hold and the first word of the next engine still flushes it
        as dictation -- the original harm, in a narrower cell than
        before (wh-provider-switch-stale-hold.2.1). The wait is
        _PROVIDER_EXIT_WAIT_S in main.py, ten seconds. Lengthening it is
        not the way out, because no finite
        wait closes the cell and every extra second is a second the
        switch's own cleanup is still on the loop. Removing the limit
        needs the same thing the limit below needs: an identity on
        transcript frames that lets the processor reject the old
        engine's words outright, at which point the hold does not have
        to be dropped at the switch at all.

        crewcut: the held words are sacrificed rather than typed, the
        same trade-off stop() makes for its own reason. For the switch,
        typing them would keep what the user said, but a switch is often
        the user saying the transcription is not working, which makes the
        held words the least trustworthy text in the process; the
        handler's reason is at its call site and is a different one. To
        remove the limit for the switch, dictate the words here once a
        switch can tell "this engine misheard me" apart from "this engine
        is fine and I want the other one".

        Synchronous on purpose: it sends no IPC, so it cannot stall a
        caller that is midway through swapping engines.
        """
        pending = self._pending_bare_number_words
        deferred = self._bare_number_deferred_word
        # Assigning None to the property clears the held tail only while
        # the tail carries BARE_NUMBER, which is what the two slots
        # named here mean. A tail held for another kind is left alone.
        self._pending_bare_number_words = None
        self._bare_number_deferred_word = None
        if pending or deferred:
            pipeline_logger.info(
                "BARE-NUMBER candidate dropped reason=%s words=%r "
                "deferred=%r elapsed_ms=%.1f",
                reason,
                redact_transcript(" ".join(pending or [])),
                redact_transcript(deferred or ""),
                elapsed_ms(),
            )

    async def _flush_pending_bare_number_as_dictation(self) -> None:
        """Dispatch the held bare number as ordinary dictation.

        Called on every event that proves the held number was not the
        whole utterance. Clears the pending slot before the IPC so a
        failure cannot leak the word into the next consume path.
        """
        pending = self._pending_bare_number_words
        if not pending:
            return
        self._pending_bare_number_words = None
        self._bare_number_deferred_word = None
        pipeline_logger.info(
            "BARE-NUMBER candidate flushed-as-text words=%r elapsed_ms=%.1f",
            redact_transcript(" ".join(pending)), elapsed_ms(),
        )
        # One IPC per held word, in the spoken order. Sending the joined
        # text instead would change what a multi-word hold types.
        for held_word in pending:
            await self._send_to_dictation(held_word)

    async def _consume_pending_bare_number(self) -> None:
        """Execute the held bare number as the click command "click N".

        Called from the utterance-end marker handler, which proves the
        number was the whole utterance. Re-checks the overlay state at
        consume time: if the badges disappeared between hold and
        utterance end, the number is ordinary dictation after all.

        The click is synthesized as the command text "click <word>"
        (_held_bare_number_command: the spoken words for a bare hold, the
        number in digits for a spoken-click hold) and run through the
        same TextParser path a spoken "click N" takes, so downstream
        behaviour (state-aware badge routing, notices, trace ids) is
        identical. If the click pattern does not match -- a user pattern
        override changed it -- the fallback dictates what was actually
        SPOKEN (the bare number, or the whole spoken "click N"), never
        the synthesized text.
        """
        pending = self._pending_bare_number_words
        if not pending:
            return
        self._pending_bare_number_words = None
        self._bare_number_deferred_word = None
        if not self._overlay_accepts_bare_number():
            pipeline_logger.info(
                "BARE-NUMBER overlay no longer showing badges; dictating "
                "words=%r elapsed_ms=%.1f",
                redact_transcript(" ".join(pending)), elapsed_ms(),
            )
            for held_word in pending:
                await self._send_to_dictation(held_word)
            return
        if not await self._execute_bare_number_click(pending):
            for held_word in pending:
                await self._send_to_dictation(held_word)

    async def _execute_bare_number_click(self, words: list[str]) -> bool:
        """Run ``words`` as the click command. True when the click ran.

        The one place that turns a spoken bare number into a click. Two
        callers reach it: ``_consume_pending_bare_number`` for a number
        the hold captured, and ``try_bare_number_badge_click`` for a
        number the grid patterns claimed before the hold could see it
        (wh-overlay-count-homophones.1.1). Neither dictates from here --
        a False return tells the caller to type what was SPOKEN, which
        differs between them: the hold types its held words one by one,
        the grid path types the whole utterance through its own
        dictation fallback so the parse is recorded as a fallback.

        Returns False when the words name no number, and when the click
        command did not match -- a user pattern override can change it.
        """
        command_text = self._held_bare_number_command(words)
        if command_text is None:
            # The hold can open on the "number"/"numbers" filler, which
            # parses to None by itself, so a hold can reach this point
            # holding a word that is not a number. Synthesizing "click
            # number" would be worse than dictating: click_parser drops
            # that filler only when something parsing follows it
            # (click_parser.py:189), so a lone one resolves BY NAME and
            # would click an element called "number". Every hold before
            # the filler parsed on its own and could not reach here
            # (wh-click-number-dictation.1.2).
            pipeline_logger.info(
                "BARE-NUMBER held words do not parse as a number; dictating "
                "words=%r elapsed_ms=%.1f",
                redact_transcript(" ".join(words)), elapsed_ms(),
            )
            return False
        pipeline_logger.info(
            "BARE-NUMBER consumed as command=%r elapsed_ms=%.1f",
            redact_transcript(command_text), elapsed_ms(),
        )
        logger.info(
            f"Executing bare-number click: '{redact_transcript(command_text)}'"
        )
        executed = await self.text_parser.parse_and_execute(
            command_text, authorized_command=True,
        )
        if not executed:
            return False
        if self.text_parser.last_executed_pattern_type == "command":
            # A click is an irreversible side effect; a later STT
            # revision must not retract past it (same rule as
            # _execute_command).
            self._command_executed_in_utterance = True
        return True

    async def try_bare_number_badge_click(self, spoken: str) -> bool:
        """Click the badge ``spoken`` names, if badges are showing.

        wh-overlay-count-homophones.1.1. The three grid patterns match
        the whole utterance for every spoken 1..9, so the router
        classifies "six", "7", "number three", "to", "too" and "for" as
        COMMAND and finalizes them into ActionFunctions.
        grid_number_command. The bare-number hold lives under the
        DICTATE branch, so those words never reach it, and before this
        they typed while badges were showing. The grid actions call this
        from their closed-grid fallback, which keeps the precedence the
        grid already had: the grid consumes the number first, and the
        overlay is consulted only when the grid did not.

        Returns True when the click ran and the caller must NOT dictate.
        Returns False -- badges not showing, or the click did not run --
        and the caller types the words as before.
        """
        if not self._overlay_accepts_bare_number():
            return False
        return await self._execute_bare_number_click(spoken.split())

    # ========================================================================
    # HELD REPLACEMENT PREFIX (wh-trailing-question-mark-words)
    # ========================================================================
    #
    # Third hold in this file, built on the same slot-plus-flush-plus-
    # consume shape as the two above it.
    #
    # The problem it solves is a timing race, not a matching one. The STT
    # server always holds back the last confirmed word
    # (services/stt_providers/.../audio_processor.py), so the completing
    # word of a two-word replacement can reach the router later than the
    # word in front of it. When sherpa's own endpoint wins the race
    # against the silence release, that gap exceeds
    # ``replacement_timeout_ms`` and the buffer timer finalizes first.
    # The router then reports DICTATE ("Finalized as dictation") for a
    # buffer that was one word short of a replacement, and a trailing
    # "question mark" types as two spoken words.
    #
    # So a REPLACEMENT-mode buffer that finalizes to plain dictation is
    # held rather than typed, and is then either:
    #   - completed, when the next real word of the same utterance turns
    #     the held words plus that word into a replacement match. Both
    #     are consumed by the resulting EXECUTE decision, whose
    #     before_remainder handling is what makes a multi-word hold such
    #     as ["hello", "question"] type "hello" and then "?";
    #   - flushed as ordinary dictation, on anything else: a word that
    #     does not complete a replacement, a word that opens a NEW
    #     utterance, a marker event, or the utterance-end marker.
    #
    # The second pass always runs with MID_REPLACEMENT_BUFFERING, which
    # is what passes allow_commands=False into the router's
    # _resolve_finalization. A buffer the router already declined to
    # treat as a command must not become one on a retry.

    def _should_hold_replacement_prefix(
        self,
        words: list[str],
        *,
        hotword_active: bool,
        end_of_utterance: bool,
    ) -> bool:
        """Decide whether text about to be dictated must be held instead.

        wh-okay-prefix-splits-replacement. The one gate all three arming
        sites ask, so they cannot drift apart: the timeout sentinel, the
        word-driven finalization, and the remainder tail.

        Holds when the LAST word could still grow into a replacement. The
        timing race the hold was built for is not the only way the pair gets
        split -- a command that came first splits it at word speed, with no
        timer involved. "okay question mark" typed "okay question" and then
        "mark" because ``^okay Google.*$`` opened a command buffer that
        "question" disproved (trace T-17877987532).

        Two conditions refuse the hold:

        hotword_active. The one concrete reason belongs to the timeout site
        and is recorded where it arms: that site holds the RAW buffer, and
        ``_resolve_finalization`` step 3 rebuilds a hotword buffer's dictation
        payload with the wake word in front (router.py), so holding the raw
        buffer would drop the word the user spoke. Sites 2 and 3 hold the
        rebuilt payload and have no such defect; for them the refusal is
        conservative, and reviewer_0 measured that no shipped case reaches
        either site with a holdable word.

        Two mechanisms that might look like reasons here are NOT, both read
        rather than assumed (reviewer_0 round 1, finding .1.2). The second
        pass cannot turn a held buffer into a command: it runs with
        MID_REPLACEMENT_BUFFERING, which passes ``allow_commands=False``, and
        that skips both the command match and the command-prefix probe. Nor is
        the second pass less authorized for REPLACEMENTS: step 2 of
        ``_resolve_finalization`` passes ``hotword_active=False`` as a
        constant, so every FINALIZATION matches replacements the same way,
        first pass and retry alike. Both passes here are finalizations, which
        is what makes them comparable. Note the narrower claim: the buffering
        path is different. ``_decide_buffering`` sets ``target_type`` to
        "replacement" in the replacement buffering modes and hands the live
        ``hotword_active`` to that match, so the hotword state does reach
        replacement matching there.

        end_of_utterance, EXCEPT for an exact opening of a replacement
        name. The word event already closed the utterance, so any word
        arriving next opens a NEW one. That used to end the matter: a new
        utterance flushed the hold as dictation rather than completing it,
        so such a hold could never finish and waiting out the release
        deadline only delayed the text (adopted 2026-08-28 on the
        measurement recorded on the bead).
        wh-spaced-punctuation-names-unresolved.3 removed that reason for
        one narrow case. ``_resolve_pending_replacement_prefix`` now lets
        a word that opens a new utterance COMPLETE a held exact opening
        of a replacement name, so "open" / "single" / "quote" said as
        three utterances reaches the one mark it names. Such a hold CAN
        finish, so the refusal no longer applies to it.

        Two conditions keep the exception narrow, and both are needed:

        the words must be an exact opening of a replacement name, not
        merely end in a word that could grow into one. "hold it open"
        ends on a name's first word but is not an opening of anything,
        so it dictates at once as before; and

        the words must be the WHOLE utterance. A tail is not a name the
        user began: "I said back space" ends on the opening of
        ``space bar``, and holding it would make an ordinary sentence
        wait out the release deadline before its last word appeared
        (tests/e2e/test_e2e_utterance_end_replacement.py
        ::test_a_late_command_collision_pair_types_instead_of_executing).
        A user who really pauses inside a name says the opening on its
        own, which is the shape the bead recorded.

        A held word lives in Logic-process memory until a completing word,
        the utterance-end marker, or the release deadline. If Logic dies in
        that window the words are lost: the provider has already finalized
        the utterance and nothing replays it. The pre-existing holds have the
        same shape, but these two sites extend that window to the
        command-first paths, which typed at word speed before this change
        (reviewer_0 round 1, finding .1.3).

        crewcut: this whole hold is a stop-gap for word-at-a-time matching.
        Option C on wh-whole-utterance-command-matching matches commands and
        replacements against the complete utterance, which removes the split
        this hold repairs -- and with it the crash window above. Delete the
        three arming sites and this gate when Option C lands.
        """
        if hotword_active or not words:
            return False
        if end_of_utterance:
            # The one shape that may hold across a confirmed utterance
            # end: the whole utterance is an exact opening of a
            # replacement name, so the next utterance's word can finish
            # it. A payload that is only a TAIL of the utterance stays
            # refused -- "I said back space" ends on a word that opens
            # ``space bar``, and delaying it would make an ordinary
            # sentence wait out the release deadline for a name the user
            # never began.
            return (
                self._payload_is_the_whole_utterance(" ".join(words))
                and self.router.is_incomplete_replacement_name(words)
            )
        return self.router.is_incomplete_replacement_prefix(words[-1])

    async def _resolve_pending_replacement_prefix(
        self, word_event: WordEvent
    ) -> bool:
        """Give the held replacement words one more pass, using ``word_event``.

        Returns True when the event was fully consumed (the held words
        and the arriving word together matched a replacement, and the
        EXECUTE decision has already run). The caller must then stop
        processing the event. Returns False in every other case, with
        the slot cleared and the held words dictated, so the caller
        goes on to process the event normally.
        """
        held = self._pending_replacement_prefix
        if held is None:
            return False

        # Marker events carry word="" and cannot complete anything.
        if not word_event.word:
            await self._flush_pending_replacement_prefix_as_dictation()
            return False

        # wh-spaced-punctuation-names-unresolved.3, corrected by .3.1.
        # A word that opens a NEW utterance used to flush the hold
        # unconditionally, which is what made every paused punctuation
        # name type its words.
        #
        # The first version of this guard let such a word join only when
        # it ALSO ended its own utterance. That condition cannot be
        # asked on the path the program ships. Every real word
        # integrations/websocket_manager.py builds carries
        # end_of_utterance=False; at all seven of its
        # end_of_utterance=True sites the event is the empty end marker.
        # The one path that set the flag on a real word was the
        # in-process bridge (main.py _handle_stt_transcript), and
        # wh-in-process-capture-removal deleted it, so nothing sets it
        # on a real word now. So on a fresh install the
        # join never happened and the bead's own filed case still typed
        # its words -- "open" then "bracket" produced the two literal
        # words, and "open" / "single" / "quote" produced "open single"
        # and an empty double-quote wrap, which is the failure the bead
        # was filed for.
        #
        # A word that opens a new utterance is instead treated exactly
        # as the utterance-end marker is treated in
        # _consume_held_tail_at_utterance_end, and for the reason
        # _advance_held_tail already states about this same flag: the
        # previous utterance ended, and a lost end marker is a real
        # failure mode rather than a theoretical one.
        #
        #   held words that already spell a complete name fire it. The
        #   marker that would have fired it can be lost, and this word
        #   is the same proof that its utterance is over.
        #
        #   held words that are an exact opening of a name join this
        #   word, tentatively -- see the elif below -- but ONLY when the
        #   hold was armed across a confirmed utterance end. Both halves
        #   are needed. Fusing two utterances is only ever right for the
        #   case the bead names, never for an ordinary tail that merely
        #   ends in a holdable word, which is what the exact-opening
        #   test refuses. And a hold the timeout sentinel armed in the
        #   MIDDLE of an utterance is still waiting for a word in that
        #   same utterance; a new utterance's first word proves that
        #   word is not coming, so those held words are the user's text
        #   and they flush. That is the same distinction
        #   _consume_held_tail_at_utterance_end draws with the same
        #   record, and it is what
        #   tests/e2e/test_e2e_utterance_end_replacement.py
        #   ::test_a_new_utterance_never_completes_the_old_replacement
        #   pins: "question" finalized by its timer with its end marker
        #   lost, then "mark" opening the next utterance, types both
        #   words.
        #
        #   anything else flushes as dictation, as before.
        #
        # The complete-name test above is deliberately NOT narrowed the
        # same way. A complete name is not waiting for a further word at
        # all; it is waiting only for proof that its utterance is over,
        # and this word is that proof whichever site armed the hold.
        # _consume_held_tail_at_utterance_end fires a complete name
        # before it consults the same record, for the same reason.
        if word_event.start_of_utterance:
            if await self._fire_held_replacement_if_complete():
                return False
            if not (
                self._prefix_hold_crossed_utterance_end
                and self.router.is_incomplete_replacement_name(held)
            ):
                await self._flush_pending_replacement_prefix_as_dictation()
                return False
        elif self._prefix_hold_joined_new_utterance and not (
            self.router.is_incomplete_replacement_name(held)
        ):
            # The other half of the tentative join. These held words
            # were completed or extended by a word that OPENED this
            # utterance, those words already spell a whole name, and
            # the utterance has now kept going, so it is ordinary
            # dictation. This is the same question the dropped
            # end_of_utterance condition asked, moved one event later,
            # which is the earliest point the shipped path can answer
            # it. "question" then "mark my words" still types four
            # words: the held "question mark" dictates here and "my"
            # routes normally, joining "words" in its own utterance's
            # buffer.
            #
            # wh-spaced-punctuation-names-unresolved.3.1.3, codex round
            # 2. The condition on the HELD words is what this branch was
            # missing; it used to fire whenever the record was set.
            # Without it the branch flushed on the very word that
            # FINISHES the name: "open", a pause, then "single quote"
            # held ["open", "single"], recorded the join as tentative,
            # and then dictated those two words when "quote" arrived.
            # All eight three-word names in the catalog failed that way,
            # and the orphaned last word then routed on its own --
            # "quote" reached the greedy double-quote action and wrapped
            # an empty pair.
            #
            # Held words that are still an unfinished opening are
            # waiting for their next word, so this one may be it and the
            # combining code below decides. Held words that ALREADY
            # spell a name are waiting for nothing, so a further word in
            # the same utterance is the proof that the user was
            # dictating: held "question mark" plus "my" types its words,
            # and held "open single quote" plus "please" types four
            # words rather than inserting the mark and swallowing
            # "please".
            #
            # The ARRIVING word is deliberately not consulted here. A
            # first version of this fix also asked whether held + [word]
            # was still on its way to a name, and that test was measured
            # dead on 2026-09-05: with it removed, all 103 tests in
            # test_pipeline_punctuation_names.py, test_held_tail.py,
            # test_utterance_word_list.py and
            # e2e/test_e2e_utterance_end_replacement.py still passed. It
            # cannot change an outcome because the combining code below
            # asks the same question of the same words -- when the
            # arriving word leads nowhere, ``is_incomplete_replacement
            # _name(combined)`` is False there and the hold flushes as
            # dictation with the word left for the caller, which is
            # exactly what an extra test here would have done one step
            # earlier. A branch no test can distinguish is a permanent
            # mutation survivor, so it is not carried.
            await self._flush_pending_replacement_prefix_as_dictation()
            return False

        # A hold that was already tentative STAYS tentative while it
        # grows. Reading only start_of_utterance here would clear the
        # record on the very word that extended the name, and the words
        # after a finished name would then be absorbed into it:
        # "open" then "single quote please" would insert the mark and
        # swallow "please" instead of typing four words.
        joined_new_utterance = (
            word_event.start_of_utterance
            or self._prefix_hold_joined_new_utterance
        )
        combined = held + [word_event.word]
        decision = self.router.decide_timeout(
            combined,
            hotword_active=False,
            mode=ProcessingMode.MID_REPLACEMENT_BUFFERING,
        )
        if decision.action != Action.EXECUTE:
            # wh-spaced-punctuation-names-unresolved.3: a three-word name
            # said with pauses reaches here at its middle word. "open" +
            # "single" spells nothing yet but is still the exact opening
            # of "open single quote", so keep holding and wait for the
            # last word. The hold keeps the SAME release deadline -- no
            # timer is armed here -- so a growing hold cannot outlive the
            # one replacement_timeout_ms bound its arming site set.
            if self.router.is_incomplete_replacement_name(combined):
                self._pending_replacement_prefix = combined
                self._prefix_hold_joined_new_utterance = (
                    joined_new_utterance
                )
                pipeline_logger.info(
                    "REPLACEMENT-PREFIX extended text=%r elapsed_ms=%.1f",
                    redact_transcript(" ".join(combined)), elapsed_ms(),
                )
                return True
            await self._flush_pending_replacement_prefix_as_dictation()
            return False

        # wh-spaced-punctuation-names-unresolved.3 (the ruling on the
        # bead): a completed name commits its mark only when the
        # completing word ENDS its utterance. A completing word that does
        # not is still inside an utterance that may keep going, so the
        # pair is held under the same deadline and the rest of the
        # utterance is decided together with it. The utterance end or the
        # deadline then closes the hold.
        #
        # What this does NOT do, measured 2026-09-05 and stated here
        # because the first version of this comment claimed it: a further
        # word does not flush the pair as dictation. Each longer list is
        # put to decide_timeout again, and for "question mark my words"
        # that keeps returning EXECUTE -- the shipped "a name in the
        # middle of an utterance inserts its mark" behaviour that
        # TestTheNameInTheMiddleOfAnUtterance pins. The measured output is
        # ['?', 'my words'], which is the same text the unpaused sequence
        # already produces (['?', 'my', 'words']), and that agreement is
        # the point: a pause inside one utterance must not change the
        # result.
        #
        # The separation case the bead names -- "question" and "mark my
        # words" as two SEPARATE utterances -- is refused by the
        # tentative record this branch keeps
        # (_prefix_hold_joined_new_utterance, set from
        # joined_new_utterance). The guard above cannot refuse it any
        # more, because on the shipped path the arriving word carries
        # nothing that tells a one-word utterance from a longer one;
        # the elif in that guard refuses it on the NEXT word instead.
        if not word_event.end_of_utterance:
            self._pending_replacement_prefix = combined
            self._prefix_hold_joined_new_utterance = joined_new_utterance
            pipeline_logger.info(
                "REPLACEMENT-PREFIX completed-and-held text=%r "
                "elapsed_ms=%.1f",
                redact_transcript(" ".join(combined)), elapsed_ms(),
            )
            return True

        # Clear the slot before the IPC so a failure inside the decision
        # cannot leak the words into a later flush or consume path.
        self._pending_replacement_prefix = None
        self._last_replacement_surface = None
        pipeline_logger.info(
            "REPLACEMENT-PREFIX completed text=%r payload=%r elapsed_ms=%.1f",
            redact_transcript(" ".join(combined)),
            redact_transcript(decision.payload or ""),
            elapsed_ms(),
        )
        await self._execute_decision(decision)
        self._record_replacement_delivery(combined)
        return True

    async def _fire_held_replacement_if_complete(self) -> bool:
        """Insert the mark the held words spell, when they spell one.

        wh-spaced-punctuation-names-unresolved.3. The held slot can now
        carry a COMPLETE replacement name -- ``question mark`` whose
        second word did not end its utterance -- so the two paths that
        close a hold without a further word (the release deadline and the
        utterance-end consume) ask this first. Returns True when the held
        words were consumed as the replacement; False leaves the slot
        untouched for the caller's own handling.

        This is NOT a give-up path, so it does not owe the words to
        ``_send_to_dictation``: it delivers exactly what the user asked
        for. The callers keep that obligation for their False branch.
        """
        held = self._pending_replacement_prefix
        if held is None:
            return False
        decision = self.router.decide_timeout(
            held,
            hotword_active=False,
            mode=ProcessingMode.MID_REPLACEMENT_BUFFERING,
        )
        if decision.action != Action.EXECUTE:
            return False
        # Clear the slot before the IPC, the same ordering the completion
        # and flush paths use, so a failure cannot leak the words into a
        # later flush or consume path.
        self._pending_replacement_prefix = None
        self._last_replacement_surface = None
        pipeline_logger.info(
            "REPLACEMENT-PREFIX fired-on-close text=%r payload=%r "
            "elapsed_ms=%.1f",
            redact_transcript(" ".join(held)),
            redact_transcript(decision.payload or ""),
            elapsed_ms(),
        )
        await self._execute_decision(decision)
        self._record_replacement_delivery(held)
        return True

    async def _send_end_utterance(self, utterance_id) -> None:
        """Send the end_utterance IPC, recording what it leaves exposed.

        wh-spaced-punctuation-names-unresolved.3.1.2: the Input side
        resets its paste counter on this IPC
        (ui/ui_action_handler.py:1170), so words already delivered
        before it have left the retractable span for good. Words still
        HELD here are the ones that can cross into the next span, and
        this is the only moment at which they can be told apart from
        the new utterance's own words -- so the candidate is taken
        here, which is why all three end_utterance sends go through
        this one method.

        Whether the candidate actually reaches the screen is a
        different question, answered at delivery in
        ``_flush_pending_replacement_prefix_as_dictation``. This IPC is
        not proof of anything being typed (codex round 3, case C).
        """
        # Anything delivered before this reset can never be retracted
        # again, so the confirmed record goes.
        self._earlier_utterance_words_in_retract_span = []
        self._earlier_utterance_words_generation = None
        self._earlier_utterance_words_surface = None
        held = self._pending_replacement_prefix
        self._words_held_across_utterance_end = list(held) if held else []
        await self.app.send_command({
            'action': 'end_utterance',
            'params': {'utterance_id': utterance_id},
        })

    def _earlier_words_still_in_live_span(
        self, current: Optional[int],
    ) -> list[str]:
        """The record's words, while the span they were typed into lives.

        wh-spaced-punctuation-names-unresolved.3.1.7, codex round 5.
        The record names text a correction is about to remove, and that
        is only true while no ``start_utterance`` has gone out since the
        delivery that earned it: the Input side resets its paste counter
        on every one (ui/ui_action_handler.py:1126), and text typed
        before a reset can never be retracted again. Restoring it would
        type it a SECOND time, beside the copy the user can still see.

        ``current`` IS THE CALLER'S NUMBER, NOT ONE THIS READS, and
        that is the whole of .3.1.8 (codex round 6). The legacy arm
        captures the count in the same breath as the retract's
        enqueue, because both messages travel on the one outbound
        queue and the Input side consumes them in arrival order
        (input_proc.py:2140), so a start sent while the request waits
        sits BEHIND the retract -- the Input side removes the earlier
        words first and resets afterwards. Reading the count after
        that response saw the later start and suppressed a restoration
        the screen needed.

        THE LEGACY ARM IS THE ONLY CALLER. The editor arm does not
        call this at all, because the count governs the Input paste
        counter and has no authority over the editor ledger (.3.1.9,
        codex round 6). Its own reason is written where it restores,
        in ``_restore_earlier_utterance_words``, and rests on
        ``CreditLedger.retract_all_and_replay`` returning
        ``RetractResult(0, 0, FAILURE_SESSION_MISMATCH)`` on a session
        mismatch (shared/ledger.py:445).

        ``_clear_record_delivered_before`` asks a narrower question at
        a narrower moment, and THIS CHECK ALONE GIVES THE SAME ANSWER
        for every input the shipped app can produce. That clear
        compares against the number the WORD has carried since it was
        queued (integrations/websocket_manager.py:1318 and :1555), so a
        ``start_utterance`` sent after that enqueue -- while the word
        still waits in ``word_queue`` -- moves the count without moving
        the word's number. And it runs from one place, the
        start-of-utterance branch at :1173, so a correction processed
        before the next utterance's first word never reaches it.

        Where that clear drops a STAMPED record, this check drops the
        same record. Its ``recorded < started`` cannot hold while
        ``recorded`` equals ``current``, because the count only rises
        (app.py:1133), so a recorded number below the word's stamp is
        also below any later reading of the count. The two can answer
        differently only on the unstamped inputs described next.

        A missing number restores, which is the behaviour every caller
        had before this check existed. THE TWO SITES DIFFER ON PURPOSE:
        ``_clear_record_delivered_before`` drops an unstamped record and
        this keeps it. Each falls back to what its own moment did before
        the numbers existed, and neither fallback can be reached by the
        shipped app: ``WheelHouseApp`` sets the count at app.py:297 and
        moves it at app.py:1133, so a missing number means an app that
        keeps no count at all -- a test stand-in, never production.
        """
        earlier = self._earlier_utterance_words_in_retract_span
        if not earlier:
            return []
        # .3.1.10, codex round 7. THIS ARM OWNS THE LEGACY SURFACE
        # ONLY. Every question below is about the Input paste span, so
        # asking any of them about words the EDITOR typed is asking the
        # wrong screen: the legacy retract removes nothing of theirs,
        # and restoring them would type a second copy beside the one
        # still in the editor document. The record is not dropped here
        # -- it still describes live editor text that an editor
        # correction of this same utterance has to restore.
        surface = self._earlier_utterance_words_surface
        if surface is not None and surface != "legacy":
            pipeline_logger.info(
                "RETRACT keeping earlier-utterance words off the legacy "
                "restore: they were delivered on surface=%s "
                "elapsed_ms=%.1f",
                surface, elapsed_ms(),
            )
            return []
        recorded = self._earlier_utterance_words_generation
        if recorded is None or current is None or recorded == current:
            return earlier
        pipeline_logger.info(
            "RETRACT dropping earlier-utterance words: a later start "
            "took them out of the span recorded=%s current=%s "
            "elapsed_ms=%.1f",
            recorded, current, elapsed_ms(),
        )
        self._earlier_utterance_words_in_retract_span = []
        self._earlier_utterance_words_generation = None
        self._earlier_utterance_words_surface = None
        return []

    def _restore_earlier_utterance_words(self, replay_text: str) -> str:
        """Put the earlier utterance's words back in front of the replay.

        The retract just removed the whole credited span, and part of
        that span belonged to an utterance the correction says nothing
        about. What goes back is the SPOKEN words, never the delivered
        text: the replay puts them through ``process_word_event``, so a
        pair that produced a mark produces that same mark again, and a
        pair that was dictated is dictated again.

        The record deliberately survives this call: the replay
        re-credits those characters into the same live span, so a
        chained correction of the same utterance has to restore them
        again. Only the next end_utterance clears it.

        THIS ARM RESTORES UNCONDITIONALLY. It asks no question about
        the Input side's start_utterance count, and .3.1.9 (codex
        round 6) is what that question cost when it was asked here.
        The count governs the Input paste counter; the editor session
        is governed by the ledger's own utterance id, set from
        ``show_editor`` and the implicit-start path
        (terminal_editor_window.py:411, :413 and :611). A later
        start_utterance moves the count without touching the ledger,
        so the editor retract still peels the earlier words' runs
        while the guard was suppressing their replay.

        Restoring cannot double text ON THIS SURFACE. The decision is
        made before the retract, so it cannot know the ledger's
        answer -- but it does not need to: on a session mismatch
        ``CreditLedger.retract_all_and_replay`` returns
        ``RetractResult(0, 0, FAILURE_SESSION_MISMATCH)``
        (shared/ledger.py:445), removing nothing and installing
        nothing, so the extra words never reach the screen.

        THE ONE QUESTION IT DOES ASK IS WHICH SURFACE TYPED THEM
        (.3.1.10, codex round 7). The session defence above covers a
        ledger that does not own the CORRECTION; it says nothing about
        words that never entered the ledger at all. When the earlier
        words went out on the LEGACY path and a later word of the same
        utterance opened the editor, the session MATCHES, the ledger
        peels only the correction's own run, and prepending would type
        the earlier words into the editor while the legacy copy stays
        on screen untouched. The record survives either way: it still
        describes live Input text that a legacy correction of this
        same utterance has to restore.
        """
        earlier = self._earlier_utterance_words_in_retract_span
        if not earlier:
            return replay_text
        surface = self._earlier_utterance_words_surface
        if surface is not None and surface != "editor":
            pipeline_logger.info(
                "RETRACT keeping earlier-utterance words off the editor "
                "replay: they were delivered on surface=%s "
                "elapsed_ms=%.1f",
                surface, elapsed_ms(),
            )
            return replay_text
        pipeline_logger.info(
            "RETRACT restoring earlier-utterance words text=%r "
            "elapsed_ms=%.1f",
            redact_transcript(" ".join(earlier)), elapsed_ms(),
        )
        return " ".join(earlier + replay_text.split())

    async def _redeliver_earlier_utterance_words(
        self, start_count_at_retract: Optional[int],
    ) -> bool:
        """Type the earlier utterance's words back, as text.

        The retract just removed the whole credited span, and part of
        that span belonged to an utterance the correction says nothing
        about. Those words go back through the same dictation path
        that delivered them the first time, so what the user sees
        again is what the user saw before: text that was dictated
        literally is dictated literally, and text that was a mark is
        the same mark.

        This deliberately does NOT hand the words to the replay loop.
        That loop labels everything it receives as a single utterance,
        so the restored words would sit beside the corrected final
        with no boundary between them and the in-utterance name
        matcher would be free to join the two (codex round 3, case B).

        The record survives this call: the redelivery re-credits those
        characters into the same live span, so a chained correction of
        the same utterance has to restore them again. Only the next
        start_utterance or end_utterance clears it, and a
        start_utterance that had ALREADY gone out when this retract was
        ISSUED takes those words out of the span altogether (.3.1.7,
        and .3.1.8 for the moment that word "issued" names).

        THIS IS THE ONLY GUARDED ARM. The editor arm asks no such
        question, because the count governs the Input paste counter and
        not the editor ledger (.3.1.9; see
        ``_restore_earlier_utterance_words``).

        Returns True when words were actually sent, which tells the
        caller its replay is a continuation rather than a fresh
        sentence.
        """
        earlier = self._earlier_words_still_in_live_span(
            start_count_at_retract,
        )
        if not earlier:
            return False
        restored = " ".join(earlier)
        pipeline_logger.info(
            "RETRACT restoring earlier-utterance words text=%r "
            "elapsed_ms=%.1f",
            redact_transcript(restored), elapsed_ms(),
        )
        return await self._send_to_dictation(restored) is not None

    def _arm_replacement_prefix_release(
        self, *, across_utterance_end: bool
    ) -> None:
        """Arm the ONE release deadline a freshly held prefix gets.

        The three arming sites called ``_start_timeout`` directly. They
        call this instead so the bound and the
        ``_prefix_hold_crossed_utterance_end`` record are set together
        and cannot drift apart (wh-spaced-punctuation-names-unresolved.3).

        ``across_utterance_end`` is the same value the site passed to
        ``_should_hold_replacement_prefix`` as ``end_of_utterance``: the
        utterance was already over when the hold was armed, so the word
        that completes it can only come from the next utterance. Only
        such a hold survives an utterance-end marker; a hold armed
        mid-utterance (the timeout sentinel's, whose sentinel event
        carries ``end_of_utterance=False``) is still closed by the
        marker exactly as before.

        The deadline itself is unchanged: one ``replacement_timeout_ms``
        timer, armed once here, never re-armed while the hold grows.
        """
        self._prefix_hold_crossed_utterance_end = across_utterance_end
        self._prefix_hold_joined_new_utterance = False
        self._start_timeout(self.replacement_timeout_ms)

    async def _flush_pending_replacement_prefix_as_dictation(self) -> None:
        """Dispatch the held replacement words as ordinary dictation.

        Called on every event that proves no completing word is coming.
        Clears the pending slot before the IPC so a failure cannot leak
        the words into a later consume path. Safe to call when the slot
        is already empty.
        """
        pending = self._pending_replacement_prefix
        if pending is None:
            return
        self._pending_replacement_prefix = None
        text = " ".join(pending)
        pipeline_logger.info(
            "REPLACEMENT-PREFIX flushed-as-text text=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        # Same dictation routing the regular DICTATE branch uses, so the
        # held text follows the focus-redirect policy.
        surface = await self._send_to_dictation(text)
        # wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3.
        # This is the moment, and the only moment, at which the words
        # that crossed the previous end_utterance are known to be on
        # screen. A refusal by the screen-read gate returns None here
        # and records nothing, which is what stops a later correction
        # from typing words the user never saw (case C).
        self._record_delivered_earlier_utterance_words(surface, len(pending))

    def _read_start_utterance_count(self) -> Optional[int]:
        """The app's running count of start_utterance commands sent.

        wh-spaced-punctuation-names-unresolved.3.1.5. The Input side
        resets its paste counter on every start_utterance
        (ui/ui_action_handler.py:1126), and the processor never sees
        that command, so this count is how a delivery learns which
        reset it falls between. ``None`` from an app that does not keep
        the count means "unknown", and the TWO readers then fall back
        in OPPOSITE directions, each to what its own moment did before
        the numbers existed: ``_clear_record_delivered_before`` drops
        the record, and ``_earlier_words_still_in_live_span`` keeps it.
        This sentence said that every comparison falls back to clearing
        until 2026-09-06, which held only while the clear was the one
        reader.

        THE SECOND READER NO LONGER CALLS THIS ITSELF (.3.1.8, codex
        round 6). ``_earlier_words_still_in_live_span`` takes the
        number from its caller, because the two callers need it read at
        different moments: the legacy arm captures it as the retract is
        enqueued, and the editor arm reads it at its own call. The
        fallback directions above are unchanged.
        """
        return getattr(self.app, 'utterance_start_generation', None)

    def _clear_record_delivered_before(self, word_event) -> None:
        """Drop the record when the new utterance's start outran it.

        wh-spaced-punctuation-names-unresolved.3.1.2 case A and
        .3.1.5 are the same moment seen from two sides, and only these
        numbers separate them. WebSocketManager sends start_utterance
        and only then queues this word
        (integrations/websocket_manager.py:1295 and :1541), so by the
        time this word is consumed the Input side has already reset its
        paste counter. What the processor cannot see is whether its own
        last delivery went out before or after that command:

          case A -- the hold expired in the gap after the previous
          end_utterance. Its text left the span when this
          start_utterance reset the counter, so restoring it would type
          the word a SECOND time. The record must go.

          .3.1.5 -- the release sentinel was already in word_queue when
          start_utterance went out, so its flush landed INSIDE this new
          span. The correction that empties this span must put those
          words back. The record must stay.

        The record's number is the count read at its delivery; the
        word's number is the count just after its own start_utterance
        was enqueued. Strictly smaller means the delivery came first,
        which is case A. Equal means the delivery came after, which is
        .3.1.5. Either number missing means no producer stamped it, and
        the record goes -- the pre-.3.1.5 behaviour, which errs toward a
        LOST word rather than a doubled one. That direction was stated
        backwards here until 2026-09-06. Traced at 423921a9^: the whole
        of this branch was an unconditional
        ``self._earlier_utterance_words_in_retract_span = []``, and
        dropping the record is what makes the correction put nothing
        back. Case E in tests/e2e/test_e2e_held_prefix_restore_scope.py
        carries the measurement from that code -- screen 'This', the
        flushed prefix gone.

        Equal is NOT proof on its own that the record is still live:
        the word's number is the one it was queued with, so a
        start_utterance sent while it waited leaves the two equal with
        the span already reset (.3.1.7).
        ``_earlier_words_still_in_live_span`` is what both restores
        ask, and it is the check that has to be right.

        THIS DROP IS REDUNDANT IN FACT, not merely in appearance. That
        guard gives the same answer for every input the shipped app can
        produce, and the proof is a measurement: the gate mutation that
        disabled this call survived the full sweep at d1b003f6 on
        2026-09-06 with no failing test at all. The mutation was
        retired for that reason. This call stays because it costs
        nothing and frees the record at the boundary rather than at the
        next correction (ruling, 2026-09-06).
        """
        started = getattr(word_event, 'utterance_start_generation', None)
        recorded = self._earlier_utterance_words_generation
        if started is None or recorded is None or recorded < started:
            self._earlier_utterance_words_in_retract_span = []
            self._earlier_utterance_words_generation = None
            self._earlier_utterance_words_surface = None

    def note_replacement_text_delivered(
        self, surface: Optional[str], generation: Optional[int],
    ) -> None:
        """Record what a replacement's insertion step actually did.

        wh-spaced-punctuation-names-unresolved.3.1.6, boss ruling
        2026-09-06. ``command_engine._execute_rule`` owns the two arms
        that can put a replacement on screen -- the hidden editor
        (command_engine.py:405-437) and the ordinary insertion after
        the screen-read gate (:463-511 and :546-600) -- so the outcome
        is reported from there rather than guessed here. A guess made
        before the decision runs can disagree with what the rule did,
        and the editor arm is exempt from the gate, so that is exactly
        where a guess would diverge.

        ``surface`` is ``None`` when the step typed nothing.
        ``generation`` is the start_utterance count read immediately
        before that step's enqueue.
        """
        self._last_replacement_surface = surface
        if surface is not None:
            self._last_delivery_generation = generation

    def _record_replacement_delivery(
        self, spoken_words: list[str],
    ) -> None:
        """Promote the boundary candidate when held words became a mark.

        wh-spaced-punctuation-names-unresolved.3.1.6, codex round 4.
        ``_record_delivered_earlier_utterance_words`` had exactly one
        caller, the literal dictation flush, so a hold that resolved
        into a MARK promoted nothing at all. The mark is still text
        inside the live retractable span, and part of it was spelled by
        the earlier utterance's words, so a correction that removes the
        mark owes those words back exactly as a correction that removes
        dictated text does.

        ``spoken_words`` is what this delivery consumed, and its LENGTH
        is what travels on -- earlier SPOKEN words, never characters of
        the one-character mark. The record names spoken words, and the
        restore replays them through ``process_word_event``, so a pair
        that produced a mark produces that same mark again.

        The end-marker consume path also arrives here, and promoting
        there is harmless: its caller sends ``end_utterance`` straight
        afterwards, and that send empties the record and re-takes an
        empty candidate.

        The surface comes from ``note_replacement_text_delivered``,
        which the insertion step itself calls, so a step that typed
        nothing records nothing and codex round 3 case C stays fixed.
        The caller clears the slot before running the decision, so a
        rule that never reached an insertion step leaves it ``None``.
        """
        self._record_delivered_earlier_utterance_words(
            self._last_replacement_surface, len(spoken_words),
        )
        self._last_replacement_surface = None

    def _record_delivered_earlier_utterance_words(
        self, surface, delivered_word_count: int,
    ) -> None:
        """Confirm the boundary candidate once it has really been typed.

        wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3.
        ``_send_end_utterance`` names the candidate: the words that
        were still held when the previous utterance closed. Only a
        delivery can promote that candidate into a record of text
        inside the live retractable span, and only the words that were
        actually part of this delivery.

        ``surface`` is where the text actually landed: ``"editor"`` or
        ``"legacy"``, from ``_send_to_dictation``'s return or from
        ``note_replacement_text_delivered`` when the held words left as
        a mark instead of as text (speech/command_engine.py:434, :551
        and :598 pass those same two strings). ``None`` means nothing
        was typed, so nothing is recorded.

        THE SURFACE IS RECORDED, NOT ONLY READ (.3.1.10, codex round
        7). Both surfaces put text somewhere a correction can remove,
        but they are two different somewheres -- the legacy path lands
        inside the Input paste counter, the editor path in credit
        ledger runs -- and only the correction on the matching surface
        removes these words. A restore on the other surface adds a
        second copy beside the one still on screen, so each arm reads
        this field and restores only its own.

        The candidate is consumed either way. It described one hold at
        one boundary, and that hold has now resolved.
        """
        candidate = self._words_held_across_utterance_end
        self._words_held_across_utterance_end = []
        if not candidate or surface is None:
            return
        # The hold can only have grown since the boundary, never
        # shrunk, but a delivery shorter than the candidate would mean
        # the two have drifted -- record only what this delivery
        # actually carried.
        self._earlier_utterance_words_in_retract_span = candidate[
            :delivered_word_count
        ]
        self._earlier_utterance_words_generation = (
            self._last_delivery_generation
        )
        self._earlier_utterance_words_surface = surface

    async def _execute_command(
        self, command_text: str, *, hotword_authorized: bool = False
    ):
        """Execute command text via TextParser.

        hotword_authorized carries the buffer's hotword state, captured
        before _reset_to_idle cleared it. Passing it through (instead of
        a blanket True) makes the engine's first-match walk apply the
        SAME hotword gate the router applied, so both layers resolve to
        the same pattern. A blanket True let switch-to-app (hotword
        required, earlier in the file) steal every "go to ..." buffer
        the router had resolved to cursor-navigate while the hotword was
        inactive (wh-voice-access-parity.3.5). Remainder processing must
        still NOT authorize (wh-qj70s).
        """
        pipeline_logger.info(
            "EXECUTING command=%r elapsed_ms=%.1f",
            redact_transcript(command_text), elapsed_ms(),
        )
        logger.info(f"Executing command: '{redact_transcript(command_text)}'")
        executed = await self.text_parser.parse_and_execute(
            command_text, authorized_command=hotword_authorized,
        )
        if executed:
            # wh-med0: only true commands (irreversible side effects like
            # 'press enter' or 'delete line') block subsequent retraction.
            # Replacements (pure text substitutions like 'period -> .')
            # are dictation under a different spelling and remain
            # retractable, so they MUST NOT flip this flag. Without this
            # check, an STT mishearing of the leading audio as a
            # replacement word causes the gate to fire and silently drops
            # the corrected final when STT later revises that audio away.
            if self.text_parser.last_executed_pattern_type == "command":
                self._command_executed_in_utterance = True
            logger.info(
                f"Pattern executed successfully: '{redact_transcript(command_text)}' "
                f"(type={self.text_parser.last_executed_pattern_type})"
            )
        else:
            logger.info(f"No command matched, sending to dictation: '{redact_transcript(command_text)}'")
            await self._send_to_dictation(command_text)

    async def _process_remainder(
        self,
        remainder: str,
        *,
        word_event: Optional[WordEvent] = None,
        hotword_active: bool = False,
    ):
        """Process remainder text. Replacements only -- commands are dictated.

        wh-oe7u.1 / wh-oe7u.2: the previous implementation routed
        remainder text through TextParser.parse_and_execute, which
        executed any non-hotword command pattern that matched
        (``hello period backspace`` therefore fired a backspace key).
        It also collapsed before-and-after into a single remainder, so
        a later-spoken replacement could execute before earlier-spoken
        text was dictated.

        The new shape:
          1. Find the earliest replacement match in the remainder text
             via ``_find_earliest_replacement`` (lowest match.start;
             same start -> longest match.end; identical span ->
             catalog order).
          2. Dictate any text BEFORE the match -- that text arrived
             earlier in spoken order.
          3. Execute the matched replacement via
             ``TextParser._execute_rule`` directly (bypassing
             ``_execute_command`` so ``_command_executed_in_utterance``
             stays untouched -- replacements are retractable).
          4. Loop on the substring after the match.
          5. If no replacement matches, dictate the entire current
             text and stop.

        Commands in the remainder are intentionally unreachable here.
        Trailing-position commands belong to a separate product feature
        (``wh-2vz``); this path stays replacement-only.

        Args:
            remainder: Text remaining after a partial pattern match
            word_event: The word event whose decision released this
                remainder, when the caller allows a trailing replacement
                prefix to be held. Only the AFTER remainder passes one;
                see the call site in ``_execute_decision``.
            hotword_active: Whether the buffer that produced this remainder
                was hotword-authorized. The caller captures it before
                ``_reset_to_idle`` clears the live flag.
        """
        current_text = remainder

        while current_text:
            logger.debug(f"Processing remainder: '{redact_transcript(current_text)}'")

            winner = self._find_earliest_replacement(current_text)
            if winner is None:
                # No replacement matched -- dictate the remaining text.
                # Command words land here too (and are correctly dictated).
                #
                # wh-okay-prefix-splits-replacement (site 3, trace
                # T-17877991226): "backspace question mark" fired the
                # Backspace command and released "question" here, so
                # "question" typed as a word and "mark" followed it. When
                # the last word could still grow into a replacement, hold
                # the text instead. Any pass may hold: the tail left after
                # an earlier replacement is the last thing the user spoke,
                # so "backspace hello period question" plus a later "mark"
                # holds "question" on the second pass and converts it.
                if word_event is not None:
                    held_words = current_text.split()
                    if self._should_hold_replacement_prefix(
                        held_words,
                        hotword_active=hotword_active,
                        end_of_utterance=word_event.end_of_utterance,
                    ):
                        self._pending_replacement_prefix = held_words
                        pipeline_logger.info(
                            "REPLACEMENT-PREFIX held text=%r site=%s "
                            "elapsed_ms=%.1f",
                            redact_transcript(" ".join(held_words)),
                            "command-remainder",
                            elapsed_ms(),
                        )
                        self._arm_replacement_prefix_release(
                            across_utterance_end=(
                                word_event.end_of_utterance
                            ),
                        )
                        return
                logger.debug(
                    f"No replacement in remainder, dictating: "
                    f"'{redact_transcript(current_text)}'"
                )
                await self._send_to_dictation(current_text)
                return

            match, pattern_data = winner
            before_text = current_text[:match.start()].strip()
                        # Empty span guard: if a pathological pattern matches with
            # match.end() == match.start(), advancing on after_text alone
            # would loop forever. Dictate and bail.
            if match.end() == match.start():
                logger.warning(
                    "Replacement pattern matched empty span in remainder "
                    "%r; dictating verbatim to avoid infinite loop",
                    redact_transcript(current_text),
                )
                await self._send_to_dictation(current_text)
                return
            after_text = current_text[match.end():].strip()

            if before_text:
                logger.debug(
                    f"Dictating text before remainder match: "
                    f"'{redact_transcript(before_text)}'"
                )
                await self._send_to_dictation(before_text)

            executed = await self.text_parser._execute_rule(
                match,
                pattern_data['actions'],
                pattern_data.get('validation_group'),
            )
            if executed:
                # Observability: record the matched pattern's type so log
                # readers see "replacement" for this execution. Set only on
                # success; on failure leave the prior value (per wh-med0).
                self.text_parser.last_executed_pattern_type = "replacement"
                logger.debug(
                    "Replacement remainder match executed: %r",
                    redact_transcript(current_text[match.start():match.end()]),
                )
            else:
                logger.warning(
                    "Replacement remainder _execute_rule returned False "
                    "for pattern %r against %r",
                    pattern_data.get('compiled_pattern'),
                    redact_transcript(current_text),
                )
            # _command_executed_in_utterance is NOT touched: replacements
            # are pure text substitutions and remain retractable (wh-med0).

            current_text = after_text

    def _find_earliest_replacement(self, text: str):
        """Find the earliest replacement match in ``text``.

        Iterates over the text parser's replacement patterns, runs the
        compiled regex via ``search``, applies the same numeric
        validation PatternMatcher uses, and selects the winner by:
          1. Lowest match.start()  -- spoken order.
          2. Longest match.end()   -- multi-word beats single on same
             start (e.g. ``question mark`` over ``question``).
          3. Catalog order         -- first listed wins on identical
             span.

        Returns ``(match, pattern_data)`` on success, ``None`` if no
        replacement pattern matches the text. Greedy patterns are
        skipped to mirror PatternMatcher.match_complete semantics
        (wh-oe7u.1 / wh-oe7u.2).
        """
        matcher = self.text_parser.matcher
        candidates = []
        for idx, pattern_data in enumerate(self.text_parser.patterns):
            if pattern_data.get('pattern_type') != 'replacement':
                continue
            if pattern_data.get('is_greedy', False):
                continue
            compiled = pattern_data['compiled_pattern']
            match = compiled.search(text)
            if not match:
                continue
            validation_group = pattern_data.get('validation_group')
            if not matcher.validate_numeric(match, validation_group):
                continue
            # Sort key: (start asc, -end asc -> longest end first, idx asc).
            candidates.append((
                match.start(),
                -match.end(),
                idx,
                match,
                pattern_data,
            ))

        if not candidates:
            return None
        candidates.sort()
        _, _, _, match, pattern_data = candidates[0]
        return match, pattern_data

    def _reset_to_idle(self):
        """Reset state to IDLE."""
        # Only cancel timeout if we're NOT inside the timeout handler itself
        # (Otherwise we'd be cancelling the task we're currently running in)
        current_task = asyncio.current_task()
        if self.timeout_task is not current_task:
            self._cancel_timeout()
        self.buffer.clear()
        self.hotword_active = False
        self.mode = ProcessingMode.IDLE
        logger.info("Returned to IDLE mode")

    async def _send_pending_utterance_end(self):
        """Send deferred end_utterance if one is pending.

        This is called after dictation or command execution completes,
        ensuring clipboard restoration happens AFTER paste operations.
        This fixes the race condition where clipboard was restored before
        the app could read it for the paste.
        """
        if self._pending_utterance_end is not None:
            logger.debug(f"Sending deferred end_utterance for {self._pending_utterance_end}")
            # wh-overlay-slow-uia-stale-badges.7.1.2: only a deferred
            # end for the CURRENT utterance closes the dictation
            # sentence. A STALE id here would fire mid-way through the
            # NEXT utterance, and closing the live sentence there would
            # let a screen read jump between two words of one sentence
            # and get those words refused. The production raise path
            # that produced a stale id is closed: the per-word handler
            # clears the slot (wh-pending-utterance-end-stale-slot). So
            # this comparison now guards an injected id and any future
            # producer. The stale end_utterance itself still ships;
            # refusing it is the clipboard manager's own id check.
            if self._pending_utterance_end == self._current_utterance_id:
                self.sentence_closed_event.set()
            await self._send_end_utterance(self._pending_utterance_end)
            self._pending_utterance_end = None

    # wh-g2-refactor.18: the wh-n8bu drain-chain safety machinery
    # (_should_hold_for_drain, _on_redirect_drain_complete,
    # _release_dictation_holdback) was deleted with the focus-redirect path.

    # ========================================================================
    # ATOMIC ACTIONS (Building blocks used by transitions)
    # ========================================================================

    async def _handle_retraction(self, word_event: WordEvent):
        """Handle a retraction marker: cancel buffers, retract pasted text, replay final.

        Args:
            word_event: Retraction marker WordEvent with retraction_full_text
        """
        # 1. Cancel any pending buffer/timeout and reset to IDLE. Remember
        #    whether the buffer held the utterance's words: buffered words
        #    were withheld from typing (they were accumulating toward a
        #    command match), so the Input process never saw a paste and its
        #    retract will answer nothing_to_retract -- which must NOT drop
        #    the corrected final in that case (wh-click-number-dictation).
        buffered_words = list(self.buffer)
        had_buffered_words = bool(buffered_words)
        self._cancel_timeout()
        self.buffer.clear()
        self.hotword_active = False
        self.mode = ProcessingMode.IDLE

        # 2. Check if utterance is retractable (no commands executed)
        if self._command_executed_in_utterance:
            logger.info("Retraction skipped: command executed in utterance")
            return

        # 3. Send retract IPC and wait for ACK. Every "not_retracted"
        #    reason is terminal: no retry, no replay. The earlier
        #    100 ms retry for `editor_unconfirmed` was unreachable
        #    (production code stopped generating that reason) and
        #    has been removed under wh-g2-refactor.14 -- the G2 path
        #    in Section 2 of the design refinements collapses retract
        #    and replay into one Qt main-thread call, which closes
        #    the paste-vs-ack data-loss window structurally instead
        #    of plastering over it with a retry. See
        #    docs/design/2026-05-20-g2-refactor-design-refinements.md
        #    Section 2 "Round 1 update -- deepseek concern F" for the
        #    full rationale.
        #
        # wh-g2-refactor.18 (slice 18.32.1): when the prior writes for
        # this utterance went into the persistent editor, retract via
        # the editor IPC instead of the legacy ``retract``. The legacy
        # IPC undoes against the Input process's shadow buffer, which
        # never saw the editor writes; using it here would leave the
        # editor's text intact and silently desync the two surfaces.
        final_text = word_event.retraction_full_text or ""
        if (
            self._used_editor_this_utterance
            and self.logic_controller is not None
        ):
            # wh-editor-retract-ledger-authoritative: the retract is
            # whole-utterance (the GUI peels ALL ledger runs), so the
            # mirror count below is only advisory diagnostics. The old
            # `_editor_chars_this_utterance > 0` gate is deliberately
            # gone: a mirror of 0 with editor use means every insert
            # response timed out Logic-side -- exactly the drift case
            # where the words may still have landed in the editor and
            # MUST be retracted before the replay.
            chars_to_retract = self._editor_chars_this_utterance
            uid = self._current_utterance_id
            uid_str = str(uid) if uid is not None else str(
                word_event.utterance_id or 0,
            )
            # wh-spaced-punctuation-names-unresolved.3.1.2: the editor
            # retract is whole-utterance -- the GUI peels ALL ledger
            # runs -- so a held prefix written into this utterance's
            # runs goes with it exactly as it does on the legacy path.
            # Restore it into the replay for the same reason. This arm
            # has no e2e coverage: reaching it needs a wired
            # logic_controller, which the e2e harness does not build.
            editor_replay = self._restore_earlier_utterance_words(final_text)
            try:
                retract_ok = await self.logic_controller.retract_editor_text(
                    chars_requested=chars_to_retract,
                    utterance_id=uid_str,
                    replay_text=editor_replay,
                    whole_utterance=True,
                )
            except Exception:
                logger.exception(
                    "retract_editor_text raised (utterance=%s chars=%d)",
                    uid_str, chars_to_retract,
                )
                return
            # Reset the editor accounting to reflect the replay text
            # that the GUI handler just inserted inline. The replay is
            # the new state of the utterance; a subsequent (chained)
            # retract should target the replay's span. The unit is
            # grapheme clusters -- the same unit the ledger peels and
            # accumulates per word (wh-editor-retract-dup.1.1) -- NOT
            # len(final_text) (raw code points).
            #
            # wh-editor-retract-dup.2.2: the ledger records the replay
            # run's cluster count over the CANONICAL text (CRLF / CR
            # mapped to LF; see ledger._canonical_text). The hand-rolled
            # segmenter counts a raw "\r\n" as TWO clusters, so counting
            # final_text unnormalised over-counts by one per CRLF. A
            # chained retract would then request more clusters than the
            # ledger holds, hit ledger_underrun, and skip the replay.
            # Normalise line endings the same way the ledger does before
            # counting so the two sides agree. (NFC -- the ledger's other
            # canonical step -- does not change the cluster count, so it
            # is not needed here.)
            self._editor_chars_this_utterance = count_grapheme_clusters(
                normalize_line_endings(editor_replay),
            )
            # wh-whole-utterance-command-matching (Stage 1): the inline
            # replay never re-enters process_word_event, so the arrival
            # append cannot rebuild the word list the way the
            # word-by-word replay below does. Mirror the corrected
            # final in directly -- but only when the GUI confirmed the
            # retract-and-replay (codex finding
            # wh-whole-utterance-command-matching.1.1). On a False
            # return (timeout, echo mismatch, stale_generation,
            # replay_failed, underrun) the correction was not installed,
            # so the list keeps the spoken words -- the same outcome as
            # the legacy path's failed retract, where no replay runs.
            # The advisory _editor_chars_this_utterance recompute above
            # deliberately stays unconditional: that is pre-existing
            # wh-editor-retract-ledger-authoritative design, healed by
            # the next STT update.
            if retract_ok:
                self._words_this_utterance = editor_replay.split()
            # ``_used_editor_this_utterance`` stays True so a follow-up
            # retract on this utterance keeps using the editor path.
            # The GUI handler performs the replay inline, so the
            # speech-side word-by-word replay below would double-insert.
            return

        # wh-spaced-punctuation-names-unresolved.3.1.8, codex round
        # 6. Read the count HERE, in the same breath as the enqueue
        # below, and not after the response. Both messages travel on
        # the one outbound queue, so a start_utterance sent while this
        # request waits sits BEHIND it: the Input side processes the
        # retract first, while its paste counter still holds the
        # earlier words, and removes them. A count read after the
        # response would see that later start and suppress a
        # restoration the screen needs -- a newer start cannot undo a
        # deletion that already happened. The read is atomic with the
        # enqueue for the same reason the increment is (app.py:1127):
        # nothing between send_request's entry and its put_nowait
        # awaits.
        start_count_at_retract = self._read_start_utterance_count()
        try:
            response = await self.app.send_request(
                action='retract',
                params={},
            )
        except Exception as e:
            logger.error(f"Retract IPC failed: {e}")
            return

        replay_text = final_text
        # Only the successful-retract arm below can restore anything;
        # the not_retracted arms leave the earlier words on screen.
        restored_earlier = False
        if response.get('status') != 'retracted':
            reason = response.get('reason', 'unknown')
            if reason == 'nothing_to_retract' and had_buffered_words:
                # wh-click-number-dictation: every word of the utterance was
                # held in the command buffer (cleared in step 1 above), so
                # nothing of it is on screen -- there was nothing TO retract,
                # and the corrected final must still replay or a provider
                # FINAL rewrite ("click three" -> "click 3") silently kills
                # the buffered command. This is the Logic-buffer analog of
                # the Input side's wh-j3mgc letter-buffer rule. Every other
                # not_retracted reason stays terminal: text may be visible
                # on screen, and replaying on top of it would double it.
                #
                # The final can also REGRESS the buffer ('click 136' arrived
                # as the final '1'); replaying that fragment loses the
                # command and types stray text. When the final carries FEWER
                # words than the buffer held, replay the buffered words
                # instead. Equal-or-more words trusts the final, which keeps
                # the wanted digit rewrite ('click three' -> 'click 3').
                if len(final_text.split()) < len(buffered_words):
                    replay_text = " ".join(buffered_words)
                    logger.info(
                        "Retraction unnecessary (nothing pasted; words were "
                        "command-buffered) -- final shrank the buffer, "
                        "replaying the buffered words instead"
                    )
                else:
                    logger.info(
                        "Retraction unnecessary (nothing pasted; words were "
                        "command-buffered) -- replaying corrected final"
                    )
            else:
                logger.info(f"Retraction not performed: {reason}")
                return
        else:
            # wh-spaced-punctuation-names-unresolved.3.1.2: the retract
            # succeeded, so the whole credited span is gone -- including
            # any words a hold carried across the previous utterance's
            # end_utterance. Put those back in front of the corrected
            # final; the correction speaks only for the newest
            # utterance. Deliberately inside the retracted arm: the
            # not_retracted branches above run while the text is still
            # on screen, and prepending there would double it.
            #
            # wh-spaced-punctuation-names-unresolved.3.1.2, codex round
            # 3 case B: the words go back as TEXT, not through the
            # replay loop below. That loop labels everything it is
            # given as one utterance (start_of_utterance only on index
            # 0, end_of_utterance never), so a prepended word and the
            # corrected final become neighbours inside a single
            # utterance and the in-utterance name matcher joins them.
            # Words the earlier utterance dictated literally BECAUSE it
            # ended would come back as a punctuation mark.
            restored_earlier = await self._redeliver_earlier_utterance_words(
                start_count_at_retract,
            )

        # 4. Replay through normal pipeline
        if not replay_text:
            logger.warning("Retraction marker has no final text to replay")
            return

        logger.info(f"Replaying retracted text: '{redact_transcript(replay_text)}'")
        words = replay_text.split()
        for i, word in enumerate(words):
            replay_event = WordEvent(
                word=word,
                # The restored earlier words opened this sentence, so
                # the corrected final's first word continues it. Saying
                # otherwise would capitalize it a second time
                # (wh-spaced-punctuation-names-unresolved.3.1.2).
                start_of_utterance=(i == 0 and not restored_earlier),
                end_of_utterance=False,
                utterance_id=word_event.utterance_id,
            )
            await self.process_word_event(replay_event)

        # wh-2vz: the replay does not produce its own
        # is_utterance_end_marker, so a trailing word in the replayed
        # text would otherwise stay held until the NEXT utterance and
        # then leak into that utterance's dictation. Flush as text
        # instead so the replayed word lands in the retracted
        # utterance's IPC stream. Retraction-after-an-Enter-press is
        # already destructive; firing Enter a second time would be
        # worse, so dictating the word as text is the conservative
        # outcome.
        await self._flush_pending_trailing_word_as_dictation()
        # wh-trailing-question-mark-words: belt-and-braces backstop for
        # the same reason. The top-of-loop pass already flushes on the
        # retraction marker that reached this handler, and the replay
        # cannot hold a prefix of its own (holding needs a timeout
        # sentinel, and the replay loop runs to completion without
        # yielding to the timer), so this is the second lock on a slot
        # whose leak would type words into the next utterance.
        await self._flush_pending_replacement_prefix_as_dictation()

    async def _send_to_dictation(self, text: str):
        """Send text to application for insertion.

        wh-g2-refactor.18 (slice 18.32.1): consults the focus-redirect
        policy first. When the policy returns ``open_editor=True`` and
        a ``logic_controller`` is wired, the text routes to the
        persistent hidden editor via ``insert_editor_word``. Otherwise
        it falls through to the standard ``intelligent_insert_text``
        path that types into whatever currently has foreground.

        The per-utterance editor tracking
        (``_used_editor_this_utterance`` / ``_editor_chars_this_utterance``)
        is updated when a write reaches the editor; the retract path
        reads those fields to pick between ``retract_editor_text`` and
        the legacy ``retract`` IPC.

        Args:
            text: Text to insert (single word or buffered phrase)

        Returns:
            The surface that actually received the text: ``"editor"``,
            ``"legacy"`` for the ``intelligent_insert_text`` path, or
            ``None`` when nothing was typed at all. Callers that need
            to know whether characters really entered a retractable
            span read this (wh-spaced-punctuation-names-unresolved
            .3.1.2, codex round 3 case C); every other caller ignores
            it, as they all did when this method returned nothing.
        """
        if not text:
            return None
        # wh-spaced-punctuation-names-unresolved.3.1.5: read the app's
        # start_utterance count immediately before the enqueue, with
        # nothing awaited in between, so the number names the span this
        # text really lands in.
        self._last_delivery_generation = self._read_start_utterance_count()
        if await self.maybe_route_to_editor(text):
            return "editor"
        # wh-overlay-slow-uia-stale-badges.7: while the Input process
        # runs a screen read, its ONE command loop is blocked, so an
        # intelligent_insert_text sent now queues behind the read and
        # types seconds late into whatever has focus by then. Late
        # dictation is worse than lost dictation: drop the word, never
        # queue it. The editor path above is exempt on purpose -- an
        # editor insert does not cross the Input command loop. The
        # bare-number path never reaches here (it consumes as "click N"
        # earlier in the DICTATE branch), so numbers keep working.
        if self._screen_read_refuses_dictation():
            pipeline_logger.info(
                "DICTATION refused (screen read in flight) text=%r "
                "elapsed_ms=%.1f",
                redact_transcript(text), elapsed_ms(),
            )
            return None
        pipeline_logger.info(
            "DICTATING text=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        # A word is actually going out: the dictation sentence is open
        # until the next end_utterance send closes it.
        # wh-overlay-slow-uia-stale-badges.7.1.7: remember whether the
        # sentence was closed before this word opened it. The
        # pre-enqueue IpcDeliveryError (outbound queue full) proves the
        # payload never reached Input, so no end_utterance could ever
        # close the sentence this word opened -- restore it. A timeout
        # is different: the durable request may still deliver late and
        # type, so on a timeout the sentence stays conservatively open.
        # A word joining an ALREADY-open sentence restores nothing: the
        # earlier accepted word's end_utterance still closes it.
        # wh-overlay-slow-uia-stale-badges.7.1.11: the package path, not
        # `from app import ...` -- the launcher loads main top-level while
        # main.py builds WheelHouseApp from services.wheelhouse.app, so the
        # same file backs TWO module objects with distinct exception
        # classes. The production app raises the package classes; a
        # top-level import here would make this isinstance miss them.
        from services.wheelhouse.app import IpcDeliveryError

        sentence_was_closed = self.sentence_closed_event.is_set()
        self.sentence_closed_event.clear()
        # The editor route above awaited, so read the count again here.
        # app.send_request enqueues without awaiting first (app.py:1359),
        # so this number is the one the Input process will see this text
        # arrive under (wh-spaced-punctuation-names-unresolved.3.1.5).
        self._last_delivery_generation = self._read_start_utterance_count()
        # Send to app via UI actions and WAIT for completion
        # This prevents utterance_end from restoring clipboard before insertion finishes
        try:
            await self.app.send_request(
                action='intelligent_insert_text',
                params={'insertion_string': text}
            )
        except IpcDeliveryError:
            if sentence_was_closed:
                self.sentence_closed_event.set()
            raise
        return "legacy"

    def _screen_read_refuses_dictation(self) -> bool:
        """True while a screen read is in flight and younger than the cap.

        Reads ``logic_controller.screen_read_in_flight_since`` (a
        ``time.monotonic()`` stamp set around the Input round trip).
        Anything but a real number -- the attribute missing, None, or a
        Mock auto-attribute from a legacy test fixture -- means no read.
        ``bool`` is excluded explicitly because it is an ``int``
        subclass, and a fixture setting the field to True must read as
        malformed, not as a stamp from t=1.

        The cap follows ``[click] screen_read_timeout_ms``
        (wh-overlay-slow-uia-stale-badges.3), read defensively off the
        controller's ``click_config`` and accepted only as a real number.
        Anything else -- no controller attribute, no config, a Mock
        auto-attribute, a bool -- falls back to the floor, which is what
        the FakeController fixtures get.
        """
        since = getattr(
            self.logic_controller, "screen_read_in_flight_since", None,
        )
        if not isinstance(since, (int, float)) or isinstance(since, bool):
            return False
        limit_ms = getattr(
            getattr(self.logic_controller, "click_config", None),
            "screen_read_timeout_ms",
            None,
        )
        cap_s = _READ_GATE_MAX_REFUSAL_S
        if isinstance(limit_ms, (int, float)) and not isinstance(limit_ms, bool):
            cap_s = max(
                cap_s, limit_ms / 1000.0 + _READ_GATE_REFUSAL_SLACK_S
            )
        return (time.monotonic() - since) < cap_s

    async def maybe_route_to_editor(self, text: str) -> bool:
        """Consult the focus-redirect policy and route to the editor on a hit.

        Returns True when the text was successfully forwarded to the
        persistent hidden editor via ``logic_controller.insert_editor_word``
        and the caller MUST NOT also run the legacy
        ``intelligent_insert_text`` path. Returns False on every other
        outcome (policy not wired, logic_controller not wired,
        policy declined the redirect, policy raised, or the editor IPC
        itself raised).

        Public entry: also called from ``command_engine._execute_rule``
        so a replacement's text-insertion step routes into the editor
        when we are at a terminal prompt, instead of typing the
        replacement output into the terminal directly.

        wh-g2-refactor.18 (slice 18.32.1): this is the production
        wiring of the persistent editor IPC that was previously dead
        code. Without it, the persistent editor existed but was never
        written into.

        wh-editor-retract-dup: editor routing is STICKY per utterance.
        ``should_redirect`` answers "should I OPEN the editor", not
        "should this word go to the editor". Once the first word of an
        utterance has opened the editor, the policy declines every later
        word (``editor_already_open`` from the mirror, or ``not_a_terminal``
        because the editor now holds foreground). The pre-fix code then
        dropped words 2..N onto the legacy ``intelligent_insert_text``
        path, where the CreditLedger never saw them; the MODE3 retraction
        therefore retracted only the first word's span and replayed the
        full final, duplicating the un-tracked words. Once
        ``_used_editor_this_utterance`` is True we route directly to the
        editor and skip the policy consult entirely.
        """
        if self.logic_controller is None:
            return False
        utterance_id = self._current_utterance_id

        # Sticky path: a word of this utterance already went into the
        # editor, so this one does too -- no policy re-consult, no
        # second show.
        if self._used_editor_this_utterance:
            if utterance_id is None:
                return False
            return await self._insert_word_into_editor(
                text, str(utterance_id), show_terminal_hwnd=None,
            )

        # First word of the utterance: consult the focus-redirect policy.
        if self.focus_redirect_policy is None:
            return False
        provider = self._focused_hwnd_provider
        if provider is None:
            return False
        try:
            focused_hwnd = int(provider() or 0)
        except Exception:
            logger.exception(
                "focus_redirect: focused_hwnd_provider raised; "
                "falling through to legacy dictation"
            )
            return False
        try:
            decision = await self.focus_redirect_policy.should_redirect(
                focused_hwnd,
            )
        except Exception:
            logger.exception(
                "focus_redirect: should_redirect raised; falling "
                "through to legacy dictation"
            )
            return False
        if not decision.open_editor:
            return False
        if utterance_id is None:
            # The IPC schema rejects empty utterance_id strings. A
            # DICTATE without a known utterance_id is rare (test
            # fixtures that synthesise events without one); fail safe
            # to the legacy path so the word still reaches the user.
            logger.debug(
                "focus_redirect: no current_utterance_id; falling "
                "through to legacy dictation"
            )
            return False
        return await self._insert_word_into_editor(
            text,
            str(utterance_id),
            show_terminal_hwnd=int(decision.target_terminal_hwnd or 0),
        )

    async def _insert_word_into_editor(
        self,
        text: str,
        utterance_str: str,
        *,
        show_terminal_hwnd: Optional[int],
    ) -> bool:
        """Route a single dictation word into the persistent editor.

        Shared tail for both the first-word (policy-accept) path and the
        sticky subsequent-word path. ``show_terminal_hwnd`` is the terminal
        HWND to reveal the editor for on the first redirected word of an
        utterance; pass ``None`` on the sticky path so no second show fires.

        Accumulates the editor-reported insert count (not ``len(text)``)
        into ``_editor_chars_this_utterance`` so the retract span accounts
        for the leading space the editor's TextPerfector adds to words 2..N
        (wh-editor-retract-dup). Always returns True: a routed word must not
        also flow through the legacy path, even when the editor IPC raised
        or the editor declined the insert.
        """
        pipeline_logger.info(
            "DICTATING text=%r via=editor utterance=%s elapsed_ms=%.1f",
            redact_transcript(text), utterance_str, elapsed_ms(),
        )
        # wh-wisp-07m: on the first redirected word of an utterance,
        # reveal the persistent editor so the user can see the words
        # land and press Enter to submit them. The editor is
        # pre-constructed at GUI startup but stays hidden until this
        # producer fires. Subsequent words in the same utterance skip
        # the show and only route the insert IPC.
        if not self._used_editor_this_utterance and show_terminal_hwnd is not None:
            try:
                self.logic_controller.show_editor_persistent(
                    show_terminal_hwnd,
                )
            except Exception:
                logger.exception(
                    "focus_redirect: show_editor_persistent raised; "
                    "editor may remain hidden but insert IPC will "
                    "still attempt the write"
                )
        # wh-editor-retract-dup.2.1: this utterance is now committed to the
        # editor route -- the policy accepted the redirect (first word) or
        # the sticky path brought us here (later words), and
        # show_editor_persistent has revealed the editor and taken
        # foreground. Mark the utterance sticky BEFORE attempting the
        # insert. If insert_editor_word then raises, words 2..N must still
        # route to the editor; leaving the flag False lets word 2
        # re-consult should_redirect (which now declines because the
        # editor is open) and fall through to the legacy path, recreating
        # the split-utterance duplication bug.
        self._used_editor_this_utterance = True
        try:
            inserted = await self.logic_controller.insert_editor_word(
                text, utterance_str,
            )
        except Exception:
            logger.exception(
                "focus_redirect: insert_editor_word raised; word "
                "may have been lost (utterance=%s len=%d)",
                utterance_str, len(text),
            )
            return True
        self._editor_chars_this_utterance += int(inserted or 0)
        return True
    
    def _start_timeout(self, duration_ms: int):
        """Start timeout for automatic buffer finalization.

        Cancels any existing timeout, bumps the generation token, then
        creates a new task carrying that token (wh-oe7u.4). The handler
        does not mutate processor state directly; it enqueues a typed
        sentinel onto ``word_queue`` so the processing loop is the
        single writer.

        Args:
            duration_ms: Timeout duration in milliseconds
        """
        # _cancel_timeout already bumps timeout_token. Calling it first
        # invalidates any sentinel an earlier task may already have
        # enqueued. The new task captures the post-bump value.
        self._cancel_timeout()
        snapshot_token = self.timeout_token
        self.timeout_task = asyncio.create_task(
            self._timeout_handler(duration_ms, snapshot_token)
        )
        logger.debug(
            "Started timeout: %dms (token=%d)", duration_ms, snapshot_token,
        )

    def _cancel_timeout(self):
        """Cancel active timeout task and invalidate any in-flight sentinel.

        Safe to call even if no timeout is active. Bumps the generation
        token so any sentinel already enqueued by a now-cancelled task
        becomes a no-op when the processing loop dequeues it
        (wh-oe7u.4).
        """
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
            logger.debug("Cancelled timeout")
        self.timeout_task = None
        # Bump after cancellation so any sentinel the cancelled task
        # enqueued before being cancelled becomes stale.
        self.timeout_token += 1

    async def _timeout_handler(self, duration_ms: int, token: int):
        """Handle timeout expiration for buffered patterns.

        Sleeps for ``duration_ms`` then enqueues a typed timeout-finalize
        sentinel carrying ``token`` onto ``word_queue``. The processing
        loop consumes it and runs ``decide_timeout`` /
        ``_execute_decision`` against the live buffer (wh-oe7u.4).

        State mutation never happens in this task. The CancelledError
        path explicitly re-raises without enqueuing so a cancellation
        race cannot leak a stale sentinel; the token check at the
        consumer side is the second line of defense.

        Args:
            duration_ms: Duration to wait before finalizing
            token: Generation token snapshotted at start; the consumer
                ignores the sentinel unless this still matches
                ``self.timeout_token`` (cancellation/_reset_to_idle/stop
                bump the token).
        """
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            logger.info("Timeout expired (%dms, token=%d)", duration_ms, token)

            # If stop() ran while we were sleeping, do not enqueue a
            # sentinel (wh-3pvsu / wh-oe7u.4). The consumer-side token
            # check would catch it anyway, but skipping the enqueue
            # avoids stuffing the queue during shutdown.
            if self._stopped:
                logger.debug("Timeout fired after stop(); not enqueuing sentinel")
                return

            try:
                self.word_queue.put_nowait(WordEvent.timeout_finalize(token=token))
            except Exception as e:
                # put_nowait failure is fail-safe: log and skip. Better to
                # miss a finalization than to mutate state from this task.
                logger.error(
                    "Timeout sentinel enqueue failed: %s; skipping finalization", e,
                )
        except asyncio.CancelledError:
            # Cancelled before sleep returned. Do NOT enqueue a sentinel
            # from this path; the consumer-side token check is a backup,
            # but the contract is that cancelled tasks never write.
            logger.debug("Timeout cancelled")
            raise
        except Exception as e:
            # Don't let errors crash the process via the global exception
            # handler. Same resilience as _processing_loop's per-word
            # error handling.
            logger.error(f"Error in timeout handler: {e}", exc_info=True)
