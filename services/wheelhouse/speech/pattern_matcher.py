"""PatternMatcher - Consolidated pattern matching logic.

This module centralizes all pattern matching decisions that were previously
duplicated across SpeechRouter and TextParser:

1. Fullmatch vs search decision (command vs replacement)
2. Numeric validation via words_to_int()
3. Remainder extraction after partial matches
4. Prefix matching for buffering decisions

Usage:
    matcher = PatternMatcher(catalog)
    result = matcher.match_complete("delete 5", hotword_active=False)
    if result and result.matched:
        # Use result.match_object, result.remainder, result.actions, etc.
"""

import re
import logging

from utils.redact import redact_transcript
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Sequence

from .pattern_catalog import (
    PatternCatalog,
    PatternType,
    _normalize_lookup_word,
)

logger = logging.getLogger(__name__)


# wh-9f51.3.4: deliberately duplicated from pattern_catalog._LOOKUP_PUNCT_STRIP.
#
# The two constants currently hold the same value but serve different
# contracts:
#
#   * pattern_catalog._LOOKUP_PUNCT_STRIP gates "find the right pattern
#     entry for this catalog key". Adding a character there changes
#     which first-word tokens map to a catalog candidate set.
#
#   * pattern_matcher._MATCHER_PUNCT_STRIP gates "strip trailing
#     STT/ITN-injected formatting characters before retrying a
#     '^...$' fullmatch". Adding a character here changes which
#     characters are eligible to be dropped from the input text on
#     the retry path (the first-try fullmatch against the original
#     text always runs first, so a parameterized capture that wants
#     to consume the character still wins).
#
# Keeping the two constants separate means a future maintainer who
# extends one side does not silently change the other. If they DO need
# to move together, update both with a comment explaining why. The
# duplication is 7 characters; the clarity gain is worth the DRY
# violation.
_MATCHER_PUNCT_STRIP = ".,;:!?"


def _normalize_first_word_in_text(text: str) -> str:
    """Normalize STT-injected trailing punctuation on the FIRST word of ``text``.

    wh-9f51.3.1 / wh-9f51.3.2: when the STT/ITN attaches sentence
    punctuation to the first word of a multi-word utterance, the
    joined buffer text looks like ``"backspace, 3"`` -- the comma is
    embedded mid-string where the trailing-rstrip retry in
    ``_match_command_with_punct_retry`` cannot reach it. The single
    word case is handled by the retry helper; the multi-word case
    needs the punctuation stripped at the word boundary BEFORE
    joining (or, equivalently, before the fullmatch input is built).

    This helper does that boundary normalization in one place so both
    ``match_complete`` and ``can_continue`` can share the same logic.

    Trade-off: this discards the stripped character. In the
    single-word path the retry helper folds the tail back into
    ``remainder`` so the user-spoken comma still surfaces as
    dictation. We deliberately do NOT do the same for multi-word
    text: the only realistic source of mid-string punctuation between
    the first and second word is STT-injected sentence punctuation
    ("backspace three" -> "backspace," + "3" -> "backspace, 3"), and
    surfacing it as a dictated character would type a stray comma the
    user did not speak. If a future utterance genuinely needs the
    character preserved (e.g., a parameterized first-word pattern in
    a multi-word context), the original-text first-try inside the
    retry helper will already have captured it, so we only reach this
    boundary-normalization path when the original text failed to
    match and the punctuation is the most likely culprit.

    Returns ``text`` unchanged when no first-word normalization is
    needed (single word, or first word already has no trailing
    punctuation in the strip set).
    """
    if not text:
        return text
    # Split on the first run of whitespace only so the remainder of the
    # text (which may have its own runs of spaces or other content) is
    # preserved byte-for-byte.
    first_word, sep, rest = text.partition(" ")
    if not sep:
        # Single-word text: leave it alone and let the retry helper
        # handle trailing punctuation. The retry path preserves the
        # stripped tail through ``remainder`` for the single-word case.
        return text
    normalized = _normalize_lookup_word(first_word)
    if normalized == first_word:
        return text
    return normalized + sep + rest


@dataclass
class MatchResult:
    """Result of a pattern match operation.

    Contains all information needed by consumers (Router, TextParser) to
    process the match without re-running matching logic.

    Attributes:
        matched: True if pattern matched
        pattern_type: "command" or "replacement"
        match_object: The re.Match object for group extraction
        matched_text: The text that was matched
        remainder: Text AFTER the match (for chaining, executes after)
        before_remainder: Text BEFORE the match (executes first)
        requires_hotword: Whether pattern requires hotword activation
        validation_group: Capture group for numeric validation (e.g., "g1")
        is_greedy: Whether pattern is greedy (wants more words)
        actions: List of action steps to execute
        pattern_data: Full pattern data dict for additional metadata
    """
    matched: bool
    pattern_type: str = ""
    match_object: Optional[re.Match] = None
    matched_text: str = ""
    remainder: str = ""
    before_remainder: str = ""  # Text BEFORE the match (should execute first)
    requires_hotword: bool = False
    validation_group: Optional[str] = None
    is_greedy: bool = False
    actions: List[Dict[str, Any]] = field(default_factory=list)
    pattern_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def groups(self) -> Tuple:
        """Get capture groups from match."""
        if self.match_object:
            return self.match_object.groups()
        return ()

    def group(self, n: int) -> Optional[str]:
        """Get specific capture group."""
        if self.match_object:
            try:
                return self.match_object.group(n)
            except IndexError:
                return None
        return None


class PatternMatcher:
    """Centralized pattern matching logic.

    Consolidates duplicate matching code from SpeechRouter and TextParser
    into a single source of truth.
    """

    def __init__(self, catalog: PatternCatalog):
        """Initialize with pattern catalog.

        Args:
            catalog: PatternCatalog instance with loaded patterns
        """
        self.catalog = catalog
        self._words_to_int = None  # Lazy-loaded to avoid circular imports

    def _get_words_to_int(self):
        """Lazy-load words_to_int to avoid circular imports."""
        if self._words_to_int is None:
            try:
                from .actions import words_to_int
                self._words_to_int = words_to_int
            except ImportError:
                logger.warning("Could not import words_to_int, numeric validation disabled")
                self._words_to_int = lambda x: None
        return self._words_to_int

    def next_word_fills_numeric_count(self, tokens: Sequence[str]) -> bool:
        """True if the first real word in ``tokens`` is a numeric count.

        Skips leading tokens that are entirely STT punctuation (a lone
        "," from the speech engine), strips the matcher's punctuation
        set off the first real word, and asks words_to_int whether it is
        a number ("3" or "three"). Used by the router's command-prefix
        finalization to detect that STT punctuation severed a count from
        its command ("delete , 3" from spoken "delete 3"): the count
        still sits in the buffer, just past a standalone punctuation
        token (wh-midword-punct-severs-count.3.1). Encapsulates the
        punctuation set and words_to_int so the router does not reach
        into matcher internals.
        """
        for token in tokens:
            stripped = token.strip(_MATCHER_PUNCT_STRIP)
            if not stripped:
                continue  # standalone punctuation token; keep looking
            return self._get_words_to_int()(stripped) is not None
        return False

    def match_complete(
        self,
        text: str,
        pattern_type: Optional[str] = None,
        hotword_active: bool = False,
        first_word: Optional[str] = None
    ) -> Optional[MatchResult]:
        """Try to match text against patterns.

        This is the SINGLE SOURCE OF TRUTH for fullmatch vs search logic:
        - Commands (^ anchor): Use fullmatch - entire text must match
        - Replacements (no anchor): Use search - can match within text

        Args:
            text: Text to match against patterns
            pattern_type: Optional filter - "command" or "replacement"
            hotword_active: Whether hotword is currently active
            first_word: Optional first word for pattern lookup optimization

        Returns:
            MatchResult if matched, None if no match
        """
        if not text:
            return None

        # Get first word for pattern lookup
        if first_word is None:
            first_word = text.split()[0] if text.split() else text

        # wh-9f51.1: STT/ITN may attach sentence punctuation to a token,
        # so "backspace comma" becomes the single token "backspace,". The
        # catalog already tolerates this via _normalize_lookup_word, but
        # the fullmatch path below still runs against the raw text, where
        # the trailing comma blocks a '^...$'-anchored command match.
        # Normalize the lookup key here too so the candidate set is
        # consistent, then strip the same trailing characters off the
        # text used for fullmatch and surface the stripped tail through
        # remainder so downstream replacement / dictation handles it.
        normalized_first_word = _normalize_lookup_word(first_word)

        patterns = list(self.catalog.get_matching_patterns(normalized_first_word))

        # wh-9f51.3.1: for multi-word text the first-word's trailing
        # punctuation is mid-string after the buffer join, where the
        # retry helper's rstrip cannot reach it. Normalize the first
        # word at the boundary BEFORE the fullmatch so commands like
        # "backspace three" (STT tokens "backspace," + "3" -> joined
        # "backspace, 3") match the count-suffix pattern instead of
        # falling through to dictation. Single-word text is unchanged
        # here so the retry helper continues to surface the tail
        # through remainder for the wh-9f51.1 single-word case.
        match_text_input = _normalize_first_word_in_text(text)

        # For replacement patterns with search(), we also need to check ALL
        # replacement patterns since the match could be anywhere in the text.
        # This handles cases like "hello comma world" where "comma" is mid-text.
        if pattern_type != "command":
            all_patterns = self.catalog.get_all_patterns()
            seen_patterns = {id(p[0]) for p in patterns}
            for p in all_patterns:
                if p['pattern_type'] == 'replacement':
                    compiled = p['compiled_pattern']
                    if id(compiled) not in seen_patterns:
                        patterns.append((compiled, 'replacement', p))
                        seen_patterns.add(id(compiled))

        for compiled_pattern, ptype, data in patterns:
            # Filter by pattern type if specified
            if pattern_type and ptype != pattern_type:
                continue

            # Skip greedy patterns for complete matching
            is_greedy = data.get('is_greedy', False) if data else False

            # CORE LOGIC: Determine matching strategy from ^ anchor
            # This consolidates the duplicate logic from Router and TextParser
            #
            # wh-9f51.1: for command patterns (^...$), the trailing
            # STT/ITN-injected punctuation in `text` would otherwise
            # block fullmatch (e.g. "backspace," against ^back ?space$).
            # Try fullmatch on the ORIGINAL text first so parameterized
            # commands like ^press\s*(.+)$ can capture the trailing
            # punctuation when it is the spoken argument ("press." ->
            # capture "."). Only on first-try failure do we retry with
            # the rstripped text and fold the stripped tail back into
            # remainder. wh-9f51.2.2.
            if compiled_pattern.pattern.startswith('^'):
                # wh-9f51.3.1: pass the boundary-normalized text so the
                # first-word's STT-injected punctuation is stripped at
                # the join boundary. For the single-word case
                # match_text_input == text, so the retry helper's
                # tail-folding behaviour is preserved unchanged.
                match, match_text, tail = self._match_command_with_punct_retry(
                    compiled_pattern, match_text_input
                )
            else:
                # Replacement pattern: search - can match within text
                match_text = text
                tail = ""
                match = compiled_pattern.search(text)

            if match:
                # Check hotword requirement
                requires_hotword = data.get('requires_hotword', False) if data else False
                if requires_hotword and not hotword_active:
                    continue

                # Validate numeric capture groups
                validation_group = data.get('validation_group') if data else None
                if validation_group and not self.validate_numeric(match, validation_group):
                    continue

                # Calculate matched text and remainder.
                # For the trailing-punctuation command case (wh-9f51.1)
                # `match` ran against `match_text` (without the tail),
                # so its start/end indices are relative to match_text.
                # `before` is computed off match_text (its prefix is
                # identical to text's prefix at those indices), while
                # `after` includes the stripped tail so downstream
                # replacement / dictation sees the original punctuation
                # the user spoke. When the original-text match succeeded
                # `tail` is "" so the behaviour is unchanged for
                # parameterized captures.
                matched_text = match_text[match.start():match.end()]
                before = match_text[:match.start()].strip()
                after = (match_text[match.end():] + tail).strip()
                # Split before/after for correct execution order:
                # before_remainder executes FIRST (arrived earlier)
                # remainder (after) executes AFTER the matched pattern
                before_remainder = before
                remainder = after

                return MatchResult(
                    matched=True,
                    pattern_type=ptype,
                    match_object=match,
                    matched_text=matched_text,
                    remainder=remainder,
                    before_remainder=before_remainder,
                    requires_hotword=requires_hotword,
                    validation_group=validation_group,
                    is_greedy=is_greedy,
                    actions=data.get('actions', []) if data else [],
                    pattern_data=data or {}
                )

        return None

    @staticmethod
    def _match_command_with_punct_retry(
        compiled_pattern: "re.Pattern[str]",
        text: str,
    ) -> Tuple[Optional[re.Match], str, str]:
        """Run fullmatch on a command pattern with STT-punctuation retry.

        Two-stage matching (wh-9f51.2.2): try fullmatch on the ORIGINAL
        text first so parameterized commands like ``^press\\s*(.+)$`` can
        capture trailing punctuation as part of the argument (e.g. the
        spoken "press period" arrives as "press." and the "." is the
        captured key). Only if the original-text match fails do we retry
        with trailing punctuation stripped, so the backspace-comma case
        ("backspace," -> ^backspace$ with "," as remainder) still works.

        The shared helper is used by both ``match_complete`` and
        ``match_single_pattern`` (wh-9f51.2.1) so the command execution
        path and the routing path agree on punctuation handling.

        Returns:
            (match, match_text, tail) where:
              - match is the re.Match object or None
              - match_text is the text the match was run against
              - tail is the punctuation stripped off (empty when the
                first-try match succeeded; non-empty only on the
                retry path)
        """
        # First attempt: fullmatch the original text. This preserves
        # captures that legitimately include trailing punctuation.
        match = compiled_pattern.fullmatch(text)
        if match:
            return match, text, ""

        # Second attempt: strip trailing STT-injected punctuation and
        # retry. The stripped tail is returned so the caller can fold
        # it back into remainder for downstream replacement / dictation.
        # wh-9f51.3.4: uses the matcher's own _MATCHER_PUNCT_STRIP so
        # the contract is independent of pattern_catalog's lookup-key
        # normalization set.
        stripped = text.rstrip(_MATCHER_PUNCT_STRIP)
        if stripped != text:
            tail = text[len(stripped):]
            retry = compiled_pattern.fullmatch(stripped)
            if retry:
                return retry, stripped, tail

        # Third attempt: STT/ITN can attach punctuation to an INTERIOR
        # word of a multi-word command ("back space, 3" from spoken
        # "back space three"). The first-word pre-pass
        # (_normalize_first_word_in_text) only cleans the first word,
        # and the trailing rstrip above only cleans the last word, so a
        # comma on a middle word severed the count from its command
        # (wh-midword-punct-severs-count). Strip the punctuation set
        # from every interior word boundary and retry. This runs ONLY
        # after the original-text fullmatch has already failed, so a
        # parameterized capture that legitimately contains punctuation
        # (e.g. ^press\s*(.+)$ capturing "control, c") is unaffected --
        # it matched at the first attempt and never reached here.
        #
        # The LAST word's trailing punctuation is preserved as the tail
        # (via the rstrip above) so a spoken trailing comma still
        # surfaces in remainder. Interior punctuation is discarded as
        # STT noise, matching the wh-9f51.3 first-word convention. The
        # buffer is joined with single spaces, so split(" ") recovers
        # the exact word tokens.
        #
        # An interior word that is ENTIRELY punctuation (e.g. a
        # standalone "," token from "delete, 3 items left") strips to
        # "". We must NOT retry in that case: rejoining leaves a double
        # space, and a count pattern like ^delete\s*(\d+)?$ has \s*
        # between the command word and the count, so \s* would absorb
        # both spaces and fire a spurious irreversible command
        # ("delete , 3" -> three deletes). A standalone punctuation word
        # means the user dictated punctuation, not a command; bail so it
        # falls through to dictation (wh-midword-punct-severs-count.1.1).
        interior_tail = text[len(stripped):]
        words = stripped.split(" ")
        if len(words) > 1:
            stripped_words = [w.strip(_MATCHER_PUNCT_STRIP) for w in words]
            if "" not in stripped_words:
                normalized = " ".join(stripped_words)
                if normalized != stripped:
                    retry = compiled_pattern.fullmatch(normalized)
                    if retry:
                        return retry, normalized, interior_tail

        return None, text, ""

    def match_single_pattern(
        self,
        text: str,
        pattern_data: dict,
        authorized_command: bool = False,
    ) -> Optional[MatchResult]:
        """Match text against a single pattern.

        Used by TextParser to maintain first-match-wins ordering while
        delegating fullmatch vs search logic to PatternMatcher.

        Args:
            text: Text to match
            pattern_data: Pattern dict with 'compiled_pattern', 'pattern_type', etc.
            authorized_command: True only when the caller has already vetted the
                buffer through the router's hotword gate. The remainder path in
                SpeechProcessor never sets this, which is what stops a
                hotword-required command (e.g. ``save``) from being executed
                via a replacement remainder like ``hello period save``
                (wh-qj70s). Default is fail-closed.

        Returns:
            MatchResult if matched, None otherwise
        """
        compiled_pattern = pattern_data['compiled_pattern']
        ptype = pattern_data.get('pattern_type', 'replacement')
        data = pattern_data

        # Hotword authorization gate: refuse hotword-required patterns unless
        # the caller has explicitly vetted the input upstream.
        if data.get('requires_hotword', False) and not authorized_command:
            return None

        # CORE LOGIC: Determine matching strategy from ^ anchor.
        #
        # wh-9f51.2.1: this is the execution path that
        # CommandEngine.parse_and_execute drives. It must apply the
        # same trailing-punctuation retry as match_complete so the
        # SpeechProcessor command branch ("backspace,") matches
        # ^backspace$ instead of falling through to a literal type.
        # The shared helper carries the wh-9f51.2.2 two-stage
        # semantics so parameterized captures ("press." capturing
        # ".") still succeed on the first attempt.
        #
        # wh-9f51.3.1: also normalize the first word at the
        # boundary so multi-word execution paths (e.g. a future
        # CommandEngine call against "backspace, 3") behave the
        # same way as match_complete. Single-word text is unchanged
        # so the retry helper's tail-folding still surfaces the
        # spoken punctuation through remainder.
        if compiled_pattern.pattern.startswith('^'):
            match_text_input = _normalize_first_word_in_text(text)
            match, match_text, tail = self._match_command_with_punct_retry(
                compiled_pattern, match_text_input
            )
        else:
            match_text = text
            tail = ""
            match = compiled_pattern.search(text)

        if match:
            # Calculate matched text and remainder. When the punct-retry
            # stripped a tail, fold it onto the after-slice so downstream
            # dictation still sees the original spoken punctuation.
            matched_text = match_text[match.start():match.end()]
            before = match_text[:match.start()].strip()
            after = (match_text[match.end():] + tail).strip()
            remainder = f"{before} {after}".strip() if before else after

            return MatchResult(
                matched=True,
                pattern_type=ptype,
                match_object=match,
                matched_text=matched_text,
                remainder=remainder,
                requires_hotword=data.get('requires_hotword', False),
                validation_group=data.get('validation_group'),
                is_greedy=data.get('is_greedy', False),
                actions=data.get('actions', []),
                pattern_data=data
            )

        return None

    def match_for_routing(
        self,
        buffer: List[str],
        pattern_type: str,
        hotword_active: bool = False
    ) -> Optional[MatchResult]:
        """Match for routing decisions (SpeechRouter use case).

        Optimized for the Router's needs:
        - Uses first word from buffer for lookup
        - Joins buffer to text
        - Skips greedy patterns

        Args:
            buffer: List of words in buffer
            pattern_type: "command" or "replacement"
            hotword_active: Whether hotword is active

        Returns:
            MatchResult if matched, None otherwise
        """
        if not buffer:
            return None

        text = " ".join(buffer)
        first_word = buffer[0]

        return self.match_complete(
            text=text,
            pattern_type=pattern_type,
            hotword_active=hotword_active,
            first_word=first_word
        )

    def is_pattern_complete(
        self,
        buffer: List[str],
        pattern_type: str,
        hotword_active: bool = False
    ) -> bool:
        """Check if buffer contains a complete (non-greedy) pattern.

        Used by Router to decide if buffering should end.

        Args:
            buffer: List of words in buffer
            pattern_type: "command" or "replacement"
            hotword_active: Whether hotword is active

        Returns:
            True if buffer matches a complete, non-greedy pattern
        """
        result = self.match_for_routing(buffer, pattern_type, hotword_active)
        if result and result.matched and not result.is_greedy:
            return True
        return False

    def can_continue(
        self,
        buffer: List[str],
        pattern_type: str,
        hotword_active: bool = False
    ) -> bool:
        """Check if buffer could potentially match with more words.

        Used by Router to decide whether to continue buffering.
        Returns True if more words might complete a pattern.

        Args:
            buffer: Current buffer contents
            pattern_type: "command" or "replacement"
            hotword_active: Whether the hotword is currently active. When
                False (the fail-closed default), patterns whose data sets
                requires_hotword=True cannot match and are skipped -- mirroring
                match_complete's skip. This lets the router finalize a buffer
                whose only candidate patterns require the hotword (e.g. a
                non-hotword utterance starting with 'save') as dictation at
                once instead of buffering to command_timeout (wh-4o1aj).

        Returns:
            True if pattern could match with more words, False if impossible
        """
        if not buffer:
            return True  # Empty buffer can always continue

        first_word = buffer[0]
        patterns = self.catalog.get_matching_patterns(first_word)
        if not patterns:
            return False  # No patterns start with this word

        buffer_text = " ".join(buffer)
        # wh-9f51.3.2: normalize the first word's STT-injected trailing
        # punctuation at the join boundary so the fullmatch path agrees
        # with match_complete. The previous prefix-fallback at Strategy 2
        # below saved this in practice (re.match is anchored at start but
        # not end, so trailing characters did not block it), but the
        # fullmatch path otherwise lied about whether the buffer already
        # matches and the design only worked by accident. Same helper
        # both findings 1 and 2 use.
        fullmatch_text = _normalize_first_word_in_text(buffer_text)
        words_to_int = self._get_words_to_int()

        for compiled_pattern, ptype, data in patterns:
            if ptype != pattern_type:
                continue

            # wh-4o1aj: skip patterns that require the hotword when it is
            # inactive, mirroring match_complete's skip (lines ~287-289). A
            # pattern that cannot match without the hotword cannot be a reason
            # to keep buffering, so the router can finalize the buffer as
            # dictation immediately instead of waiting command_timeout.
            requires_hotword = data.get('requires_hotword', False) if data else False
            if requires_hotword and not hotword_active:
                continue

            # Strategy 1: Already matches exactly
            match = compiled_pattern.fullmatch(fullmatch_text)
            if match:
                # Validate numeric if needed
                validation_group = data.get('validation_group') if data else None
                if validation_group:
                    if not self.validate_numeric(match, validation_group):
                        continue
                return True  # Already matches, could also continue

            # Strategy 2: Prefix match - buffer is start of pattern
            pattern_str = compiled_pattern.pattern
            if pattern_str.endswith('$'):
                prefix_pattern_str = pattern_str[:-1].lstrip('^')
                try:
                    test_regex = re.compile(f"^{prefix_pattern_str}", re.IGNORECASE)
                    if test_regex.match(buffer_text):
                        return True
                except re.error:
                    pass

            # Strategy 3: Check if buffer could continue to match with more words
            # Only return True if the buffer is a valid prefix that could continue
            # (not if it already contains invalid data like "delete xyz")
            if ' ' in pattern_str or '\\s' in pattern_str:
                # Multi-word pattern - check if current buffer is valid prefix
                # For patterns with optional numeric like "delete (\d+)?",
                # "delete" alone can continue, but "delete xyz" cannot
                if len(buffer) == 1:
                    # Single word - could potentially continue
                    return True
                # Multiple words - already checked above, don't assume can continue

        return False

    def cannot_match(
        self,
        buffer: List[str],
        pattern_type: str,
        hotword_active: bool = False
    ) -> bool:
        """Check if buffer cannot possibly match any pattern.

        Inverse of can_continue for clearer Router logic.

        Args:
            buffer: Current buffer contents
            pattern_type: "command" or "replacement"
            hotword_active: Forwarded to can_continue. When False (default),
                requires_hotword patterns are treated as unable to match, so a
                hotword-only buffer reports cannot_match=True (wh-4o1aj).

        Returns:
            True if no pattern can match this buffer, False if match possible
        """
        return not self.can_continue(buffer, pattern_type, hotword_active)

    def validate_numeric(
        self,
        match: Optional[re.Match],
        validation_group: Optional[str]
    ) -> bool:
        """Validate numeric capture group using words_to_int.

        This is the SINGLE SOURCE OF TRUTH for numeric validation,
        consolidating duplicate logic from Router and TextParser.

        Args:
            match: The regex match object (can be None)
            validation_group: Group identifier like "g1", "g2", etc.

        Returns:
            True if valid (or no validation needed), False if invalid
        """
        if not validation_group:
            return True

        if match is None:
            return True  # No match to validate

        words_to_int = self._get_words_to_int()
        if words_to_int is None:
            return True  # Can't validate, assume valid

        try:
            group_num = int(validation_group[1:])  # "g1" -> 1
            if len(match.groups()) >= group_num:
                captured_value = match.group(group_num)
                if captured_value is not None:
                    if words_to_int(captured_value) is None:
                        logger.debug(f"Numeric validation failed for '{redact_transcript(captured_value)}'")
                        return False
        except (ValueError, IndexError) as e:
            logger.warning(f"Validation group parse error: {e}")

        return True

    def get_pattern_type(self, word: str) -> PatternType:
        """Get pattern type for a word (delegates to catalog).

        Args:
            word: Word to check

        Returns:
            PatternType.COMMAND, PatternType.REPLACEMENT, or PatternType.NONE
        """
        return self.catalog.get_pattern_type(word)
