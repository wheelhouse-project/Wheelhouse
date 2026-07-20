"""Deterministic pattern -> plain-English explainer.

``explain_pattern`` turns one pattern dict -- the same per-pattern shape the
Pattern Manager window receives from ``pm_get_patterns`` (``raw_pattern``,
``requires_hotword``, ``raw_actions`` as raw TOML action dicts, plus optional
``phrases`` list, optional ``type`` of ``command``/``replacement``, optional
``position`` of ``trailing``) -- into a multi-line English description
(wh-pattern-editor-explainer; spec:
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 10).

Output grammar (pinned by tests/test_pattern_explainer.py):

1. Wake-word line, only when ``requires_hotword``:
   ``You must say '<hotword>' first.``
2. Trigger line. Commands (``^``-anchored: the pipeline fullmatches them
   against the whole utterance, speech/pattern_matcher.py) read
   ``Say 'save'.``; replacements (unanchored: matched with search anywhere
   in dictated text) read ``When you say 'period' anywhere while
   dictating...``; trailing-position commands read ``Say 'submit' as the
   last word of what you say...``. A phrase list renders directly as
   ``Say 'editor' (or 'code editor', 'vs code').``.
3. One sentence per action step from the function catalog
   (speech/action_catalog.py labels with the step's actual parameter
   values resolved in), numbered when there is more than one step.
   Internal-audience housekeeping steps fold to a short fixed sentence.

Translation honesty: the trigger translator only claims to understand a
small regex subset (anchors, ``\\b``, literal words, optional letters and
spaces ``x?``, literal alternation groups including one level of nested
optional group, ``(.+)``/``(.*)`` as "any words", ``(\\d+)`` as "a number",
``\\s+``/``\\s*``, a bare trailing ``.*``/``.+``). ANY other construct makes
the whole trigger fall back to quoting the raw expression -- never a wrong
translation.

Hard constraint: dependency-free. The GUI process imports this module, so
it may import only the stdlib and speech.action_catalog (enforced by a
bare-subprocess test, same style as test_action_catalog.py).
"""

import re

from .action_catalog import CATALOG_BY_NAME

# A cross-product of literal alternatives larger than this is unreadable in
# a sentence; fall back to the raw expression instead of listing them all.
_MAX_VARIANTS = 6

_PURE_GROUP_RE = re.compile(r"g\d+")
_EMBEDDED_GROUP_RE = re.compile(r"\bg\d+\b")

# Trigger-side fallback wording. tests/test_pattern_explainer.py keys the
# shipped-pattern allowlist off this exact phrase.
_FALLBACK_TEMPLATE = 'something matching the expression "{raw}"'

# Internal-audience steps (audience == "internal" in the catalog) are
# housekeeping the user never picked; describe them with one short fixed
# sentence instead of label + params. set_speech_interaction_mode is
# deliberately NOT here: it is the sole visible effect of the speech-mode
# patterns, so it keeps the normal label + params rendering.
_INTERNAL_BRIEF = {
    "skip_clipboard_restore": (
        "Keeps the copied text on the clipboard afterward."
    ),
    "capture_clipboard": "Saves the clipboard text for a later step.",
    "add_hint_to_stt": (
        "Sends the copied text to the speech engine as a vocabulary hint."
    ),
}


class _UnsupportedConstruct(Exception):
    """Raised when the trigger translator meets regex it cannot honestly
    render; the caller falls back to quoting the raw expression."""


# ---------------------------------------------------------------------------
# Trigger-side regex translation
# ---------------------------------------------------------------------------


def _find_group_end(raw, start):
    """Index of the ``)`` matching the ``(`` at ``start``, or raise."""
    depth = 0
    i = start
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _UnsupportedConstruct(raw)


def _split_alternation(body):
    """Split group content on top-level ``|`` (respecting nesting/escapes)."""
    branches = []
    depth = 0
    current = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            current.append(body[i:i + 2])
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            branches.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    branches.append("".join(current))
    return branches


def _parse_trigger(raw):
    """Translate a trigger regex into ``(variants, suffixes)``.

    ``variants`` is an ordered list of literal spoken phrases (the
    cross-product of optional letters/spaces and alternation groups,
    include-the-optional-part first). ``suffixes`` is an ordered list of
    ``(kind, optional)`` with kind ``"words"`` (from ``(.+)``/``(.*)``) or
    ``"number"`` (from ``(\\d+)``), describing what may follow the phrase.
    Raises _UnsupportedConstruct on anything outside the supported subset.
    """
    variants = [""]
    suffixes = []
    i = 0
    n = len(raw)

    def _append_alternatives(alts):
        nonlocal variants
        if suffixes:
            # Literal content after a capture would need "then say X" -- not
            # supported; be honest and fall back.
            raise _UnsupportedConstruct(raw)
        merged = [v + a for v in variants for a in alts]
        if len(merged) > _MAX_VARIANTS:
            raise _UnsupportedConstruct(raw)
        variants = merged

    while i < n:
        ch = raw[i]
        if ch == "^":
            if i != 0:
                raise _UnsupportedConstruct(raw)
            i += 1
        elif ch == "$":
            if i != n - 1:
                raise _UnsupportedConstruct(raw)
            i += 1
        elif ch == "\\":
            if i + 1 >= n:
                raise _UnsupportedConstruct(raw)
            nxt = raw[i + 1]
            if nxt == "b":
                i += 2  # word boundary: no spoken content
            elif nxt == "s":
                quant = raw[i + 2] if i + 2 < n and raw[i + 2] in "+*?" else ""
                if quant in ("*", "?"):
                    _append_alternatives([" ", ""])
                else:
                    _append_alternatives([" "])
                i += 2 + (1 if quant else 0)
            elif nxt == "d":
                quant = raw[i + 2] if i + 2 < n and raw[i + 2] in "+*?" else ""
                if quant != "+":
                    raise _UnsupportedConstruct(raw)
                suffixes.append(("number", False))
                i += 3
            elif nxt.isalnum():
                # \w, \S, \n, backreferences, ... -- not translatable.
                raise _UnsupportedConstruct(raw)
            else:
                # Escaped literal, e.g. the "\ " re.escape produces for
                # phrase-list expressions.
                optional = i + 2 < n and raw[i + 2] == "?"
                _append_alternatives([nxt, ""] if optional else [nxt])
                i += 2 + (1 if optional else 0)
        elif ch == "(":
            end = _find_group_end(raw, i)
            content = raw[i + 1:end]
            optional_after = end + 1 < n and raw[end + 1] == "?"
            next_i = end + 1 + (1 if optional_after else 0)
            if content.startswith("?:"):
                body = content[2:]
            elif content.startswith("?"):
                # Lookarounds, named groups, inline flags: not translatable.
                raise _UnsupportedConstruct(raw)
            else:
                body = content
            if body in (".+", ".*"):
                suffixes.append(("words", body == ".*" or optional_after))
            elif body == r"\d+":
                suffixes.append(("number", optional_after))
            elif body == r"\d*":
                suffixes.append(("number", True))
            else:
                alts = []
                for branch in _split_alternation(body):
                    branch_variants, branch_suffixes = _parse_trigger(branch)
                    if branch_suffixes:
                        raise _UnsupportedConstruct(raw)
                    alts.extend(branch_variants)
                if optional_after:
                    alts.append("")
                _append_alternatives(alts)
            i = next_i
        elif ch == ".":
            quant = raw[i + 1] if i + 1 < n else ""
            if quant in ("+", "*"):
                lazy = i + 2 < n and raw[i + 2] == "?"
                suffixes.append(("words", quant == "*"))
                i += 2 + (1 if lazy else 0)
            else:
                raise _UnsupportedConstruct(raw)
        elif ch in "|[]{}*+?":
            raise _UnsupportedConstruct(raw)
        else:
            optional = i + 1 < n and raw[i + 1] == "?"
            _append_alternatives([ch, ""] if optional else [ch])
            i += 1 + (1 if optional else 0)

    cleaned = []
    seen = set()
    for variant in variants:
        collapsed = " ".join(variant.split())
        if collapsed and collapsed not in seen:
            seen.add(collapsed)
            cleaned.append(collapsed)
    if not cleaned:
        # Nothing speakable (pure captures/whitespace): fall back.
        raise _UnsupportedConstruct(raw)
    return cleaned, suffixes


def _phrase_display(variants):
    quoted = [f"'{v}'" for v in variants]
    if len(quoted) == 1:
        return quoted[0]
    return f"{quoted[0]} (or {', '.join(quoted[1:])})"


def _suffix_clauses(suffixes):
    parts = []
    for kind, optional in suffixes:
        noun = "any words" if kind == "words" else "a number"
        if optional:
            parts.append(f", optionally followed by {noun}")
        else:
            parts.append(f" followed by {noun}")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Action-side step sentences
# ---------------------------------------------------------------------------


def _is_repeat_count(value):
    """True when a trailing hk param is the peeled-off repeat count."""
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and (
        value.isdigit() or _PURE_GROUP_RE.fullmatch(value) is not None
    )


def _render_value(value, kind, func):
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return repr(value)
    if func == "gs" and value == "capture_clipboard":
        # gs's magic param: the name of the earlier capture_clipboard step
        # whose stored result becomes the query (see the catalog entry).
        return "the saved clipboard text"
    if _PURE_GROUP_RE.fullmatch(value):
        noun = "the number you say" if kind == "number" else "the words you say"
        return f"{noun} ({value})"
    embedded = _EMBEDDED_GROUP_RE.search(value)
    if embedded:
        return f"'{value}' ({embedded.group(0)} = the words you say)"
    return f"'{value}'"


def _bind_params(entry, func, params):
    """Pair catalog param definitions with actual values -> [(name, text)]."""
    if func == "hk":
        # hk takes a flat variadic key list; a trailing number (or numeric
        # capture group) is peeled off as the repeat count.
        keys = list(params)
        repeat = None
        if keys and _is_repeat_count(keys[-1]):
            repeat = keys.pop()
        bound = []
        if keys:
            bound.append(("keys", ", ".join(str(k) for k in keys)))
        if repeat is not None:
            bound.append(("repeat", _render_value(repeat, "number", func)))
        return bound
    definitions = list(entry["params"])
    bound = [
        (definition["name"], _render_value(value, definition["kind"], func))
        for definition, value in zip(definitions, params)
    ]
    for extra in params[len(definitions):]:
        bound.append((None, _render_value(extra, "text", func)))
    return bound


def _describe_text_step(params):
    """Special sentence for the ``text`` replacement action with a literal
    template, matching the spec's "WheelHouse types Y instead" reading.
    Returns None when the generic label + params rendering should apply."""
    if (
        isinstance(params, list)
        and len(params) == 1
        and isinstance(params[0], str)
        and not _EMBEDDED_GROUP_RE.search(params[0])
    ):
        if params[0] == "":
            return "Types nothing (the matched words are discarded)."
        return f"Types '{params[0]}' instead of the matched words."
    return None


def _describe_step(action):
    """One English sentence for a single raw action dict. Never raises."""
    func = action.get("function", "")
    params = action.get("params", [])
    if not isinstance(params, list):
        params = []
    if not isinstance(func, str) or not func:
        return "Does nothing (empty step)."
    entry = CATALOG_BY_NAME.get(func)
    if entry is None:
        # Spec section 14: a missing catalog entry degrades to the bare
        # function name, never a crash.
        joined = ", ".join(str(p) for p in params)
        return f"Runs {func}({joined})."
    if func == "text":
        special = _describe_text_step(params)
        if special is not None:
            return special
    if func in _INTERNAL_BRIEF:
        return _INTERNAL_BRIEF[func]
    bound = _bind_params(entry, func, params)
    label = entry["label"]
    if not bound:
        return f"{label}."
    if len(bound) == 1:
        return f"{label} ({bound[0][1]})."
    rendered = "; ".join(
        f"{name}: {value}" if name else value for name, value in bound
    )
    return f"{label} ({rendered})."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pattern_kind(pattern):
    """Classify an entry as ``trailing`` / ``command`` / ``replacement``.

    The single classification seam shared by this explainer and the
    manager window's Type badge (wh-pattern-editor-r4.2), so the two can
    never disagree. Trailing position wins (the catalog loader
    special-cases it before the anchor check); then an explicit type from
    the editor; then the pipeline's own rule: ^-anchored -> command
    (fullmatch), unanchored -> replacement (search, matches
    mid-dictation). speech/pattern_catalog.py.
    """
    if not isinstance(pattern, dict):
        return "command"
    if pattern.get("position") == "trailing":
        return "trailing"
    if pattern.get("type") in ("command", "replacement"):
        return pattern["type"]
    raw = pattern.get("raw_pattern")
    if isinstance(raw, str) and raw.startswith("^"):
        return "command"
    return "replacement"


def explain_pattern(pattern, hotword):
    """Return a deterministic plain-English description of ``pattern``.

    Pure function: no I/O, no state. ``pattern`` is one entry in the
    pm_get_patterns shape (see module docstring); ``hotword`` is the
    effective wake word to name in the "You must say ... first." sentence.
    Never raises on malformed input -- unknown constructs degrade to
    quoting the raw material.
    """
    if not isinstance(pattern, dict):
        return "This pattern could not be read."

    raw = pattern.get("raw_pattern")
    if not isinstance(raw, str):
        raw = ""

    # --- Trigger side -----------------------------------------------------
    variants = None
    suffixes = []
    phrases = pattern.get("phrases")
    if isinstance(phrases, list):
        cleaned = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
        if cleaned:
            variants = cleaned
    if variants is None:
        try:
            variants, suffixes = _parse_trigger(raw)
        except _UnsupportedConstruct:
            variants = None
            suffixes = []
    if variants is None:
        display = _FALLBACK_TEMPLATE.format(raw=raw)
        clauses = ""
    else:
        display = _phrase_display(variants)
        clauses = _suffix_clauses(suffixes)

    # --- Pattern kind -----------------------------------------------------
    kind = pattern_kind(pattern)

    raw_actions = pattern.get("raw_actions")
    if isinstance(raw_actions, list):
        steps = [a for a in raw_actions if isinstance(a, dict)]
    else:
        steps = []

    lines = []
    if pattern.get("requires_hotword"):
        wake = hotword.strip() if isinstance(hotword, str) else ""
        if wake:
            lines.append(f"You must say '{wake}' first.")
        else:
            lines.append("You must say the wake word first.")

    folded = False
    if kind == "trailing":
        lines.append(
            f"Say {display}{clauses} as the last word of what you say; "
            "the words you said before it are typed as dictation."
        )
    elif kind == "command":
        lines.append(f"Say {display}{clauses}.")
    else:
        stem = f"When you say {display}{clauses}"
        if clauses:
            stem += ","
        stem += " anywhere while dictating"
        if len(steps) == 1 and steps[0].get("function") == "text":
            special = _describe_text_step(steps[0].get("params"))
            if special == "Types nothing (the matched words are discarded).":
                lines.append(f"{stem}, Wheelhouse discards it (types nothing).")
                folded = True
            elif special is not None:
                text_value = steps[0]["params"][0]
                lines.append(
                    f"{stem}, Wheelhouse types '{text_value}' instead."
                )
                folded = True
        if not folded:
            lines.append(f"{stem}:")

    # --- Action side --------------------------------------------------------
    if not folded:
        if not steps:
            lines.append("This pattern has no action steps.")
        else:
            described = [_describe_step(a) for a in steps]
            if len(described) == 1:
                lines.append(described[0])
            else:
                lines.extend(
                    f"{index}. {sentence}"
                    for index, sentence in enumerate(described, 1)
                )

    return "\n".join(lines)
