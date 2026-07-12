"""Unit tests for the Chromium browser DOM folding rules (wh-24e4w).

Covers the three folding rules from the v5 design doc
(docs/plans/2026-05-21-voice-element-clicking-design-v5.md, section
"Browser DOM corrections") plus the effective-browser-process-list helper.

The module under test is pure: it operates on a flat ``list[ElementMatch]``
(the shape ``walk_window``'s ``browser_correction_hook`` receives) and uses
bounds containment to recover the parent/child relationship the rules are
phrased in. No comtypes, no live COM, no real Chromium app -- every fixture
here is synthetic in-memory data.
"""

from ui.browser_dom_corrections import (
    DEFAULT_BROWSER_PROCESSES,
    apply_dom_corrections,
    effective_browser_processes,
)
from ui.element_types import ElementMatch


# Numeric UIA control-type ids the locale-invariant predicates compare against.
# Mirrors the constants exported by ui.uia_walker; duplicated here so the test
# fixtures read clearly without an import dependency on the walker's full
# constant block.
UIA_GROUP_CTID = 50026
UIA_TEXT_CTID = 50020
UIA_HYPERLINK_CTID = 50005
UIA_LISTITEM_CTID = 50007


# Default control_type_id per English role string, so the bulk of the existing
# tests (which pass canonical English role strings) keep folding via the id
# path without every call site spelling out the id. Roles not in this map --
# notably "heading", which has no UIA control-type id -- default to 0.
_ROLE_TO_CTID = {
    "group": UIA_GROUP_CTID,
    "groupcontrol": UIA_GROUP_CTID,
    "text": UIA_TEXT_CTID,
    "textcontrol": UIA_TEXT_CTID,
    "hyperlink": UIA_HYPERLINK_CTID,
    "link": UIA_HYPERLINK_CTID,
    "list item": UIA_LISTITEM_CTID,
    "listitem": UIA_LISTITEM_CTID,
}


def _match(
    item_id: str,
    name: str,
    role: str,
    bounds: tuple[int, int, int, int],
    *,
    display_number: int = 0,
    control_type_id: int | None = None,
) -> ElementMatch:
    """Build a synthetic ElementMatch with only the fields the rules read.

    ``control_type_id`` defaults to the numeric UIA id implied by the English
    ``role`` string (via ``_ROLE_TO_CTID``); pass it explicitly to model a
    non-English locale (role string in another language) or a wrong/zero id.
    """
    if control_type_id is None:
        control_type_id = _ROLE_TO_CTID.get(role.strip().casefold(), 0)
    return ElementMatch(
        item_id=item_id,
        display_number=display_number,
        name=name,
        role=role,
        bounds=bounds,
        monitor_id=0,
        score=0.0,
        is_eligible=False,
        source="uia",
        invoke_supported=False,
        is_enabled=True,
        control_ref=None,
        control_type_id=control_type_id,
    )


def _ids(matches: list[ElementMatch]) -> set[str]:
    return {m.item_id for m in matches}


# ---------------------------------------------------------------------------
# Rule 1: GroupControl whose only useful descendant is one TextControl with the
# same name -> drop the GroupControl, keep the inner TextControl.
# ---------------------------------------------------------------------------

def test_rule1_group_with_single_same_name_text_child_folds():
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40))
    text = _match("t1", "Inbox", "text", (5, 5, 80, 20))
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"t1"}


def test_rule1_uia_control_type_names_also_fold():
    group = _match("g1", "Inbox", "GroupControl", (0, 0, 100, 40))
    text = _match("t1", "Inbox", "TextControl", (5, 5, 80, 20))
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"t1"}


def test_rule1_no_fold_when_names_differ():
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40))
    text = _match("t1", "Drafts", "text", (5, 5, 80, 20))
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"g1", "t1"}


def test_rule1_no_fold_when_group_has_two_useful_children():
    group = _match("g1", "Inbox", "group", (0, 0, 200, 40))
    text_a = _match("t1", "Inbox", "text", (5, 5, 80, 20))
    text_b = _match("t2", "Inbox", "text", (100, 5, 80, 20))
    result = apply_dom_corrections([group, text_a, text_b])
    assert _ids(result) == {"g1", "t1", "t2"}


def test_rule1_no_fold_when_descendant_is_not_text():
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40))
    button = _match("b1", "Inbox", "button", (5, 5, 80, 20))
    result = apply_dom_corrections([group, button])
    assert _ids(result) == {"g1", "b1"}


# ---------------------------------------------------------------------------
# Rule 2: ListItem whose only useful descendant is a Hyperlink -> drop the
# ListItem, keep the Hyperlink.
# ---------------------------------------------------------------------------

def test_rule2_listitem_with_single_hyperlink_child_folds():
    item = _match("li1", "Docs", "list item", (0, 0, 100, 30))
    link = _match("a1", "Docs", "hyperlink", (5, 5, 80, 18))
    result = apply_dom_corrections([item, link])
    assert _ids(result) == {"a1"}


def test_rule2_folds_even_when_names_differ():
    # Rule 2 keys on role only, not name equality (unlike rule 1).
    item = _match("li1", "row label", "list item", (0, 0, 100, 30))
    link = _match("a1", "Open document", "hyperlink", (5, 5, 80, 18))
    result = apply_dom_corrections([item, link])
    assert _ids(result) == {"a1"}


def test_rule2_no_fold_when_child_is_not_hyperlink():
    item = _match("li1", "Docs", "list item", (0, 0, 100, 30))
    text = _match("t1", "Docs", "text", (5, 5, 80, 18))
    result = apply_dom_corrections([item, text])
    assert _ids(result) == {"li1", "t1"}


def test_rule2_no_fold_with_two_useful_children():
    item = _match("li1", "Docs", "list item", (0, 0, 200, 30))
    link_a = _match("a1", "Docs", "hyperlink", (5, 5, 80, 18))
    link_b = _match("a2", "More", "hyperlink", (100, 5, 80, 18))
    result = apply_dom_corrections([item, link_a, link_b])
    assert _ids(result) == {"li1", "a1", "a2"}


# ---------------------------------------------------------------------------
# Rule 3: Hyperlink containing a HeadingControl -> drop the HeadingControl,
# keep the outer Hyperlink.
# ---------------------------------------------------------------------------

def test_rule3_hyperlink_containing_heading_folds():
    link = _match("a1", "Story title", "hyperlink", (0, 0, 200, 50))
    heading = _match("h1", "Story title", "heading", (5, 5, 180, 30))
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1"}


def test_rule3_uia_heading_control_name_folds():
    link = _match("a1", "Story title", "hyperlink", (0, 0, 200, 50))
    heading = _match("h1", "Story title", "HeadingControl", (5, 5, 180, 30))
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1"}


def test_rule3_no_fold_when_inner_is_not_heading():
    link = _match("a1", "Story title", "hyperlink", (0, 0, 200, 50))
    text = _match("t1", "Story title", "text", (5, 5, 180, 30))
    # Inner is text, not a heading -- rule 3 does not apply (rule 1 needs a
    # group parent, not a hyperlink), so nothing folds.
    result = apply_dom_corrections([link, text])
    assert _ids(result) == {"a1", "t1"}


def test_rule3_no_containment_means_no_fold():
    # Disjoint rectangles -> heading is not inside the hyperlink.
    link = _match("a1", "Story title", "hyperlink", (0, 0, 50, 50))
    heading = _match("h1", "Other", "heading", (100, 0, 50, 50))
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1", "h1"}


# ---------------------------------------------------------------------------
# General behaviour.
# ---------------------------------------------------------------------------

def test_empty_list_returns_empty():
    assert apply_dom_corrections([]) == []


def test_unrelated_matches_pass_through_unchanged():
    a = _match("b1", "Save", "button", (0, 0, 40, 20))
    b = _match("b2", "Cancel", "button", (50, 0, 40, 20))
    result = apply_dom_corrections([a, b])
    assert _ids(result) == {"b1", "b2"}


def test_order_is_preserved_for_survivors():
    a = _match("b1", "Save", "button", (0, 0, 40, 20))
    group = _match("g1", "Inbox", "group", (50, 0, 100, 40))
    text = _match("t1", "Inbox", "text", (55, 5, 80, 20))
    c = _match("b2", "Cancel", "button", (200, 0, 40, 20))
    result = apply_dom_corrections([a, group, text, c])
    assert [m.item_id for m in result] == ["b1", "t1", "b2"]


# ---------------------------------------------------------------------------
# Effective browser-process-list helper.
# ---------------------------------------------------------------------------

def test_effective_list_is_starter_when_extend_empty():
    result = effective_browser_processes(list(DEFAULT_BROWSER_PROCESSES), [])
    assert result == [p.lower() for p in DEFAULT_BROWSER_PROCESSES]


def test_effective_list_appends_extend_entries():
    result = effective_browser_processes(["chrome.exe"], ["myapp.exe"])
    assert result == ["chrome.exe", "myapp.exe"]


def test_effective_list_dedups_case_insensitively_preserving_order():
    result = effective_browser_processes(
        ["Chrome.exe", "brave.exe"], ["chrome.exe", "vivaldi.exe"]
    )
    assert result == ["chrome.exe", "brave.exe", "vivaldi.exe"]


def test_effective_list_default_starter_contains_known_browsers():
    lowered = {p.lower() for p in DEFAULT_BROWSER_PROCESSES}
    assert {"brave.exe", "chrome.exe", "msedge.exe", "code.exe"} <= lowered


def test_effective_list_handles_none_extend():
    result = effective_browser_processes(["chrome.exe"], None)
    assert result == ["chrome.exe"]


# ---------------------------------------------------------------------------
# Equal-bounds folds (reviewer_0 finding 1 + 6). Coincident rects are the most
# common real Chromium fold (a wrapper sharing its child's exact rect).
# ---------------------------------------------------------------------------

def test_equal_bounds_group_text_folds():
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40))
    text = _match("t1", "Inbox", "text", (0, 0, 100, 40))
    # group precedes text in tree order, so group is the ancestor.
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"t1"}


def test_equal_bounds_hyperlink_heading_folds():
    link = _match("a1", "Story", "hyperlink", (0, 0, 200, 50))
    heading = _match("h1", "Story", "heading", (0, 0, 200, 50))
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1"}


# ---------------------------------------------------------------------------
# Nested chain listitem > link > heading (reviewer_0 finding 2 + 4 + 6).
# Direct-descendant model: listitem's sole direct descendant is the link
# (the heading is nested inside the link), so rule 2 folds the listitem; rule
# 3 folds the heading under the link. Final survivor is the link alone.
# ---------------------------------------------------------------------------

def test_nested_listitem_link_heading_chain_folds_to_link():
    item = _match("li1", "Open", "list item", (0, 0, 200, 40), display_number=1)
    link = _match("a1", "Open", "hyperlink", (5, 5, 180, 30), display_number=2)
    heading = _match("h1", "Open", "heading", (10, 10, 160, 20), display_number=3)
    result = apply_dom_corrections([item, link, heading])
    assert _ids(result) == {"a1"}


# ---------------------------------------------------------------------------
# Multi-heading: a hyperlink containing TWO headings -> both fold away.
# ---------------------------------------------------------------------------

def test_hyperlink_with_two_headings_folds_both():
    link = _match("a1", "Story", "hyperlink", (0, 0, 200, 80))
    h1 = _match("h1", "Title", "heading", (5, 5, 180, 30))
    h2 = _match("h2", "Subtitle", "heading", (5, 40, 180, 30))
    result = apply_dom_corrections([link, h1, h2])
    assert _ids(result) == {"a1"}


# ---------------------------------------------------------------------------
# Dual-rule: a group whose sole same-name child is a list item that itself
# wraps a sole hyperlink. Rule 1 (group->text) does NOT apply (child is a list
# item, not text), so the group survives; rule 2 folds the list item to the
# link. Documented outcome: group and link survive, list item drops.
# ---------------------------------------------------------------------------

def test_dual_rule_group_wraps_listitem_wraps_link():
    group = _match("g1", "Nav", "group", (0, 0, 200, 40), display_number=1)
    item = _match("li1", "Nav", "list item", (5, 5, 180, 30), display_number=2)
    link = _match("a1", "Home", "hyperlink", (10, 10, 160, 20), display_number=3)
    result = apply_dom_corrections([group, item, link])
    # group's sole direct descendant is the list item (not text) -> no rule-1
    # fold. list item's sole direct descendant is the link -> rule-2 fold.
    assert _ids(result) == {"g1", "a1"}


# ---------------------------------------------------------------------------
# Overlapping non-fold (reviewer_0 finding 3). A heading that appears BEFORE
# its supposed hyperlink "parent" in tree order must NOT be folded: tree order
# makes the earlier element the ancestor, so the heading is the ancestor here
# and the hyperlink the (sole) descendant -- neither fold rule matches that
# direction (no rule folds a heading-parent).
# ---------------------------------------------------------------------------

def test_overlapping_heading_before_hyperlink_does_not_fold():
    heading = _match("h1", "Story", "heading", (0, 0, 200, 50))
    link = _match("a1", "Story", "hyperlink", (0, 0, 200, 50))
    result = apply_dom_corrections([heading, link])
    assert _ids(result) == {"h1", "a1"}


# ---------------------------------------------------------------------------
# Pre-order ancestry for ALL containment (gemini finding A / wh-9f3t.19.1).
# In a strict UIA pre-order walk an ancestor always precedes its descendants,
# so a later element can never be the ancestor of an earlier one even if it
# geometrically encloses it (z-stacking / absolute positioning).
# ---------------------------------------------------------------------------

def test_later_larger_element_does_not_contain_earlier_smaller():
    heading = _match("h1", "Story", "heading", (10, 10, 50, 50), display_number=1)
    link = _match("a1", "Story", "hyperlink", (0, 0, 200, 200), display_number=2)
    # The hyperlink appears AFTER the heading in tree order, so it cannot be the
    # heading's parent even though it geometrically encloses it -> no fold.
    result = apply_dom_corrections([heading, link])
    assert _ids(result) == {"h1", "a1"}


# ---------------------------------------------------------------------------
# Same-rule nested scaffolding collapse (gemini finding B / wh-9f3t.19.2).
# A nested Group > Group > Text (all same bounds, same name) must fully
# collapse to the Text alone, not leave Group1 + Text sharing a rect.
# ---------------------------------------------------------------------------

def test_nested_group_group_text_collapses_to_text():
    group1 = _match("g1", "Inbox", "group", (0, 0, 100, 40), display_number=1)
    group2 = _match("g2", "Inbox", "group", (0, 0, 100, 40), display_number=2)
    text = _match("t1", "Inbox", "text", (0, 0, 100, 40), display_number=3)
    result = apply_dom_corrections([group1, group2, text])
    assert _ids(result) == {"t1"}


# ---------------------------------------------------------------------------
# Purity (reviewer_0 finding 6): inputs are not mutated and a new list object
# is returned.
# ---------------------------------------------------------------------------

def test_apply_does_not_mutate_inputs_and_returns_new_list():
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40))
    text = _match("t1", "Inbox", "text", (5, 5, 80, 20))
    matches = [group, text]
    before = [
        (m.item_id, m.name, m.role, m.bounds, m.display_number) for m in matches
    ]
    result = apply_dom_corrections(matches)
    # Input list identity and contents unchanged.
    assert matches == [group, text]
    assert matches[0] is group and matches[1] is text
    after = [
        (m.item_id, m.name, m.role, m.bounds, m.display_number) for m in matches
    ]
    assert before == after
    # Returned list is a distinct object.
    assert result is not matches


# ---------------------------------------------------------------------------
# Locale invariance (wh-l4h.1.12). On non-English Windows the walker emits
# LOCALIZED control-type strings (German "Gruppe", "Text", "Link",
# "Listenelement"), so string-only role matching silently fails and NOTHING
# folds. The four clean predicates (group/text/hyperlink/listitem) now compare
# the numeric UIA control-type id, which is identical across display languages,
# so the fold fires regardless of the localized role string.
# ---------------------------------------------------------------------------

def test_rule1_folds_with_german_role_strings_via_control_type_id():
    # German UIA localized control types: group -> "Gruppe", text -> "Text".
    group = _match(
        "g1", "Inbox", "Gruppe", (0, 0, 100, 40), control_type_id=UIA_GROUP_CTID
    )
    text = _match(
        "t1", "Inbox", "Text", (5, 5, 80, 20), control_type_id=UIA_TEXT_CTID
    )
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"t1"}


def test_rule2_folds_with_german_role_strings_via_control_type_id():
    # German: list item -> "Listenelement", hyperlink -> "Link".
    item = _match(
        "li1", "Docs", "Listenelement", (0, 0, 100, 30),
        control_type_id=UIA_LISTITEM_CTID,
    )
    link = _match(
        "a1", "Docs", "Link", (5, 5, 80, 18), control_type_id=UIA_HYPERLINK_CTID
    )
    result = apply_dom_corrections([item, link])
    assert _ids(result) == {"a1"}


def test_rule3_folds_hyperlink_with_german_role_string_via_control_type_id():
    # The hyperlink (outer) is matched by id even with a German role string;
    # the inner heading still matches by its localized/canonical string because
    # UIA has no Heading control-type id (see module docstring).
    link = _match(
        "a1", "Story title", "Link", (0, 0, 200, 50),
        control_type_id=UIA_HYPERLINK_CTID,
    )
    heading = _match("h1", "Story title", "heading", (5, 5, 180, 30))
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1"}


def test_id_based_rules_do_not_fold_on_english_string_with_wrong_id():
    # The locale-invariance claim's converse: an English role STRING that would
    # have matched the old string predicate does NOT fold when the numeric id
    # is wrong/zero. This proves the four clean predicates key on the id, not
    # the string.
    group = _match("g1", "Inbox", "group", (0, 0, 100, 40), control_type_id=0)
    text = _match("t1", "Inbox", "text", (5, 5, 80, 20), control_type_id=0)
    result = apply_dom_corrections([group, text])
    assert _ids(result) == {"g1", "t1"}


def test_heading_predicate_remains_string_based_documented_limitation():
    # _is_heading necessarily stays string-based: UIA has no Heading
    # control-type id, so a heading carries control_type_id 50020 (Text) and
    # cannot be told apart from ordinary text by id. Rule 3 must still fold a
    # heading recognised purely by its (here English) localized role string.
    link = _match(
        "a1", "Story", "hyperlink", (0, 0, 200, 50),
        control_type_id=UIA_HYPERLINK_CTID,
    )
    # Heading exposed as a Text control (id 50020) but role string "heading".
    heading = _match(
        "h1", "Story", "heading", (5, 5, 180, 30), control_type_id=UIA_TEXT_CTID
    )
    result = apply_dom_corrections([link, heading])
    assert _ids(result) == {"a1"}
