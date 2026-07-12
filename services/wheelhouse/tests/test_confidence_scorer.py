"""Unit tests for the voice-element-clicking confidence scorer (wh-lnjnx).

Covers every row of the eligibility truth table and every additive scoring
signal from the v5 design doc
(docs/plans/2026-05-21-voice-element-clicking-design-v5.md, section
"Matching and clear-winner rule"). is_eligible and score are pure functions
over ElementQuery / ElementMatch -- no I/O, no config, no COM.
"""

import pytest

from ui.confidence_scorer import is_eligible, score
from ui.element_types import ElementMatch, ElementQuery


def _query(name: str, role: str | None = None) -> ElementQuery:
    return ElementQuery(
        name=name,
        role=role,
        ordinal=None,
        spatial=None,
        raw_utterance=name,
    )


def _match(
    name: str,
    role: str = "Button",
    *,
    invoke_supported: bool = False,
    is_enabled: bool = True,
) -> ElementMatch:
    return ElementMatch(
        item_id="m1",
        display_number=1,
        name=name,
        role=role,
        bounds=(0, 0, 10, 10),
        monitor_id=0,
        score=0.0,
        is_eligible=False,
        source="uia",
        invoke_supported=invoke_supported,
        is_enabled=is_enabled,
        control_ref=object(),
    )


# --- Eligibility table -------------------------------------------------------


def test_row_a_exact_match_eligible():
    # (a) Yes: name exact match (case-insensitive).
    q = _query("cancel")
    m = _match("Cancel", invoke_supported=False, is_enabled=False)
    assert is_eligible(q, m) is True


def test_row_b_starts_with_plus_role_eligible():
    # (b) Yes: starts-with AND role matches.
    q = _query("save", role="Button")
    m = _match("Save As", role="Button", invoke_supported=False, is_enabled=False)
    assert is_eligible(q, m) is True


def test_row_b_starts_with_plus_invoke_eligible():
    # (b) Yes: starts-with AND InvokePattern (no role, not enabled).
    q = _query("save", role="MenuItem")
    m = _match("Save As", role="Button", invoke_supported=True, is_enabled=False)
    assert is_eligible(q, m) is True


def test_row_b_starts_with_plus_enabled_eligible():
    # (b) Yes: starts-with AND enabled (no role, no invoke).
    q = _query("save", role="MenuItem")
    m = _match("Save As", role="Button", invoke_supported=False, is_enabled=True)
    assert is_eligible(q, m) is True


def test_row_c_substring_plus_role_plus_ratio_eligible():
    # (c) Yes: substring AND role match AND both ratio thresholds hold.
    # query "submit" (6) in control "Resubmit" (8): ratio 6/8 = 0.75 >= 0.6,
    # length 6 >= 4. Substring but not starts-with / exact.
    q = _query("submit", role="Button")
    m = _match("Resubmit", role="Button")
    assert is_eligible(q, m) is True


def test_row_d_disabled_exact_eligible():
    # (d) Yes: disabled real control, exact name.
    q = _query("delete")
    m = _match("Delete", invoke_supported=False, is_enabled=False)
    assert is_eligible(q, m) is True


def test_row_d_disabled_starts_with_eligible():
    # (d) Yes: disabled real control, starts-with name, no role / no invoke.
    q = _query("delete", role="MenuItem")
    m = _match("Delete All", role="Button", invoke_supported=False, is_enabled=False)
    assert is_eligible(q, m) is True


def test_row_e_substring_alone_ineligible():
    # (e) No: substring alone -- no role match, not exact / starts-with.
    # "mit" is a substring of "Submit" but the query has no role and is not a
    # prefix.
    q = _query("mit", role=None)
    m = _match("Submit", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_row_e_substring_none_role_clears_thresholds_ineligible():
    # (e) regression for wh-9f3t.2.1 / wh-9f3t.2.2: a no-role substring query
    # that CLEARS both length-ratio thresholds must still be ineligible,
    # because there is no role signal. "submit" (6) in "Resubmit" (8): length
    # 6 >= 4 and ratio 6/8 = 0.75 >= 0.6, so the thresholds pass; only the
    # missing role signal makes it ineligible. The earlier row-e test used a
    # 3-char name that failed the length gate first, so it passed for the
    # wrong reason and would not have caught the role-None bug.
    q = _query("submit", role=None)
    m = _match("Resubmit", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_row_f_substring_role_ratio_below_threshold_ineligible():
    # (f) No: substring + role match but ratio below threshold.
    # "file" (4) in "Profile" (7): ratio 4/7 = 0.57 < 0.6 -> ineligible.
    q = _query("file", role="Button")
    m = _match("Profile", role="Button")
    assert is_eligible(q, m) is False


def test_row_f_substring_role_length_below_threshold_ineligible():
    # (f) No: substring + role match but query length below 4.
    # "go" (2) in "Logout" (6): length 2 < 4 -> ineligible even though
    # ratio 2/6 also fails.
    q = _query("og", role="Button")
    m = _match("Logout", role="Button")
    assert is_eligible(q, m) is False


def test_row_g_no_name_signal_ineligible():
    # (g) No: no name signal at all.
    q = _query("cancel", role="Button")
    m = _match("Submit", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_row_h_pattern_enablement_only_no_name_ineligible():
    # (h) No: pure pattern / enablement match with no name overlap.
    q = _query("ok")
    m = _match("Settings", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


# --- Empty / whitespace query name (codex wh-9f3t.3.1) -----------------------


def test_empty_query_name_ineligible_despite_startswith():
    # An empty query name carries no positive name signal. Every string
    # starts with "" and contains "", so without an explicit guard the
    # starts-with branch would make EVERY non-empty control eligible. The
    # v5 rule requires a positive name signal, so an empty name is
    # ineligible -- even with role match, InvokePattern, and enabled all
    # corroborating.
    q = _query("", role="Button")
    m = _match("Cancel", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_empty_query_name_ineligible_no_role():
    # Empty name, no role -- the parser leaving only a role (e.g. "click the
    # button") yields an empty name. Must be ineligible.
    q = _query("", role=None)
    m = _match("Submit", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_whitespace_query_name_ineligible():
    # A whitespace-only name is not a positive name signal either.
    q = _query("   ", role="Button")
    m = _match("Cancel", role="Button", invoke_supported=True, is_enabled=True)
    assert is_eligible(q, m) is False


def test_score_empty_query_name_awards_no_name_bonus():
    # score must not award the +0.4 starts-with bonus for an empty query
    # name. Isolate: non-matching role, no invoke, not enabled -> 0.0 (no
    # name signal credited). A buggy startswith("") would wrongly add +0.4.
    q = _query("", role="MenuItem")
    m = _match("Cancel", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.0)


def test_score_empty_query_name_credits_only_other_signals():
    # With an empty name but a matching role and enabled control, score
    # credits the role (+0.3) and enabled (+0.1) signals only, never a name
    # bonus: total 0.4, not 0.8.
    q = _query("", role="Button")
    m = _match("Cancel", role="Button", invoke_supported=False, is_enabled=True)
    assert score(q, m) == pytest.approx(0.4)


# --- Ratio boundary cases ----------------------------------------------------


def test_ratio_boundary_just_below_ineligible():
    # "file"/"Profile" is the spec's canonical below-threshold example.
    q = _query("file", role="Button")
    m = _match("Profile", role="Button")
    assert is_eligible(q, m) is False


def test_ratio_boundary_at_threshold_eligible():
    # query "files" (5) in "Profiles" (... ) -- pick a control where the ratio
    # is exactly 0.6 and length >= 4. "abcd" (4) in "abcdef" (6): 4/6 = 0.667.
    # For an exact 0.6 boundary: "abc" len 3 fails length gate, so use a case
    # that meets both: query "abcde" (5) in control "Xabcde" -> substring not
    # prefix, ratio 5/6 = 0.83. Use a clean >=0.6 case.
    q = _query("abcde", role="Button")
    m = _match("Xabcde", role="Button")
    assert is_eligible(q, m) is True


def test_ratio_exactly_point_six_eligible():
    # Exactly 0.6: query length 6 against control length 10. "ckabcd" not a
    # prefix of "fyckabcdzz" -- substring with ratio 6/10 = 0.6 (>= threshold).
    q = _query("ckabcd", role="Button")
    m = _match("fyckabcdzz", role="Button")
    assert is_eligible(q, m) is True


# --- Scoring signals ---------------------------------------------------------


def test_score_name_exact():
    # +0.5 name exact only. Isolate the name signal: give the query a role
    # that does NOT match (so no +0.3 role bonus), no invoke, not enabled.
    q = _query("cancel", role="MenuItem")
    m = _match("Cancel", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.5)


def test_score_name_starts_with():
    # +0.4 name starts-with only (non-matching role isolates the name signal).
    q = _query("save", role="MenuItem")
    m = _match("Save As", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.4)


def test_score_name_substring():
    # +0.3 name substring only (not prefix, not exact; non-matching role).
    q = _query("submit", role="MenuItem")
    m = _match("Resubmit", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.3)


def test_score_role_match():
    # +0.3 role match. Use a control with no name overlap to isolate role.
    q = _query("zzz", role="Button")
    m = _match("Settings", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.3)


def test_score_invoke_supported():
    # +0.1 InvokePattern only (non-matching role, no name overlap, not enabled).
    q = _query("zzz", role="MenuItem")
    m = _match("Settings", role="Button", invoke_supported=True, is_enabled=False)
    assert score(q, m) == pytest.approx(0.1)


def test_score_is_enabled():
    # +0.1 enabled only (non-matching role, no name overlap, no invoke).
    q = _query("zzz", role="MenuItem")
    m = _match("Settings", role="Button", invoke_supported=False, is_enabled=True)
    assert score(q, m) == pytest.approx(0.1)


def test_score_name_signals_mutually_exclusive_exact():
    # An exact match awards the single highest name signal (+0.5), NOT
    # +0.5 + 0.4 + 0.3. Isolate by using a non-matching role, no invoke,
    # not enabled.
    q = _query("save", role="MenuItem")
    m = _match("Save", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.5)


def test_score_combined_exact_role_invoke_enabled():
    # +0.5 (exact) + 0.3 (role) + 0.1 (invoke) + 0.1 (enabled) = 1.0.
    q = _query("cancel", role="Button")
    m = _match("Cancel", role="Button", invoke_supported=True, is_enabled=True)
    assert score(q, m) == pytest.approx(1.0)


def test_score_combined_substring_role_enabled():
    # +0.3 (substring) + 0.3 (role) + 0.1 (enabled) = 0.7.
    q = _query("submit", role="Button")
    m = _match("Resubmit", role="Button", invoke_supported=False, is_enabled=True)
    assert score(q, m) == pytest.approx(0.7)


def test_score_role_none_earns_no_role_points():
    # query.role is None is NOT a role match (wh-9f3t.2.1): no role to compare,
    # so the +0.3 role signal is not credited. Isolate: control name has no
    # overlap, no invoke, not enabled -> score 0.0.
    q = _query("zzz", role=None)
    m = _match("Settings", role="Button", invoke_supported=False, is_enabled=False)
    assert score(q, m) == pytest.approx(0.0)


# --- Locale-invariant role matching (wh-l4h.1.15) ----------------------------
#
# query.role holds a canonical English control-type NAME ("Button", ...);
# match.role is the LOCALIZED CachedLocalizedControlType string (German
# "Schaltflaeche" for a button). The role comparison must use the numeric
# control_type_id, not the localized string, so a role-qualified query still
# matches on non-English Windows. control_type_id == 0 (the unknown sentinel)
# falls back to the localized-string comparison so older fixtures and synthetic
# matches do not regress.

UIA_BUTTON_ID = 50000


def _match_localized(
    name: str,
    role: str,
    control_type_id: int,
    *,
    invoke_supported: bool = False,
    is_enabled: bool = True,
) -> ElementMatch:
    return ElementMatch(
        item_id="m1",
        display_number=1,
        name=name,
        role=role,
        bounds=(0, 0, 10, 10),
        monitor_id=0,
        score=0.0,
        is_eligible=False,
        source="uia",
        invoke_supported=invoke_supported,
        is_enabled=is_enabled,
        control_ref=object(),
        control_type_id=control_type_id,
    )


def test_localized_role_substring_eligible_by_id():
    # German button localized role "Schaltflaeche" does not equal canonical
    # "Button" as a string, but the numeric id matches, so the substring+role
    # eligibility row (c) is satisfied.
    q = _query("submit", role="Button")
    m = _match_localized("Resubmit", "Schaltflaeche", UIA_BUTTON_ID)
    assert is_eligible(q, m) is True


def test_localized_role_substring_ineligible_wrong_id():
    # A localized role string whose numeric id is NOT the queried role's id is
    # not a role match, so the substring row (c) fails -> ineligible.
    q = _query("submit", role="Button")
    m = _match_localized("Resubmit", "Schaltflaeche", 50004)  # Edit, not Button
    assert is_eligible(q, m) is False


def test_localized_role_earns_score_bonus_by_id():
    # The +0.3 role bonus honors the id: substring (+0.3) + role-by-id (+0.3)
    # + enabled (+0.1) = 0.7, even though the role STRING does not match.
    q = _query("submit", role="Button")
    m = _match_localized(
        "Resubmit", "Schaltflaeche", UIA_BUTTON_ID, is_enabled=True
    )
    assert score(q, m) == pytest.approx(0.7)


def test_zero_control_type_id_falls_back_to_string():
    # control_type_id == 0 (sentinel) -> fall back to the localized-string
    # casefold comparison. English string "Button" matches canonical "Button".
    q = _query("submit", role="Button")
    m = _match("Resubmit", role="Button")  # default control_type_id == 0
    assert is_eligible(q, m) is True
    # substring 0.3 + role 0.3 + enabled 0.1 (fixture defaults is_enabled=True)
    assert score(q, m) == pytest.approx(0.7)


def test_zero_control_type_id_string_mismatch_no_role():
    # control_type_id == 0 falls back to string; a localized string that does
    # not equal the canonical name is NOT a role match under the fallback.
    q = _query("submit", role="Button")
    m = _match("Resubmit", role="Schaltflaeche")  # ctid 0, localized string
    assert is_eligible(q, m) is False


def test_query_role_not_in_map_falls_back_to_string():
    # A query.role not present in NAME_TO_CONTROL_TYPE_ID (defensive) falls back
    # to the localized-string comparison even when control_type_id is set.
    q = _query("submit", role="ScrollBar")  # not a canonical _ROLE_KEYWORDS value
    m = _match_localized("Resubmit", "ScrollBar", 50000)
    assert is_eligible(q, m) is True  # string match wins via fallback


def test_localized_role_none_still_not_a_match():
    # query.role is None remains NOT a match (wh-9f3t.2.1 row e) regardless of
    # the match's control_type_id: a no-role substring query stays ineligible.
    q = _query("submit", role=None)
    m = _match_localized("Resubmit", "Schaltflaeche", UIA_BUTTON_ID)
    assert is_eligible(q, m) is False
    # substring 0.3 + enabled 0.1 (fixture defaults is_enabled=True); no role bonus
    assert score(q, m) == pytest.approx(0.4)
