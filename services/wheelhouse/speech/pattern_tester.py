r"""Try-it evaluation for the Pattern Manager editor (wh-pattern-editor-test-messages).

Answers the two Pattern Manager try-it questions -- "what does this text do
right now?" (``pm_test_phrase``) and "what would this draft do?"
(``pm_test_draft``) -- using the SAME in-memory objects the runtime matches
with, so the answer cannot drift from runtime behavior (spec section 7 of
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md).

Inputs are the already-loaded merged pattern list (``TextParser.patterns``,
i.e. ``PatternCatalog.get_all_patterns()`` in merged system+user order) and
the live ``PatternMatcher``. This module performs NO file reads of its own
and needs no LogicController; the thin ``pm_test_phrase`` /
``pm_test_draft`` branches in ``main.py._handle_pattern_manager_action``
unpack the message, call in here, and put the response on the GUI queue.

Matching semantics mirror the runtime [PARSE] path
(``command_engine.TextParser.parse_and_execute``): iterate the merged
pattern list in order, ``PatternMatcher.match_single_pattern`` per entry
(same ``^``-anchor fullmatch-vs-search decision, same STT-punctuation
retries), first match wins. Two deliberate choices, documented because they
are user-facing honesty decisions rather than drift:

* The wake word is assumed spoken (``authorized_command=True``). The try-it
  box wants "which pattern responds" plus a ``requires_hotword`` flag the
  UI renders as "you must say the wake word first" -- not a silent
  no-match for every hotword-gated command.
* A match whose numeric ``validation_group`` fails ``words_to_int`` is
  SKIPPED and the walk continues -- ``PatternMatcher.match_complete``'s
  router behavior. (The [PARSE] loop itself aborts on validation failure,
  but the router's gate runs first at runtime, so "skip and keep looking"
  is what the user actually experiences: "delete xyz" dictates, it does
  not delete.)

Identity fields (``pattern_id`` / ``trigger_display`` / ``is_user_created``)
prefer an entry's ``raw_pattern`` / ``is_user`` keys when present. Today's
``PatternCatalog.get_all_patterns()`` entries do not carry them, so the
module falls back to the compiled pattern string -- identical to the raw
file string for every pattern except the shipped ``(\d+)`` patterns, whose
compiled form is the transformed ``(\w+)`` (speech/pattern_transform.py) --
and to ``is_user_created=False``. Enriching the catalog entries with those
two keys makes this module exact with no further changes here.

Draft evaluation builds the draft through the same paths ``create_pattern``
uses (``PatternManager._resolve_regex_and_phrases`` for trigger/phrases,
``PatternManager.generate_actions`` for the step list) and compiles it the
way ``PatternCatalog._build_structures`` would (``transform_pattern`` +
IGNORECASE), then simulates the catalog merge per ``_merge_entries``' real
rules: a draft whose trigger key (strip+casefold of the raw expression)
matches an existing entry REPLACES that entry in place; a new key APPENDS
after everything. ``exclude_pattern_id`` removes the pattern being edited
from the simulation so it cannot shadow its own replacement; when the
edited entry keeps no key match elsewhere, the draft takes its slot.

Known approximation, accepted and documented: when the excluded pattern was
a user override of a built-in, the shadowed built-in's entry no longer
exists in memory (the merge replaced it), so a simulation of "edit the
override to a different trigger" cannot show the built-in resurfacing. The
in-memory list simply has no record of the replaced entry.

Bounded matching (wh-pattern-editor-r0.4): every draft match and every
saved-pattern match executes in the safe_regex worker process instead of
inline ``re`` -- ``pm_test_draft`` fires on a debounce while the user is
still typing, and a catastrophic-backtracking draft matched inline would
freeze the Logic asyncio loop with no hands-free recovery. The matching
SEMANTICS still mirror ``PatternMatcher.match_single_pattern`` (anchor-
driven fullmatch-vs-search, the same STT-punctuation retry candidates); a
draft timeout becomes the ``draft_error`` string, and a saved-pattern
timeout aborts the whole test with a failure naming the pattern.
"""
import re
from typing import Any, Dict, List, Optional

from .pattern_manager import PatternManager
from .pattern_matcher import _MATCHER_PUNCT_STRIP, _normalize_first_word_in_text
from .pattern_transform import transform_pattern
from .safe_regex import RegexTimeout, match_bounded

# Pinned user-facing text for a draft that exceeds the match budget
# (wh-pattern-editor-r0.4).
_DRAFT_TIMEOUT_ERROR = (
    "This pattern takes too long to match. It could freeze Wheelhouse. "
    "Simplify the expression (avoid nested repeats like (\\w+\\s*)+)."
)


def _raw_pattern(entry: Dict[str, Any]) -> str:
    """Return the entry's raw pattern string, or the compiled string.

    ``raw_pattern`` is the module's identity seam (see module docstring);
    the compiled fallback is exact for every pattern the numeric transform
    left untouched.
    """
    raw = entry.get("raw_pattern")
    if isinstance(raw, str) and raw:
        return raw
    return entry["compiled_pattern"].pattern


def _entry_identity(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Identity block shared by pm_test_phrase's match and pm_test_draft's
    shadowed_by: the same id scheme and trigger display the manager tree
    uses (PatternManager), so the window can select the row."""
    raw = _raw_pattern(entry)
    return {
        "pattern_id": PatternManager.pattern_id(raw),
        "trigger_display": PatternManager._trigger_display(raw),
        "is_user_created": bool(entry.get("is_user", False)),
    }


class _BoundedMatch:
    """Duck-typed stand-in for the ``re.Match`` surface this module uses.

    ``match_bounded`` runs the real match in the safe_regex worker and
    returns picklable groups instead of a Match object; ``_resolve_steps``
    and ``PatternMatcher.validate_numeric`` only call ``.groups()`` and
    ``.group(n)`` (n >= 1, length-guarded), so this shim carries the
    captures across the process boundary.
    """

    def __init__(self, groups):
        self._groups = tuple(groups)

    def groups(self):
        return self._groups

    def group(self, index):
        return self._groups[index - 1]


def _command_match_candidates(text: str) -> List[str]:
    """The candidate texts the runtime's punct-retry would fullmatch, in order.

    Mirrors ``match_single_pattern`` for '^'-anchored patterns: first-word
    boundary normalization, then the original text, the trailing-punctuation
    rstrip, and the interior-word strip -- the same stages as
    ``_match_command_with_punct_retry`` (wh-9f51), regenerated here because
    the bounded matcher needs candidate TEXTS to ship to the worker rather
    than in-process Match objects.
    """
    normalized_input = _normalize_first_word_in_text(text)
    candidates = [normalized_input]
    stripped = normalized_input.rstrip(_MATCHER_PUNCT_STRIP)
    if stripped != normalized_input:
        candidates.append(stripped)
    words = stripped.split(" ")
    if len(words) > 1:
        stripped_words = [w.strip(_MATCHER_PUNCT_STRIP) for w in words]
        if "" not in stripped_words:
            joined = " ".join(stripped_words)
            if joined != stripped:
                candidates.append(joined)
    return candidates


def _match_entry(text: str, entry: Dict[str, Any], matcher):
    """Match one catalog entry the way the runtime would, or return None.

    Mirrors ``match_single_pattern``'s semantics -- anchor-driven
    fullmatch-vs-search, the STT-punctuation retries, hotword assumed
    spoken (see module docstring) -- but every regex executes in the
    safe_regex worker so a catastrophic-backtracking pattern raises
    RegexTimeout instead of freezing the Logic asyncio loop
    (wh-pattern-editor-r0.4). The numeric validation skip mirrors
    ``match_complete``'s router gate.

    Raises:
        RegexTimeout: The entry's pattern exceeded the match budget.
    """
    compiled = entry["compiled_pattern"]
    found = None
    if compiled.pattern.startswith("^"):
        for candidate in _command_match_candidates(text):
            found = match_bounded(
                compiled.pattern, candidate,
                flags=compiled.flags, mode="fullmatch",
            )
            if found is not None:
                break
    else:
        found = match_bounded(compiled.pattern, text, flags=compiled.flags)
    if found is None:
        return None
    match = _BoundedMatch(found["groups"])
    if not matcher.validate_numeric(match, entry.get("validation_group")):
        return None
    return match


def _saved_pattern_timeout_error(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Abort envelope for a saved pattern that exceeded the match budget.

    First timeout aborts the whole test -- continuing would just queue more
    runaway matches behind a pattern that already proved pathological. The
    offending pattern is named by its trigger display (an expression-only
    pattern falls back to quoting the raw expression).
    """
    name = PatternManager._trigger_display(_raw_pattern(entry))
    return {
        "success": False,
        "error": (
            f"Test aborted: the saved pattern '{name}' takes too long to "
            f"match and could freeze Wheelhouse. Edit or delete that "
            f"pattern."
        ),
    }


def _resolve_steps(
    actions: List[Dict[str, Any]], match,
) -> List[Dict[str, Any]]:
    """Resolve gN references in action params from the captured groups.

    ``match`` is anything with ``.groups()`` -- an ``re.Match`` or the
    ``_BoundedMatch`` shim.

    Faithful mirror of ``TextParser._execute_rule``'s static parameter
    resolution: a whole-param ``"gN"`` becomes the captured string (None
    when the group did not participate, exactly what the runtime passes),
    and embedded markers like ``"(g1)"`` are string-replaced only for
    groups that captured. Execution-time context chaining (a function's
    string return feeding a later step) cannot happen without running the
    functions, so those params are shown unresolved.
    """
    context: Dict[str, Any] = {f"g{i}": None for i in range(1, 10)}
    context.update({
        f"g{i + 1}": group
        for i, group in enumerate(match.groups())
        if group is not None
    })

    steps: List[Dict[str, Any]] = []
    for step in actions:
        params = step.get("params", [])
        resolved: List[Any] = []
        for p in params:
            if isinstance(p, str):
                if p in context:
                    resolved.append(context[p])
                else:
                    result = p
                    for key, value in context.items():
                        if value is not None and key in result:
                            result = result.replace(key, value)
                    resolved.append(result)
            else:
                resolved.append(p)
        steps.append({"function": step.get("function"), "params": resolved})
    return steps


def run_test_phrase(
    text: str, patterns: List[Dict[str, Any]], matcher,
) -> Dict[str, Any]:
    """Answer pm_test_phrase: which pattern responds to ``text`` right now.

    Args:
        text: What the user typed into the try-it box.
        patterns: The live merged pattern list (``TextParser.patterns``).
        matcher: The live ``PatternMatcher``.

    Returns:
        ``{"success": True, "match": None}`` when nothing responds, else
        ``{"success": True, "match": {pattern_id, trigger_display,
        requires_hotword, groups, resolved_steps, is_user_created}}`` for
        the first pattern that does (merged-catalog order, exactly the
        [PARSE] walk). A saved pattern that exceeds the match budget aborts
        the test with ``{"success": False, "error": ...}`` naming it
        (wh-pattern-editor-r0.4).
    """
    for entry in patterns:
        try:
            result = _match_entry(text, entry, matcher)
        except RegexTimeout:
            return _saved_pattern_timeout_error(entry)
        if result is None:
            continue
        match_info = _entry_identity(entry)
        match_info["requires_hotword"] = bool(
            entry.get("requires_hotword", False),
        )
        match_info["groups"] = list(result.groups())
        match_info["resolved_steps"] = _resolve_steps(
            entry.get("actions", []), result,
        )
        return {"success": True, "match": match_info}
    return {"success": True, "match": None}


def _build_draft_entry(draft: Dict[str, Any]):
    """Build a catalog-shaped entry for the draft, via create_pattern's paths.

    The draft is create-shaped: simple drafts carry ``trigger`` or
    ``phrases`` plus ``action_type``/``action_params``; advanced drafts
    carry a raw ``expression`` and/or raw ``actions`` steps
    (wh-pattern-editor-advanced). Resolution goes through the SAME
    ``PatternManager._resolve_block_content`` seam the save paths use, so
    the try-it answer can never validate differently from the save it
    previews (including the type-vs-anchor honesty check on raw
    expressions).

    Returns ``(entry, None)`` on success or ``(None, error_string)`` when
    the draft does not validate/compile -- the error string is
    user-readable and becomes ``draft_error`` (NOT a handler failure).
    """
    try:
        regex, _stored_phrases, actions, _explicit_type = (
            PatternManager._resolve_block_content(
                draft.get("trigger"),
                draft.get("pattern_type", "command"),
                draft.get("action_type"),
                draft.get("action_params"),
                draft.get("phrases"),
                draft.get("expression"),
                draft.get("actions"),
            )
        )
        # Compile exactly the way PatternCatalog._build_structures does:
        # numeric transform first, then IGNORECASE.
        transformed, meta = transform_pattern(regex)
        compiled = re.compile(transformed, re.IGNORECASE)
    except re.error as exc:
        return None, f"Expression does not compile: {exc}"
    except KeyError as exc:
        return None, f"Missing required value: {exc}"
    except ValueError as exc:
        return None, str(exc)

    return {
        "compiled_pattern": compiled,
        # Same auto-detection as _build_structures: the ^ anchor decides.
        "pattern_type": "command" if regex.startswith("^") else "replacement",
        "actions": actions,
        "requires_hotword": bool(draft.get("requires_hotword", False)),
        "validation_group": meta.get("validation_group"),
        "is_greedy": meta.get("is_greedy", False),
        "raw_pattern": regex,
        "is_user": True,
    }, None


def _simulate_merge(
    patterns: List[Dict[str, Any]],
    draft_entry: Dict[str, Any],
    exclude_pattern_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Place the draft in the merged list per PatternCatalog._merge_entries.

    Same trigger key (strip+casefold of the raw expression) replaces the
    existing entry IN PLACE -- which is also how an unchanged-trigger edit
    lands, since the excluded stale entry has the draft's own key. A new
    key appends after everything. When the excluded entry is removed
    without a key match elsewhere, the draft takes its slot: the rewritten
    user block keeps its file position, so its merged position among the
    appended user entries is unchanged.
    """
    draft_key = PatternManager._trigger_key(draft_entry["raw_pattern"])
    simulated: List[Dict[str, Any]] = []
    placed = False
    excluded_slot: Optional[int] = None

    for entry in patterns:
        if (
            not placed
            and PatternManager._trigger_key(_raw_pattern(entry)) == draft_key
        ):
            simulated.append(draft_entry)
            placed = True
            continue
        if (
            exclude_pattern_id
            and PatternManager.pattern_id(_raw_pattern(entry))
            == exclude_pattern_id
        ):
            excluded_slot = len(simulated)
            continue
        simulated.append(entry)

    if not placed:
        if excluded_slot is not None:
            simulated.insert(excluded_slot, draft_entry)
        else:
            simulated.append(draft_entry)
    return simulated


def run_test_draft(
    draft: Dict[str, Any],
    text: str,
    patterns: List[Dict[str, Any]],
    matcher,
) -> Dict[str, Any]:
    """Answer pm_test_draft: for ``text``, does the draft respond first?

    Args:
        draft: Create-shaped fields (``trigger`` OR ``phrases``,
            ``pattern_type``, ``action_type``, ``action_params``, optional
            ``requires_hotword``) plus optional ``exclude_pattern_id`` --
            the id of the pattern being edited, so it does not shadow
            itself.
        text: What the user typed into the try-it line.
        patterns: The live merged pattern list (``TextParser.patterns``).
        matcher: The live ``PatternMatcher``.

    Returns:
        ``{success, draft_error, draft_matches, winner, shadowed_by,
        groups, resolved_steps}``. A draft that fails validation or
        compilation returns ``success=True`` with a user-readable
        ``draft_error`` (the dialog shows it under the field); a draft
        that exceeds the match budget gets the pinned timeout
        ``draft_error``, while a SAVED pattern exceeding it aborts the
        test with ``{"success": False, "error": ...}`` naming it
        (wh-pattern-editor-r0.4). ``winner`` is ``'draft'`` /
        ``'existing'`` / ``'none'``; ``shadowed_by`` identifies the
        earlier pattern when the draft loses; ``groups`` and
        ``resolved_steps`` are reported for the draft when it wins.
    """
    response: Dict[str, Any] = {
        "success": True,
        "draft_error": None,
        "draft_matches": False,
        "winner": "none",
        "shadowed_by": None,
        "groups": [],
        "resolved_steps": [],
    }

    draft_entry, error = _build_draft_entry(draft)
    if error is not None:
        response["draft_error"] = error
        return response

    # A draft whose trigger key collides with a DIFFERENT existing USER
    # pattern would be rejected by the save (wh-pattern-editor-r0.1), so
    # the preview reports that rejection -- in the save's exact words --
    # instead of simulating a merge the save will never perform
    # (wh-pattern-editor-r2.1). Built-in (non-user) matches stay a draft
    # win: overriding a built-in is the Customize flow working as designed.
    draft_key = PatternManager._trigger_key(draft_entry["raw_pattern"])
    exclude_id = draft.get("exclude_pattern_id")
    for entry in patterns:
        if not entry.get("is_user"):
            continue
        raw = _raw_pattern(entry)
        if PatternManager._trigger_key(raw) != draft_key:
            continue
        if exclude_id and PatternManager.pattern_id(raw) == exclude_id:
            continue
        response["draft_error"] = (
            PatternManager._duplicate_trigger_message(raw)
        )
        return response

    simulated = _simulate_merge(
        patterns, draft_entry, draft.get("exclude_pattern_id"),
    )

    for entry in simulated:
        try:
            result = _match_entry(text, entry, matcher)
        except RegexTimeout:
            if entry is draft_entry:
                return _draft_timeout_response(response)
            return _saved_pattern_timeout_error(entry)
        if result is None:
            continue
        if entry is draft_entry:
            response["draft_matches"] = True
            response["winner"] = "draft"
            response["groups"] = list(result.groups())
            response["resolved_steps"] = _resolve_steps(
                draft_entry["actions"], result,
            )
        else:
            response["winner"] = "existing"
            response["shadowed_by"] = _entry_identity(entry)
            try:
                response["draft_matches"] = (
                    _match_entry(text, draft_entry, matcher) is not None
                )
            except RegexTimeout:
                return _draft_timeout_response(response)
        return response

    return response


def _draft_timeout_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Reset the response to the pinned draft-timeout answer.

    A pathological draft is reported like any other invalid draft --
    ``success=True`` with a ``draft_error`` -- and any partial winner /
    shadow info is cleared: the user must fix the draft before order
    questions mean anything.
    """
    response["draft_error"] = _DRAFT_TIMEOUT_ERROR
    response["draft_matches"] = False
    response["winner"] = "none"
    response["shadowed_by"] = None
    response["groups"] = []
    response["resolved_steps"] = []
    return response
