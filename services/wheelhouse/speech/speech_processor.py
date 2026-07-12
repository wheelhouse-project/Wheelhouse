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
import logging
import re
from typing import Optional

from utils.redact import redact_transcript

from .word_event import WordEvent
from .pattern_catalog import PatternCatalog, PatternType
from services.wheelhouse.shared.context_mirror import ContextMirror
from services.wheelhouse.shared.grapheme import (
    count_grapheme_clusters,
    normalize_line_endings,
)
from .domain import ProcessingMode, Action, Decision
from .router import SpeechRouter
from utils.trace_context import set_trace, elapsed_ms

logger = logging.getLogger(__name__)
pipeline_logger = logging.getLogger("wheelhouse.pipeline")


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

        # Context Mirror
        self.context_mirror = ContextMirror()
        self.context_mirror.init_reader()
        
        # Router
        self.router = SpeechRouter(catalog, hotword)
        
        # State machine
        self.mode = ProcessingMode.IDLE
        self.buffer: list[str] = []
        self.timeout_task: Optional[asyncio.Task] = None
        self.hotword_active = False  # Track if current buffer was triggered by hotword
        self._pending_utterance_end: Optional[int] = None  # Deferred end_utterance until buffer finalizes

        # Retraction support: track if any command was executed in current utterance
        self._command_executed_in_utterance: bool = False

        # wh-2vz: trailing-position command candidate held back from
        # dictation until we know whether it is the actual last word of
        # the utterance. See the trailing-commands section near the
        # bottom of this file for the lifecycle.
        self._pending_trailing_word: Optional[str] = None

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

    @property
    def current_context(self) -> dict:
        """Get the current active window context from shared memory.
        
        :flow: Context Mirroring
        :step: 2
        :description: Reads the latest window context from shared memory.
        :data_in: None (reads from shared memory).
        :data_out: Dictionary with 'app_name', 'window_title', 'timestamp'.
        :execution_context: Logic Process
        :consumes_from: Context Mirroring
        """
        return self.context_mirror.read_context()
    
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
        # Drop any deferred end_utterance so it cannot be flushed across
        # shutdown.
        self._pending_utterance_end = None
        # wh-2vz: drop any held trailing candidate on shutdown. Sending
        # an IPC during stop() would race the shutdown sequence; the
        # word is sacrificed to make stop deterministic.
        self._pending_trailing_word = None
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
                    # Continue processing next word

        except asyncio.CancelledError:
            logger.info("Processing loop cancelled")
            raise
    
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

        # wh-2vz: any non-end-marker, non-sentinel event that arrives
        # proves a held trailing candidate is NOT the last word of the
        # utterance. Flush it as plain dictation before processing the
        # new event. The utterance_end_marker branch (further down)
        # consumes the candidate instead and must NOT flush -- the
        # check below excludes it.
        #
        # wh-2vz.1.2 (codex round 1): also exclude
        # is_timeout_finalize_marker. Queue sentinels are not spoken
        # input; a stale or fresh sentinel arriving between a held
        # candidate and the utterance_end_marker must not dictate the
        # held word. The sentinel-handling block below either drops it
        # as stale (token mismatch) or returns early in IDLE mode
        # (which is the mode the processor is in whenever a trailing
        # candidate is held, because the DICTATE branch sets IDLE
        # before holding the candidate).
        if (
            self._pending_trailing_word is not None
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
        ):
            await self._flush_pending_trailing_word_as_dictation()

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
            decision = self.router.decide_timeout(self.buffer, self.hotword_active)
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
            await self.app.send_command({
                'action': 'end_utterance',
                'params': {'utterance_id': word_event.utterance_id},
            })
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
            # machinery is gone. Fire any held trailing action now and
            # proceed with end-marker handling. The action's IPC is
            # fire-and-forget; the subsequent end_utterance IPC closes
            # the utterance for clipboard / retraction accounting.
            await self._consume_pending_trailing_word_at_utterance_end()
            if self.mode == ProcessingMode.IDLE:
                # wh-oe7u.4: timeout finalization now runs inside the
                # processing loop via the queue sentinel, so the loop is
                # blocked during the IPC await and the next utterance_end
                # cannot be dequeued until finalization is done.
                await self.app.send_command({
                    'action': 'end_utterance',
                    'params': {'utterance_id': word_event.utterance_id}
                })
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
                finalize_decision = self.router.decide_timeout(self.buffer, self.hotword_active)
                await self._execute_decision(finalize_decision)
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

        # Auto-finalize previous utterance if new one starts while buffering
        if self.mode != ProcessingMode.IDLE and word_event.start_of_utterance:
            logger.info(f"New utterance {word_event.utterance_id} started while buffering. Finalizing previous buffer.")
            finalize_decision = self.router.decide_timeout(self.buffer, self.hotword_active)
            await self._execute_decision(finalize_decision)
            # Note: _execute_decision resets mode to IDLE

        # Delegate decision to Router
        decision = self.router.decide(
            word_event,
            self.mode,
            self.buffer,
            self.current_context,
            hotword_active=self.hotword_active,
            command_timeout_ms=self.command_timeout_ms,
            replacement_timeout_ms=self.replacement_timeout_ms,
            greedy_timeout_ms=self.greedy_timeout_ms
        )

        pipeline_logger.info(
            "ROUTED action=%s mode=%s reason=%r word=%r elapsed_ms=%.1f",
            decision.action.name, self.mode.name, decision.reason,
            redact_transcript(word_event.word), elapsed_ms(),
        )

        await self._execute_decision(decision)

    async def _execute_decision(self, decision: Decision):
        """Execute the routing decision.
        
        Handles side effects like buffering, executing commands, or sending dictation.
        """
        if decision.reason:
            logger.debug(f"Decision: {decision.action.name} ({decision.reason})")

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
            self._reset_to_idle()
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
            self._reset_to_idle()
            # Execute BEFORE remainder first (buffered content that arrived earlier)
            if decision.before_remainder:
                await self._process_remainder(decision.before_remainder)
            # Execute the matched pattern
            await self._execute_command(decision.payload)
            # Execute AFTER remainder last (content that arrived after the match)
            if decision.remainder:
                await self._process_remainder(decision.remainder)
            # wh-g2-refactor.18: the focus-redirect transfer for
            # replacement inserts is gone with the redirect path.
            # Send deferred end_utterance AFTER command execution completes.
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
    # Both STT paths (remote WebSocketManager and in-process STTManager)
    # reliably emit ``is_utterance_end_marker`` at the end of every utterance,
    # so the trailing decision is anchored on that marker. Each real word
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
        match = compiled.match(word)
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

    async def _execute_command(self, command_text: str):
        """Execute command text via TextParser."""
        pipeline_logger.info(
            "EXECUTING command=%r elapsed_ms=%.1f",
            redact_transcript(command_text), elapsed_ms(),
        )
        logger.info(f"Executing command: '{redact_transcript(command_text)}'")
        # authorized_command=True: router's match_complete() already vetted
        # hotword for this buffer. Remainder processing must NOT set this
        # (wh-qj70s).
        executed = await self.text_parser.parse_and_execute(
            command_text, authorized_command=True,
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

    async def _process_remainder(self, remainder: str):
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
        """
        current_text = remainder

        while current_text:
            logger.debug(f"Processing remainder: '{redact_transcript(current_text)}'")

            winner = self._find_earliest_replacement(current_text)
            if winner is None:
                # No replacement matched -- dictate the remaining text.
                # Command words land here too (and are correctly dictated).
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
            await self.app.send_command({
                'action': 'end_utterance',
                'params': {'utterance_id': self._pending_utterance_end}
            })
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
        # 1. Cancel any pending buffer/timeout and reset to IDLE
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
            try:
                await self.logic_controller.retract_editor_text(
                    chars_requested=chars_to_retract,
                    utterance_id=uid_str,
                    replay_text=final_text,
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
                normalize_line_endings(final_text),
            )
            # ``_used_editor_this_utterance`` stays True so a follow-up
            # retract on this utterance keeps using the editor path.
            # The GUI handler performs the replay inline, so the
            # speech-side word-by-word replay below would double-insert.
            return

        try:
            response = await self.app.send_request(
                action='retract',
                params={},
            )
        except Exception as e:
            logger.error(f"Retract IPC failed: {e}")
            return

        if response.get('status') != 'retracted':
            logger.info(f"Retraction not performed: {response.get('reason', 'unknown')}")
            return

        # 4. Replay final's words through normal pipeline
        if not final_text:
            logger.warning("Retraction marker has no final text to replay")
            return

        logger.info(f"Replaying retracted text: '{redact_transcript(final_text)}'")
        words = final_text.split()
        for i, word in enumerate(words):
            replay_event = WordEvent(
                word=word,
                start_of_utterance=(i == 0),
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
        """
        if not text:
            return
        if await self.maybe_route_to_editor(text):
            return
        pipeline_logger.info(
            "DICTATING text=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        # Send to app via UI actions and WAIT for completion
        # This prevents utterance_end from restoring clipboard before insertion finishes
        await self.app.send_request(
            action='intelligent_insert_text',
            params={'insertion_string': text}
        )

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
