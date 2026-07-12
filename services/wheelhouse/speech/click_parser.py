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

Edge cases:

* Empty / whitespace-only input, or input that collapses to an empty name
  (e.g. just "the"), returns None -> caller dictates.
* A role keyword that is the *only* word ("button", "check box") is treated as
  the NAME (role=None), not as a role with an empty name: stripping it would
  leave nothing to search for, and a control literally named "button" is a more
  useful target than an empty query.
"""

from typing import Optional

from ui.element_types import ElementQuery

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


class ClickCommandParser:
    """Stateless parser: text-after-"click" -> Optional[ElementQuery]."""

    @staticmethod
    def parse(target_text: str) -> Optional[ElementQuery]:
        """Parse the spoken target into an ElementQuery.

        Args:
            target_text: the words spoken after "click" (grammar group g1),
                e.g. "the cancel button". Case and surrounding whitespace are
                normalized; the original string is preserved as raw_utterance.

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
        )
