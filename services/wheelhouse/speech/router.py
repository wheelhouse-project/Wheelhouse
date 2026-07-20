import logging
import re
from typing import Optional, List, Sequence, Tuple

from .domain import ProcessingMode, Action, Decision
from .word_event import WordEvent
from .pattern_catalog import PatternCatalog, PatternType
from .pattern_matcher import PatternMatcher
from .pattern_transform import extract_literal_prefix

logger = logging.getLogger(__name__)


def _word_matches_hotword(word: str, hotword: str) -> bool:
    """Case- and hyphen-insensitive wake-word equality.

    STT engines disagree on the hyphen in a hyphenated wake word:
    Parakeet usually emits "X-ray" but can fuse it, and a user hotword
    override may be written with or without the hyphen. Stripping
    hyphens from both sides makes "xray" match wake word "x-ray" and
    "x-ray" match wake word "xray" (wh-parakeet-xray-hotword).
    Lowercases both arguments itself so a caller that skips the usual
    ``hotword.lower()`` storage step cannot get a silent false negative
    (deepseek review, wh-parakeet-xray-hotword.1.2).
    """
    w = word.lower()
    h = hotword.lower()
    return w == h or w.replace("-", "") == h.replace("-", "")


class SpeechRouter:
    """Decision engine for speech processing.
    
    Encapsulates the truth table logic and pattern matching rules to decide
    how to route words (Buffer, Execute, Dictate, etc.).
    
    :flow: Speech Processing
    :step: 3.1
    :description: Pure logic component that makes routing decisions based on state and input.
    :consumes_from: Speech Processing
    :produces_for: Command and Dictation Routing
    """
    
    def __init__(self, catalog: PatternCatalog, hotword: str = "x-ray"):
        self.catalog = catalog
        self.hotword = hotword.lower()
        # Wake word captured at the fresh-hotword detection that started the
        # current buffer. The dictation fallback reconstructs its prefix from
        # THIS snapshot, not the live self.hotword, so a live wake-word swap
        # (apply_hotword) mid-utterance cannot rewrite an already-started
        # utterance's prefix (bulletproof.5.2). Only read when hotword_active
        # is True, which is always preceded by a fresh detection that refreshes
        # it; initialized here as a defensive fallback.
        self._active_hotword = self.hotword
        self.matcher = PatternMatcher(catalog)

    def decide(
        self,
        word_event: WordEvent,
        mode: ProcessingMode,
        buffer: List[str],
        context: dict,
        hotword_active: bool = False,
        command_timeout_ms: int = 1000,
        replacement_timeout_ms: int = 400,
        greedy_timeout_ms: int = 5000
    ) -> Decision:
        """Make a routing decision for a word event.

        :flow: Speech Processing
        :step: 3.2
        :description: Evaluates word against truth table and current buffer state.
        :data_in: WordEvent, current mode, buffer, context.
        :data_out: Decision object specifying Action and payload.

        Args:
            word_event: The word event to route (required, cannot be None)
            mode: Current processing mode
            buffer: Current buffer contents (defaults to empty list if None)
            context: Additional context dictionary
            hotword_active: Whether hotword is currently active
            command_timeout_ms: Timeout for command buffering
            replacement_timeout_ms: Timeout for replacement buffering

        Returns:
            Decision object specifying action and payload

        Raises:
            ValueError: If word_event is None
        """
        # Input validation
        if word_event is None:
            raise ValueError("word_event cannot be None")
        if buffer is None:
            buffer = []
        if context is None:
            context = {}

        # 1. Check for Utterance End
        if word_event.is_utterance_end_marker:
            return Decision(Action.IGNORE, reason="Utterance end handled by processor")

        word = word_event.word
        
        # 2. Hotword Detection (Fresh Utterance)
        if mode == ProcessingMode.IDLE and word_event.start_of_utterance:
            if _word_matches_hotword(word, self.hotword):
                # Snapshot the wake word that started this buffer. If the live
                # wake word is swapped mid-utterance (apply_hotword), the
                # dictation fallback must still reconstruct the prefix the user
                # actually spoke, not the new word (bulletproof.5.2).
                self._active_hotword = self.hotword
                return Decision(
                    Action.TRANSITION,
                    target_mode=ProcessingMode.HOTWORD_BUFFERING,
                    timeout_ms=command_timeout_ms,
                    reason="Fresh hotword detected"
                )

        # 3. IDLE Mode Routing
        if mode == ProcessingMode.IDLE:
            return self._decide_idle(
                word_event, command_timeout_ms, replacement_timeout_ms, greedy_timeout_ms, hotword_active
            )

        # 4. BUFFERING Mode Routing
        return self._decide_buffering(word_event, mode, buffer, hotword_active, command_timeout_ms, replacement_timeout_ms, greedy_timeout_ms)

    def _greedy_timeout_for_buffer(
        self,
        buffer: Sequence[str],
        pattern_types: Tuple[str, ...],
        hotword_active: bool,
        greedy_timeout_ms: int,
    ) -> Optional[int]:
        """Probe ``buffer`` for a greedy fullmatch OR prefix under ``pattern_types``.

        Returns ``greedy_timeout_ms`` when any candidate type produces either:

        * a fullmatch on a greedy pattern (the original single-word case --
          e.g. ``parentheses`` fullmatching ``\\bparentheses(.*)$``), OR
        * a prefix match on a greedy multi-word pattern (e.g. buffer
          ``["angle"]`` is a prefix of ``\\bangle brackets(.*)$``).

        The prefix case fixes a race where a two-word greedy replacement
        ("angle brackets" / "single quotes") would attract only the standard
        short timer after the first word arrived, then time out before the
        second word landed. The helper is the single source of greedy-timer
        truth, so every entry point that decides a buffer timer agrees.

        ``pattern_types`` is a tuple of strings (e.g. ``("command",
        "replacement")``). It is intentionally typed as ``Tuple[str, ...]``
        and runtime-guarded against a bare ``str``: a single string is itself
        a ``Sequence[str]`` and would silently iterate character-by-character,
        causing the helper to never find a match. The two-layer check is
        belt-and-suspenders -- static narrowing for type checkers, runtime
        ``TypeError`` for dynamic callers.
        """
        if isinstance(pattern_types, str):
            raise TypeError(
                "_greedy_timeout_for_buffer.pattern_types must be a tuple of "
                "strings, not a bare str. Passing 'command' would iterate "
                "character-by-character and silently return None. Pass "
                "('command',) instead."
            )
        if not buffer:
            return None
        for ptype in pattern_types:
            result = self.matcher.match_for_routing(list(buffer), ptype, hotword_active)
            if result and result.matched and result.is_greedy:
                return greedy_timeout_ms
        # Fall through to the prefix probe. A multi-word greedy pattern (e.g.
        # ``\bangle brackets(.*)$``) will not fullmatch a one-word buffer, but
        # the buffer's first word IS a prefix of the pattern's literal
        # word-sequence and the user clearly intends to keep speaking.
        if self._buffer_is_greedy_prefix(buffer, pattern_types, hotword_active):
            return greedy_timeout_ms
        return None

    def _buffer_is_greedy_prefix(
        self,
        buffer: Sequence[str],
        pattern_types: Tuple[str, ...],
        hotword_active: bool,
    ) -> bool:
        """Return True if ``buffer`` is a prefix of any greedy pattern.

        A "greedy pattern" here is any compiled pattern in the catalog whose
        data dict has ``is_greedy=True`` AND whose source regex ends with a
        greedy capture (``(.*)``, ``(.+)``, ``.*``, or ``.+``). The literal
        text before that greedy tail is the "literal prefix" we test against.

        The buffer is considered a prefix when the joined buffer text either
        fullmatches the literal-prefix regex or matches the regex built from
        the first N literal words of the prefix (case-insensitive). We use
        the catalog's first-word index (the buffer's first word) to bound the
        candidate set, mirroring how the rest of the router probes patterns.

        Candidates whose data dict has ``requires_hotword=True`` are skipped
        when ``hotword_active`` is False. This mirrors the authorization gate
        that ``PatternMatcher.match_for_routing`` applies to fullmatches; the
        prefix probe must not attract the long greedy timer for hotword-only
        commands on a fresh non-hotword utterance.
        """
        if not buffer:
            return False
        first_word = buffer[0]
        candidates = self.catalog.get_matching_patterns(first_word)
        if not candidates:
            return False
        buffer_text = " ".join(buffer)
        for compiled_pattern, ptype, data in candidates:
            if ptype not in pattern_types:
                continue
            if not (data and data.get("is_greedy", False)):
                continue
            if data.get("requires_hotword", False) and not hotword_active:
                continue
            # Prefer the prefix pre-computed at catalog load time
            # (wh-greedy-prefix-precompute); fall back to runtime
            # extraction only for data dicts that predate the field
            # (synthetic test catalogs).
            literal_prefix = data.get("literal_prefix")
            if literal_prefix is None:
                literal_prefix = self._extract_literal_prefix(
                    compiled_pattern.pattern
                )
            if not literal_prefix:
                continue
            if self._buffer_matches_literal_prefix(buffer_text, literal_prefix):
                return True
        return False

    @staticmethod
    def _extract_literal_prefix(pattern_str: str) -> str:
        """Strip anchors / boundaries / greedy tail and return the literal core.

        Thin wrapper over ``pattern_transform.extract_literal_prefix`` --
        the single implementation the catalog also uses at load time
        (wh-greedy-prefix-precompute). Kept as a method for the synthetic
        -catalog fallback in ``_buffer_is_greedy_prefix`` and for the
        load-time-vs-runtime consistency test.
        """
        return extract_literal_prefix(pattern_str)

    @staticmethod
    def _buffer_matches_literal_prefix(buffer_text: str, literal_prefix: str) -> bool:
        """Return True if ``buffer_text`` matches the start of ``literal_prefix``.

        ``literal_prefix`` is a regex fragment (e.g. ``angle brackets`` or
        ``activates?``). We try two strategies:

        1. fullmatch the whole literal prefix -- the buffer already completes
           the literal portion of the greedy pattern (e.g. ``"parentheses"``
           fullmatching ``parentheses``).
        2. fullmatch successive N-word truncations of the literal prefix --
           the buffer is a prefix of the multi-word literal (e.g.
           ``"angle"`` fullmatching just ``angle``, the first word of
           ``angle brackets``).
        """
        try:
            literal_re = re.compile("^" + literal_prefix + "$", re.IGNORECASE)
        except re.error:
            return False
        if literal_re.match(buffer_text):
            return True
        words = literal_prefix.split()
        if not words:
            return False
        for n in range(1, len(words)):
            partial = " ".join(words[:n])
            try:
                partial_re = re.compile("^" + partial + "$", re.IGNORECASE)
            except re.error:
                continue
            if partial_re.match(buffer_text):
                return True
        return False

    def _decide_idle(
        self,
        word_event: WordEvent,
        command_timeout_ms: int,
        replacement_timeout_ms: int,
        greedy_timeout_ms: int,
        hotword_active: bool,
    ) -> Decision:
        """Handle routing when in IDLE mode."""
        word = word_event.word
        pattern_type = self.catalog.get_pattern_type(word)

        # Truth Table Logic
        match (word_event.start_of_utterance, pattern_type):
            # FRESH_PASSTHROUGH & MID_PASSTHROUGH
            case (_, PatternType.NONE):
                return Decision(Action.DICTATE, payload=word, reason="Passthrough (Not in catalog)")

            # FRESH_COMMAND
            case (True, PatternType.COMMAND):
                # Check if single word is complete and cannot continue
                if self._is_single_word_complete(word, "command", hotword_active) and self._cannot_match_with_next_word([word], "command"):
                    # wh-int8-punctuation-mishears: a whole-utterance-only
                    # pattern (sound-alike punctuation alias) must not fire
                    # until the utterance provably ends -- the next word, if
                    # one arrives, disproves the alias. Buffer instead; the
                    # end marker or timeout finalizes it.
                    if not self._matches_whole_utterance_only([word], "command", hotword_active):
                        return Decision(Action.EXECUTE, payload=word, reason="Single word command complete")

                # wh-l4h.1.14: hotword-aware impossibility check. If the word's
                # only candidate command patterns require the hotword and the
                # hotword is inactive, the word can never match a command, so
                # finalize as dictation IMMEDIATELY instead of buffering for the
                # full command_timeout. Mirrors the wh-4o1aj gate in
                # _decide_buffering. Reuses the hotword-aware matcher check;
                # when the hotword is active the word stays a live candidate and
                # this guard does not fire.
                if self._cannot_match([word], "command", hotword_active):
                    return Decision(
                        Action.DICTATE,
                        payload=word,
                        reason="Fresh hotword-only command impossible (hotword inactive)",
                    )

                greedy = self._greedy_timeout_for_buffer(
                    [word], ("command",), hotword_active, greedy_timeout_ms
                )
                if greedy is not None:
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        target_mode=ProcessingMode.COMMAND_BUFFERING,
                        timeout_ms=greedy,
                        reason="Fresh command buffering (greedy match available)"
                    )
                return Decision(
                    Action.BUFFER,
                    payload=word,
                    target_mode=ProcessingMode.COMMAND_BUFFERING,
                    timeout_ms=command_timeout_ms,
                    reason="Fresh command buffering"
                )

            # FRESH_REPLACEMENT
            case (True, PatternType.REPLACEMENT):
                # Check if single word is complete and cannot continue
                if self._is_single_word_complete(word, "replacement") and self._cannot_match_with_next_word([word], "replacement"):
                     # For replacement, "Execute" means run through TextParser (unified)
                     return Decision(Action.EXECUTE, payload=word, reason="Single word replacement complete")

                greedy = self._greedy_timeout_for_buffer(
                    [word], ("replacement",), hotword_active, greedy_timeout_ms
                )
                if greedy is not None:
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                        timeout_ms=greedy,
                        reason="Fresh replacement buffering (greedy match available)"
                    )
                return Decision(
                    Action.BUFFER,
                    payload=word,
                    target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                    timeout_ms=replacement_timeout_ms,
                    reason="Fresh replacement buffering"
                )

            # MID_COMMAND_PASSTHROUGH
            case (False, PatternType.COMMAND):
                return Decision(Action.DICTATE, payload=word, reason="Mid-utterance command passthrough")

            # MID_REPLACEMENT_BUFFER
            case (False, PatternType.REPLACEMENT):
                greedy = self._greedy_timeout_for_buffer(
                    [word], ("replacement",), hotword_active, greedy_timeout_ms
                )
                if greedy is not None:
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                        timeout_ms=greedy,
                        reason="Mid-utterance replacement buffering (greedy match available)"
                    )
                return Decision(
                    Action.BUFFER,
                    payload=word,
                    target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                    timeout_ms=replacement_timeout_ms,
                    reason="Mid-utterance replacement buffering"
                )

            case _:
                # Should not happen
                return Decision(Action.DICTATE, payload=word, reason="Unhandled case fallback")

    def _decide_buffering(
        self,
        word_event: WordEvent,
        mode: ProcessingMode,
        buffer: List[str],
        hotword_active: bool,
        command_timeout_ms: int,
        replacement_timeout_ms: int,
        greedy_timeout_ms: int = 5000
    ) -> Decision:
        """Handle routing when in BUFFERING mode."""
        word = word_event.word
        
        # Simulate adding word to buffer
        new_buffer = buffer + [word]
        new_buffer_text = " ".join(new_buffer)
        
        # Determine target type for checks
        if mode in (ProcessingMode.COMMAND_BUFFERING, ProcessingMode.HOTWORD_BUFFERING):
            target_type = "command"
        else:
            target_type = "replacement"

        # 1. Check for Utterance End
        if word_event.end_of_utterance:
            # Must finalize. The Processor will handle the "Finalize" logic (try match, else dictate)
            # We can return a special action or just DICTATE if we know it won't match?
            # Actually, Processor's _finalize logic is complex. 
            # Let's return a FINALIZE action so Processor calls its finalize logic.
            # But wait, I defined Action.EXECUTE, DICTATE...
            # Let's add a generic "PROCESS_BUFFER" or handle it here.
            # If I return DICTATE, it dictates the word. What about the buffer?
            # The Decision payload should probably be the *full* text if we are finalizing?
            # Or the Processor handles the buffer.
            
            # Let's look at the Processor refactor plan.
            # "Implement _execute_decision(decision) to handle the side effects"
            # If I return Decision(Action.FINALIZE), the processor can call _finalize_and_return_to_idle().
            # But I didn't add FINALIZE to Action enum.
            # Let's use EXECUTE if it matches, DICTATE if it doesn't?
            # But "Finalize" tries Command -> Replacement -> Dictate.
            # The Router should do this logic.
            
            return self._resolve_finalization(new_buffer, hotword_active)

        # 2. Check for Complete Pattern
        result = self.matcher.match_for_routing(new_buffer, target_type, hotword_active)
        if result and result.matched and not result.is_greedy:
            # wh-int8-punctuation-mishears: a whole-utterance-only pattern
            # (sound-alike punctuation alias) matches the buffer, but the
            # utterance may still continue ("come on" -> "come on over").
            # Keep buffering; the end marker (step 1), a disproving next
            # word (step 3), or the timeout finalizes it.
            if result.pattern_data.get("whole_utterance_only"):
                timeout = command_timeout_ms if mode in (ProcessingMode.COMMAND_BUFFERING, ProcessingMode.HOTWORD_BUFFERING) else replacement_timeout_ms
                return Decision(
                    Action.BUFFER,
                    payload=word,
                    timeout_ms=timeout,
                    reason="Whole-utterance-only pattern matched; awaiting utterance end",
                )

            # Check for unfilled optional numeric group: if the pattern has a
            # validation_group but the captured value is None, the optional count
            # hasn't been spoken yet. Continue buffering so it can arrive.
            # Example: "back space" matches but "back space three" is better.
            # Timeout will finalize if no number comes.
            if result.validation_group and self._has_unfilled_numeric_group(result):
                timeout = command_timeout_ms if mode in (ProcessingMode.COMMAND_BUFFERING, ProcessingMode.HOTWORD_BUFFERING) else replacement_timeout_ms
                return Decision(Action.BUFFER, payload=word, timeout_ms=timeout, reason="Pattern matches but optional count unfilled, continue buffering")

            # If the match is mid-buffer (either side has leftover text),
            # keep the leftovers on the Decision so the Processor can
            # dictate the prefix (before_remainder) before executing the
            # match and process the suffix (remainder) afterward. Dropping
            # the prefix was wh-8jy: buffer ['question', 'period'] matched
            # the bare '\bperiod\b' replacement mid-string and the Router
            # emitted only "period", losing "question".
            if result.remainder or result.before_remainder:
                return Decision(
                    Action.EXECUTE,
                    payload=result.matched_text,
                    remainder=result.remainder,
                    before_remainder=result.before_remainder,
                    reason=(
                        f"Pattern complete with before='{result.before_remainder}' "
                        f"after='{result.remainder}'"
                    ),
                )
            return Decision(Action.EXECUTE, payload=new_buffer_text, reason="Pattern complete")
            
        # 3. Check for Impossible Pattern
        # Skip for HOTWORD_BUFFERING (don't know if command or dictation yet)
        if mode != ProcessingMode.HOTWORD_BUFFERING:
            if self._cannot_match(new_buffer, target_type, hotword_active):
                # If Command failed, check if it could be a Replacement
                if mode == ProcessingMode.COMMAND_BUFFERING:
                    if self._can_match_replacement(new_buffer):
                        # wh-greedy-helper-impl follow-up: this branch is also
                        # a word-buffer entry point. If the switched-to
                        # replacement is greedy (fullmatch OR prefix), use the
                        # long greedy timer instead of the short replacement
                        # one so a slow STT cannot race the timer.
                        switch_greedy = self._greedy_timeout_for_buffer(
                            new_buffer, ("replacement",), hotword_active, greedy_timeout_ms
                        )
                        return Decision(
                            Action.BUFFER,
                            payload=word,
                            target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                            timeout_ms=switch_greedy if switch_greedy is not None else replacement_timeout_ms,
                            reason=(
                                "Switch to replacement buffering (greedy match available)"
                                if switch_greedy is not None
                                else "Switch to replacement buffering"
                            ),
                        )

                # Impossible -> Finalize
                return self._resolve_finalization(new_buffer, hotword_active)

        # 4. Continue Buffering
        # wh-greedy-buffer-race / wh-greedy-hotword-replacement-gap: when the
        # current buffer already matches a greedy "swallow the rest" pattern
        # (one containing .* or .+), the user's intent is to consume the
        # entire utterance. Use the longer greedy timer so end-of-utterance
        # reliably wins the race against the buffer timer. In
        # HOTWORD_BUFFERING mode the route is undecided, so probe both command
        # and replacement patterns; the step 2 fullmatch above only probed
        # the target_type and would miss a greedy replacement that the hotword
        # path will eventually classify as a replacement.
        if mode == ProcessingMode.HOTWORD_BUFFERING:
            probe_types: Tuple[str, ...] = ("command", "replacement")
        else:
            probe_types = (target_type,)
        greedy = self._greedy_timeout_for_buffer(
            new_buffer, probe_types, hotword_active, greedy_timeout_ms
        )
        if greedy is not None:
            return Decision(
                Action.BUFFER,
                payload=word,
                timeout_ms=greedy,
                reason="Continue buffering (greedy match available)",
            )
        timeout = command_timeout_ms if mode in (ProcessingMode.COMMAND_BUFFERING, ProcessingMode.HOTWORD_BUFFERING) else replacement_timeout_ms
        return Decision(Action.BUFFER, payload=word, timeout_ms=timeout, reason="Continue buffering")

    def decide_timeout(self, buffer: List[str], hotword_active: bool) -> Decision:
        """Make a decision when timeout expires.
        
        :flow: Speech Processing
        :step: 3.3
        :description: Resolves buffer state when timeout occurs.
        :data_in: Current buffer contents and hotword state.
        :data_out: Decision to EXECUTE (if matched) or DICTATE (fallback).
        """
        return self._resolve_finalization(buffer, hotword_active)

    def _resolve_finalization(self, buffer: List[str], hotword_active: bool) -> Decision:
        """Resolve finalization logic: Command -> Replacement -> Dictate.

        Uses PatternMatcher for consolidated matching logic.
        """
        if not buffer:
            return Decision(Action.IGNORE, reason="Empty buffer, nothing to finalize")

        buffer_text = " ".join(buffer)

        # 1. Try Command (uses PatternMatcher.match_for_routing)
        # The payload is the RAW buffer text, not result.matched_text.
        # The command engine re-matches this payload downstream
        # (speech_processor._execute_command -> command_engine.
        # parse_and_execute -> match_single_pattern), and that re-match
        # re-applies the same punctuation normalization the matcher used
        # here. So STT punctuation on the last word ("backspace,") and
        # between a command word and its count ("back space, 3") is
        # normalized at execution time, not here. That downstream
        # re-match is the load-bearing step for the count surviving on
        # this whole-buffer path; test_interior_comma_whole_buffer_command
        # pins it (wh-midword-punct-severs-count.1.3).
        result = self.matcher.match_for_routing(buffer, "command", hotword_active)
        if result and result.matched:
            return Decision(Action.EXECUTE, payload=buffer_text, reason="Finalized as command")

        # 1b. Try a command PREFIX of the buffer (wh-cmd-prefix-not-split).
        # Commands are ^...$-anchored, so step 1 only matches the whole
        # buffer; 'backspace hello world' fell through to dictation and
        # the command word was typed as text. Search longest-prefix-first
        # so 'select all hello' executes 'select all', not a shorter
        # match. The unmatched suffix rides the EXECUTE decision's
        # remainder, which the processor already handles (replacements
        # apply, the rest dictates -- the wh-8jy machinery). Greedy
        # prefixes are excluded: a greedy command consumes the rest of
        # the buffer, so step 1 already decided it does not match.
        #
        # Every prefix shares the first word, so when the catalog has no
        # command pattern starting with it, no prefix can match -- skip
        # the whole probe loop instead of running it once per word on
        # every finalization (wh-cmd-prefix-not-split.2.1).
        # get_matching_patterns normalizes its lookup key (wh-9f51.1),
        # so an STT punctuation tail on the first token cannot cause a
        # wrong skip.
        first_word_has_command = any(
            ptype == "command"
            for _, ptype, _ in self.catalog.get_matching_patterns(buffer[0])
        )
        if first_word_has_command:
            for k in range(len(buffer) - 1, 0, -1):
                result = self.matcher.match_for_routing(buffer[:k], "command", hotword_active)
                if (
                    result
                    and result.matched
                    and not result.is_greedy
                    and not result.before_remainder
                ):
                    # wh-int8-punctuation-mishears: a whole-utterance-only
                    # pattern (sound-alike punctuation alias) may never fire
                    # as a prefix of a longer utterance -- "come home" must
                    # dictate, not execute "come" as a comma. Skip it; the
                    # buffer falls through to dictation.
                    if result.pattern_data.get("whole_utterance_only"):
                        continue
                    # wh-midword-punct-severs-count.3.1: reject a prefix
                    # whose optional numeric count is UNFILLED when a
                    # number that would fill it sits just past standalone
                    # STT punctuation. "delete , 3" (from spoken "delete
                    # 3") matches the prefix "delete ," -> bare "delete"
                    # by stripping the lone comma, then dictates "3" --
                    # firing the countless command and severing the
                    # count. The whole-buffer matcher already bails on
                    # this shape; without this guard the prefix loop
                    # revives the spurious-command class reviewer_0
                    # removed. Skip so it falls through to dictation.
                    # This does NOT block "delete hello world": "hello"
                    # is not a count, so next_word_fills_numeric_count is
                    # False and the leading command still executes.
                    #
                    # Accepted trade-off (wh-midword-punct-severs-count.4.1):
                    # words_to_int maps the homophones "for" -> 4, "to" and
                    # "too" -> 2, so "delete , for example" is suppressed to
                    # dictation. This is deliberate. It matches the
                    # system-wide count-word definition ("delete for" with
                    # no comma already fires four deletes), and using a
                    # stricter number check only here would make the guard
                    # disagree with the command engine's own validation. For
                    # an accessibility tool, dictating rather than firing a
                    # delete on that ambiguous input is the safe direction.
                    if self._has_unfilled_numeric_group(
                        result
                    ) and self.matcher.next_word_fills_numeric_count(buffer[k:]):
                        continue
                    # result.remainder here can only be the matcher's
                    # punctuation-retry tail (a fullmatch leaves no other
                    # leftover). That tail is STT/ITN-attached noise
                    # between the command and the next word -- the
                    # wh-9f51.3 convention discards it, so it must not be
                    # typed into the suffix (wh-cmd-prefix-not-split.1.1).
                    remainder = " ".join(buffer[k:])
                    return Decision(
                        Action.EXECUTE,
                        payload=result.matched_text,
                        remainder=remainder,
                        reason=f"Finalized as command prefix with after='{remainder}'",
                    )

        # 2. Try Replacement (uses PatternMatcher.match_for_routing)
        result = self.matcher.match_for_routing(buffer, "replacement", hotword_active=False)
        if result and result.matched:
            if result.remainder or result.before_remainder:
                return Decision(
                    Action.EXECUTE,
                    payload=result.matched_text,
                    remainder=result.remainder,
                    before_remainder=result.before_remainder,
                    reason=f"Finalized as replacement with before='{result.before_remainder}' after='{result.remainder}'"
                )
            else:
                return Decision(Action.EXECUTE, payload=buffer_text, reason="Finalized as replacement")

        # 3. Fallback to Dictation
        final_text = buffer_text
        if hotword_active:
            # Reconstruct from the wake word captured when this buffer started,
            # not the live self.hotword, so a mid-utterance wake-word swap does
            # not insert a word the user never spoke (bulletproof.5.2).
            final_text = f"{self._active_hotword} {buffer_text}"

        return Decision(Action.DICTATE, payload=final_text, reason="Finalized as dictation")

    # ========================================================================
    # HELPER METHODS (Moved from SpeechProcessor)
    # ========================================================================

    def _is_single_word_complete(self, word: str, target_type: str, hotword_active: bool = False) -> bool:
        return self._is_pattern_complete([word], target_type, hotword_active)

    def _matches_whole_utterance_only(
        self, buffer: List[str], target_type: str, hotword_active: bool = False
    ) -> bool:
        """True when the buffer's routing match is a whole-utterance-only pattern.

        Whole-utterance-only patterns (sound-alike punctuation aliases,
        wh-int8-punctuation-mishears) must not execute before the utterance
        provably ends. Uses the same first-match-wins lookup the execute
        paths use, so the check agrees with the pattern that would fire.
        """
        result = self.matcher.match_for_routing(buffer, target_type, hotword_active)
        return bool(
            result
            and result.matched
            and result.pattern_data.get("whole_utterance_only")
        )

    def _is_pattern_complete(self, buffer: List[str], target_type: str, hotword_active: bool = False) -> bool:
        """Check if buffer contains a complete pattern.

        Delegates to PatternMatcher.is_pattern_complete().
        """
        return self.matcher.is_pattern_complete(buffer, target_type, hotword_active)

    def _cannot_match(self, buffer: List[str], target_type: str, hotword_active: bool = False) -> bool:
        """Check if buffer cannot match any pattern.

        Delegates to PatternMatcher.cannot_match(). hotword_active is forwarded
        so a buffer whose only candidate command patterns require the hotword
        reports cannot_match=True when the hotword is inactive, letting
        _decide_buffering finalize it as dictation immediately instead of
        waiting command_timeout (wh-4o1aj).
        """
        return self.matcher.cannot_match(buffer, target_type, hotword_active)

    def _cannot_match_with_next_word(self, buffer: List[str], target_type: str) -> bool:
        """Check if buffer cannot possibly match any pattern even with more words.

        Returns True if buffer is "closed" and additional words cannot lead to a match.
        Returns False if more words could potentially complete a pattern.

        This is used to decide whether single-word patterns should execute immediately
        or wait for potential continuation (e.g., "delete" waiting for optional count).
        """
        if not buffer:
            return False  # Empty buffer can always continue

        first_word = buffer[0]
        patterns = self.catalog.get_matching_patterns(first_word)
        if not patterns:
            return True  # No patterns start with this word

        buffer_text = " ".join(buffer)

        for compiled_pattern, pattern_type, data in patterns:
            if pattern_type != target_type:
                continue

            pattern_str = compiled_pattern.pattern

            # Check 1: Multi-word patterns (contains space in literal part)
            # If pattern has a space and buffer doesn't yet, more words could match
            # Strip regex anchors/boundaries to check for literal spaces
            stripped = pattern_str.replace(r'\b', '').replace('^', '').replace('$', '')
            if " " in stripped and " " not in buffer_text:
                return False  # CAN continue - pattern expects more words

            # Check 2: Patterns with optional components (quantifiers)
            # These patterns can potentially accept more input
            if "?" in pattern_str or "*" in pattern_str or "+" in pattern_str:
                # Numeric patterns can continue with digits
                if "\\d" in pattern_str:
                    return False  # CAN continue
                # Word patterns can continue with more words
                if "\\w" in pattern_str or "\\s" in pattern_str:
                    return False  # CAN continue
                # General check: does pattern match with trailing space?
                if compiled_pattern.match(buffer_text + " "):
                    return False  # CAN continue

        return True  # No patterns found that can continue

    def _has_unfilled_numeric_group(self, result) -> bool:
        """Check if a match result has an unfilled optional numeric group.

        Returns True if the pattern has a validation_group (indicating an optional
        numeric parameter like repeat count) and the captured value is None
        (meaning no number was spoken yet).

        This is used to decide whether to continue buffering: patterns like
        "back space" match without a count, but we should wait for a potential
        "three" before executing.

        Args:
            result: MatchResult from PatternMatcher

        Returns:
            True if the numeric group exists but captured None
        """
        if not result.validation_group or not result.match_object:
            return False
        try:
            group_num = int(result.validation_group[1:])  # "g1" -> 1
            return result.match_object.group(group_num) is None
        except (ValueError, IndexError):
            return False

    def _can_match_replacement(self, buffer: List[str]) -> bool:
        """Check if buffer could match a replacement pattern.

        Used when switching from COMMAND_BUFFERING to REPLACEMENT_BUFFERING.
        Returns True if any replacement pattern indexed under the first word
        matches the buffer text.

        Note: Uses search() because replacement patterns can match mid-text.
        Limitation: Only checks patterns indexed under the first word.
        """
        if not buffer:
            return False

        first_word = buffer[0]
        patterns = self.catalog.get_matching_patterns(first_word)
        buffer_text = " ".join(buffer)

        for compiled_pattern, pattern_type, data in patterns:
            if pattern_type == "replacement":
                if compiled_pattern.search(buffer_text):
                    return True
        return False
