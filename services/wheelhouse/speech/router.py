import logging
from typing import Optional, List, Sequence, Tuple

from .domain import ProcessingMode, Action, Decision
from .word_event import WordEvent
from .pattern_catalog import PatternCatalog, PatternType
from .pattern_matcher import PatternMatcher
from .pattern_transform import (
    build_literal_prefix_matchers,
    extract_literal_prefix,
)
from .number_word_parser import number_phrase_can_extend

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
        hotword_active: bool = False,
        command_timeout_ms: int = 1000,
        replacement_timeout_ms: int = 400,
        greedy_timeout_ms: int = 5000,
        utterance_words: Optional[List[str]] = None,
    ) -> Decision:
        """Make a routing decision for a word event.

        :flow: Speech Processing
        :step: 3.2
        :description: Evaluates word against truth table and current buffer state.
        :data_in: WordEvent, current mode, buffer.
        :data_out: Decision object specifying Action and payload.

        Args:
            word_event: The word event to route (required, cannot be None)
            mode: Current processing mode
            buffer: Current buffer contents (defaults to empty list if None)
            hotword_active: Whether hotword is currently active
            command_timeout_ms: Timeout for command buffering
            replacement_timeout_ms: Timeout for replacement buffering
            utterance_words: Complete word list of the current
                utterance, including this event's word (see
                ``decide_timeout``). Only the end_of_utterance
                finalization path reads it -- the in-process STT
                bridge flags the utterance's last real word, and that
                finalization needs the same whole_utterance_only span
                check the marker/timeout path applies
                (wh-whole-utterance-command-matching.3.1.1).

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
        return self._decide_buffering(word_event, mode, buffer, hotword_active, command_timeout_ms, replacement_timeout_ms, greedy_timeout_ms, utterance_words)

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
            # Prefer the matchers compiled at catalog load time
            # (wh-greedy-prefix-precompute, extended by
            # wh-lru-cache-hot-paths.1.5). They live on the pattern data, so
            # WheelHouse's reference to them dies with the pattern:
            # PatternCatalog.reload() rebuilds these dicts, and the catalog
            # then holds nothing for a user rule the pattern editor deletes.
            # A cache on this class could not be reached from reload() and
            # retained them instead. CPython's own regex cache keeps the
            # compiled pattern until eviction either way; see
            # build_literal_prefix_matchers (wh-lru-cache-hot-paths.1.6).
            matchers = data.get("literal_prefix_matchers")
            if matchers is None:
                # Fall back to runtime extraction and compilation only for
                # data dicts that predate the fields (synthetic test
                # catalogs). This path compiles on every call by design: no
                # cache here, because the prefix is not guaranteed to come
                # from a bounded set.
                literal_prefix = data.get("literal_prefix")
                if literal_prefix is None:
                    literal_prefix = self._extract_literal_prefix(
                        compiled_pattern.pattern
                    )
                if not literal_prefix:
                    continue
                matchers = build_literal_prefix_matchers(literal_prefix)
            if any(matcher.match(buffer_text) for matcher in matchers):
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
        ``activates?``) and may carry ``\\s+`` / ``\\s*`` whitespace escapes
        as word separators: ``extract_literal_prefix`` keeps a trailing
        ``\\s+`` (the shipped click command's prefix is ``click\\s+``), and a
        source pattern may join its literal words with ``\\s+`` instead of a
        real space. Those escapes are normalized to single spaces (with the
        result stripped, so a trailing separator vanishes) before matching --
        otherwise strategy 1 demands trailing whitespace the buffer never
        contains, and ``str.split()`` sees the escape as part of one long
        token, so a one-word buffer like ``"click"`` never matched its own
        prefix and fell to the short command timer instead of the greedy one
        (wh-click-number-dictation).

        Two strategies against the normalized prefix:

        1. fullmatch the whole literal prefix -- the buffer already completes
           the literal portion of the greedy pattern (e.g. ``"parentheses"``
           fullmatching ``parentheses``).
        2. fullmatch successive N-word truncations of the literal prefix --
           the buffer is a prefix of the multi-word literal (e.g.
           ``"angle"`` fullmatching just ``angle``, the first word of
           ``angle brackets``).

        This helper compiles on every call and is used only where no
        pre-computed matchers are available: synthetic test catalogs whose
        pattern data predates the ``literal_prefix_matchers`` field. The
        production path in ``_buffer_is_greedy_prefix`` reads the matchers the
        catalog built at load time instead.

        Nothing here is cached, and neither is ``build_literal_prefix_matchers``.
        Read that function's docstring for the two review findings that
        established where the compiled matchers belong
        (wh-lru-cache-hot-paths.1.2 and .1.5). The short version: this function
        takes ``buffer_text``, which is whatever the user said, so a cache here
        retains the spoken text; and a cache keyed on ``literal_prefix`` alone
        still grows without bound, because a user rule can supply any prefix
        and ``PatternCatalog.reload()`` cannot reach a cache on this class.
        """
        return any(
            matcher.match(buffer_text)
            for matcher in build_literal_prefix_matchers(literal_prefix)
        )

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
                    if self._replacement_patterns_for_first_word([word]):
                        greedy = self._greedy_timeout_for_buffer(
                            [word], ("replacement",), hotword_active, greedy_timeout_ms
                        )
                        return Decision(
                            Action.BUFFER,
                            payload=word,
                            target_mode=ProcessingMode.REPLACEMENT_BUFFERING,
                            timeout_ms=(
                                greedy
                                if greedy is not None
                                else replacement_timeout_ms
                            ),
                            reason="Fresh replacement buffering after impossible command",
                        )
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
                if self._replacement_patterns_for_first_word([word]):
                    greedy = self._greedy_timeout_for_buffer(
                        [word], ("replacement",), hotword_active, greedy_timeout_ms
                    )
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        target_mode=ProcessingMode.MID_REPLACEMENT_BUFFERING,
                        timeout_ms=(
                            greedy if greedy is not None else replacement_timeout_ms
                        ),
                        reason="Mid-utterance replacement buffering after command collision",
                    )
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
        greedy_timeout_ms: int = 5000,
        utterance_words: Optional[List[str]] = None,
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
            
            return self._resolve_finalization(
                new_buffer,
                hotword_active,
                allow_commands=mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING,
                utterance_words=utterance_words,
            )

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

            # An optional count keeps the buffer open in two shapes, and
            # both mean the same thing: the number may not be finished.
            #
            # UNFILLED -- the count has not been spoken at all. "back
            # space" matches, but "back space three" is the better match,
            # so wait for a number to arrive.
            #
            # GROWABLE -- the count is filled with a phrase that another
            # number word could still extend. "twenty" is a complete count
            # and "twenty three" is a different one, so executing the
            # moment "twenty" arrived pressed backspace twenty times and
            # then dictated "three" (wh-whole-utterance-command-matching.4).
            # number_phrase_can_extend answers this against the parser's
            # own grammar, so "23", "ninety nine" and "ten" still execute
            # at once; only a phrase a word could really extend waits.
            #
            # Either way the wait ends at the end of the utterance: the
            # end marker finalizes the buffer at step 1, and the timeout
            # finalizes a buffer no end marker reaches. A next word that
            # cannot extend the count does NOT end the wait where it
            # arrives. It makes the buffer impossible, and step 3 defers
            # an impossible buffer to the utterance end
            # (wh-whole-utterance-command-matching.3), so the prefix loop
            # still produces "backspace three hello" -> three presses
            # then "hello", but at the end marker rather than at "hello".
            # Measured, not assumed (wh-whole-utterance-command-matching
            # .4.1.3): "hello" and "question" reach that same branch at
            # the same moment, so releasing the command at "hello" would
            # also release it at "question" and split "question mark",
            # which is the word-speed split that deferral removed.
            # crewcut: both checks below read ONE count -- the first
            # numeric capture -- and only the growable check asks whether
            # that capture is still at the open end of the text. Two
            # limits follow, both accepted on this branch
            # (wh-whole-utterance-command-matching.4.1.2 and .4.1.4).
            # A pattern with two counts gets the wait for the first one
            # and nothing for the second, because
            # pattern_transform.py:3175 publishes validation_group as the
            # first widened capture alone. And an UNFILLED count waits
            # even when its position is already closed, so a pattern that
            # puts a required literal after an optional count, or that
            # leaves the first capture empty because a different
            # alternation branch matched, waits for a count that can
            # never arrive. To remove both: publish every widened capture
            # number, then ask the match which count position is still
            # open instead of using the first validation_group as a proxy
            # -- and audit the other _has_unfilled_numeric_group caller
            # in the finalization prefix path plus every validate_numeric
            # caller, which read the same metadata. Neither limit is
            # reachable through a shipped pattern: backspace is the one
            # shipped pattern that reaches these checks, it has a single
            # count, and that count sits at the open end. A pattern
            # written in the Advanced editor can reach both.
            if result.validation_group:
                unfilled = self._has_unfilled_numeric_group(result)
                growable = not unfilled and self._count_can_still_grow(result)
                if unfilled or growable:
                    timeout = command_timeout_ms if mode in (ProcessingMode.COMMAND_BUFFERING, ProcessingMode.HOTWORD_BUFFERING) else replacement_timeout_ms
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        timeout_ms=timeout,
                        reason=(
                            "Pattern matches but optional count could still grow, continue buffering"
                            if growable
                            else "Pattern matches but optional count unfilled, continue buffering"
                        ),
                    )

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

                # wh-whole-utterance-command-matching.3: an impossible
                # utterance-start buffer DEFERS to the utterance end
                # instead of finalizing at word speed. The buffer keeps
                # accumulating the utterance's words with the mode's
                # fixed timeout as the no-end-marker fallback; the end
                # marker (step 1), the timeout (decide_timeout), the
                # new-utterance auto-finalize, or the lifecycle close
                # then matches the COMPLETE word list. This is what lets
                # "backspace question mark" fire backspace and type "?"
                # instead of splitting at word speed. Mid-utterance
                # speculative buffers (MID_REPLACEMENT_BUFFERING) keep
                # the immediate finalization: their word-speed release
                # is the Stage-4 passthrough behavior, out of scope
                # here.
                if mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING:
                    timeout = (
                        command_timeout_ms
                        if mode is ProcessingMode.COMMAND_BUFFERING
                        else replacement_timeout_ms
                    )
                    return Decision(
                        Action.BUFFER,
                        payload=word,
                        timeout_ms=timeout,
                        reason=(
                            "Impossible buffer deferred to utterance end"
                        ),
                    )

                # Impossible -> Finalize
                return self._resolve_finalization(
                    new_buffer,
                    hotword_active,
                    allow_commands=False,
                )

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

    def decide_timeout(
        self,
        buffer: List[str],
        hotword_active: bool,
        mode: Optional[ProcessingMode] = None,
        utterance_words: Optional[List[str]] = None,
    ) -> Decision:
        """Make a decision when timeout expires.

        :flow: Speech Processing
        :step: 3.3
        :description: Resolves buffer state when timeout occurs.
        :data_in: Current buffer contents and hotword state.
        :data_out: Decision to EXECUTE (if matched) or DICTATE (fallback).

        utterance_words is the complete word list of the utterance the
        buffer belongs to (wh-whole-utterance-command-matching.3). When
        provided, a whole-buffer match on a whole_utterance_only
        pattern fires only when the buffer really spans that utterance.
        None keeps the legacy fire-on-buffer-match behavior for callers
        that have no utterance context.
        """
        return self._resolve_finalization(
            buffer,
            hotword_active,
            allow_commands=mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING,
            utterance_words=utterance_words,
        )

    def _resolve_finalization(
        self,
        buffer: List[str],
        hotword_active: bool,
        *,
        allow_commands: bool = True,
        utterance_words: Optional[List[str]] = None,
    ) -> Decision:
        """Resolve finalization logic: Command -> Replacement -> Dictate.

        Uses PatternMatcher for consolidated matching logic.

        utterance_words: see ``decide_timeout``. None means no
        utterance context (legacy callers); the whole_utterance_only
        span check is skipped.
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
        result = (
            self.matcher.match_for_routing(buffer, "command", hotword_active)
            if allow_commands
            else None
        )
        if result and result.matched:
            # wh-whole-utterance-command-matching.3: a
            # whole_utterance_only pattern means the ENTIRE utterance
            # (patterns.toml doc block), and under the end-marker
            # deferral a buffer no longer always equals the utterance.
            # With utterance context available, fire the alias only
            # when the buffer spans the utterance; otherwise fall
            # through (the prefix loop already skips these, so the
            # buffer resolves as replacement or dictation).
            if (
                utterance_words is not None
                and result.pattern_data.get("whole_utterance_only")
                and not self._buffer_spans_utterance(
                    buffer, utterance_words, hotword_active
                )
            ):
                pass
            else:
                return Decision(
                    Action.EXECUTE,
                    payload=buffer_text,
                    reason="Finalized as command",
                )

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
        first_word_has_command = allow_commands and any(
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

    def _buffer_spans_utterance(
        self,
        buffer: List[str],
        utterance_words: List[str],
        hotword_active: bool,
    ) -> bool:
        """True when the buffer covers the utterance's complete word list.

        wh-whole-utterance-command-matching.3: the buffer spans the
        utterance when the utterance's words ARE the buffer, or when
        the only word in front of the buffer is the active wake word --
        the TRANSITION that activates the hotword clears it from the
        buffer, so "x-ray save" with the hotword active is still "save"
        as the whole utterance. Any other head word means earlier
        speech streamed past this buffer, so a whole-utterance-only
        pattern must not fire.
        """
        head_len = len(utterance_words) - len(buffer)
        if head_len < 0 or utterance_words[head_len:] != buffer:
            return False
        head = utterance_words[:head_len]
        if not head:
            return True
        if hotword_active and len(head) == 1:
            # Judge the head with the same hyphen-insensitive equality
            # that detected it (step 2 of decide), against the same
            # snapshot the dictation fallback reconstructs from -- an
            # exact-string compare here rejects a fused "xray" that
            # detection accepted (wh-whole-utterance-command-matching
            # .3.1.2), and the live catalog value can differ from the
            # wake word this buffer actually started with
            # (bulletproof.5.2).
            if _word_matches_hotword(head[0], self._active_hotword):
                return True
        return False

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

    def _count_can_still_grow(self, result) -> bool:
        """Check whether a FILLED count could be extended by another word.

        The sibling of ``_has_unfilled_numeric_group``: that one asks
        whether a count has arrived, this one asks whether the count that
        arrived is finished. A spoken number reaches the router one word
        at a time, so a group holding "twenty" is a complete count AND a
        prefix of "twenty three"; acting on it cut the number short
        (wh-whole-utterance-command-matching.4).

        The answer comes from speech/number_word_parser.py, the one
        word-to-integer implementation, so there is no second copy of the
        vocabulary here and no list to keep in step. The aliases and zero
        options match speech/actions.py:words_to_int, which is what
        actually parses this count when the command runs -- passing a
        different pair would make this answer disagree with the value the
        command engine derives.

        The count must also END the matched text, because that is the
        only place a later word could join it. A pattern may put literal
        text after its capture -- "^tab (\\d+) times$" -- and then the
        word after the count has already been spoken by the time the
        pattern matches, so the count is final and waiting protects
        nothing; it only delays the command to the end marker or the
        command timeout (wh-whole-utterance-command-matching.4.1.1). No
        shipped pattern has that shape (backspace is the one shipped
        pattern that reaches this check at all, and its count ends the
        match), but the Advanced pattern editor lets a user write one
        and records no whole_utterance_only flag for a new entry, so a
        user pattern does reach here. The subject string is the joined
        buffer text (pattern_matcher.py:588), so its end is the position
        the next spoken word would land after.

        Args:
            result: MatchResult from PatternMatcher.

        Returns:
            True only for a filled count that ends the matched text and
            that some number word extends. False for an unfilled count
            (that is the other helper's question), for a count spoken as
            digits, for a count the pattern already bounds, and for a
            phrase nothing can extend.
        """
        # The first-capture limit this shares with
        # _has_unfilled_numeric_group is recorded as one crewcut: comment
        # at their shared call site in _decide_buffering.
        if not result.validation_group or not result.match_object:
            return False
        try:
            group_num = int(result.validation_group[1:])  # "g1" -> 1
            captured = result.match_object.group(group_num)
            group_end = result.match_object.end(group_num)
        except (ValueError, IndexError):
            return False
        if captured is None:
            return False
        if group_end != len(result.match_object.string):
            return False
        return number_phrase_can_extend(captured, aliases=True, zero=True)

    def _replacement_patterns_for_first_word(self, buffer: List[str]):
        """Return replacement candidates indexed under ``buffer``'s first word.

        This is the shared first-word lookup for replacement routing. A
        first-word candidate is enough to begin replacement buffering, while
        ``_can_match_replacement`` below additionally tests the complete
        buffered text before switching an existing command buffer.
        """
        if not buffer:
            return []

        return [
            compiled_pattern
            for compiled_pattern, pattern_type, _data
            in self.catalog.get_matching_patterns(buffer[0])
            if pattern_type == "replacement"
        ]

    def is_incomplete_replacement_prefix(self, word: str) -> bool:
        """True when ``word`` alone could still grow into a replacement.

        wh-okay-prefix-splits-replacement. SpeechProcessor asks this about the
        LAST word of text it is about to dictate. A True answer means the word
        is a viable replacement prefix that has not matched yet, so a word
        arriving next could still complete the pair -- "question" before
        "mark", "full" before "stop".

        This is deliberately a single-word question, unlike
        ``_can_match_replacement`` above, which tests the whole buffer against
        patterns indexed under its FIRST word. That first-word limitation is
        the reason "okay question" never reaches replacement buffering: the
        pair is indexed under "question", not under "okay".

        Measured on 2026-08-28 against the shipped catalog: 47 of the 200
        indexed first words answer True. All 47 already open a buffer from
        IDLE today, so this does not add a class of delayed words.
        """
        if not word:
            return False
        if self.matcher.is_pattern_complete([word], "replacement", False):
            return False
        return not self.matcher.cannot_match([word], "replacement", False)

    def is_incomplete_replacement_name(self, words: List[str]) -> bool:
        """True when ``words`` TOGETHER are an unfinished replacement name.

        wh-spaced-punctuation-names-unresolved.3. The multi-word sibling
        of ``is_incomplete_replacement_prefix`` above, and the same two
        questions in the same order: the words do not spell a complete
        replacement yet, and they can still grow into one. The only
        difference is that the whole word list is asked, not just its
        last word.

        The single-word method cannot answer for a paused name whose
        words arrive one utterance apart. "open" alone is a viable first
        word, but "open single" -- the state after the second utterance
        of "open single quote" -- has "single" as its last word, and
        ``is_incomplete_replacement_prefix("single")`` says nothing about
        whether the pair opens a real name. Asking the whole list does:
        ``PatternMatcher.can_continue`` reaches
        ``_buffer_opens_literal_prefix``, which tests the buffer against
        the anchored matchers the catalog compiled for each pattern
        (``literal_prefix_matchers`` for a greedy pattern,
        ``literal_body_matchers`` for the ``^...$`` / ``\\b...\\b``
        punctuation names Stage A gave bodies to). Those matchers answer
        True only for the pattern's whole literal opening or an exact
        N-word truncation of it, so ["open", "single"] answers True and
        ["open", "the"] answers False.

        Measured against the shipped catalog on 2026-09-05: True for
        ["open"], ["open", "single"], ["question"], ["greater"],
        ["greater", "than"], ["pound", "sterling"]; False for
        ["open", "the"], ["open", "single", "quote"] (already complete),
        ["question", "mark"] (already complete), ["hold", "it", "open"],
        ["hello"], ["the"].
        """
        if not words:
            return False
        if self.matcher.is_pattern_complete(words, "replacement", False):
            return False
        return not self.matcher.cannot_match(words, "replacement", False)

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

        buffer_text = " ".join(buffer)

        return any(
            compiled_pattern.search(buffer_text)
            for compiled_pattern in self._replacement_patterns_for_first_word(buffer)
        )
