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
from typing import Dict, List, Set, Tuple, Optional, Any
from .pattern_buildable import slot_identity
from .pattern_block_text import locate_pattern_block
from .pattern_identity import (
    DOC_ID_KEY,
    DOC_KIND,
    ORIGIN_KEY,
    ORIGIN_OWN,
    Identity,
    entry_text_candidates,
    is_valid_doc_id,
    legacy_candidates,
    normalized_text_key,
)
from .pattern_transform import (
    _expand_sequence,
    build_literal_prefix_matchers,
    extract_full_literal_body,
    transform_pattern,
)

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


# The action that saves a hint in the speech engine
# (speech/actions.py ActionFunctions.add_hint_to_stt).
HINT_ACTION_NAME = "add_hint_to_stt"


def _actions_need_hint_engine(actions: Any) -> bool:
    """True when any action step calls the hint action.

    wh-boost-engine-qualification: such a pattern matches only while the
    running speech engine applies hints (or has not reported). A step that
    is not a table, or a non-list value, is hand-edit garbage and counts as
    no hint action.
    """
    if not isinstance(actions, list):
        return False
    return any(
        isinstance(step, dict) and step.get("function") == HINT_ACTION_NAME
        for step in actions
    )


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
    
    def __init__(
        self,
        patterns_file: str,
        user_patterns_file: Optional[str] = None,
        *,
        migrate_legacy_ids: bool = False,
    ):
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
            migrate_legacy_ids: When True, a load that resolves a pre-doc_id
                override to exactly one built-in WRITES that name into the
                user file, so the association survives a release that
                rewrites the built-in's expression
                (wh-pattern-override-doc-id.3.1). False everywhere but the
                Logic process's own catalog: one writer, and every tool,
                test and preview that builds a catalog stays memory-only.
                See ``_write_recovered_ids`` for what the write may and may
                not touch.
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
        # The entries as the two files spell them, kept so a caller can ask
        # what a DIFFERENT user file would produce without re-reading disk
        # (wh-pattern-override-doc-id.3.5). The built lists above cannot
        # answer that: they have lost the user file's order, the rows the
        # build dropped, and the identity a legacy resolution attached.
        self._raw_system_entries: List[Dict[str, Any]] = []
        self._raw_user_entries: List[Dict[str, Any]] = []
        self._patterns_file = patterns_file  # Store for reload()
        self._user_patterns_file = (
            user_patterns_file
            if user_patterns_file is not None
            else self._default_user_patterns_file()
        )
        self._migrate_legacy_ids = migrate_legacy_ids

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
    def _merge_key(entry: Dict[str, Any]) -> Optional[Identity]:
        """Return the merge key for a raw entry, from the one shared rule.

        Delegates to ``speech.pattern_buildable.slot_identity`` and adds
        nothing. The key decides only whether a user entry replaces a
        built-in; it is never the compiled pattern. An entry that identifies
        as nothing returns None and is always appended.

        This used to be the entry's pattern text, normalized by strip and
        casefold, and computed here. A regex is editable, so a release that
        rewrote a built-in's expression orphaned every override saved against
        it (wh-pattern-override-doc-id). The rule now lives in one place that
        the manager listing and the draft simulation call too, so the three
        cannot disagree about which built-in an override belongs to.

        The delegate refuses an entry whose expression the build would
        reject, so a broken rule can no longer take a built-in's place and
        leave the command with no responder
        (wh-pattern-override-doc-id.3.4).
        """
        return slot_identity(entry)

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

    @staticmethod
    def _same_trigger(left: Any, right: Any) -> bool:
        """True when two raw entries carry the same normalized expression.

        It separates a rule that was superseded from one that was displaced
        (wh-pattern-override-doc-id.2.1). A displaced rule keeps running
        under its own trigger; a superseded one could never match again.
        """
        left_text = left.get("pattern") if isinstance(left, dict) else None
        right_text = right.get("pattern") if isinstance(right, dict) else None
        if not isinstance(left_text, str) or not isinstance(right_text, str):
            return False
        return normalized_text_key(left_text) == normalized_text_key(right_text)

    def _merge_entries(
        self,
        system_entries: List[Dict[str, Any]],
        user_entries: List[Dict[str, Any]],
        recovered: Optional[Dict[int, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Merge user entries onto system entries by identity (user wins).

        A user entry whose identity matches a system entry replaces that
        system entry in place, preserving the built-in's position in the
        order (which matters for order-sensitive replacement patterns). A
        user entry with an identity nothing shipped is appended after all
        system entries.

        The identity is the built-in's ``doc_id`` when both carry one, and
        the normalized pattern text otherwise. Every shipped pattern now
        carries an id and a user file written before ids existed carries
        none, so those two never match on identity alone: such an entry
        would be appended behind the built-in it was written to replace,
        and lose. The only association that file holds is its expression,
        so an entry with no id that no built-in matches by identity is
        migrated onto the built-in carrying that same text -- but only when
        exactly one does (wh-pattern-override-doc-id A4). Two candidates
        means the file does not say which was meant; guessing would hand a
        user's replacement to a command they never touched, so the entry is
        appended, kept, and reported. See ``speech.pattern_identity``.

        Two user entries can claim one built-in, and both are kept. The
        LAST claimant in user-file order holds the built-in's slot: it
        is the newer customisation, and in the sequence that produces
        two claims it is the one carrying the built-in's own trigger.
        Every earlier claimant is appended after all system entries,
        where it keeps running under its own trigger, and a warning
        names both. The one exception is a claimant whose trigger equals
        the trigger that replaced it: appending that would add a rule
        which can never match and put two rules on one phrase, and two
        copies of one expression collapsed before ids existed, so it is
        superseded outright (wh-pattern-override-doc-id.2.1).

        A shipped identity names the built-in's own position for the whole
        merge. A third claimant arriving after a displacement therefore
        reaches that position, not the row an earlier claimant was moved
        to. Without the anchor it read the moved row as a free built-in
        slot, wrote over a saved rule, and stayed at the end of the
        command order itself (wh-pattern-override-doc-id.3.2).

        *recovered* is how a caller asks which names this merge had to
        recover from expression text. When a dict is passed, every user
        entry the legacy branch resolved to exactly one SHIPPED name is
        recorded there as ``{position in user_entries: doc_id}``. Nothing
        else goes in it: an entry that names itself already carries its
        name in the file, and an ambiguous one has no single answer to
        record. The merge behaves identically whether or not the dict is
        passed -- it is a report, not a switch
        (wh-pattern-override-doc-id.3.1).
        """
        merged = list(system_entries)
        key_to_index: Dict[Identity, int] = {}
        for i, entry in enumerate(merged):
            key = self._merge_key(entry)
            if key is not None:
                key_to_index[key] = i
        # The built-in's own position, fixed for the whole merge. A
        # shipped identity names that position and nothing else: repointing
        # it at a row a claimant was moved to sent the next claimant to the
        # end of the command order and let it overwrite the moved rule
        # (wh-pattern-override-doc-id.3.2).
        builtin_slots: Dict[Identity, int] = dict(key_to_index)
        # Built from the SYSTEM entries only: the legacy question is
        # "which shipped pattern did this un-named override replace?",
        # and a user entry is never an answer to it.
        candidates = entry_text_candidates(system_entries)

        # The indices a user entry has taken. A slot the built-in still
        # holds may be replaced outright; a slot another user entry holds
        # must not lose that entry (wh-pattern-override-doc-id.2.1).
        claimed: set = set()

        for user_position, user_entry in enumerate(user_entries):
            key = self._merge_key(user_entry)
            index: Optional[int] = None
            claimed_identity: Optional[Identity] = None
            matches: List[Identity] = []
            if key is not None and key in key_to_index:
                index = key_to_index[key]
                claimed_identity = key
            else:
                matches = legacy_candidates(user_entry, key, candidates)
                if len(matches) == 1:
                    # A pre-doc_id override, unambiguously placed. Record
                    # the slot under the entry's own key too, so a second
                    # copy of the same expression lands here rather than
                    # being appended -- which is what the old text-only
                    # merge did.
                    index = key_to_index[matches[0]]
                    claimed_identity = matches[0]
                    # Report the recovered name so the caller can write it
                    # into the file and stop recovering it every load
                    # (wh-pattern-override-doc-id.3.1).
                    #
                    # The DOC_KIND test cannot fail today and is kept as a
                    # statement of what may be written, not as a live
                    # branch. A built-in carrying no doc_id contributes a
                    # TEXT identity under its own normalized text, so a
                    # user entry whose text matches it also matches it by
                    # identity and is settled in the branch above -- this
                    # one is only reached when the identity match missed.
                    # If a later change did let a text identity arrive
                    # here, writing its expression into a doc_id key would
                    # invent a name nothing ships, and every override
                    # written under that invented name would break at the
                    # next release. Cheaper to refuse it than to find out.
                    if (
                        recovered is not None
                        and matches[0][0] == DOC_KIND
                    ):
                        recovered[user_position] = matches[0][1]
            if index is not None:
                # The last claimant holds the built-in's slot: it is the
                # newer customisation, and in the sequence that produces two
                # claims it is the one carrying the built-in's own trigger.
                # Whoever it displaces is appended and keeps running, which
                # is where a user entry matching no built-in already goes.
                displaced = merged[index] if index in claimed else None
                if displaced is not None and self._same_trigger(
                    displaced, user_entry
                ):
                    # Superseded outright rather than displaced. Appending a
                    # rule whose trigger equals the rule that replaced it
                    # adds one that can never match, and puts two rules on
                    # one phrase where there has only ever been one. Two
                    # copies of one expression collapsed before doc_ids
                    # existed and still do.
                    displaced = None
                merged[index] = user_entry
                claimed.add(index)
                if displaced is not None:
                    # Two pieces of bookkeeping the moved row needs
                    # (wh-pattern-override-doc-id.3.2). ``claimed`` keeps
                    # meaning "a user entry sits here". And the identity
                    # that named the built-in's slot goes on naming it, so
                    # the next claimant of that built-in reaches the
                    # built-in's own position rather than wherever this
                    # rule was moved to. Only a key the user's own entry
                    # introduced follows that entry to its new row, which
                    # is what lets a second copy of one expression find
                    # the copy that moved and supersede it.
                    merged.append(displaced)
                    claimed.add(len(merged) - 1)
                    displaced_key = self._merge_key(displaced)
                    if (
                        displaced_key is not None
                        and displaced_key not in builtin_slots
                        and key_to_index.get(displaced_key) == index
                    ):
                        key_to_index[displaced_key] = len(merged) - 1
                    logger.warning(
                        "Two saved rules claim the built-in %r. %r takes "
                        "its place in the command order and %r now runs "
                        "after every built-in instead. Both still run. "
                        "Delete the one you no longer want in the Pattern "
                        "Manager.",
                        claimed_identity[1] if claimed_identity else "",
                        user_entry.get("pattern"),
                        displaced.get("pattern"),
                    )
                key_to_index[key] = index
                continue
            if len(matches) > 1:
                # Not silent: a built-in the user may have switched off is
                # still answering, and only a reader can say which of these
                # the entry meant.
                logger.warning(
                    "Pattern override %r in %s matches %d built-in "
                    "patterns by text (%s) and carries no doc_id, so it "
                    "cannot be matched to one of them. It is kept and "
                    "still runs, but it no longer replaces a built-in. "
                    "Add doc_id = \"<one of those>\" to the entry to "
                    "restore the replacement.",
                    user_entry.get("pattern"),
                    user_entry.get("_source_file", self._user_patterns_file),
                    len(matches),
                    ", ".join(identity[1] for identity in matches),
                )
            merged.append(user_entry)
            claimed.add(len(merged) - 1)
            if key is not None and key not in key_to_index:
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
        List[Dict[str, Any]],
        List[Dict[str, Any]],
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
            command_hotword, trailing_commands, system_entries,
            user_entries). The last two are the RAW entries the two files
            spell, returned rather than stored so the caller can put them
            on self in the same atomic swap as everything else -- a failed
            reload must leave the old raw entries in place beside the old
            built structures (wh-pattern-override-doc-id.3.5).
        """
        system_entries, system_hotword = self._parse_file(
            self._patterns_file, require_hotword=True,
        )
        system_entries = self._tag_source(system_entries, self._patterns_file)

        user_entries: List[Dict[str, Any]] = []
        user_hotword: Optional[str] = None
        # Where each kept user entry sat in the file's own [[pattern]] list.
        # _tag_source drops a non-table entry, so its output position is not
        # the file position, and the migration below addresses blocks by
        # the file position (wh-pattern-override-doc-id.3.1).
        file_positions: List[int] = []
        if self._user_patterns_file and os.path.exists(self._user_patterns_file):
            try:
                raw_user_entries, user_hotword = self._parse_file(
                    self._user_patterns_file, require_hotword=False,
                )
                user_entries = self._tag_source(
                    raw_user_entries, self._user_patterns_file,
                )
                file_positions = [
                    position
                    for position, entry in enumerate(raw_user_entries)
                    if isinstance(entry, dict)
                ]
            except Exception:
                logger.warning(
                    "User patterns file %s failed to load; using system "
                    "patterns only",
                    self._user_patterns_file, exc_info=True,
                )
                user_entries, user_hotword = [], None
                file_positions = []

        # Only the one catalog that asked collects the report; every other
        # loader merges exactly as before and writes nothing.
        recovered: Optional[Dict[int, str]] = (
            {} if self._migrate_legacy_ids else None
        )
        merged_entries = self._merge_entries(
            system_entries, user_entries, recovered,
        )
        command_hotword = self._effective_hotword(system_hotword, user_hotword)

        first_words, all_patterns, pattern_count, trailing_commands = (
            self._build_structures(merged_entries, self._patterns_file)
        )

        # PARSE, MERGE, BUILD, and only THEN write. The catalog this load
        # returns is built from the entries in memory, not from a re-read of
        # the migrated file, so a write that fails cannot change what the
        # person's commands do this run. The two still agree afterwards: the
        # written doc_id makes the next load reach the same built-in by name
        # instead of by expression text (wh-pattern-override-doc-id.3.1).
        if recovered:
            targets = {
                file_positions[position]: (
                    doc_id, user_entries[position].get("pattern"),
                )
                for position, doc_id in recovered.items()
                if position < len(file_positions)
            }
            entry_of_file_position = {
                file_positions[position]: position
                for position in recovered
                if position < len(file_positions)
            }
            # The entries returned here are the snapshot every later reader
            # works from -- the try-it preview above all, which saves what
            # the snapshot says. A block the write named on disk and not in
            # the snapshot leaves that reader believing the block still has
            # no name, which is the .3.5 defect returning for exactly the
            # rows this migration touched (wh-pattern-override-doc-id.3.8).
            # So the snapshot takes the keys the file took, and takes them
            # only for the blocks the write reports -- never for a block it
            # refused or failed on. A REPLACEMENT dict, not an edit in
            # place: the build above holds these same dicts, and it is
            # built from what the merge resolved in memory.
            for file_position in self._write_recovered_ids(targets):
                position = entry_of_file_position[file_position]
                user_entries[position] = {
                    **user_entries[position],
                    DOC_ID_KEY: targets[file_position][0],
                    ORIGIN_KEY: ORIGIN_OWN,
                }
        return (
            first_words, all_patterns, pattern_count, command_hotword,
            trailing_commands, system_entries, user_entries,
        )

    # The copy of the user file this migration keeps, named so it can never
    # be the copy the Pattern Manager's own save keeps. The editor writes
    # ``<user file>.bak`` before every save; a migration writing to that
    # name would destroy the one copy an edit could be undone from.
    BACKUP_SUFFIX = ".pre-doc-id-migration.bak"

    # Written beside the target so os.replace stays on one volume, and named
    # after the target so a leftover is obviously ours.
    _TEMP_SUFFIX = ".doc-id-migration.tmp"

    def _write_recovered_ids(
        self, recovered: Dict[int, Tuple[str, Any]],
    ) -> Set[int]:
        """Write recovered built-in names into the user patterns file.

        *recovered* maps a block's position in the file's ``[[pattern]]``
        list to the ``(doc_id, expression)`` the merge recovered for it.
        Two lines go into each of those blocks, immediately after its
        ``[[pattern]]`` header::

            doc_id = "window-maximize"
            origin = "user"

        and nothing else in the file changes. That is the shape
        ``create_pattern`` writes for a Customize today, so a migrated block
        ends up as the editor would have written it. ``origin`` does not make
        the block independent: ``is_own_rule`` also requires the absence of a
        doc_id, and this block now has one.

        WHY AFTER THE HEADER and not after the pattern line. A hand-edited
        expression can be a triple-quoted string spanning several lines, and
        the header is the one line in a block whose end is certain.

        WHY THE FILE IS EDITED AS TEXT. A person hand-edits this file. Their
        comments, blank lines, key order and quoting are theirs, and parsing
        the file and writing it back from the parsed data would lose all of
        it. Only the inserted lines are new.

        WHY EACH BLOCK IS CHECKED AGAIN HERE. The located block must parse
        on its own, hold exactly one table, carry the expression the merge
        resolved, and carry no ``doc_id`` of its own. A block failing any of
        those is left alone -- the same defence ``update_pattern`` applies
        before it rewrites a block.

        AND WHY THE WHOLE FILE IS CHECKED TOO. Read in isolation, a fragment
        cut out of somebody's string literal is itself a valid block, so the
        per-block check above cannot tell a misplaced insert from a correct
        one. ``locate_pattern_block`` is what refuses to count a
        header-looking line inside a string; this last check is what makes a
        mistake there harmless instead of silent. The file that would be
        installed is parsed beside the original, and the two must hold the
        same tables in the same order with the same values, differing only
        by the two keys just written, on exactly the blocks they were
        written to. Anything else is refused with a warning and nothing is
        written (wh-pattern-override-doc-id.3.7).

        RETURNS the file positions actually written, so the caller can put
        the same two keys on the entries it retains and leave a refused or
        failed block untouched (wh-pattern-override-doc-id.3.8).

        NOTHING HERE MAY STOP THE LOAD. Every failure -- a read-only file, a
        refused rename, a file another program holds open -- is logged at
        warning level and the load continues with the association it already
        holds in memory. Only durability waits for the next load
        (wh-pattern-override-doc-id.3.1, constraint 4).
        """
        path = self._user_patterns_file
        temp_path = path + self._TEMP_SUFFIX
        try:
            with open(path, "rb") as handle:
                original_bytes = handle.read()
            original_text = original_bytes.decode("utf-8")
            newline = "\r\n" if "\r\n" in original_text else "\n"
            lines = original_text.splitlines(keepends=True)

            # Descending, so inserting into one block cannot move the line
            # numbers of a block still to be found.
            written: Set[int] = set()
            for position in sorted(recovered, reverse=True):
                doc_id, expression = recovered[position]
                start, end = locate_pattern_block(lines, position)
                if start is None or end is None:
                    continue
                if not self._block_takes_the_id(
                    "".join(lines[start:end]), expression,
                ):
                    continue
                if not lines[start].endswith(("\n", "\r")):
                    lines[start] = lines[start] + newline
                lines[start + 1:start + 1] = [
                    f'{DOC_ID_KEY} = "{doc_id}"{newline}',
                    f'{ORIGIN_KEY} = "{ORIGIN_OWN}"{newline}',
                ]
                written.add(position)

            if not written:
                return set()

            new_text = "".join(lines)
            # Refuse to install a file that would not load. Nothing below
            # can produce one, so reaching this is a defect in the insert.
            tomllib.loads(new_text)

            if not self._only_the_named_blocks_took_the_keys(
                original_text, new_text, written,
            ):
                logger.warning(
                    "Refusing to write recovered built-in names into %s: the "
                    "file it would install differs from the original by more "
                    "than the two names for block(s) %s. The saved patterns "
                    "still replace their built-ins this run.",
                    path, sorted(written),
                )
                return set()

            backup_path = path + self.BACKUP_SUFFIX
            if not os.path.exists(backup_path):
                # Only the FIRST migration keeps a copy. A later run must
                # not replace the original with an already-migrated one.
                with open(backup_path, "wb") as handle:
                    handle.write(original_bytes)

            with open(temp_path, "wb") as handle:
                handle.write(new_text.encode("utf-8"))
            os.replace(temp_path, path)
            logger.info(
                "Wrote the built-in name into %d saved pattern(s) in %s, so "
                "the association survives a rewrite of the built-in.",
                len(written), path,
            )
            return written
        except Exception as exc:
            logger.warning(
                "Could not write recovered built-in names into %s: %s. The "
                "saved patterns still replace their built-ins this run; the "
                "names will be written at the next load.",
                path, exc,
            )
            return set()
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning(
                        "Left a temporary file behind at %s", temp_path,
                    )

    @staticmethod
    def _only_the_named_blocks_took_the_keys(
        original_text: str, new_text: str, positions: Set[int],
    ) -> bool:
        """Whether *new_text* is *original_text* plus the two names, only.

        The last check before the file is installed, and the only one that
        reads the whole document rather than one block. Both texts are
        parsed and compared table by table: every table outside *positions*
        must be identical, every table inside must be identical apart from
        gaining a ``doc_id`` and ``origin = "user"``, the count and order
        must match, and everything outside the ``[[pattern]]`` list -- the
        hotword above all -- must be untouched. A block that already named
        a built-in is not one this migration may claim, so a table inside
        *positions* that carried a ``doc_id`` before refuses too.

        Comments, blank lines, key order and quoting are NOT compared,
        because the parser does not see them. They are protected by the
        insert itself, which copies every other line through unchanged, and
        by the tests that compare the file's bytes.
        """
        try:
            before_doc = tomllib.loads(original_text)
            after_doc = tomllib.loads(new_text)
        except Exception:
            return False
        before = before_doc.pop("pattern", [])
        after = after_doc.pop("pattern", [])
        if before_doc != after_doc:
            return False
        if not isinstance(before, list) or not isinstance(after, list):
            return False
        if len(before) != len(after):
            return False
        for index, (was, now) in enumerate(zip(before, after)):
            if not isinstance(was, dict) or not isinstance(now, dict):
                # tomllib only puts tables in an array of tables, so this
                # is unreachable; it is here so a comparison can never be
                # made between two things this walk cannot read.
                if was != now:
                    return False
                continue
            if index not in positions:
                if was != now:
                    return False
                continue
            if DOC_ID_KEY in was:
                return False
            if not is_valid_doc_id(now.get(DOC_ID_KEY)):
                return False
            if now.get(ORIGIN_KEY) != ORIGIN_OWN:
                return False
            named = {DOC_ID_KEY, ORIGIN_KEY}
            if {
                key: value for key, value in now.items() if key not in named
            } != {
                key: value for key, value in was.items() if key not in named
            }:
                return False
        return True

    @staticmethod
    def _block_takes_the_id(block_text: str, expression: Any) -> bool:
        """Whether a located raw-text block is the one the merge resolved.

        It must parse on its own, hold exactly one ``[[pattern]]`` table,
        carry the expression the merge matched, and name no built-in yet.
        """
        try:
            parsed = tomllib.loads(block_text)
        except Exception:
            return False
        tables = parsed.get("pattern", [])
        if len(tables) != 1 or not isinstance(tables[0], dict):
            return False
        table = tables[0]
        return (
            table.get("pattern") == expression
            and DOC_ID_KEY not in table
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

                    # A pattern that saves a hint needs an engine that
                    # applies hints (wh-boost-engine-qualification, ruling
                    # 1 of 2026-09-21). Derived from the actions, not read
                    # from a patterns.toml key, so the Pattern Manager's
                    # write-back cannot drop it and a user-made pattern
                    # with the same action obeys the same rule. The
                    # matcher refuses such a pattern only when the running
                    # engine reports that it does not apply hints.
                    requires_hint_engine = _actions_need_hint_engine(
                        actions_list
                    )
                    if requires_hint_engine:
                        data_dict["requires_hint_engine"] = True

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
                        literal_prefix = auto_metadata.get("literal_prefix", "")
                        data_dict["literal_prefix"] = literal_prefix
                        # The compiled matchers the probe tests against live
                        # here too, not in a cache on the router
                        # (wh-lru-cache-hot-paths.1.5). A user rule from the
                        # writable user_patterns.toml can contribute any
                        # prefix, of any length, in any number, so a bounded
                        # cache keyed on the prefix bounds the entry count and
                        # not the retained bytes. Stored on the pattern data,
                        # WheelHouse's reference to the matchers dies with the
                        # rule: reload() rebuilds these dicts, so the catalog
                        # holds nothing for a rule the user deletes. CPython's
                        # own regex cache keeps the compiled pattern longer;
                        # build_literal_prefix_matchers states that bound
                        # (wh-lru-cache-hot-paths.1.6).
                        data_dict["literal_prefix_matchers"] = (
                            build_literal_prefix_matchers(literal_prefix)
                        )
                        logger.debug(f"Auto-detected greedy pattern: {pattern_str}")
                    else:
                        # wh-review-pattern-fixes.12: a NON-greedy anchored
                        # pattern's "literal prefix" is its WHOLE body
                        # (``^push to talk mode$`` -> ``push to talk mode``),
                        # so a pause after two words of a 3+-word literal
                        # command keeps listening instead of finalizing as
                        # dictation. Same load-time placement rationale as
                        # the greedy branch above (wh-lru-cache-hot-paths.1.5)
                        # and the same per-pattern constant bound on matcher
                        # count (build_literal_prefix_matchers caps it), so
                        # the added load cost is one bounded compile set per
                        # multi-word pattern. Stored under its OWN key: the
                        # greedy timer and the greedy-prefix consistency
                        # tests key off literal_prefix /
                        # literal_prefix_matchers, and a non-greedy pattern
                        # must never become eligible for the greedy timer.
                        literal_body = extract_full_literal_body(
                            transformed_pattern
                        )
                        if literal_body and (
                            " " in literal_body or "\\s" in literal_body
                        ):
                            data_dict["literal_body_matchers"] = (
                                build_literal_prefix_matchers(literal_body)
                            )

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
                    built_entry: Dict[str, Any] = {
                        'compiled_pattern': compiled,
                        'pattern_type': pattern_type,
                        'actions': actions_list,
                        'requires_hotword': requires_hotword,
                        'validation_group': auto_metadata.get("validation_group"),
                        'is_greedy': auto_metadata.get("is_greedy", False),
                        'raw_pattern': pattern_str,
                        'is_user': source_file == self._user_patterns_file,
                        'whole_utterance_only': whole_utterance_only,
                        'requires_hint_engine': requires_hint_engine,
                    }
                    # Carry the durable name forward so anything working on
                    # the built list can ask the same identity question the
                    # merge asked. The draft simulation in pattern_tester is
                    # the caller that needs it: without the id it would place
                    # a Customize draft by text and disagree with the save it
                    # previews (wh-pattern-override-doc-id A6). Absent, never
                    # None, and only for a well-formed id -- a malformed one
                    # merges on text, so carrying it would let a later reader
                    # key on something the merge does not honor.
                    entry_doc_id = rule.get(DOC_ID_KEY)
                    if is_valid_doc_id(entry_doc_id):
                        built_entry[DOC_ID_KEY] = entry_doc_id
                    # ``origin`` is deliberately NOT carried here. Nothing
                    # reads it from a built entry: the draft simulation
                    # works on the RAW user entries and asks the real merge
                    # (_simulate_save), and the only remaining built-list
                    # reader, _simulate_merge, asks the question of the
                    # draft alone. Carrying it would be a key no code reads
                    # and no test could hold to anything
                    # (wh-pattern-override-doc-id.3.3).
                    all_patterns.append(built_entry)

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
            (re.Pattern matching the word case-insensitively), ``actions``
            (the raw action list) and ``requires_hint_engine``.
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
            # wh-boost-engine-qualification: the same derived flag the
            # leading entries carry; SpeechProcessor reads it.
            "requires_hint_engine": _actions_need_hint_engine(actions_list),
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
                trailing_commands, system_entries, user_entries,
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
        self._raw_system_entries = system_entries
        self._raw_user_entries = user_entries

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
                trailing_commands, system_entries, user_entries,
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
        self._raw_system_entries = system_entries
        self._raw_user_entries = user_entries

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

        # Expand the whole pattern into its concrete variants first, and
        # take the first word of each (wh-and-sign-index-defect). This runs
        # BEFORE the two regex branches below because neither of them can
        # tell a leading optional group from an alternation that merely
        # CONTAINS one: both find the group's end with [^)]+, which stops at
        # the first close paren. The ampersand pattern then read
        # \b(?:ampersand|and sign(?:ed)?)\b, and on it the scan stopped
        # inside at "(?:ed", the optional-prefix branch's \)\? then matched
        # the inner group's own ")?", and the branch fired on a pattern with
        # no optional prefix at all. It indexed "ampersand" and dropped the
        # aliases "and sign" and "and signed", so saying either one typed
        # the words instead of inserting '&'. That entry carries no
        # alternation any more -- David's answer to QUESTIONS-2026-09-05
        # item 94 dropped the two aliases once the repair made them
        # reachable and measurement showed what an unanchored replacement
        # on "and" costs ordinary dictation -- so the defect this branch
        # repairs is now pinned on a fixture shape in
        # tests/test_pattern_catalog_and_sign.py rather than on a shipped
        # pattern.
        #
        # _expand_sequence is the balanced-parenthesis reader the crewcut
        # comment further down asks for. It uses _find_group_end to find a
        # group's real end and _split_top_level_alternation to split on '|'
        # at depth 0, so a nested group cannot cut a branch short. It is
        # imported by its private name deliberately: the two modules are
        # halves of one pattern subsystem, and acceptance A5 of
        # wh-and-sign-index-defect asks for these helpers rather than a
        # second parser.
        #
        # It returns None for every shape it cannot expand EXACTLY -- a
        # capturing group, a character class, an unbounded quantifier -- and
        # those fall through to the branches below unchanged. Re-measured
        # over all 319 shipped patterns after item 94, with a counting
        # wrapper around the call rather than a guess at what reaches it:
        # 68 patterns are answered by an earlier branch and never reach it,
        # it is called on the other 251, and of those it declines 132 and
        # expands 119. Two shipped patterns get a different answer than the
        # old branches gave, and both differ only by dropping a repeated
        # word ('^(?:patterns?|pattern manager)$' returned 'pattern' twice,
        # '^(?:come|kama|commer|come on)$' returned 'come' twice). The index
        # deduplicates on insert, so neither changes a lookup. No shipped
        # pattern gains or loses an index word.
        expanded = _expand_sequence(cleaned)
        if expanded is not None:
            for variant in expanded:
                for word in self._extract_simple_words(variant):
                    lowered = word.lower()
                    if lowered and lowered not in first_words:
                        first_words.append(lowered)
            if first_words:
                return first_words

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
            # ...and the same prefix one group deeper: ((?:click|tap)...).
            # [^)]+ above stops at the FIRST close paren, so on that shape it
            # captures '(?:click|tap' -- the inner group's OPENING paren comes
            # along and the '?:' test just above never fires. The split on '|'
            # then yields ['(?:click', 'tap'] and _extract_simple_words drops
            # the first alternative, so the word never enters the first-word
            # index and the router never searches the pattern for it
            # (wh-grid-click-nested-group; the shipped case was
            # ^((?:click|tap)[.!?]?)$, where a spoken "click" was typed as text
            # while its alias "tap" in the same pattern worked).
            #
            # crewcut: this strips the nested group's opening paren, but
            # [^)]+ above still stops at the first close paren. Since
            # wh-and-sign-index-defect the expander above answers first, so
            # this branch is reached only for shapes the expander refuses --
            # in practice a CAPTURING outer group, which is exactly the shape
            # written here. The two limits below therefore still stand, and
            # both need a capturing group to reach them. Inside a
            # NON-capturing group the expander now handles them correctly.
            # The repair here reaches only the shape where the nested group
            # holds the WHOLE alternation AND what follows it starts a new
            # word --
            # ^((?:a|b)[.!?]?)$ or ^((?:a|b)\s+...)$. Two shapes stay wrong,
            # in two different ways:
            #
            #   ^((?:a|b)|c)$   loses 'c'. An alternative placed AFTER the
            #                   nested group sits past the first close paren,
            #                   so it is never seen. Indexed under fewer
            #                   words than it should be.
            #   ^((?:up|down)load)$
            #                   indexes 'up' and 'down', which the pattern can
            #                   never start with, and still misses the real
            #                   first words 'upload' and 'download'. When the
            #                   continuation is GLUED to the alternative
            #                   rather than separated from it, the split on
            #                   '|' cuts a word in half. Before this repair
            #                   the same shape indexed only 'down', so the
            #                   repair adds a second wrong word here; a wrong
            #                   word costs a failed regex match and nothing
            #                   else, and the pattern was already unreachable
            #                   under its true first words either way.
            #
            # No shipped pattern has either shape: a grep for '((?:' over
            # every .toml under speech/ returns nothing as of 2026-09-04, the
            # rewrite of the grid-click trigger having removed the last one.
            # To remove the limit, replace the first-close-paren match with a
            # paren-balancing scan of the leading group, split the
            # alternatives from that, and append whatever follows the group
            # to each alternative before extracting words.
            elif content.startswith('(?:'):
                content = content[3:]
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

        # Extract first word-like sequence. Digit literals are accepted too
        # (wh-grid-speech-routing): the bare mouse-grid numbers pattern
        # alternates literal digits ("(1|2|...|9)"), and without a digit
        # branch here the spoken/ITN token "5" would never be indexed as a
        # command first word, so the pattern could never fire at runtime.
        #
        # An interior hyphen stays part of the word. The speech engine sends
        # one token per word, so a hum like "mm-hmm" arrives whole, hyphen
        # included. Stopping at the hyphen indexed the key "mm", the lookup
        # for the real token "mm-hmm" missed, and the router passed the word
        # through to dictation as "Not in catalog" -- the shipped
        # filler-sound filter never ran on it. Taking the whole token also
        # keeps the truncated stem OUT of the index, which matters because
        # "mm" alone is the abbreviation for millimeter and matches no
        # pattern. A trailing hyphen cannot attach: the group after it
        # requires letters, so the separator class in the mouse-grid
        # patterns ("(right|double)[\s-]+click", whose brackets this
        # function has already replaced with spaces) still yields "right".
        word_match = re.match(
            r'^\s*([a-z]+(?:-[a-z]+)*|[0-9]+)', text, re.IGNORECASE
        )
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

    def get_raw_system_entries(self) -> List[Dict[str, Any]]:
        """Return shipped entries, including origins hidden by overrides.

        Try-it needs the original expression to model a trigger move. A
        shallow copy per entry keeps its identity edits out of live state.
        """
        return [dict(entry) for entry in self._raw_system_entries]

    def get_raw_user_entries(self) -> List[Dict[str, Any]]:
        """Return the user file's entries as the file spells them.

        A shallow copy per entry, so a caller can rewrite one block's fields
        to model an edit without touching the catalog's own state. The
        entries keep their ``_source_file`` tag, which is what decides
        ``is_user`` when they are built again.

        The built list ``get_all_patterns`` returns cannot stand in for
        this. It has lost the user file's order (the merge reorders), the
        rows the build dropped (a no-action override is absent from it
        entirely), and the built-in identity a legacy text resolution
        attached to a row carrying no id of its own -- the three facts the
        merge decides a slot with (wh-pattern-override-doc-id.3.5).
        """
        return [dict(entry) for entry in self._raw_user_entries]

    def build_from_user_entries(
        self, user_entries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return the pattern list THESE user entries would produce.

        The shipped entries and both build steps are the catalog's own, so
        the answer is the one a save of this user file followed by a reload
        would give. Nothing is stored: the catalog is unchanged when this
        returns, which is what lets the try-it preview ask the question
        without a file write (wh-pattern-override-doc-id.3.5).

        Args:
            user_entries: Raw ``[[pattern]]`` entries in user-file order,
                usually ``get_raw_user_entries()`` with one block rewritten.
                They are tagged as the user file's own, exactly as the
                loader tags them, so a caller can add a block without
                knowing where the user file lives. Without the tag the
                build reads the added entry as a shipped one and
                ``is_user`` comes out false.

        Returns:
            The list ``get_all_patterns`` would return afterwards.
        """
        tagged = self._tag_source(user_entries, self._user_patterns_file)
        merged = self._merge_entries(self._raw_system_entries, tagged)
        _first_words, all_patterns, _count, _trailing = (
            self._build_structures(merged, self._patterns_file)
        )
        return all_patterns

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
