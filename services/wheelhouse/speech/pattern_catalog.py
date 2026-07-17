"""Pattern catalog for fast first-word lookup in speech patterns.

This module provides a fast lookup table for determining whether an incoming
word could be the start of a multi-word command or replacement pattern. It
loads patterns from the unified patterns.toml file and extracts first words
to enable O(1) lookup instead of O(n) regex matching on every word.

Key Classes:
  - PatternCatalog: Fast lookup table and single source of truth for patterns.
  - PatternType: Enum distinguishing COMMAND vs REPLACEMENT patterns.

Key Features:
  - Single source of truth - loads all patterns from patterns.toml
  - Provides patterns to TextParser (no duplicate loading)
  - Extracts first words from regex patterns for buffering decisions
  - Handles simple alternations like (word1|word2)
  - Handles optional prefixes like (?:prefix )?word
  - O(1) lookup for "could this be a pattern start?"
  - Returns matching patterns for full pattern matching

Typical Usage:
  from speech.pattern_catalog import PatternCatalog
  
  catalog = PatternCatalog("speech/config/patterns.toml")
  
  # Fast lookup for buffering
  if catalog.could_be_pattern_start("backspace"):
      patterns = catalog.get_matching_patterns("backspace")
      # Buffer the word and wait for more
  else:
      # Send immediately to dictation
  
  # Get all patterns for execution
  patterns = catalog.get_all_patterns()
  for pattern in patterns:
      # Use pattern['compiled_pattern'], pattern['actions'], etc.
"""
import re
import tomllib
import logging
import os
from enum import Enum, auto
from typing import Dict, List, Tuple, Optional, Any
from .pattern_transform import transform_pattern

logger = logging.getLogger(__name__)


# wh-9f51.1: STT/ITN post-processing can attach sentence punctuation to
# the spoken word. For instance the utterance "backspace comma" lands
# as the single token "backspace," in the transcript. The PatternCatalog
# lookup dict is keyed on the bare lowercased command word, so the raw
# token misses and the utterance falls through to dictation. Strip the
# punctuation off the lookup key (but never off the data itself --
# downstream still needs the comma so it can type a literal "," for the
# user). Only leading/trailing characters in this set are stripped; the
# inner content is preserved so escaped-literal first words like
# "*cough*" still resolve.
_LOOKUP_PUNCT_STRIP = ".,;:!?"


def _normalize_lookup_word(word: str) -> str:
    """Normalize an incoming word for first-word catalog lookup.

    Strips trailing sentence punctuation that the STT/ITN may attach to
    a command word (e.g. "backspace," from the spoken "backspace
    comma") and lowercases the result. Inner punctuation is preserved
    so escaped-literal first words like "*cough*" still resolve. Empty
    input is returned unchanged; a token that is nothing but
    punctuation falls back to its lowercased original so the lookup
    misses cleanly without crashing.

    wh-9f51.2.4: trailing-only. The matcher only rstrips on the
    fullmatch retry path, so stripping leading punctuation here would
    let could_be_pattern_start(",backspace") return True while
    match_complete(",backspace") returns None -- catalog would route
    a leading-punctuated word into command buffering that the matcher
    cannot complete. STT/ITN injects trailing punctuation but not
    leading punctuation in normal use; symmetry with the matcher is
    more valuable than speculative leading-strip coverage.
    """
    if not word:
        return word
    stripped = word.rstrip(_LOOKUP_PUNCT_STRIP)
    if not stripped:
        return word.lower()
    return stripped.lower()


class PatternType(Enum):
    """Classification of pattern types for speech processor decision logic.
    
    This enum enables the speech processor to distinguish between commands
    and replacements, particularly for Row 8 of the truth table where
    replacements must buffer mid-utterance while commands must not.
    
    The requires_hotword field (accessed via pattern data) indicates whether
    a command requires a hotword prefix for safety. This affects timeout
    duration but doesn't change the fundamental pattern type.
    
    See: docs/REFACTORING_GUIDE.md truth table, Row 8
    """
    COMMAND = auto()       # Command patterns (check data['requires_hotword'] for hotword requirement)
    REPLACEMENT = auto()   # Text replacements (e.g., "mary smith" → "Mary Smith")
    NONE = auto()          # Not in catalog


class PatternCatalog:
    """
    Fast lookup table for pattern first words.
    
    :flow: Multi-Word Pattern Catalog
    :description: This flow provides O(1) lookup to determine if an incoming word could be 
    the start of a multi-word command or replacement pattern. It solves the problem of 
    word-by-word STT delivery breaking multi-word patterns like "backspace 3" or "mary smith".
    
    The catalog loads patterns from `speech/config/patterns.toml`, extracts possible first 
    words from regex patterns, and builds a hash table for instant lookup. When a word arrives, 
    the SpeechProcessor can quickly check if it should start buffering or pass 
    through immediately for zero latency.
    
    Additionally, PatternCatalog serves as the single source of truth for all patterns,
    providing them to TextParser for execution without duplicate loading.
    
    **Pattern Loading Flow:**
    1. Load patterns from unified `patterns.toml`
    2. Auto-detect pattern type from ^ anchor (commands vs replacements)
    3. For each pattern, extract possible first words:
       - Simple literals: `^backspace` → ["backspace"]
       - Alternations: `^(backspace|back space)` → ["backspace", "back"]
       - Optional prefixes: `^(?:go )?down` → ["go", "down"]
    4. Build hash table: `{first_word: [(compiled_pattern, type, data), ...]}`
    5. Store complete pattern data for TextParser execution
    6. Result: O(1) lookup for buffering + single source for execution
    
    **Integration Points:**
    - Called by: `SpeechProcessor` during word processing
    - Called by: `TextParser` during initialization
    - Uses: Pattern configuration file `speech/config/patterns.toml`
    - Provides: Fast first-word lookup and complete pattern data
    
    **Performance Characteristics:**
    - Initialization: O(n) where n = number of patterns (~84 patterns)
    - Lookup: O(1) hash table lookup
    - Memory: ~63 first-word entries with compiled regex patterns
    - Zero runtime overhead for non-pattern words
    """
    
    def __init__(self, patterns_file: str, user_patterns_file: Optional[str] = None):
        """
        Initialize pattern catalog by loading and indexing patterns.

        Loads the shipped system file (``patterns_file``) plus an optional
        writable user file (``user_patterns_file``) and merges them: a user
        pattern whose normalized pattern string matches a built-in replaces
        the built-in (user wins); a user pattern with a new trigger is added.
        The user file may also override COMMAND_HOTWORD. See the split design
        doc (2026-07-08-system-user-patterns-split-design.md).

        If the system file is missing, malformed, or lacks COMMAND_HOTWORD,
        the catalog starts in degraded mode (pattern_count == 0, no voice
        commands) instead of crashing. Fix the file and call reload() to
        recover. A missing or malformed *user* file is not fatal: the catalog
        loads the system patterns only and logs a warning.

        Args:
            patterns_file: Path to the shipped system patterns.toml.
            user_patterns_file: Path to the writable user_patterns.toml. When
                None, resolves to ``get_user_data_dir()/user_patterns.toml``.
                Pass an explicit path in tests to stay hermetic.
        """
        self.first_words: Dict[str, List[Tuple[re.Pattern, str, Any]]] = {}
        self.all_patterns: List[Dict[str, Any]] = []  # For TextParser execution
        # wh-2vz: trailing-position commands. Pattern entries that set
        # ``position = "trailing"`` are NOT indexed in first_words or
        # all_patterns. They live in this separate map keyed by the
        # lowercased single literal word so SpeechProcessor can look them
        # up when end_of_utterance=True. Each value is a dict with
        # ``compiled_pattern`` (re.Pattern) and ``actions`` (list).
        self.trailing_commands: Dict[str, Dict[str, Any]] = {}
        self.pattern_count = 0
        self.command_hotword = None  # Will be loaded from patterns.toml (required)
        self._patterns_file = patterns_file  # Store for reload()
        self._user_patterns_file = (
            user_patterns_file
            if user_patterns_file is not None
            else self._default_user_patterns_file()
        )

        self._load_patterns()

        if self.pattern_count == 0:
            error_msg = (
                f"No patterns loaded from configuration file:\n"
                f"  - Patterns: {patterns_file}\n"
                f"Starting in degraded mode -- no voice commands will work.\n"
                f"Fix the TOML file and trigger a hot-reload to recover."
            )
            logger.critical(error_msg)
        else:
            logger.info(f"PatternCatalog loaded {self.pattern_count} patterns with {len(self.first_words)} first-word entries")
    
    @staticmethod
    def _default_user_patterns_file() -> str:
        """Resolve the default writable user patterns file path.

        ``get_user_data_dir()`` returns ``services/wheelhouse/data`` in a
        source checkout and ``%APPDATA%/WheelHouse/data`` under a frozen
        build, so the user file survives a shipped-patterns update either
        way (wh-k8ef). Returns an empty string if resolution fails, which the
        loader treats as "no user file".
        """
        try:
            from utils.system import get_user_data_dir
            return str(get_user_data_dir() / "user_patterns.toml")
        except Exception:
            logger.warning(
                "Could not resolve the user patterns directory; "
                "loading system patterns only",
                exc_info=True,
            )
            return ""

    @staticmethod
    def _trigger_key(entry: Dict[str, Any]) -> Optional[str]:
        """Return the merge key for a raw entry: its normalized pattern string.

        The key is the entry's ``pattern`` value normalized by strip +
        casefold. It is used ONLY for equality, to decide whether a user
        entry replaces a built-in; it is never the compiled pattern. An
        entry with no usable pattern string returns None (never merges by
        key; always appended).
        """
        pattern = entry.get("pattern")
        if not isinstance(pattern, str):
            return None
        return pattern.strip().casefold()

    def _parse_file(
        self, patterns_file: str, require_hotword: bool,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Read one TOML file and return its raw entries and hotword.

        Args:
            patterns_file: Path to a patterns TOML file.
            require_hotword: When True (the system file), a missing
                COMMAND_HOTWORD raises ValueError. When False (the user
                file), a missing COMMAND_HOTWORD returns None.

        Returns:
            Tuple of (entries, command_hotword_or_None). ``entries`` is the
            raw ``[[pattern]]`` list; no lookup structures are built here.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            tomllib.TOMLDecodeError: If TOML syntax is invalid.
            ValueError: If require_hotword and COMMAND_HOTWORD is missing.
        """
        if not os.path.exists(patterns_file):
            error_msg = f"FATAL: Patterns file not found: {patterns_file}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)

        with open(patterns_file, 'rb') as f:
            data = tomllib.load(f)

        if "COMMAND_HOTWORD" in data:
            command_hotword = data["COMMAND_HOTWORD"]
        elif require_hotword:
            error_msg = (
                f"FATAL: COMMAND_HOTWORD not found in {patterns_file}\n"
                f"Please add: COMMAND_HOTWORD = \"x-ray\" (or your preferred hotword) "
                f"to the top of {patterns_file}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        else:
            command_hotword = None

        return data.get("pattern", []), command_hotword

    def _merge_entries(
        self,
        system_entries: List[Dict[str, Any]],
        user_entries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge user entries onto system entries by trigger key (user wins).

        A user entry whose trigger key matches a system entry replaces that
        system entry in place, preserving the built-in's position in the
        order (which matters for order-sensitive replacement patterns). A
        user entry with a new trigger is appended after all system entries.
        """
        merged = list(system_entries)
        key_to_index: Dict[str, int] = {}
        for i, entry in enumerate(merged):
            key = self._trigger_key(entry)
            if key is not None:
                key_to_index[key] = i

        for user_entry in user_entries:
            key = self._trigger_key(user_entry)
            if key is not None and key in key_to_index:
                merged[key_to_index[key]] = user_entry  # replace in place
            else:
                merged.append(user_entry)
                if key is not None:
                    key_to_index[key] = len(merged) - 1

        return merged

    @staticmethod
    def _tag_source(
        entries: List[Any], source_file: str,
    ) -> List[Dict[str, Any]]:
        """Return the dict entries of *entries*, each tagged with its origin.

        Each returned entry is a shallow copy carrying a ``_source_file`` key
        so per-entry log messages in _build_structures can name the file the
        entry actually came from, instead of always naming the system file
        (wh-user-patterns-split.9.1). Non-dict entries -- e.g. a hand-edit that
        wrote ``pattern = [1, 2, 3]`` as a top-level array instead of
        ``[[pattern]]`` tables -- are dropped with a warning so one malformed
        entry cannot crash the merge or the build.
        """
        tagged: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                logger.warning(
                    "Skipping non-table pattern entry %r in %s",
                    entry, source_file,
                )
                continue
            copied = dict(entry)
            copied["_source_file"] = source_file
            tagged.append(copied)
        return tagged

    def _effective_hotword(
        self, system_hotword: Optional[str], user_hotword: Optional[str],
    ) -> Optional[str]:
        """Return the hotword to use: the user override when valid, else system.

        A valid user override is a non-empty string after strip() that is a
        single word (no internal whitespace). A user value that is present but
        empty, not a string, or multi-word is ignored with a logged warning, so
        a bad override can never leave every hotword-gated command unreachable.
        A missing user hotword (None) is normal and falls back silently.
        """
        stripped = user_hotword.strip() if isinstance(user_hotword, str) else ""
        # The router matches an STT token against this hotword with exact
        # equality, so a hand-edited value with surrounding or internal
        # whitespace would never fire. Require a single non-empty word
        # (wh-user-patterns-split.8.1, wh-user-patterns-split-bulletproof.3.1).
        if stripped and len(stripped.split()) == 1:
            logger.info("Using user COMMAND_HOTWORD override: %r", stripped)
            return stripped
        if user_hotword is not None:
            logger.warning(
                "Ignoring invalid user COMMAND_HOTWORD %r (must be a single "
                "non-empty word); keeping system hotword %r",
                user_hotword, system_hotword,
            )
        return system_hotword

    def _build_all(self) -> Tuple[
        Dict[str, List[Tuple[re.Pattern, str, Any]]],
        List[Dict[str, Any]],
        int,
        str,
        Dict[str, Dict[str, Any]],
    ]:
        """Load the system file plus the optional user file and build structures.

        Parses both files, merges their entries (user overrides built-in by
        trigger), resolves the effective hotword, and builds the lookup
        structures from the single merged entry list.

        A missing or malformed user file is not fatal: it logs a warning and
        the catalog loads the system patterns only. A missing/malformed
        SYSTEM file propagates (FileNotFoundError / TOMLDecodeError /
        ValueError) so the degraded-mode handling in _load_patterns/reload
        still applies.

        Returns:
            Tuple of (first_words, all_patterns, pattern_count,
            command_hotword, trailing_commands).
        """
        system_entries, system_hotword = self._parse_file(
            self._patterns_file, require_hotword=True,
        )
        system_entries = self._tag_source(system_entries, self._patterns_file)

        user_entries: List[Dict[str, Any]] = []
        user_hotword: Optional[str] = None
        if self._user_patterns_file and os.path.exists(self._user_patterns_file):
            try:
                raw_user_entries, user_hotword = self._parse_file(
                    self._user_patterns_file, require_hotword=False,
                )
                user_entries = self._tag_source(
                    raw_user_entries, self._user_patterns_file,
                )
            except Exception:
                logger.warning(
                    "User patterns file %s failed to load; using system "
                    "patterns only",
                    self._user_patterns_file, exc_info=True,
                )
                user_entries, user_hotword = [], None

        merged_entries = self._merge_entries(system_entries, user_entries)
        command_hotword = self._effective_hotword(system_hotword, user_hotword)

        first_words, all_patterns, pattern_count, trailing_commands = (
            self._build_structures(merged_entries, self._patterns_file)
        )
        return (
            first_words, all_patterns, pattern_count, command_hotword,
            trailing_commands,
        )

    def _build_structures(
        self, patterns: List[Dict[str, Any]], patterns_file: str,
    ) -> Tuple[
        Dict[str, List[Tuple[re.Pattern, str, Any]]],
        List[Dict[str, Any]],
        int,
        Dict[str, Dict[str, Any]],
    ]:
        """Build the lookup structures from a merged list of raw entries.

        Args:
            patterns: Merged ordered list of raw ``[[pattern]]`` entries.
            patterns_file: Label used only in log messages.

        Returns:
            Tuple of (first_words, all_patterns, pattern_count,
            trailing_commands). Command-vs-replacement type is auto-detected
            per entry from the ``^`` anchor. A single bad entry (invalid
            regex, or a rejected trailing entry) is skipped; the rest load.
        """
        first_words: Dict[str, List[Tuple[re.Pattern, str, Any]]] = {}
        all_patterns: List[Dict[str, Any]] = []
        trailing_commands: Dict[str, Dict[str, Any]] = {}
        pattern_count = 0

        for rule in patterns:
            pattern_str = rule.get("pattern")
            actions_list = rule.get("actions")
            requires_hotword = rule.get("requires_hotword", False)
            position = rule.get("position", "leading")
            # Attribute per-entry errors to the file the entry came from, so a
            # bad hand-edited user pattern is not blamed on the shipped system
            # file (wh-user-patterns-split.9.1). Falls back to the passed
            # label for any entry that predates the tagging.
            source_file = rule.get("_source_file", patterns_file)

            if pattern_str and actions_list:
                # A user could hand-edit user_patterns.toml with a non-string
                # 'pattern' value (e.g. `pattern = 5`, valid TOML). Skip it
                # like any other bad entry instead of raising AttributeError
                # on `.startswith('^')` below, which would escape the
                # per-entry guard and wipe the whole catalog to zero patterns
                # (wh-user-patterns-split.8.2).
                if not isinstance(pattern_str, str):
                    logger.error(
                        "Pattern entry has non-string 'pattern' value %r in "
                        "%s; skipping entry",
                        pattern_str, source_file,
                    )
                    continue
                # wh-2vz: trailing-position commands are stored in a
                # separate map and never enter the leading-pattern
                # routing structures. Validate the v1 single-word
                # constraint and skip the entry on failure so a typo
                # in patterns.toml cannot break the rest of the file.
                if position == "trailing":
                    if requires_hotword:
                        # Trailing-position commands fire when the
                        # word is the last word of an utterance. A
                        # hotword "x-ray" would have to PRECEDE the
                        # command, but the position contract puts the
                        # command word LAST. The two combine
                        # incoherently; reject at load time so a
                        # future patterns.toml maintainer notices.
                        logger.error(
                            "Trailing-position pattern %r in %s sets "
                            "requires_hotword=true. Trailing commands "
                            "cannot require a hotword; skipping entry.",
                            pattern_str, source_file,
                        )
                        continue
                    trailing_entry = self._build_trailing_entry(
                        pattern_str, actions_list,
                    )
                    if trailing_entry is None:
                        # Validation already logged a specific message.
                        continue
                    key, entry = trailing_entry
                    if key in trailing_commands:
                        logger.warning(
                            "Duplicate trailing-command word %r in %s; "
                            "keeping the first entry",
                            key, source_file,
                        )
                        continue
                    trailing_commands[key] = entry
                    pattern_count += 1
                    continue

                if position != "leading":
                    logger.warning(
                        "Unknown position=%r for pattern %r in %s; "
                        "treating as leading",
                        position, pattern_str, source_file,
                    )

                try:
                    # Auto-detect pattern type from ^ anchor
                    is_command = pattern_str.startswith('^')
                    pattern_type = "command" if is_command else "replacement"

                    # Auto-detect special patterns and transform if needed
                    transformed_pattern, auto_metadata = transform_pattern(pattern_str)

                    compiled = re.compile(transformed_pattern, re.IGNORECASE)

                    # Build pattern data
                    data_dict: Dict[str, Any] = {
                        "actions": actions_list,
                        "requires_hotword": requires_hotword
                    }

                    # Whole-utterance-only patterns (sound-alike punctuation
                    # aliases, wh-int8-punctuation-mishears) may fire only
                    # when they match the ENTIRE utterance: the router skips
                    # every early-execute path for them and they resolve at
                    # utterance end (end marker or timeout). Only a real TOML
                    # boolean counts: a truthy non-bool ("true", 1) is
                    # hand-edit garbage and degrades to disabled so the two
                    # rebuilt representations below can never disagree.
                    raw_whole_utterance = rule.get("whole_utterance_only", False)
                    if not isinstance(raw_whole_utterance, bool):
                        logger.warning(
                            "Non-boolean whole_utterance_only=%r for pattern "
                            "%r in %s; treating as disabled",
                            raw_whole_utterance, pattern_str, source_file,
                        )
                        raw_whole_utterance = False
                    elif raw_whole_utterance and not is_command:
                        # The router's whole-utterance gates exist only on
                        # the command paths; a replacement executes without
                        # consulting the flag, so honoring it here would
                        # promise safety the runtime does not deliver
                        # (wh-int8-punctuation-mishears.1.4).
                        logger.warning(
                            "whole_utterance_only is only supported on "
                            "^-anchored command patterns; ignoring it for "
                            "replacement pattern %r in %s",
                            pattern_str, source_file,
                        )
                        raw_whole_utterance = False
                    whole_utterance_only = raw_whole_utterance
                    if whole_utterance_only:
                        data_dict["whole_utterance_only"] = True

                    # Add auto-detected validation metadata
                    if auto_metadata.get("validation_group"):
                        data_dict["validation_group"] = auto_metadata["validation_group"]
                        logger.debug(f"Auto-detected numeric pattern: {pattern_str} -> {transformed_pattern}, validation={auto_metadata['validation_group']}")

                    # Add greedy flag if detected, plus the load-time
                    # literal prefix the router's prefix probe reads
                    # instead of re-parsing the regex on the hot path
                    # (wh-greedy-prefix-precompute).
                    if auto_metadata.get("is_greedy"):
                        data_dict["is_greedy"] = True
                        data_dict["literal_prefix"] = auto_metadata.get(
                            "literal_prefix", ""
                        )
                        logger.debug(f"Auto-detected greedy pattern: {pattern_str}")

                    # Extract first words and index
                    extracted_words = self._extract_first_words(transformed_pattern)
                    for word in extracted_words:
                        if word not in first_words:
                            first_words[word] = []
                        first_words[word].append((compiled, pattern_type, data_dict))

                    # Store complete pattern data for TextParser execution.
                    # raw_pattern is the PRE-transform expression: the try-it
                    # messages hash it for an id that matches the manager
                    # tree (the compiled string differs for transformed
                    # numeric patterns). is_user marks user-file entries
                    # (wh-pattern-editor-test-messages).
                    all_patterns.append({
                        'compiled_pattern': compiled,
                        'pattern_type': pattern_type,
                        'actions': actions_list,
                        'requires_hotword': requires_hotword,
                        'validation_group': auto_metadata.get("validation_group"),
                        'is_greedy': auto_metadata.get("is_greedy", False),
                        'raw_pattern': pattern_str,
                        'is_user': source_file == self._user_patterns_file,
                        'whole_utterance_only': whole_utterance_only,
                    })

                    pattern_count += 1

                except re.error as e:
                    logger.error(
                        "Invalid regex pattern '%s' in %s: %s",
                        pattern_str, source_file, e,
                    )

        logger.info(f"Loaded {pattern_count} patterns from {patterns_file}")

        # wh-2vz: warn when a word is registered as both a leading
        # first_word AND a trailing command. The leading entry's router
        # behaviour will fire first on single-word utterances, silently
        # pre-empting the trailing intercept. A clean run has no
        # collisions; surface any so a maintainer notices.
        collisions = sorted(set(trailing_commands.keys()) & set(first_words.keys()))
        if collisions:
            logger.warning(
                "Words registered as BOTH leading and trailing in the "
                "merged system+user patterns: %s. The leading match fires "
                "first; the trailing entry is unreachable for single-word "
                "utterances. Remove one of the duplicates.",
                collisions,
            )

        return (
            first_words, all_patterns, pattern_count, trailing_commands,
        )

    def _build_trailing_entry(
        self, pattern_str: str, actions_list: List[Dict[str, Any]],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Validate and compile a ``position = "trailing"`` pattern entry.

        v1 contract (wh-2vz): a trailing entry is a single literal word
        that fires its action when it is the last word of an utterance.
        Any pattern string that is not a single word is rejected with a
        logged warning; the rest of patterns.toml continues to load.

        Args:
            pattern_str: The raw value of the ``pattern`` field.
            actions_list: The raw value of the ``actions`` field.

        Returns:
            ``(lowercased_word, entry_dict)`` on success; ``None`` if the
            entry failed validation. The entry_dict has ``compiled_pattern``
            (re.Pattern matching the word case-insensitively) and
            ``actions`` (the raw action list).
        """
        if not isinstance(pattern_str, str) or not pattern_str.strip():
            logger.warning(
                "Trailing-position pattern has empty pattern string; "
                "skipping",
            )
            return None

        # v1 supports only single-word literals. Strip optional regex
        # anchors so the user can write either ``submit`` or ``^submit$``
        # without surprising behaviour, but reject anything more
        # elaborate.
        candidate = pattern_str.strip()
        if candidate.startswith("^"):
            candidate = candidate[1:]
        if candidate.endswith("$"):
            candidate = candidate[:-1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", candidate):
            logger.warning(
                "Trailing-position pattern %r is not a single literal "
                "word; skipping (v1 supports only single-word trailing "
                "commands)",
                pattern_str,
            )
            return None

        word = candidate.lower()
        try:
            compiled = re.compile(rf"^{re.escape(word)}$", re.IGNORECASE)
        except re.error as e:
            logger.error(
                "Failed to compile trailing-position pattern %r: %s",
                pattern_str, e,
            )
            return None

        return word, {
            "compiled_pattern": compiled,
            "actions": actions_list,
        }

    def _load_patterns(self):
        """
        Load and merge the system and user patterns files.

        :flow: Multi-Word Pattern Catalog
        :step: 1
        :description: Loads patterns from the system and user TOML files and initializes the catalog.
        :data_out: Populated self.first_words and self.all_patterns structures.

        Auto-detects pattern type based on ^ anchor:
        - Patterns with ^ anchor = commands
        - Patterns without ^ anchor = replacements

        On error loading the SYSTEM file, logs the problem and returns early,
        preserving whatever data was already in self (empty on first load,
        populated on reload). A missing/malformed user file is handled inside
        _build_all and is never fatal.
        """
        try:
            (
                first_words, all_patterns, pattern_count, command_hotword,
                trailing_commands,
            ) = self._build_all()
        except tomllib.TOMLDecodeError as e:
            error_msg = f"TOML syntax error in {self._patterns_file}: {e}"
            logger.error(error_msg, exc_info=True)
            return
        except Exception as e:
            error_msg = f"Failed to load patterns from {self._patterns_file}: {e}"
            logger.error(error_msg, exc_info=True)
            return

        self.first_words = first_words
        self.all_patterns = all_patterns
        self.pattern_count = pattern_count
        self.command_hotword = command_hotword
        self.trailing_commands = trailing_commands

    def reload(self) -> bool:
        """
        Hot-reload patterns from disk.

        Re-reads both the system and user files, builds new data structures in
        local variables, then atomically swaps them onto self. If any error
        occurs loading the system file, the old data is preserved and the
        error is logged.

        Returns:
            True if reload succeeded, False if it failed (old data preserved).
        """
        try:
            (
                first_words, all_patterns, pattern_count, command_hotword,
                trailing_commands,
            ) = self._build_all()
        except Exception:
            logger.error(
                f"Reload failed for {self._patterns_file} -- keeping old data",
                exc_info=True,
            )
            return False

        # Atomic swap (cooperative single-threaded asyncio -- no lock needed)
        self.first_words = first_words
        self.all_patterns = all_patterns
        self.pattern_count = pattern_count
        self.command_hotword = command_hotword
        self.trailing_commands = trailing_commands

        logger.info(
            f"PatternCatalog reloaded: {pattern_count} patterns, "
            f"{len(first_words)} first-word entries, "
            f"{len(trailing_commands)} trailing-command entries"
        )
        return True
    
    
    def _extract_first_words(self, pattern_str: str) -> List[str]:
        """
        Extract possible first words from a regex pattern.
        
        Handles:
        - Simple literals: "^backspace" → ["backspace"]
        - Simple patterns: "browser$" → ["browser"]
        - Alternations: "^(backspace|back space)" → ["backspace", "back"]
        - Optional prefixes: "^(?:go )?down" → ["go", "down"]
        - Word boundaries: "\\b word\\b" → ["word"]
        - Inline flags: "(?i)pattern" → "pattern"
        - Escaped literals: "\\*cough\\*" → ["*cough*"]
        - Escaped literal alternations: "\\*(?:cough|sniff)\\*" → ["*cough*", "*sniff*"]
        
        Args:
            pattern_str: Regex pattern string
            
        Returns:
            List of possible first words (lowercase)
        """
        # Check if this is a simple single-word pattern
        # Examples: 'period$', '^undo$', 'keyboard$', '\bcomma\b'
        # First, remove actual word boundary escapes \b (not the character)
        cleaned = pattern_str.replace('\\b', '')
        # Match: optional ^, then word characters, then optional $
        single_word_pattern = re.match(r'^\^?(\w+)\$?$', cleaned)
        if single_word_pattern:
            # Return the single word
            return [single_word_pattern.group(1).lower()]
        
        # Handle escaped literal wrappers: \*word\* or \*(?:word1|word2)\*
        # These represent literal asterisk characters in STT output (e.g., *cough*)
        # Must come before general extraction which strips \* characters
        escaped_literal = re.match(r'^\\\*(.+)\\\*$', pattern_str)
        if escaped_literal:
            inner = escaped_literal.group(1)
            # Check for alternation group: (?:word1|word2|...)
            alt_match = re.match(r'\(\?:([^)]+)\)', inner)
            if alt_match:
                alternatives = alt_match.group(1).split('|')
            else:
                alternatives = [inner]
            first_words = []
            for alt in alternatives:
                alt = alt.strip()
                alt_words = alt.split()
                if len(alt_words) == 1:
                    # Single word/hyphenated: *cough* or *mm-hmm*
                    first_words.append(f'*{alt_words[0]}*'.lower())
                else:
                    # Multi-word: *clears throat* → first word is *clears
                    first_words.append(f'*{alt_words[0]}'.lower())
            return first_words

        first_words = []

        # Remove inline regex flags like (?i), (?m), etc.
        cleaned = re.sub(r'\(\?[iLmsux]+\)', '', pattern_str)
        
        # Remove lookbehind and lookahead assertions: (?<!...), (?<=...), (?!...), (?=...)
        # These don't affect the first word, just context around it
        cleaned = re.sub(r'\(\?[<!]=?[^)]*\)', '', cleaned)
        
        # Remove common regex anchors and boundaries
        cleaned = cleaned.lstrip('^').replace('\\b', '')

        # Handle optional space joining two word parts: "back ?space" → ["back", "backspace"]
        # The ? makes the preceding space optional, so the pattern matches both
        # "backspace" (one word) and "back space" (two words). Index both variants.
        optional_space_match = re.match(r'^([a-z]+) \?([a-z]+)', cleaned, re.IGNORECASE)
        if optional_space_match:
            first_part = optional_space_match.group(1).lower()
            second_part = optional_space_match.group(2).lower()
            return [first_part, first_part + second_part]

        # Handle optional non-capturing groups: (?:prefix )?word
        # Example: "(?:go )?down" → extract both "go" and "down"
        optional_prefix_match = re.match(r'\(\?:([^)]+)\s*\)\?(.+)', cleaned)
        if optional_prefix_match:
            prefix = optional_prefix_match.group(1).strip()
            remainder = optional_prefix_match.group(2).strip()
            
            # Extract word from prefix
            prefix_words = self._extract_simple_words(prefix)
            first_words.extend(prefix_words)
            
            # Extract word from remainder
            remainder_words = self._extract_simple_words(remainder)
            first_words.extend(remainder_words)
            
            return [w.lower() for w in first_words if w]
        
        # Handle alternations: (word1|word2|word3) or (?:word1|word2|word3)
        # Example: "(backspace|back space)" → ["backspace", "back"]
        alternation_match = re.match(r'\(([^)]+)\)', cleaned)
        if alternation_match:
            content = alternation_match.group(1)
            # Strip non-capturing group prefix (?:...)
            if content.startswith('?:'):
                content = content[2:]
            alternatives = content.split('|')
            for alt in alternatives:
                words = self._extract_simple_words(alt)
                first_words.extend(words)
            
            return [w.lower() for w in first_words if w]
        
        # Simple case: extract first word from pattern
        words = self._extract_simple_words(cleaned)
        first_words.extend(words)
        
        return [w.lower() for w in first_words if w]
    
    def _extract_simple_words(self, text: str) -> List[str]:
        r"""
        Extract simple literal words from text, handling optional characters.
        
        :flow: Multi-Word Pattern Catalog
        :step: 2
        :consumes_from: Multi-Word Pattern Catalog
        :description: Extracts first-word variants from regex patterns, including optional
            character expansion. Enables catalog to index both "quote" and "quotes" from
            pattern "quotes?" for O(1) lookup.
        :data_in: Regex pattern text (e.g., "quotes?", "backspace", "(word1|word2)")
        :data_out: List of first-word variants (e.g., ["quote", "quotes"])
        
        Handles patterns like "quotes?" → returns both "quote" and "quotes"
        
        Detection Logic:
        - Matches pattern: /^\s*([a-z]+)(\w)?/i
        - Captures: base word + optional character
        - Returns: [base, base+char]
        
        Example: "quotes?" → regex match groups ("quote", "s") → returns ["quote", "quotes"]
        
        This ensures catalog lookup works for both "quote" and "quotes" spoken commands,
        even though pattern only contains "quotes?" in the TOML.
        
        Args:
            text: Text possibly containing regex syntax
            
        Returns:
            List of literal words found (with optional character variations)
        """
        # Check for pattern like "word?" (optional last character)
        optional_char_match = re.match(r'^\s*([a-z]+)(\w)\?', text, re.IGNORECASE)
        if optional_char_match:
            base_word = optional_char_match.group(1)
            optional_char = optional_char_match.group(2)
            # Return both variants: with and without the optional character
            return [base_word, base_word + optional_char]
        
        # Remove common regex quantifiers and grouping
        text = re.sub(r'[?*+\[\]()\\]+', ' ', text)
        
        # Extract first word-like sequence
        word_match = re.match(r'^\s*([a-z]+)', text, re.IGNORECASE)
        if word_match:
            return [word_match.group(1)]
        
        return []
    
    def could_be_pattern_start(self, word: str) -> bool:
        """
        Fast O(1) check if word could start a pattern.

        Args:
            word: The word to check (case-insensitive)

        Returns:
            True if word could be the start of a multi-word pattern
        """
        # wh-9f51.1: strip STT/ITN-attached sentence punctuation so the
        # spoken "backspace comma" (transcribed "backspace,") still
        # resolves the backspace command instead of falling through.
        return _normalize_lookup_word(word) in self.first_words
    
    def get_pattern_type(self, word: str) -> PatternType:
        """
        Determine the pattern type for a given first word.
        
        :flow: Multi-Word Pattern Catalog
        :step: 3
        :produces_for: Speech Processing
        :description: Classifies first words as COMMAND or REPLACEMENT type for buffering decisions.
            Commands get priority when words can start both types (mixed patterns).
        :data_in: First word from speech utterance
        :data_out: PatternType enum (COMMAND, REPLACEMENT, or NONE)
        
        This is critical for Row 8 of the truth table: replacements must
        buffer mid-utterance, while commands must not.
        
        Args:
            word: The first word to check (case-insensitive)
            
        Returns:
            PatternType indicating whether word starts a COMMAND, REPLACEMENT,
            or NONE (not in catalog)
            
        Classification Strategy:
            - If not in catalog → NONE
            - If any pattern is "command" type → COMMAND (priority)
            - If any pattern is "replacement" type → REPLACEMENT
        
        Priority Rationale:
        When a word can start both command and replacement patterns (e.g., "quotes"),
        prioritize COMMAND classification. This ensures fresh utterances enter
        COMMAND_BUFFERING mode first, check command patterns, then switch to
        REPLACEMENT_BUFFERING if command fails (via mode switching logic).
        
        Example: "quotes" starts both:
        - Command: "^quotes? this$" (wrap selection)
        - Replacement: "\\bquotes? (?!this)(.+)$" (wrap following text)
        → Returns COMMAND, enters COMMAND_BUFFERING
        → If "this" follows, matches command
        → If other words follow, switches to REPLACEMENT_BUFFERING
              
        The requires_hotword field indicates whether a command requires a
        hotword prefix. Access it via: catalog.get_matching_patterns(word)[0][2]['requires_hotword']
        
        See: docs/REFACTORING_GUIDE.md truth table, Rows 2-3, 6-8
        """
        # wh-9f51.1: see _normalize_lookup_word; the same trailing/
        # leading punctuation tolerance applies here so type-checking
        # callers (SpeechRouter) agree with could_be_pattern_start.
        word_lower = _normalize_lookup_word(word)
        if word_lower not in self.first_words:
            return PatternType.NONE

        patterns = self.first_words[word_lower]
        has_replacement = False
        has_command = False
        
        for _, type_str, data in patterns:
            if type_str == "replacement":
                has_replacement = True
            elif type_str == "command":
                has_command = True
        
        # Priority: Commands take precedence (for mixed cases)
        # When a word can start both a command and replacement pattern,
        # prioritize command classification so fresh utterances enter
        # COMMAND_BUFFERING mode and check command patterns first
        if has_command:
            return PatternType.COMMAND
        
        if has_replacement:
            return PatternType.REPLACEMENT
        
        # Fallback (shouldn't reach here if data loaded correctly)
        return PatternType.NONE
    
    def get_matching_patterns(self, word: str) -> List[Tuple[re.Pattern, str, Any]]:
        """
        Get all patterns that could start with this word.

        Args:
            word: The first word (case-insensitive)

        Returns:
            List of (compiled_pattern, type, data) tuples where:
            - compiled_pattern: Compiled regex pattern
            - type: "command" or "replacement"
            - data: Pattern-specific data (actions, requires_hotword, etc.)
        """
        # wh-9f51.1: same punctuation normalization as
        # could_be_pattern_start so the candidate set is consistent
        # between the buffering hint and the actual pattern fetch.
        return self.first_words.get(_normalize_lookup_word(word), [])
    
    def get_all_patterns(self) -> List[Dict[str, Any]]:
        """
        Get all patterns for execution by TextParser.
        
        Returns list of pattern dictionaries with:
        - compiled_pattern: Compiled regex Pattern object
        - pattern_type: "command" or "replacement"
        - actions: List of action steps to execute
        - requires_hotword: Boolean indicating hotword requirement
        - validation_group: Optional numeric validation group (e.g., "g2")
        - is_greedy: Boolean indicating greedy matching
        
        This method provides TextParser with all patterns loaded from patterns.toml,
        eliminating the need for TextParser to load patterns independently.
        """
        return self.all_patterns
    
    def get_all_first_words(self) -> List[str]:
        """Return all indexed first words for debugging."""
        return sorted(self.first_words.keys())

    def get_trailing_command(self, word: str) -> Optional[Dict[str, Any]]:
        """Return the trailing-command entry for ``word``, or None (wh-2vz).

        The lookup is case-insensitive. The returned entry has a
        ``compiled_pattern`` (re.Pattern) and ``actions`` (list of step
        dicts) that the SpeechProcessor passes to TextParser._execute_rule
        when the trailing word arrives with end_of_utterance=True.
        """
        # wh-9f51.1: trailing words inherit the same STT/ITN punctuation
        # tolerance as the leading-word lookup methods. "submit." (from
        # "submit.") resolves to the same entry as "submit".
        return self.trailing_commands.get(_normalize_lookup_word(word))
