"""Pattern Manager - backend for the Pattern Manager UI.

Handles TOML read/write, regex generation from user input,
validation, and conflict detection for voice command patterns.
"""
import hashlib
import logging
import os
import re
import shutil
import tomllib
from typing import Any

from .phrase_expression import generate_expression, normalize_phrases
from .pattern_transform import transform_pattern
from .safe_regex import RegexTimeout, match_bounded

logger = logging.getLogger(__name__)


class PatternManager:
    """Backend for Pattern Manager UI.

    Reads patterns.toml for browsing, generates regex from user-friendly
    input, writes new user patterns, and validates for conflicts.
    """

    # Comment header regex for category detection
    _CATEGORY_RE = re.compile(
        r'^#\s*(COMMANDS|REPLACEMENTS|COMMANDS/REPLACEMENTS)\s*-\s*(.+?)\s*$'
    )

    # Adversarial corpus for the save-time catastrophic-backtracking probe
    # (wh-pattern-editor-r0.4). Best-effort by design: a small probe corpus
    # cannot catch every pathological pattern, but it stops the common
    # nested-quantifier shapes (e.g. ^(\w+\s*)+$) before they reach the
    # live catalog, where the runtime would match them against every
    # utterance inside the Logic asyncio loop.
    _BACKTRACK_PROBES = (
        "a" * 30 + "!",
        ("word " * 8).strip() + "!",
        "the quick brown fox jumps over the lazy dog!!",
    )
    _BACKTRACK_ERROR = (
        "This pattern takes too long to match and could freeze WheelHouse, "
        "so it was not saved. Simplify the expression."
    )

    # Returned by every write method when the user path is unavailable. The
    # resolver degrades to "" on a frozen-build data-dir failure
    # (bulletproof.3.2); with an empty path the atomic-write helpers would
    # build "" + ".tmp" == ".tmp" and drop a scratch file in the current
    # working directory before failing on os.replace(). Refusing up front
    # writes nothing (bulletproof.5.1).
    _NO_USER_FILE_ERROR = "User patterns file is unavailable"

    def __init__(self, patterns_file: str, user_patterns_file: str):
        """Back the Pattern Manager UI over the split system/user files.

        Args:
            patterns_file: Path to the shipped system patterns.toml. Read-only
                for browsing; never written by this class.
            user_patterns_file: Path to the writable user_patterns.toml. All
                create / delete / set_hotword writes go here.
        """
        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file

    @staticmethod
    def _trigger_key(raw_pattern: str) -> str:
        """Normalize a pattern string for override comparison (strip+casefold)."""
        return raw_pattern.strip().casefold()

    @staticmethod
    def generate_regex(trigger: str, pattern_type: str) -> str:
        """Generate regex pattern from a user-friendly trigger phrase.

        Args:
            trigger: Plain text trigger phrase (e.g., "save project")
            pattern_type: "command" or "replacement"

        Returns:
            Regex pattern string ready for compilation
        """
        words = trigger.strip().split()
        escaped_words = [re.escape(w) for w in words]
        body = r"\s+".join(escaped_words)

        if pattern_type == "command":
            return f"^{body}$"
        else:
            return rf"\b{body}\b"

    @staticmethod
    def generate_actions(action_type: str, params: dict) -> list[dict]:
        """Generate TOML action list from user-friendly input.

        Args:
            action_type: "hotkey", "text", "run", or "activate"
            params: Type-specific parameters

        Returns:
            List of action step dictionaries for patterns.toml
        """
        if action_type == "hotkey":
            return [{"function": "hk", "params": list(params["keys"])}]
        elif action_type == "text":
            return [{"function": "text", "params": [params["output"]]}]
        elif action_type == "run":
            return [{"function": "run", "params": [params["path"]]}]
        elif action_type == "activate":
            return [{"function": "activate", "params": [params["target"]]}]
        else:
            raise ValueError(f"Unknown action type: {action_type}")

    @staticmethod
    def pattern_id(pattern_string: str) -> str:
        """Compute stable SHA-256 ID from a pattern's regex string."""
        return hashlib.sha256(pattern_string.encode()).hexdigest()

    # A [[pattern]] header as tomllib accepts it: whitespace inside the
    # brackets and a trailing comment are both valid TOML that hand-edits
    # produce. The raw-text walks must count exactly what tomllib parses,
    # or delete/update locate the wrong block (wh-pattern-editor-r8.3).
    _PATTERN_HEADER_RE = re.compile(r"^\s*\[\[\s*pattern\s*\]\]\s*(?:#.*)?$")

    @classmethod
    def _is_pattern_header(cls, line: str) -> bool:
        return cls._PATTERN_HEADER_RE.match(line) is not None

    def _located_block_matches(
        self, block_text: str, pattern_id_hex: str,
    ) -> bool:
        """Whether a located raw-text block IS the pattern being targeted.

        Defends the header walk against any remaining misalignment -- for
        example a multi-line pattern string containing a header-looking
        line, which raw text cannot disambiguate. The located block must
        parse on its own and contain exactly the target pattern
        (wh-pattern-editor-r8.3).
        """
        try:
            parsed = tomllib.loads(block_text)
        except Exception:
            return False
        blocks = parsed.get("pattern", [])
        if len(blocks) != 1 or not isinstance(blocks[0], dict):
            return False
        raw = blocks[0].get("pattern")
        return isinstance(raw, str) and self.pattern_id(raw) == pattern_id_hex

    # Regex to strip common regex constructs when building human-readable trigger.
    # After TOML parsing, patterns contain literal chars like \b, \s+, \d+, etc.
    _TRIGGER_STRIP_RE = re.compile(
        r'\\\w\+?'    # \b, \s+, \d+, \w+ (backslash + letter + optional +)
        r'|[\\^$]'    # literal backslash, ^ anchor, $ anchor
        r'|\(\?[^)]*\)'  # non-capturing groups, lookaheads, lookbehinds
        r'|\([^)]*\)'    # capturing groups like (.+), (\d+)?
        r'|[?*+{}()\[\]]'  # remaining quantifiers and brackets
    )

    # A phrase-generated expression (phrase_expression.generate_expression):
    # each phrase re.escape'd, joined as alternatives in one non-capturing
    # group, anchored by pattern type. Recognizing the shape lets every
    # trigger_display caller render the spoken phrases instead of raw
    # regex (wh-pattern-editor-r8.2).
    _PHRASE_ALT_SPLIT_RE = re.compile(r"(?<!\\)\|")
    _ESCAPE_UNDO_RE = re.compile(r"\\(.)")

    @classmethod
    def _phrases_from_expression(cls, raw_pattern: str) -> list[str] | None:
        """The literal phrases of a phrase-shaped expression, or None.

        Every alternative must round-trip through re.escape exactly --
        anything carrying live regex syntax (nested groups, quantifiers)
        fails the check and falls back to the strip-based display.
        """
        if raw_pattern.startswith("^(?:") and raw_pattern.endswith(")$"):
            inner = raw_pattern[4:-2]
        elif raw_pattern.startswith(r"\b(?:") and raw_pattern.endswith(r")\b"):
            inner = raw_pattern[5:-3]
        else:
            return None
        phrases: list[str] = []
        for alt in cls._PHRASE_ALT_SPLIT_RE.split(inner):
            if not alt:
                return None
            candidate = cls._ESCAPE_UNDO_RE.sub(r"\1", alt)
            if re.escape(candidate) != alt:
                return None
            phrases.append(candidate)
        return phrases or None

    @staticmethod
    def _format_phrase_display(phrases: list[str]) -> str:
        """One display line for a phrase list: 'a' or 'a (or b, c)'."""
        if len(phrases) == 1:
            return phrases[0]
        return f"{phrases[0]} (or {', '.join(phrases[1:])})"

    @classmethod
    def _trigger_display(cls, raw_pattern: str) -> str:
        """Convert regex pattern to human-readable trigger text.

        A phrase-generated expression renders as its spoken phrases
        (wh-pattern-editor-r8.2); anything else strips regex syntax
        (anchors, word boundaries, groups, quantifiers) to extract the
        plain-text trigger phrase.
        """
        phrases = cls._phrases_from_expression(raw_pattern)
        if phrases is not None:
            return cls._format_phrase_display(phrases)
        text = cls._TRIGGER_STRIP_RE.sub(' ', raw_pattern)
        # Collapse whitespace and strip
        result = ' '.join(text.split())
        return result if result else raw_pattern

    @staticmethod
    def _describe_action(action: dict[str, Any]) -> str:
        """Generate a human-readable description for a single action step."""
        func = action.get("function", "")
        params = action.get("params", [])

        if func == "hk":
            # Filter out non-string params (e.g., repeat counts like 2)
            str_params = [p for p in params if isinstance(p, str)]
            repeat = next((p for p in params if isinstance(p, int)), None)
            keys = [k.title() if len(k) > 1 else k.upper() for k in str_params]
            desc = "Press " + "+".join(keys)
            if repeat and repeat > 1:
                desc += f" x{repeat}"
            return desc
        elif func == "text":
            return f'Insert "{params[0]}"' if params else "Insert text"
        elif func == "run":
            return f"Run {params[0]}" if params else "Run program"
        elif func == "activate":
            return f"Activate {params[0]}" if params else "Activate window"
        elif func == "insert_text":
            return "Insert captured text"
        else:
            return f"{func}({', '.join(str(p) for p in params)})"

    def _build_entry(
        self,
        pat_data: dict[str, Any],
        is_user_created: bool,
        overrides_builtin: bool,
    ) -> dict[str, Any]:
        """Build one UI pattern entry from a raw TOML pattern table."""
        raw_pattern: str = pat_data.get("pattern", "")
        raw_actions: list = pat_data.get("actions", [])
        entry = {
            "id": self.pattern_id(raw_pattern),
            "trigger_display": self._trigger_display(raw_pattern),
            "description": self._describe_actions(raw_actions),
            "requires_hotword": pat_data.get("requires_hotword", False),
            "is_user_created": is_user_created,
            "overrides_builtin": overrides_builtin,
            "raw_pattern": raw_pattern,
            "raw_actions": raw_actions,
        }
        # Carry the phrase list to the manager window so the editor dialog
        # can round-trip it (wh-pattern-editor-phrases). Only a non-empty
        # list of strings qualifies; a hand-edited garbage value is omitted,
        # so the dialog degrades to advanced mode (spec section 6).
        phrases = pat_data.get("phrases")
        if (
            isinstance(phrases, list) and phrases
            and all(isinstance(p, str) for p in phrases)
        ):
            entry["phrases"] = phrases
            # The stored phrase list is the source of truth for display:
            # it names the spoken phrases even when the expression is
            # hand-edited into an odd shape (wh-pattern-editor-r8.2).
            entry["trigger_display"] = self._format_phrase_display(phrases)
        # Carry type/position so the explainer classifies trailing patterns
        # and explicit-type patterns correctly (it falls back to the
        # ^-anchor heuristic when absent). Garbage values are omitted.
        for key in ("type", "position"):
            value = pat_data.get(key)
            if isinstance(value, str) and value:
                entry[key] = value
        # Carry whole_utterance_only so Customize/edit round-trips keep the
        # whole-utterance safety on punctuation aliases
        # (wh-int8-punctuation-mishears.1.1). Only a real boolean true on a
        # ^-anchored command pattern qualifies -- the runtime honors the
        # flag nowhere else, so carrying it on a replacement would keep a
        # dead flag alive across edits (wh-int8-punctuation-mishears.1.5).
        # Garbage is omitted, matching the catalog's strict validation.
        if (
            pat_data.get("whole_utterance_only") is True
            and raw_pattern.startswith("^")
        ):
            entry["whole_utterance_only"] = True
        return entry

    def get_all_patterns_structured(self) -> dict[str, Any]:
        """Read the system and user files and return categorized pattern data.

        Built-in patterns come from the shipped system file and are grouped by
        their category comment headers. User patterns come from the writable
        user file and are grouped under "User Patterns"; each is flagged
        ``overrides_builtin`` when its trigger matches a built-in. The reported
        hotword is the user override when set, otherwise the system value.

        Returns a dict with:
            - ``categories``: mapping of category name to ``{"patterns": [...]}``
            - ``hotword``: the effective command hotword string
        """
        categories: dict[str, dict[str, list]] = {}
        system_keys: set[str] = set()
        system_hotword = ""

        # --- System file (read-only, categorized by comment headers) ---
        if os.path.exists(self.patterns_file):
            with open(self.patterns_file, 'r', encoding='utf-8') as fh:
                raw_text = fh.read()
            data = tomllib.loads(raw_text)
            system_hotword = data.get("COMMAND_HOTWORD", "")
            toml_patterns: list[dict[str, Any]] = data.get("pattern", [])

            lines = raw_text.splitlines()
            category_markers: list[tuple[int, str]] = []
            for lineno, line in enumerate(lines):
                m = self._CATEGORY_RE.match(line)
                if m:
                    prefix = m.group(1).title()  # COMMANDS -> Commands
                    name = m.group(2).strip()
                    category_markers.append((lineno, f"{prefix} - {name}"))

            pattern_lines: list[int] = [
                lineno for lineno, line in enumerate(lines)
                if self._is_pattern_header(line)
            ]

            def _category_for_line(lineno: int) -> str | None:
                best: str | None = None
                for marker_line, cat_name in category_markers:
                    if marker_line < lineno:
                        best = cat_name
                    else:
                        break
                return best

            for idx, pat_data in enumerate(toml_patterns):
                raw_pattern: str = pat_data.get("pattern", "")
                system_keys.add(self._trigger_key(raw_pattern))

                cat = (
                    _category_for_line(pattern_lines[idx])
                    if idx < len(pattern_lines) else None
                )
                if cat is None:
                    cat = (
                        "Commands - Other" if raw_pattern.startswith("^")
                        else "Replacements - Other"
                    )

                entry = self._build_entry(
                    pat_data, is_user_created=False, overrides_builtin=False,
                )
                categories.setdefault(cat, {"patterns": []})["patterns"].append(entry)

        # --- User file (writable, grouped under "User Patterns") ---
        effective_hotword = system_hotword
        user_file_error: dict[str, Any] | None = None
        if os.path.exists(self.user_patterns_file):
            try:
                with open(self.user_patterns_file, 'rb') as fh:
                    udata = tomllib.load(fh)
            except Exception as exc:
                logger.warning(
                    "Could not parse user patterns file %s for the manager UI",
                    self.user_patterns_file, exc_info=True,
                )
                udata = {}
                # Surface the failure instead of degrading invisibly
                # (wh-pattern-editor-r0.7): the manager window shows a
                # banner from this. Pinned cross-worker contract -- the key
                # is ABSENT (not None) when the file parses fine, and
                # backup_path is set only when the .bak actually exists.
                backup = self.user_patterns_file + ".bak"
                user_file_error = {
                    "path": self.user_patterns_file,
                    "error": " ".join(str(exc).split()),
                    "backup_path": backup if os.path.exists(backup) else None,
                }

            user_hotword = udata.get("COMMAND_HOTWORD")
            if isinstance(user_hotword, str) and user_hotword.strip():
                # Show the normalized value the catalog actually applies
                # (wh-user-patterns-split.8.1). Mirror the catalog's validity
                # check: a multi-word value is ignored there and falls back to
                # the system hotword, so show the system value here too rather
                # than an unusable multi-word one
                # (wh-user-patterns-split-bulletproof.3.1).
                stripped_hw = user_hotword.strip()
                if len(stripped_hw.split()) == 1:
                    effective_hotword = stripped_hw

            for pat_data in udata.get("pattern", []):
                # Skip non-table entries so a hand-edited `pattern = [1, 2, 3]`
                # degrades to listing the valid entries instead of crashing the
                # manager (wh-user-patterns-split.10.1).
                if not isinstance(pat_data, dict):
                    continue
                raw_pattern = pat_data.get("pattern", "")
                # A valid TOML table can still carry a non-string value
                # (`pattern = 5`). The catalog skips it, so the manager UI skips
                # it too instead of crashing on _trigger_key(5) / pattern_id(5)
                # (wh-user-patterns-split.11.1).
                if not isinstance(raw_pattern, str):
                    continue
                overrides = self._trigger_key(raw_pattern) in system_keys
                entry = self._build_entry(
                    pat_data, is_user_created=True, overrides_builtin=overrides,
                )
                categories.setdefault(
                    "User Patterns", {"patterns": []},
                )["patterns"].append(entry)

        result: dict[str, Any] = {
            "categories": categories,
            "hotword": effective_hotword,
        }
        if user_file_error is not None:
            result["user_file_error"] = user_file_error
        return result

    def _describe_actions(self, actions: list[dict[str, Any]]) -> str:
        """Build a combined description for all action steps."""
        if not actions:
            return ""
        parts = [self._describe_action(a) for a in actions]
        return ", then ".join(parts)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _load_pattern_dicts(self, path: str) -> list[dict[str, Any]]:
        """Return the raw ``[[pattern]]`` list from a TOML file, or [] on any problem."""
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'rb') as fh:
                entries = tomllib.load(fh).get("pattern", [])
            # Skip non-table entries (e.g. a hand-edit that wrote
            # `pattern = [1, 2, 3]` as a top-level array). Every caller does
            # entry.get(...), which would raise AttributeError on a bare int
            # (wh-user-patterns-split.10.1). Also skip table entries whose
            # `pattern` value is not a string (e.g. `pattern = 5`): callers pass
            # it to _trigger_key/pattern_id, which assume a string, and the
            # catalog already skips such entries (wh-user-patterns-split.11.1).
            return [
                e for e in entries
                if isinstance(e, dict) and isinstance(e.get("pattern"), str)
            ]
        except Exception:
            logger.warning("Could not parse %s for validation", path, exc_info=True)
            return []

    def validate_pattern(self, trigger: str, pattern_type: str) -> dict[str, Any]:
        """Validate a proposed user pattern before creation.

        Under the system/user split (option 2), a trigger that matches a
        built-in is an allowed override, not an error; only a trigger that
        duplicates an existing USER pattern is rejected. Also warns on
        first-word overlap with any existing pattern.

        Returns:
            Dict with ``valid`` (bool), and optional ``error`` / ``warning`` strings.
        """
        # 1. Reject empty / whitespace-only trigger
        if not trigger or not trigger.strip():
            return {"valid": False, "error": "Trigger phrase cannot be empty"}

        # 2. Generate regex and confirm it compiles
        regex = self.generate_regex(trigger, pattern_type)
        try:
            re.compile(regex)
        except re.error as exc:
            return {"valid": False, "error": f"Invalid regex: {exc}"}

        system_patterns = self._load_pattern_dicts(self.patterns_file)
        user_patterns = self._load_pattern_dicts(self.user_patterns_file)
        key = self._trigger_key(regex)

        # 3. Exact duplicate of an existing USER pattern -> reject
        for pat in user_patterns:
            if self._trigger_key(pat.get("pattern", "")) == key:
                return {
                    "valid": False,
                    "error": "Duplicate: a user pattern with this trigger already exists",
                }

        # 4. Same trigger as a BUILT-IN -> allowed override, with a note
        for pat in system_patterns:
            if self._trigger_key(pat.get("pattern", "")) == key:
                return {
                    "valid": True,
                    "warning": (
                        f"This trigger matches the built-in command "
                        f"'{trigger.strip()}'. Your version will override the "
                        f"built-in."
                    ),
                }

        # 5. First-word overlap with any existing pattern -> buffering warning
        first_word = trigger.strip().split()[0].lower()
        for pat in [*system_patterns, *user_patterns]:
            existing_regex = pat.get("pattern", "")
            stripped = re.sub(r'^[\^]|^\\b', '', existing_regex)
            m = re.match(r'([a-zA-Z0-9_]+)', stripped)
            if m and m.group(1).lower() == first_word:
                return {
                    "valid": True,
                    "warning": (
                        f"This trigger shares a first word with existing pattern "
                        f"'{existing_regex}'. It will work but may cause buffering delays."
                    ),
                }

        # 6. All clear
        return {"valid": True, "error": None, "warning": None}

    def _find_user_trigger_collision(
        self, regex: str, exclude_id: str | None = None,
        entries: list | None = None,
    ) -> str | None:
        """Return the colliding user block's raw pattern string, or None.

        A save whose resolved expression shares a trigger key (strip+casefold,
        the catalog merge's rule) with a DIFFERENT existing user block must be
        rejected (wh-pattern-editor-r0.1): the two blocks would share a
        SHA-256 id, the runtime merge runs the LAST one while delete/update
        target the FIRST, so edits silently stop having any runtime effect.
        Collisions are checked against USER blocks only -- overriding a
        built-in is the Customize flow working as designed.

        Args:
            regex: The resolved expression about to be saved.
            exclude_id: For updates, the id of the block being rewritten; a
                key match on that block is the edit keeping its own trigger,
                not a collision.
            entries: The already-parsed ``[[pattern]]`` list from the SAME
                content the caller is about to rewrite. The write paths
                must pass this so the check judges exactly the bytes being
                saved -- a fresh disk read can disagree with them when a
                hand edit lands mid-save (wh-pattern-editor-r9.1). None
                falls back to reading the file, for callers holding no
                content.
        """
        key = self._trigger_key(regex)
        if entries is None:
            pats = self._load_pattern_dicts(self.user_patterns_file)
        else:
            # Same non-table / non-string filtering _load_pattern_dicts
            # applies (wh-user-patterns-split.10.1/.11.1).
            pats = [
                e for e in entries
                if isinstance(e, dict) and isinstance(e.get("pattern"), str)
            ]
        for pat in pats:
            raw = pat.get("pattern", "")
            if exclude_id is not None and self.pattern_id(raw) == exclude_id:
                continue
            if self._trigger_key(raw) == key:
                return raw
        return None

    @classmethod
    def _duplicate_trigger_message(cls, colliding_raw: str) -> str:
        """User-facing text for a user-block trigger collision. Shared with
        the draft tester (wh-pattern-editor-r2.1) so the try-it preview
        reports the rejection in the save's exact words."""
        return (
            f"Duplicate: a user pattern with this trigger already "
            f"exists ('{cls._trigger_display(colliding_raw)}'). "
            f"Edit that pattern instead."
        )

    def _duplicate_trigger_error(self, colliding_raw: str) -> dict[str, Any]:
        """Standard error envelope for a user-block trigger collision."""
        return {
            "success": False,
            "error": self._duplicate_trigger_message(colliding_raw),
        }

    @classmethod
    def _probe_backtracking(cls, regex: str) -> dict[str, Any] | None:
        """Reject expressions that blow up on the adversarial probe corpus.

        The probe runs against the TRANSFORMED pattern -- the catalog
        compiles ``transform_pattern(regex)``, which rewrites numeric
        ``(\\d+)`` groups to ``(\\w+)``, and the two can behave completely
        differently: raw ``^(\\d+)+$`` fails the corpus instantly while
        the transformed ``^(\\w+)+$`` backtracks catastrophically.
        Probing the raw expression would let that save through and the
        runaway pattern into the live catalog on reload
        (wh-pattern-editor-r4.1).

        Each probe runs in the safe_regex worker with the default 0.25s
        budget, fullmatch for '^'-anchored expressions and search otherwise
        (the runtime's anchor-driven strategy split), compiled IGNORECASE
        like the catalog compiles patterns. Returns the standard error
        envelope on a timeout, None when the expression behaves.
        """
        probed, _meta = transform_pattern(regex)
        mode = "fullmatch" if probed.startswith("^") else "search"
        try:
            for probe in cls._BACKTRACK_PROBES:
                match_bounded(probed, probe, flags=re.IGNORECASE, mode=mode)
        except RegexTimeout:
            return {"success": False, "error": cls._BACKTRACK_ERROR}
        return None

    def _corrupt_user_file_error(self, exc: Exception) -> dict[str, Any]:
        """Friendly envelope for a corrupt PRE-EXISTING user file (r0.7).

        Without this, a write would read the corrupt content verbatim,
        append, and fail the whole-file TOML validation with a parse error
        pointing at the OLD content -- a cryptic message about a file the
        user never edited. Named the file, quote the one-line parse error,
        and point at the .bak when one exists.
        """
        detail = " ".join(str(exc).split())
        backup = self.user_patterns_file + ".bak"
        message = (
            f"Your user patterns file could not be read: "
            f"{self.user_patterns_file} ({detail})."
        )
        if os.path.exists(backup):
            message += (
                f" The previous version is saved at {backup}; restoring "
                f"it will recover your patterns."
            )
        else:
            message += " Fix or remove the file, then try again."
        return {"success": False, "error": message}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @staticmethod
    def _format_toml_value(value: Any) -> str:
        """Format a Python value as inline TOML."""
        if isinstance(value, str):
            # Basic-string escaping: a raw backslash (a Windows run path)
            # or quote would otherwise fail -- or silently change -- the
            # whole-file TOML validation (wh-pattern-editor-advanced).
            escaped = (
                value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            return f'"{escaped}"'
        elif isinstance(value, bool):
            return "true" if value else "false"
        elif isinstance(value, list):
            items = ", ".join(PatternManager._format_toml_value(v) for v in value)
            return f"[{items}]"
        elif isinstance(value, dict):
            parts = []
            for k, v in value.items():
                parts.append(f"{k} = {PatternManager._format_toml_value(v)}")
            return "{ " + ", ".join(parts) + " }"
        else:
            return str(value)

    @staticmethod
    def _format_actions_toml(actions: list[dict]) -> str:
        """Format action list as TOML inline table array matching file style."""
        items = []
        for action in actions:
            items.append("    " + PatternManager._format_toml_value(action))
        return "actions = [\n" + ",\n".join(items) + "\n]"

    @classmethod
    def _resolve_regex_and_phrases(
        cls,
        trigger: str | None,
        pattern_type: str,
        phrases: list[str] | None,
    ) -> tuple[str, list[str] | None]:
        """Resolve the pattern regex from either a phrase list or a trigger.

        When ``phrases`` is a non-empty list, the expression is generated
        from it (each phrase escaped, joined as alternatives, anchored per
        pattern type -- wh-pattern-editor-phrases, spec section 6) and the
        ``trigger`` input is ignored, so it may be absent. Otherwise the
        regex comes from generate_regex(trigger, pattern_type) as before.

        Returns:
            ``(regex, stored_phrases)`` where ``stored_phrases`` is the
            normalized phrase list to write into the block's ``phrases``
            array, or None when the pattern is trigger-based.

        Raises:
            ValueError: From generate_expression on an invalid phrase list;
                the message is user-readable and becomes the error envelope.
        """
        if phrases:
            regex = generate_expression(phrases, pattern_type)
            return regex, normalize_phrases(phrases)
        if not isinstance(trigger, str) or not trigger.strip():
            # Without this guard an absent/empty trigger (data.get("trigger")
            # on a phrase-less update) would generate the regex "^$" and be
            # silently written.
            raise ValueError("Trigger phrase cannot be empty")
        return cls.generate_regex(trigger, pattern_type), None

    @staticmethod
    def _resolve_raw_expression(expression: Any, pattern_type: str) -> str:
        """Validate an advanced-mode raw expression (wh-pattern-editor-advanced).

        The runtime loader decides command-vs-replacement from the ``^``
        anchor alone (PatternCatalog._build_structures), so a declared type
        that contradicts the anchoring would make the stored ``type`` key
        lie to the explainer and the manager entries. The contradiction is
        rejected -- the user's regex is never silently rewritten.

        Raises:
            ValueError: User-readable message; becomes the error envelope
                (saves) or draft_error (try-it drafts).
        """
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("Expression cannot be empty")
        if "'''" in expression or "\n" in expression or "\r" in expression:
            # The block writer stores the expression in a TOML literal
            # multi-line-incapable string; refuse up front with a clear
            # message instead of a cryptic whole-file parse error.
            raise ValueError(
                "Expression cannot contain triple quotes or line breaks"
            )
        if pattern_type not in ("command", "replacement"):
            raise ValueError(f"Unknown pattern type: {pattern_type}")
        try:
            re.compile(expression)
        except re.error as exc:
            raise ValueError(f"Expression does not compile: {exc}")
        if pattern_type == "command" and not expression.startswith("^"):
            raise ValueError(
                "A command expression must start with '^' (WheelHouse "
                "treats unanchored expressions as replacements)"
            )
        if pattern_type == "replacement" and expression.startswith("^"):
            raise ValueError(
                "A replacement expression must not start with '^' "
                "(WheelHouse treats '^'-anchored expressions as commands)"
            )
        return expression

    @staticmethod
    def _validate_raw_actions(actions: Any) -> list[dict]:
        """Validate an advanced-mode raw step list; return normalized copies.

        Shape-only validation: each step is ``{function, params}`` with a
        non-empty string function and a list of scalar params (str/int/
        float/bool -- the TOML-writable kinds). Function names are not
        checked against the registry; the editor's picker only offers
        catalog functions, and a hand-written name is a power-user choice.

        Raises:
            ValueError: User-readable message for the error envelope.
        """
        if not isinstance(actions, list) or not actions:
            raise ValueError("At least one action step is required")
        validated: list[dict] = []
        for step in actions:
            if not isinstance(step, dict):
                raise ValueError(
                    "Each action step must be a table with a function name "
                    "and params"
                )
            function = step.get("function")
            if not isinstance(function, str) or not function.strip():
                raise ValueError("Each action step needs a function name")
            params = step.get("params", [])
            if not isinstance(params, list):
                raise ValueError(
                    f"Step '{function}': params must be a list"
                )
            for p in params:
                if isinstance(p, bool) or isinstance(p, (str, int, float)):
                    continue
                raise ValueError(
                    f"Step '{function}': parameter values must be text or "
                    f"numbers"
                )
            validated_step: dict = {
                "function": function.strip(), "params": list(params),
            }
            # Carry awaits_done through the rewrite: the runtime waits
            # for Input-process completion only when the key survives
            # (wh-pattern-editor-r8.1). Only a real boolean qualifies;
            # a hand-edited non-bool value is dropped, matching the
            # position-key precedent.
            awaits = step.get("awaits_done")
            if isinstance(awaits, bool):
                validated_step["awaits_done"] = awaits
            # Carry result the same way: the runtime stores the step's
            # return value in the execution context under this name for
            # later steps to substitute (command_engine reads
            # step.get("result")), so stripping it would silently change
            # what a hand-edited pattern does (wh-pattern-editor-r10.1).
            res_key = step.get("result")
            if isinstance(res_key, str):
                validated_step["result"] = res_key
            validated.append(validated_step)
        return validated

    @classmethod
    def _resolve_block_content(
        cls,
        trigger: str | None,
        pattern_type: str,
        action_type: str | None,
        action_params: dict | None,
        phrases: list[str] | None,
        expression: str | None,
        raw_actions: list[dict] | None,
    ) -> tuple[str, list[str] | None, list[dict], str | None]:
        """Resolve one block's regex, phrases, actions, and explicit type.

        Single seam shared by create_pattern, update_pattern, and the
        draft tester, so a draft can never validate differently from the
        save it previews. Two shapes:

        * Simple: ``trigger``/``phrases`` + ``action_type``/``action_params``
          (as before). No explicit type key is stored -- generated
          expressions are correctly anchored by construction.
        * Advanced raw: ``expression`` (used verbatim; trigger/phrases
          ignored, so no ``phrases`` key is stored and the pattern reopens
          in advanced mode, spec section 6) and/or raw ``actions`` steps.
          Raw expressions store ``explicit_type`` so the explainer and the
          manager entries classify without the anchor heuristic.

        Returns:
            ``(regex, stored_phrases, actions, explicit_type)``.

        Raises:
            ValueError: User-readable message for the error envelope.
        """
        if expression is not None:
            regex = cls._resolve_raw_expression(expression, pattern_type)
            stored_phrases: list[str] | None = None
            explicit_type: str | None = pattern_type
        else:
            regex, stored_phrases = cls._resolve_regex_and_phrases(
                trigger, pattern_type, phrases,
            )
            explicit_type = None

        if raw_actions is not None:
            actions = cls._validate_raw_actions(raw_actions)
        else:
            actions = cls.generate_actions(
                action_type,
                action_params if action_params is not None else {},
            )
        cls._validate_group_refs(regex, actions)
        return regex, stored_phrases, actions, explicit_type

    @staticmethod
    def _validate_group_refs(regex: str, actions: list[dict]) -> None:
        """Reject whole-param g<N> references beyond the expression's groups.

        The command engine substitutes a param that IS exactly ``g<N>``
        from the match context; g1..g9 are pre-seeded as None, so an
        out-of-range reference silently becomes None (or stays literal
        text for g10+) at runtime -- a pattern that saves cleanly and lies
        when spoken (wh-pattern-editor-r2.2). Only whole-param references
        are checked: an embedded ``g2`` inside longer text is replaced by
        the engine only when group 2 actually matched and is otherwise
        literal text, so a substring check would false-flag it. The count
        comes from the RAW expression -- the runtime's numeric transform
        only swaps group bodies, never adds or removes groups.

        Raises:
            ValueError: User-readable message for the error envelope.
        """
        group_count: int | None = None
        for step in actions:
            for param in step.get("params", []):
                if not isinstance(param, str):
                    continue
                ref = re.fullmatch(r"g([1-9][0-9]*)", param)
                if ref is None:
                    continue
                if group_count is None:
                    group_count = re.compile(regex).groups
                n = int(ref.group(1))
                if n > group_count:
                    raise ValueError(
                        f"Step '{step.get('function')}': '{param}' points "
                        f"at capture group {n}, but the expression has "
                        f"only {group_count} capture group(s)"
                    )

    @classmethod
    def _build_block_lines(
        cls,
        regex: str,
        actions: list[dict],
        requires_hotword: bool,
        phrases: list[str] | None,
        explicit_type: str | None = None,
        position: str | None = None,
        whole_utterance_only: bool = False,
    ) -> list[str]:
        """Render one ``[[pattern]]`` block as a list of lines.

        Shared by create_pattern and update_pattern so an edited block is
        regenerated in exactly the create format -- update rebuilds blocks
        from create-shaped data, and a second builder would let the two
        formats drift (Stage 1 handoff, wh-pattern-editor-phrases). The
        ``phrases`` array is written when present so the editor dialog can
        round-trip the list; raw advanced saves write ``type`` instead
        (wh-pattern-editor-advanced). ``position`` is a hand-edited
        runtime key the editor has no field for (trailing commands,
        wh-2vz); update_pattern passes the original block's value through
        so an edit does not silently turn a trailing command into a
        regular one (wh-pattern-editor-r3.1). ``whole_utterance_only``
        is the punctuation-alias safety flag, carried the same way so a
        Customize/edit does not turn an alias into an eager command
        (wh-int8-punctuation-mishears.1.1).
        """
        lines = [
            "[[pattern]]",
            f"pattern = '''{regex}'''",
        ]
        if phrases:
            lines.append(f"phrases = {cls._format_toml_value(list(phrases))}")
        if explicit_type:
            lines.append(f"type = {cls._format_toml_value(explicit_type)}")
        if requires_hotword:
            lines.append("requires_hotword = true")
        if position is not None:
            lines.append(f"position = {cls._format_toml_value(position)}")
        if whole_utterance_only:
            lines.append("whole_utterance_only = true")
        lines.append('source = "pattern_manager"')
        lines.append(cls._format_actions_toml(actions))
        return lines

    def create_pattern(
        self,
        trigger: str = "",
        pattern_type: str = "command",
        action_type: str | None = None,
        action_params: dict | None = None,
        requires_hotword: bool = False,
        phrases: list[str] | None = None,
        expression: str | None = None,
        actions: list[dict] | None = None,
        position: str | None = None,
        whole_utterance_only: bool = False,
    ) -> dict[str, Any]:
        """Create a new pattern in the writable user file.

        The shipped system file is never modified. The user file is created if
        it does not yet exist. A trigger that matches a built-in is allowed:
        the catalog merge makes the user entry override the built-in.

        When ``phrases`` is a non-empty list, the regex is generated from the
        phrase list instead of ``trigger`` (pass ``trigger=""``), and the
        written block carries a ``phrases = [...]`` array so the editor
        dialog can round-trip the list (wh-pattern-editor-phrases).

        Advanced-mode raw saves (wh-pattern-editor-advanced): a non-None
        ``expression`` is used verbatim as the pattern (trigger/phrases are
        ignored and no ``phrases`` key is written, so the pattern reopens in
        advanced mode), and the block stores an explicit ``type`` key. A
        non-None ``actions`` list of ``{function, params}`` steps is written
        verbatim instead of generating from ``action_type``/``action_params``.

        Returns:
            ``{"success": True, "pattern_id": hash}`` on success,
            ``{"success": False, "error": message}`` on failure.
        """
        if not self.user_patterns_file:
            return {"success": False, "error": self._NO_USER_FILE_ERROR}
        try:
            # Resolve regex and actions from the simple or raw shape
            regex, stored_phrases, action_steps, explicit_type = (
                self._resolve_block_content(
                    trigger, pattern_type, action_type, action_params,
                    phrases, expression, actions,
                )
            )

            # Validate regex compiles
            re.compile(regex)

            # Read existing user-file content (empty if the file is new).
            # A corrupt pre-existing file fails here with a friendly error
            # BEFORE the .bak copy -- copying first would clobber the
            # last-good backup with the corrupt content
            # (wh-pattern-editor-r0.7). Read BEFORE the collision check so
            # the check judges the same content the write appends to
            # (wh-pattern-editor-r9.1).
            file_exists = os.path.exists(self.user_patterns_file)
            existing_entries: list = []
            if file_exists:
                with open(self.user_patterns_file, 'r', encoding='utf-8') as fh:
                    content = fh.read()
                try:
                    existing_entries = tomllib.loads(content).get(
                        "pattern", [],
                    )
                except Exception as exc:
                    return self._corrupt_user_file_error(exc)
            else:
                content = ""

            # Reject a trigger-key collision with an existing user block
            # (wh-pattern-editor-r0.1). Placed at the shared
            # _resolve_block_content seam so the simple (trigger/phrases)
            # and raw (expression) paths are both covered.
            colliding = self._find_user_trigger_collision(
                regex, entries=existing_entries,
            )
            if colliding is not None:
                return self._duplicate_trigger_error(colliding)

            # After all other validation passes, probe for catastrophic
            # backtracking (wh-pattern-editor-r0.4) so the expression never
            # reaches the live catalog.
            probe_error = self._probe_backtracking(regex)
            if probe_error is not None:
                return probe_error

            # Build the new [[pattern]] block. A Customize of a shipped
            # positional pattern carries its position key through so the
            # user copy binds the same way (wh-pattern-editor-r8.5); same
            # for the whole_utterance_only alias flag
            # (wh-int8-punctuation-mishears.1.1). The flag is written only
            # when the resolved regex is a ^-anchored command -- on a
            # replacement it is meaningless and the catalog would disable
            # it with a startup warning on every launch
            # (wh-int8-punctuation-mishears.1.5).
            block_lines = self._build_block_lines(
                regex, action_steps, requires_hotword, stored_phrases,
                explicit_type,
                position=position if isinstance(position, str) else None,
                whole_utterance_only=(
                    whole_utterance_only is True and regex.startswith("^")
                ),
            )
            block = "\n" + "\n".join(block_lines) + "\n"

            if content and not content.endswith("\n"):
                content += "\n"
            new_content = content + block

            # Validate the complete file parses as valid TOML
            tomllib.loads(new_content)

            # Back up only once EVERY check has passed -- including the
            # whole-file parse above -- and a write will follow, the same
            # order as update/delete/set_hotword (wh-pattern-editor-r11.1).
            if file_exists:
                shutil.copy2(
                    self.user_patterns_file, self.user_patterns_file + ".bak",
                )

            # Write atomically: temp file then os.replace()
            tmp_file = self.user_patterns_file + ".tmp"
            with open(tmp_file, 'w', encoding='utf-8') as fh:
                fh.write(new_content)
            os.replace(tmp_file, self.user_patterns_file)

            pid = self.pattern_id(regex)
            logger.info("Created user pattern %s for trigger %r", pid[:12], trigger)
            return {"success": True, "pattern_id": pid}

        except Exception as exc:
            logger.error("Failed to create pattern: %s", exc)
            return {"success": False, "error": str(exc)}

    def delete_pattern(self, pattern_id_hex: str) -> dict[str, Any]:
        """Delete a user pattern from the user file by its SHA-256 ID.

        Only the user file is read and written; built-in patterns live in the
        system file and are not deletable (their IDs are simply "not found"
        here). Deleting a user override restores the built-in on the next
        reload, because the system file was never touched.

        Returns:
            ``{"success": True}`` on success,
            ``{"success": False, "error": message}`` on failure.
        """
        if not self.user_patterns_file:
            return {"success": False, "error": self._NO_USER_FILE_ERROR}
        try:
            if not os.path.exists(self.user_patterns_file):
                return {
                    "success": False,
                    "error": f"Pattern with ID {pattern_id_hex[:12]}... not found",
                }

            # Read and parse the user file. A corrupt file gets the friendly
            # error, not a cryptic parse failure (wh-pattern-editor-r0.7).
            with open(self.user_patterns_file, 'r', encoding='utf-8') as fh:
                content = fh.read()

            try:
                data = tomllib.loads(content)
            except Exception as exc:
                return self._corrupt_user_file_error(exc)
            toml_patterns: list[dict[str, Any]] = data.get("pattern", [])

            # Find the matching pattern by id and record its INDEX in the
            # parsed list. tomllib preserves array-of-tables order, so the
            # index maps 1:1 and in order to the [[pattern]] headers in the raw
            # text; deleting the index-th block avoids removing an earlier block
            # whose text merely quotes the target regex (workflow finding:
            # wrong-block-deletion). Skip non-table entries and non-string
            # `pattern` values from a hand-edit so pattern_id(raw) is not called
            # on an int, while still counting them so the index stays aligned
            # with the headers (wh-user-patterns-split.11.1/.11.2).
            target_index: int | None = None
            for idx, pat in enumerate(toml_patterns):
                if not isinstance(pat, dict):
                    continue
                raw = pat.get("pattern", "")
                if not isinstance(raw, str):
                    continue
                if self.pattern_id(raw) == pattern_id_hex:
                    target_index = idx
                    break

            if target_index is None:
                return {
                    "success": False,
                    "error": f"Pattern with ID {pattern_id_hex[:12]}... not found",
                }

            # Remove the target_index-th [[pattern]] block from the raw text by
            # counting headers, not by matching the pattern string.
            lines = content.splitlines(keepends=True)
            block_start: int | None = None
            block_end: int | None = None

            header_count = -1
            i = 0
            while i < len(lines):
                if self._is_pattern_header(lines[i]):
                    header_count += 1
                    if header_count == target_index:
                        block_start = i
                        j = i + 1
                        while j < len(lines):
                            s = lines[j].strip()
                            if self._is_pattern_header(lines[j]) or (
                                s.startswith("#") and "=====" in s
                            ):
                                break
                            j += 1
                        block_end = j
                        # Also consume any blank lines between this block and
                        # the next content.
                        while block_end < len(lines) and lines[block_end].strip() == "":
                            block_end += 1
                        break
                i += 1

            if block_start is None or not self._located_block_matches(
                "".join(lines[block_start:block_end]), pattern_id_hex,
            ):
                return {
                    "success": False,
                    "error": "Could not locate pattern block in file text",
                }

            # Remove the block
            new_lines = lines[:block_start] + lines[block_end:]
            new_content = "".join(new_lines)

            # Validate
            tomllib.loads(new_content)

            # Back up only once every check has passed and a write WILL
            # follow: a refused delete must not overwrite the last-good
            # .bak (wh-pattern-editor-r10.2).
            shutil.copy2(self.user_patterns_file, self.user_patterns_file + ".bak")

            # Write atomically
            tmp_file = self.user_patterns_file + ".tmp"
            with open(tmp_file, 'w', encoding='utf-8') as fh:
                fh.write(new_content)
            os.replace(tmp_file, self.user_patterns_file)

            logger.info("Deleted user pattern %s", pattern_id_hex[:12])
            return {"success": True}

        except Exception as exc:
            logger.error("Failed to delete pattern: %s", exc)
            return {"success": False, "error": str(exc)}

    def update_pattern(
        self, pattern_id_hex: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Rewrite the user pattern block whose ID matches, in place.

        Unlike delete-then-create, the block keeps its position among the
        ``[[pattern]]`` tables, so the user file's ordering (and therefore
        match precedence between user patterns) is unchanged
        (wh-pattern-editor-update, spec section 7). The block is regenerated
        from ``data`` with the same generation and validation steps as
        create_pattern, and the ID is recomputed from the new content so the
        manager window can re-select the row.

        Args:
            pattern_id_hex: SHA-256 ID of the block to rewrite.
            data: Same shape as create_pattern's inputs: ``trigger``,
                ``pattern_type``, ``action_type``, ``action_params``,
                optional ``requires_hotword``, and optional ``phrases``.
                When ``phrases`` is a non-empty list, the regex is generated
                from it and ``trigger`` may be absent; the regenerated block
                keeps a ``phrases = [...]`` array so the list survives the
                rewrite (wh-pattern-editor-phrases). An advanced-mode raw
                save instead carries ``expression`` and/or raw ``actions``
                (see create_pattern); the rewritten block then stores the
                raw expression plus a ``type`` key and drops any previous
                ``phrases`` array (wh-pattern-editor-advanced).

        Returns:
            ``{"success": True, "pattern_id": new_hash}`` on success,
            ``{"success": False, "error": message}`` on failure. A vanished
            ID (the pattern was changed or deleted outside this window --
            stale window state) fails without touching the file.
        """
        if not self.user_patterns_file:
            return {"success": False, "error": self._NO_USER_FILE_ERROR}
        try:
            # Validate the replacement content first, exactly like
            # create_pattern, so a bad edit fails before any backup or write.
            regex, stored_phrases, action_steps, explicit_type = (
                self._resolve_block_content(
                    data.get("trigger"), data["pattern_type"],
                    data.get("action_type"), data.get("action_params"),
                    data.get("phrases"), data.get("expression"),
                    data.get("actions"),
                )
            )
            re.compile(regex)

            not_found = {
                "success": False,
                "error": (
                    f"Pattern with ID {pattern_id_hex[:12]}... not found; it "
                    f"may have been changed or deleted outside this window"
                ),
            }
            if not os.path.exists(self.user_patterns_file):
                return not_found

            with open(self.user_patterns_file, 'r', encoding='utf-8') as fh:
                content = fh.read()

            # A corrupt pre-existing file gets the friendly error before
            # anything is modified (wh-pattern-editor-r0.7).
            try:
                parsed = tomllib.loads(content)
            except Exception as exc:
                return self._corrupt_user_file_error(exc)
            toml_patterns: list[dict[str, Any]] = parsed.get("pattern", [])

            # Find the target by INDEX in the parsed list, exactly like
            # delete_pattern: tomllib preserves array-of-tables order, so the
            # index maps 1:1 to the [[pattern]] headers in the raw text.
            # Non-table entries and non-string `pattern` values are skipped
            # for the ID check but still counted, keeping the index aligned
            # (wh-user-patterns-split.11.1/.11.2).
            target_index: int | None = None
            for idx, pat in enumerate(toml_patterns):
                if not isinstance(pat, dict):
                    continue
                raw = pat.get("pattern", "")
                if not isinstance(raw, str):
                    continue
                if self.pattern_id(raw) == pattern_id_hex:
                    target_index = idx
                    break

            if target_index is None:
                return not_found

            # Reject a trigger-key collision with a DIFFERENT user block
            # (wh-pattern-editor-r0.1); the block being updated is exempt so
            # an edit can keep its own trigger. Checked only AFTER the
            # target id is confirmed present: when the pattern was edited
            # outside this window to a same-trigger variant, the honest
            # failure is the stale-id one -- a "Duplicate ... edit that
            # pattern instead" here would point the user at the very
            # pattern they are already editing (wh-pattern-editor-r2.3).
            colliding = self._find_user_trigger_collision(
                regex, exclude_id=pattern_id_hex, entries=toml_patterns,
            )
            if colliding is not None:
                return self._duplicate_trigger_error(colliding)

            # Same save-time backtracking probe as create_pattern
            # (wh-pattern-editor-r0.4).
            probe_error = self._probe_backtracking(regex)
            if probe_error is not None:
                return probe_error

            # Locate the target_index-th [[pattern]] block in the raw text by
            # counting headers (same walk as delete_pattern).
            lines = content.splitlines(keepends=True)
            block_start: int | None = None
            block_end: int | None = None

            header_count = -1
            i = 0
            while i < len(lines):
                if self._is_pattern_header(lines[i]):
                    header_count += 1
                    if header_count == target_index:
                        block_start = i
                        j = i + 1
                        while j < len(lines):
                            s = lines[j].strip()
                            if self._is_pattern_header(lines[j]) or (
                                s.startswith("#") and "=====" in s
                            ):
                                break
                            j += 1
                        block_end = j
                        break
                i += 1

            if block_start is None:
                return {
                    "success": False,
                    "error": "Could not locate pattern block in file text",
                }

            # Unlike delete, keep the blank separator lines that trail the
            # block: only the block's own lines are replaced, so the
            # surrounding layout survives the rewrite.
            while (
                block_end > block_start + 1
                and lines[block_end - 1].strip() == ""
            ):
                block_end -= 1

            if not self._located_block_matches(
                "".join(lines[block_start:block_end]), pattern_id_hex,
            ):
                return {
                    "success": False,
                    "error": "Could not locate pattern block in file text",
                }

            # Build the replacement block in create_pattern's format,
            # carrying forward the original block's hand-edited
            # ``position`` key (wh-pattern-editor-r3.1) and the
            # ``whole_utterance_only`` alias flag
            # (wh-int8-punctuation-mishears.1.1). A wrong-typed value is
            # hand-edited garbage the runtime ignores anyway; it is
            # dropped, not re-written. The flag is also dropped when the
            # edit turns the pattern into an unanchored replacement -- the
            # runtime honors the flag only on ^-anchored commands
            # (wh-int8-punctuation-mishears.1.5).
            original_position = toml_patterns[target_index].get("position")
            original_whole_utterance = toml_patterns[target_index].get(
                "whole_utterance_only"
            )
            block_lines = self._build_block_lines(
                regex, action_steps, data.get("requires_hotword", False),
                stored_phrases, explicit_type,
                position=(
                    original_position
                    if isinstance(original_position, str) else None
                ),
                whole_utterance_only=(
                    original_whole_utterance is True
                    and regex.startswith("^")
                ),
            )
            block = "\n".join(block_lines) + "\n"

            new_content = (
                "".join(lines[:block_start]) + block + "".join(lines[block_end:])
            )

            # Validate the complete file parses as valid TOML
            tomllib.loads(new_content)

            # Create backup, then write atomically: temp file then os.replace()
            shutil.copy2(self.user_patterns_file, self.user_patterns_file + ".bak")
            tmp_file = self.user_patterns_file + ".tmp"
            with open(tmp_file, 'w', encoding='utf-8') as fh:
                fh.write(new_content)
            os.replace(tmp_file, self.user_patterns_file)

            new_pid = self.pattern_id(regex)
            logger.info(
                "Updated user pattern %s -> %s",
                pattern_id_hex[:12], new_pid[:12],
            )
            return {"success": True, "pattern_id": new_pid}

        except Exception as exc:
            logger.error("Failed to update pattern: %s", exc)
            return {"success": False, "error": str(exc)}

    def set_hotword(self, hotword: str) -> dict[str, Any]:
        """Write a COMMAND_HOTWORD override to the user file.

        Removes any existing top-level COMMAND_HOTWORD line and prepends the
        new one, so it stays ahead of any ``[[pattern]]`` tables (TOML
        requires top-level keys before array-of-tables). The user file is
        created if it does not exist; user patterns are preserved.

        Returns:
            ``{"success": True}`` on success,
            ``{"success": False, "error": message}`` on failure (including an
            empty value).
        """
        if not self.user_patterns_file:
            return {"success": False, "error": self._NO_USER_FILE_ERROR}
        try:
            if not isinstance(hotword, str) or not hotword.strip():
                return {"success": False, "error": "Hotword cannot be empty"}
            value = hotword.strip()
            if len(value.split()) > 1:
                # The router matches the wake word against a single STT token by
                # exact equality, so a multi-word value can never fire and would
                # silently disable every hotword-gated command
                # (wh-user-patterns-split-bulletproof.3.1).
                return {
                    "success": False,
                    "error": "Wake word must be a single word (no spaces)",
                }

            file_exists = os.path.exists(self.user_patterns_file)
            if file_exists:
                with open(self.user_patterns_file, 'r', encoding='utf-8') as fh:
                    content = fh.read()
                # A corrupt pre-existing file fails here with the friendly
                # error BEFORE the .bak copy, same as create/update/delete --
                # copying first would clobber the last-good backup with the
                # corrupt content (wh-pattern-editor-r0.7).
                try:
                    tomllib.loads(content)
                except Exception as exc:
                    return self._corrupt_user_file_error(exc)
            else:
                content = ""

            # Drop any existing top-level COMMAND_HOTWORD assignment.
            kept = [
                line for line in content.splitlines()
                if not re.match(r'^\s*COMMAND_HOTWORD\s*=', line)
            ]
            body = "\n".join(kept).lstrip("\n")
            hotword_line = f"COMMAND_HOTWORD = {self._format_toml_value(value)}\n"
            new_content = hotword_line + (("\n" + body) if body.strip() else "")

            # Validate the complete file parses as valid TOML
            tomllib.loads(new_content)

            # Back up only once every check has passed and a write WILL
            # follow, same invariant as create/update/delete
            # (wh-pattern-editor-r10.2).
            if file_exists:
                shutil.copy2(
                    self.user_patterns_file, self.user_patterns_file + ".bak",
                )

            tmp_file = self.user_patterns_file + ".tmp"
            with open(tmp_file, 'w', encoding='utf-8') as fh:
                fh.write(new_content)
            os.replace(tmp_file, self.user_patterns_file)

            logger.info("Set user command hotword to %r", value)
            return {"success": True}

        except Exception as exc:
            logger.error("Failed to set hotword: %s", exc)
            return {"success": False, "error": str(exc)}
