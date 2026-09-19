"""Parse the spoken target of a 'click ...' command into an ElementQuery (wh-vjwdl).

The speech grammar entry ``^click\\s+(.+)$`` captures everything after the word
"click" as group g1. This module turns that captured text into an
``ElementQuery``. It mirrors the stateless-parser shape of
``speech/navigation/parser.py``: a class with a single ``@staticmethod
parse(target_text) -> Optional[ElementQuery]`` that returns ``None`` when the
input is unusable so the caller can fall through to dictation.

Grammar (authoritative: docs/plans/2026-05-21-voice-element-clicking-design-v5.md
"Voice Command Grammar"):

* Everything after "click" is the spoken target.
* An optional *trailing* role keyword maps to a UIA control-type NAME string:
  button->Button, link->Hyperlink, menu->MenuItem, tab->TabItem,
  checkbox / "check box"->CheckBox, box/field/input->Edit. No role spoken ->
  role is None (any clickable control).
* The remaining words, with a single leading "the" dropped, form the name.

Phase 1 (this slice) always emits ordinal=None and spatial=None; those fields
are populated by later phases.

Gesture forms (wh-click-gesture-param). ``parse_command`` reads a WHOLE spoken
command instead of the words after "click", because the gesture word comes
FIRST: "right click cancel", "double click the report link", "right click 5".
The recognized prefixes are "right click" and "double click", each also
accepted as one hyphenated word ("right-click"); anything else, including no
prefix at all, is today's plain click. The gesture rides on
``ElementQuery.gesture``; ``parse`` keeps its signature and its default is
today's Invoke behaviour, so the existing grammar entry (which captures only
the target) is unchanged. Spec:
docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md, "Gestures on
named and numbered controls".

Number filler (same spec, "Refinement"). A spoken "number" immediately before a
number is filler, not part of the name -- "click number 5" means the same as
"click 5", the two-word phrase being far easier for a local STT engine to
recognize than a bare digit. The filler is dropped ONLY when what follows
actually parses as a number (``speech/number_word_parser.py``), so "click
number pad" still targets a control named "number pad". The number itself stays
the query NAME: the overlay routing in main.py parses ``query.name`` to resolve
a badge, and a bare number with no overlay open is looked up by name as before.

Edge cases:

* Empty / whitespace-only input, or input that collapses to an empty name
  (e.g. just "the"), returns None -> caller dictates.
* A role keyword that is the *only* word ("button", "check box") is treated as
  the NAME (role=None), not as a role with an empty name: stripping it would
  leave nothing to search for, and a control literally named "button" is a more
  useful target than an empty query.
"""

import re
from typing import Optional

from ui.element_types import DEFAULT_GESTURE, ClickGesture, ElementQuery

from .number_word_parser import parse_number_word

# Trailing sentence punctuation that local STT appends to the final word of an
# utterance (e.g. "click cancel." or "click the cancel button?"). Stripped from
# the last token before role detection so a punctuated role keyword ("button.")
# is still recognized and wh-tab7j's name matching never sees a stray "." that
# would miss the real control name ("Cancel").
_TRAILING_PUNCT = ".,!?;:"

# Spoken role keyword -> UIA ControlType NAME string. Single-word keys only;
# the two-word "check box" is handled separately before this lookup.
_ROLE_KEYWORDS = {
    "button": "Button",
    "link": "Hyperlink",
    "menu": "MenuItem",
    "tab": "TabItem",
    "checkbox": "CheckBox",
    "box": "Edit",
    "field": "Edit",
    "input": "Edit",
}


# Spoken gesture word -> ClickGesture, for the full-command entry point. The
# separator between the gesture word and "click" is whitespace or a hyphen, so
# both "right click" and "right-click" are recognized. Everything after the
# click word is the spoken target, exactly as in the plain grammar entry.
_GESTURE_WORDS = {
    "right": ClickGesture.RIGHT_CLICK,
    "double": ClickGesture.DOUBLE_CLICK,
}

_COMMAND_RE = re.compile(
    r"^\s*(?:(right|double)[\s-]+)?click\s+(.+)$",
    re.IGNORECASE,
)

# Filler word spoken before a number ("click number five"). Both singular and
# plural are accepted because ``parse_number_word`` accepts both, and STT
# regularly hears the plural after "numbers" commands.
_NUMBER_FILLERS = ("number", "numbers")


class ClickCommandParser:
    """Stateless parser: spoken click command -> Optional[ElementQuery]."""

    @staticmethod
    def parse_command(utterance: Optional[str]) -> Optional[ElementQuery]:
        """Parse a whole spoken click command, gesture prefix included.

        Handles "click <target>", "right click <target>" and "double click
        <target>" (the gesture word also hyphenated onto "click"). The gesture
        word comes BEFORE the click word, so it cannot be recognized by
        :meth:`parse`, which only ever sees the words after "click".

        Args:
            utterance: the full spoken command, e.g. "right click the cancel
                button". Case and surrounding whitespace are normalized.

        Returns:
            An ElementQuery carrying the recognized gesture, or None when the
            text is not a click command at all or its target is unusable
            (empty / article-only), matching :meth:`parse`'s contract so the
            caller falls through to dictation. The returned ``raw_utterance``
            is the TARGET portion, the same text :meth:`parse` would have
            stored, so both entry points agree on that field.
        """
        if utterance is None:
            return None
        match = _COMMAND_RE.match(utterance)
        if match is None:
            return None
        gesture_word = (match.group(1) or "").lower()
        gesture = _GESTURE_WORDS.get(gesture_word, DEFAULT_GESTURE)
        return ClickCommandParser.parse(match.group(2), gesture=gesture)

    @staticmethod
    def parse(
        target_text: str,
        *,
        gesture: ClickGesture = DEFAULT_GESTURE,
    ) -> Optional[ElementQuery]:
        """Parse the spoken target into an ElementQuery.

        Args:
            target_text: the words spoken after "click" (grammar group g1),
                e.g. "the cancel button". Case and surrounding whitespace are
                normalized; the original string is preserved as raw_utterance.
            gesture: which click the caller asked for. Defaults to
                :data:`ui.element_types.DEFAULT_GESTURE` -- today's Invoke
                behaviour -- so every pre-existing call site is unchanged.
                :meth:`parse_command` passes the gesture it recognized.

        Returns:
            An ElementQuery on success, or None when the input is empty /
            whitespace-only or collapses to an empty name (caller dictates).
        """
        if target_text is None:
            return None

        raw_utterance = target_text
        tokens = target_text.lower().split()
        if not tokens:
            return None

        # Strip trailing STT punctuation from the final word FIRST, before the
        # leading-article drop. Doing it first means a punctuated lone article
        # ("the.") is still recognized as the article below and collapses to
        # None instead of becoming a name (wh-9f3t.52.1). It also keeps a
        # punctuated trailing role keyword ("button.") recognizable and leaves
        # the name free of stray sentence punctuation.
        tokens[-1] = tokens[-1].rstrip(_TRAILING_PUNCT)
        if not tokens[-1]:
            tokens = tokens[:-1]
        if not tokens:
            # Input was only punctuation (e.g. "."): nothing to click.
            return None

        # Drop a single leading article.
        if tokens[0] == "the":
            tokens = tokens[1:]
        if not tokens:
            # Input was just "the" / "the." (or "the" + whitespace): nothing to click.
            return None

        # Drop a "number" filler standing in front of an actual number
        # ("click number five" -> name "five"). Guarded by parse_number_word so
        # the word survives when it is part of a real name ("click number pad"
        # -> name "number pad"). Runs before role detection: what follows the
        # filler is a number, never a role keyword.
        if len(tokens) >= 2 and tokens[0] in _NUMBER_FILLERS:
            # wh-overlay-count-homophones: aliases so "click number for"
            # drops the filler and leaves the badge number, the same as
            # "click number four". The guard itself is unchanged, so
            # "click number pad" still keeps both words.
            if parse_number_word(" ".join(tokens[1:]), aliases=True) is not None:
                tokens = tokens[1:]

        role: Optional[str] = None
        name_tokens = list(tokens)

        # Trailing role keyword detection. Only strip the keyword to a role when
        # at least one name word would remain; otherwise the lone keyword is the
        # name itself (documented edge case).
        if name_tokens == ["check", "box"]:
            # Lone "check box" -> the keyword IS the name (no name words remain).
            pass
        elif len(name_tokens) >= 3 and name_tokens[-2:] == ["check", "box"]:
            role = "CheckBox"
            name_tokens = name_tokens[:-2]
        elif len(name_tokens) >= 2 and name_tokens[-1] in _ROLE_KEYWORDS:
            role = _ROLE_KEYWORDS[name_tokens[-1]]
            name_tokens = name_tokens[:-1]

        name = " ".join(name_tokens)
        if not name:
            # Defensive: the guards above keep at least one name token, but
            # never emit an empty-name query.
            return None

        return ElementQuery(
            name=name,
            role=role,
            ordinal=None,
            spatial=None,
            raw_utterance=raw_utterance,
            gesture=gesture,
        )
