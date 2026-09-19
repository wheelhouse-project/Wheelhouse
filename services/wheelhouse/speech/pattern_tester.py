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
from typing import Any, Dict, List, Optional, Tuple

from .pattern_identity import (
    DOC_ID_KEY,
    ORIGIN_KEY,
    ORIGIN_OWN,
    is_valid_doc_id,
    legacy_candidates,
    runtime_entry_identity,
    runtime_text_candidates,
)
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


def _resolve_draft(draft: Dict[str, Any]):
    """Resolve a draft to ``((regex, actions), None)`` or ``(None, error)``.

    The one call into ``PatternManager._resolve_block_content``, which is
    the same seam ``create_pattern`` and ``update_pattern`` use, so a draft
    can never validate differently from the save it previews. Both the
    compiled preview entry and the ``[[pattern]]`` block the save would
    write are derived from this one answer rather than resolving twice.
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
    except KeyError as exc:
        return None, f"Missing required value: {exc}"
    except ValueError as exc:
        return None, str(exc)
    return (regex, actions), None


def _draft_block(draft: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``[[pattern]]`` block a save of this draft would write.

    Raw, not built: this is what goes back into the user file, so the
    catalog can merge and build it exactly as a save followed by a reload
    would (wh-pattern-override-doc-id.3.5). Returns None when the draft
    does not resolve, which the caller has already reported as a
    ``draft_error``.

    ``requires_hotword`` is carried only when true, matching the block
    writer: a false value is the default and is not written.

    ``origin`` is written unconditionally because this is the block a
    CREATE would write, and ``create_pattern`` marks every block it writes
    as the person's own rule. ``_edited_block`` takes it back out for an
    edit, where ``update_pattern`` preserves the block on disk instead
    (wh-pattern-override-doc-id.3.3).
    """
    resolved, error = _resolve_draft(draft)
    if error is not None:
        return None
    assert resolved is not None
    regex, actions = resolved
    block: Dict[str, Any] = {"pattern": regex, "actions": actions}
    if draft.get("requires_hotword", False):
        block["requires_hotword"] = True
    draft_doc_id = draft.get(DOC_ID_KEY)
    if is_valid_doc_id(draft_doc_id):
        block[DOC_ID_KEY] = draft_doc_id
    block[ORIGIN_KEY] = ORIGIN_OWN
    return block


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
    resolved, error = _resolve_draft(draft)
    if error is not None:
        return None, error
    assert resolved is not None
    regex, actions = resolved
    try:
        # Compile exactly the way PatternCatalog._build_structures does:
        # numeric transform first, then IGNORECASE.
        transformed, meta = transform_pattern(regex)
        compiled = re.compile(transformed, re.IGNORECASE)
    except re.error as exc:
        return None, f"Expression does not compile: {exc}"

    entry: Dict[str, Any] = {
        "compiled_pattern": compiled,
        # Same auto-detection as _build_structures: the ^ anchor decides.
        "pattern_type": "command" if regex.startswith("^") else "replacement",
        "actions": actions,
        "requires_hotword": bool(draft.get("requires_hotword", False)),
        "validation_group": meta.get("validation_group"),
        "is_greedy": meta.get("is_greedy", False),
        "raw_pattern": regex,
        "is_user": True,
    }
    # A Customize draft carries the built-in's doc_id, and the merge keys on
    # it, so the simulation needs it to place the draft where the save will
    # (wh-pattern-override-doc-id A6). Only a well-formed id: a malformed one
    # merges on text, and carrying it would make the entry claim an identity
    # the merge does not honor.
    draft_doc_id = draft.get(DOC_ID_KEY)
    if is_valid_doc_id(draft_doc_id):
        entry[DOC_ID_KEY] = draft_doc_id
    # No ``origin`` here, deliberately. The draft carries no such field,
    # and this entry feeds ``_simulate_merge``, which cannot tell a create
    # from an edit: marking it would agree with a create and disagree with
    # an edit of a customization saved before ids existed. That path is
    # already wrong in the states wh-pattern-override-doc-id.3.5 records
    # and survives only for the tests that pass no catalog; the shipped
    # path is ``_simulate_save``, which models the real block instead.
    return entry, None


def _simulate_merge(
    patterns: List[Dict[str, Any]],
    draft_entry: Dict[str, Any],
    exclude_pattern_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Place the draft in the merged list per PatternCatalog._merge_entries.

    Same identity replaces the existing entry IN PLACE -- which is also how
    an unchanged-trigger edit lands, since the excluded stale entry has the
    draft's own identity. A new identity appends after everything. When the
    excluded entry is removed without a match elsewhere, the draft takes its
    slot: the rewritten user block keeps its file position, so its merged
    position among the appended user entries is unchanged.

    The identity comes from ``speech.pattern_identity``, the rule the real
    merge uses, so the preview cannot answer a different question from the
    save it previews. It used to be a local copy of the normalized-text key,
    which stopped agreeing the moment the merge moved to ``doc_id``: a
    Customize draft for a built-in whose expression a release had rewritten
    was shown losing, then won on save (wh-pattern-override-doc-id A6).
    A draft that identifies as nothing matches nothing here and appends,
    which is what the real merge does with it too.

    A draft carrying no doc_id -- an edit of a rule saved before ids
    existed -- is placed by the merge's legacy rule instead: when exactly
    one entry carries its expression, the save lands in that entry's slot,
    so the preview must too (wh-pattern-override-doc-id A4). Two or more
    is the ambiguous case the merge refuses to resolve, and the draft
    appends here exactly as it will there.

    crewcut: this re-derives the placement from the BUILT list, which has
    lost three facts the merge decides with -- the user file's order, the
    rows the build dropped, and the identity a legacy resolution attached
    to a row carrying no id -- so it is wrong in the states
    wh-pattern-override-doc-id.3.5 records. ``run_test_draft`` uses
    ``_simulate_save`` instead whenever it is given a catalog, which the
    Logic process always is. This path remains for the callers that hand
    in a list of entries they built by hand rather than a catalog: the
    match-semantics tests in tests/test_pattern_tester.py and the
    placement tests in tests/test_pattern_tester_doc_id.py. Remove it by
    rebuilding those tests on a real ``PatternCatalog`` over a temporary
    pair of TOML files, the way tests/test_pattern_tester_save_agreement.py
    does, and then dropping the ``catalog=None`` argument they pass.
    """
    draft_key = runtime_entry_identity(draft_entry)
    target_key = draft_key
    if draft_key is not None and not any(
        runtime_entry_identity(entry) == draft_key for entry in patterns
    ):
        matches = legacy_candidates(
            draft_entry, draft_key, runtime_text_candidates(patterns),
        )
        if len(matches) == 1:
            target_key = matches[0]
    simulated: List[Dict[str, Any]] = []
    placed = False
    excluded_slot: Optional[int] = None

    for entry in patterns:
        if (
            not placed
            and target_key is not None
            and runtime_entry_identity(entry) == target_key
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


def _user_expressions(
    patterns: List[Dict[str, Any]], catalog,
) -> List[str]:
    """The expressions the SAVE's duplicate-trigger check would compare.

    The save reads the user FILE, not a built list:
    ``PatternManager._find_user_trigger_collision`` walks
    ``_load_pattern_dicts(self.user_patterns_file)``, which keeps every
    table whose ``pattern`` value is a string, actions or no actions. The
    build is stricter -- ``PatternCatalog._build_structures`` keeps an
    entry only ``if pattern_str and actions_list`` -- so a user block with
    an empty action list is absent from the built list entirely. Reading
    the built list therefore missed such a block and let the preview
    simulate a save the save refuses (wh-pattern-override-doc-id.3.5).

    With a catalog, this returns the raw user entries filtered exactly as
    ``_load_pattern_dicts`` filters them. Without one, it falls back to the
    built list's user rows, which is all a caller passing None can offer.
    """
    if catalog is not None:
        return [
            entry["pattern"]
            for entry in catalog.get_raw_user_entries()
            if isinstance(entry.get("pattern"), str)
        ]
    return [
        _raw_pattern(entry) for entry in patterns if entry.get("is_user")
    ]


def _simulate_save(
    catalog,
    draft_block: Dict[str, Any],
    exclude_pattern_id: Optional[str],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build the pattern list the save would produce, and find the draft.

    The save rewrites ONE ``[[pattern]]`` block of the user file in place
    (``PatternManager.update_pattern``) or appends one
    (``create_pattern``), and the catalog then merges and builds the whole
    file again. This does the same thing to the catalog's own raw entries
    and asks the catalog to build the result, so the preview answers the
    question the save answers rather than a re-derivation of it
    (wh-pattern-override-doc-id.3.5).

    Args:
        catalog: The live ``PatternCatalog``.
        draft_block: The block a save of this draft would write.
        exclude_pattern_id: The id of the block being edited, or None for
            a create.

    Returns:
        ``(patterns, draft_row)``. ``draft_row`` is the built entry the
        draft became, or None when the build produced no rule for it.
    """
    system_entries = catalog.get_raw_system_entries()
    rebuilt: List[Dict[str, Any]] = []
    replaced = False
    for entry in catalog.get_raw_user_entries():
        expression = entry.get("pattern")
        if (
            not replaced
            and exclude_pattern_id
            and isinstance(expression, str)
            and PatternManager.pattern_id(expression) == exclude_pattern_id
        ):
            block = _edited_block(entry, draft_block)
            saved_id = PatternManager._doc_id_for_save(
                block.get(DOC_ID_KEY), block["pattern"], system_entries,
                expression,
            )
            if saved_id is None:
                block.pop(DOC_ID_KEY, None)
            rebuilt.append(block)
            replaced = True
            continue
        rebuilt.append(entry)
    if not replaced:
        # A create, or an edit of a block that is no longer in the file.
        # ``create_pattern`` appends, and an edit whose target has vanished
        # fails the save outright, so appending is right for the first and
        # harmless for the second.
        block = dict(draft_block)
        if PatternManager._doc_id_for_save(
            block.get(DOC_ID_KEY), block["pattern"], system_entries,
        ) is None:
            block.pop(DOC_ID_KEY, None)
        rebuilt.append(block)

    # crewcut: this call costs about 266 ms on the shipped catalog (320
    # patterns), and it runs on the Logic process asyncio loop, so speech
    # matching stops for that long on each try-it press. Most of it is re
    # compilation: 1438 of 2188 compile calls miss the 512-entry cache,
    # which CPython clears whole when it fills, and both _merge_entries
    # (through slot_identity -> transform_pattern) and _build_structures
    # transform and compile all 320 patterns. It is the catalog's own load
    # work run again, and the SAVE already stops the same loop for 575 ms
    # in reload(), so the preview costs less than half of what pressing
    # Save costs today. Accepted for now (boss ruling 2026-09-06, recorded
    # as overridable by David).
    #
    # The remedy is NOT simply moving this to a thread. reload() swaps
    # _raw_system_entries and _raw_user_entries in two separate statements
    # (pattern_catalog.py:999-1000), run_test_draft reads the user list
    # twice (the duplicate-trigger check, then this function) and
    # build_from_user_entries reads the system list a third time, so a
    # reload landing in between can pair entries from two file states.
    # Removing this limit means one swapped attribute holding both raw
    # lists, a single snapshot read here, and an audit of every self read
    # inside _build_structures. safe_regex is already thread-safe (the
    # RLock at safe_regex.py:41 serializes match_bounded), so that half of
    # the move is free.
    simulated = catalog.build_from_user_entries(rebuilt)

    # The draft's own row, by the expression it saves under. Two user rows
    # cannot share one expression: the duplicate-trigger guard above
    # rejects the draft before this runs. ``is_user`` keeps a built-in
    # carrying the same expression from being mistaken for it.
    #
    # None means the build produced no rule for the draft, which is the
    # honest answer -- the saved rule would not run. Every draft that
    # reaches here has already resolved and compiled, and the block writer
    # cannot produce an empty action list, so the case is not reachable
    # from the editor today; it is handled rather than asserted because
    # the build's rejection rules are not this module's to guarantee.
    expression = draft_block.get("pattern")
    for entry in simulated:
        if entry.get("is_user") and entry.get("raw_pattern") == expression:
            return simulated, entry
    return simulated, None


def _edited_block(
    existing: Dict[str, Any], draft_block: Dict[str, Any],
) -> Dict[str, Any]:
    """The block ``update_pattern`` would write over ``existing``.

    The content comes from the draft and the ``doc_id`` comes from the
    block already in the file: the id names the built-in the RULE replaces,
    not the edit, so ``update_pattern`` carries the stored one forward and
    ignores any id the draft carries. ``_simulate_save`` then applies the
    shared trigger-move decision to that stored identity, just as the writer
    does, before asking the catalog to resolve the resulting block.

    ``origin`` is taken from the stored block for the same reason, and it
    is REMOVED when the stored block has none. An edit of a customization
    saved before ids existed leaves that rule eligible for the legacy
    migration, so a preview that added the key would show the rule losing
    its built-in when the save leaves it attached
    (wh-pattern-override-doc-id.3.3).
    """
    block = dict(draft_block)
    block.pop(DOC_ID_KEY, None)
    stored_doc_id = existing.get(DOC_ID_KEY)
    if is_valid_doc_id(stored_doc_id):
        block[DOC_ID_KEY] = stored_doc_id
    block.pop(ORIGIN_KEY, None)
    if existing.get(ORIGIN_KEY) == ORIGIN_OWN:
        block[ORIGIN_KEY] = ORIGIN_OWN
    return block


def run_test_draft(
    draft: Dict[str, Any],
    text: str,
    patterns: List[Dict[str, Any]],
    matcher,
    *,
    catalog,
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
        catalog: The live ``PatternCatalog``. With it the placement comes
            from the catalog's own merge and build over the raw user file
            with the draft's block written into it, which is the save
            itself rather than a re-derivation of it. See ``_simulate_save``
            and the note on ``_simulate_merge`` about the callers that
            pass None (wh-pattern-override-doc-id.3.5).

            Keyword-only and WITHOUT a default on purpose. A default would
            let a later production caller take the re-derivation path with
            nothing at the call site to show it; an explicit ``None``
            written at every call site is that sign. Only the test files
            pass None today.

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
    if draft_entry is None:
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
    for raw in _user_expressions(patterns, catalog):
        if PatternManager._trigger_key(raw) != draft_key:
            continue
        if exclude_id and PatternManager.pattern_id(raw) == exclude_id:
            continue
        response["draft_error"] = (
            PatternManager._duplicate_trigger_message(raw)
        )
        return response

    draft_block = _draft_block(draft) if catalog is not None else None
    if draft_block is not None:
        simulated, draft_row = _simulate_save(
            catalog, draft_block, exclude_id,
        )
        if draft_row is not None:
            # The rest of this function identifies the draft by object
            # identity, and the catalog built its own entry. Use that one:
            # it is the row the save produces, transforms and all.
            draft_entry = draft_row
    else:
        simulated = _simulate_merge(patterns, draft_entry, exclude_id)

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
