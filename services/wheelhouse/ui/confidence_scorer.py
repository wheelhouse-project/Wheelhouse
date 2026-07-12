"""Confidence scoring for voice-driven UI element clicking (wh-lnjnx).

Two pure functions, split out from the UIA strategy so they can be unit-tested
against plain ElementMatch lists with no COM, no I/O, and no config reads:

* ``is_eligible`` -- the v5 eligibility predicate. A match may be clicked only
  if it carries a positive name signal AND a corroborating signal, with a
  length-ratio gate guarding short substring matches.
* ``score`` -- the v5 additive ranking score for an eligible match.

The authoritative spec is the v5 design doc,
docs/plans/2026-05-21-voice-element-clicking-design-v5.md, section
"Matching and clear-winner rule". This module owns questions (1) "is this
match eligible?" and (2) "how good is it?"; the empty/non-empty list and
clear-winner outcomes live in ClearWinnerRule (separate slice).

Name signals are computed case-insensitively (str.casefold). The three name
signals are nested -- exact implies starts-with implies substring -- so the
helpers below are evaluated most-specific-first and the scorer awards only the
single highest applicable name bonus.

Role-match semantics: ``query.role is None`` means "no role constraint", which
is NOT a role match -- there is no role to compare against. A role match
requires a concrete ``query.role`` equal (case-insensitively) to ``match.role``.
This is what keeps a no-role substring query ineligible (eligibility row e); the
design table does not state the None case, and treating None as a match would
let a bare substring query click the wrong control (wh-9f3t.2.1).
"""

from ui.element_types import ElementMatch, ElementQuery
from ui.uia_walker import NAME_TO_CONTROL_TYPE_ID

# Additive ranking-score weights (v5 design doc "Ranking score" table).
_SCORE_NAME_EXACT = 0.5
_SCORE_NAME_STARTS_WITH = 0.4
_SCORE_NAME_SUBSTRING = 0.3
_SCORE_ROLE_MATCH = 0.3
_SCORE_INVOKE = 0.1
_SCORE_ENABLED = 0.1


def _name_exact(query_name: str, match_name: str) -> bool:
    return query_name == match_name


def _name_starts_with(query_name: str, match_name: str) -> bool:
    return match_name.startswith(query_name)


def _name_substring(query_name: str, match_name: str) -> bool:
    return query_name in match_name


def _role_matches(query: ElementQuery, match: ElementMatch) -> bool:
    """True only when the query names a concrete role that equals the control's.

    ``query.role is None`` means the user gave no role, so there is no role to
    match -- this returns False. That is required for eligibility row (e): a
    substring match with no role signal must be ineligible, and treating None
    as a match would make a no-role substring query (e.g. "submit" against
    "Resubmit") eligible and click the wrong control (wh-9f3t.2.1). The role
    bonus in ``score`` uses the same rule, so a no-role query earns no role
    points.

    Comparison is locale-invariant (wh-l4h.1.15): ``query.role`` holds a
    canonical UIA control-type NAME ("Button", ...) emitted by the parser, so
    it is mapped to its numeric UIA control-type id and compared against
    ``match.control_type_id`` -- the locale-invariant id the walker reads off
    the control. On non-English Windows the walker supplies a localized role
    STRING (German "Schaltflaeche" for a button) that would never equal the
    canonical English "Button", but the numeric id is the same in every locale.

    The id comparison falls back to the original localized-string casefold
    comparison in two cases so behavior never regresses below today's:
    ``match.control_type_id == 0`` (the unknown sentinel -- a synthetic match
    or a fixture that did not set the id), and ``query.role`` not present in
    ``NAME_TO_CONTROL_TYPE_ID`` (defensive: a role string outside the parser's
    canonical set).
    """
    if query.role is None:
        return False
    queried_id = NAME_TO_CONTROL_TYPE_ID.get(query.role)
    if queried_id is not None and match.control_type_id != 0:
        return match.control_type_id == queried_id
    return query.role.casefold() == match.role.casefold()


def is_eligible(
    query: ElementQuery,
    match: ElementMatch,
    *,
    min_substring_query_length: int = 4,
    min_substring_overlap_ratio: float = 0.6,
) -> bool:
    """Decide whether ``match`` is eligible to be clicked for ``query``.

    Returns True only for the "Yes" rows of the v5 eligibility table:

    * name exact match;
    * name starts-with match AND (role matches OR InvokePattern OR enabled);
    * name substring match AND role matches AND both substring length-ratio
      thresholds hold;
    * a disabled real control whose name is exact or starts-with (still
      eligible -- later surfaced as ``execution_failed:disabled``).

    Every other combination -- substring alone, substring+role below the
    ratio thresholds, no name signal, or pattern/enablement only -- is
    ineligible. All comparisons are case-insensitive.

    ``min_substring_query_length`` / ``min_substring_overlap_ratio`` are the
    ``[click]`` thresholds, passed in as parameters (defaults 4 and 0.6) so
    this function reads no config.
    """
    query_name = query.name.casefold()
    match_name = match.name.casefold()

    # An empty (or whitespace-only) query name carries no positive name
    # signal. Every string starts with "" and contains "", so without this
    # guard the starts-with branch below would make every non-empty control
    # eligible -- a parser output that leaves only a role (e.g. "click the
    # button") would click an arbitrary control. The v5 rule requires a
    # positive name signal, so a nameless query is ineligible (codex
    # wh-9f3t.3.1).
    if not query_name.strip():
        return False

    # Exact match (rows a, and the exact half of d) -- always eligible.
    if _name_exact(query_name, match_name):
        return True

    # Starts-with match. Eligible when corroborated by a role match,
    # InvokePattern, or being enabled (row b), and also eligible as a
    # disabled real control (the starts-with half of row d). Since the
    # only remaining state is "disabled, no role, no invoke", which row d
    # makes eligible, any starts-with match is eligible.
    if _name_starts_with(query_name, match_name):
        return True

    # Substring match (not exact, not starts-with). Eligible only with a role
    # match AND both length-ratio thresholds (row c). Without a role match it
    # is row e; with a role match but a failing ratio it is row f.
    if _name_substring(query_name, match_name):
        if not _role_matches(query, match):
            return False
        query_len = len(query_name)
        match_len = len(match_name)
        if query_len < min_substring_query_length:
            return False
        if query_len < min_substring_overlap_ratio * match_len:
            return False
        return True

    # No name signal (rows g, h) -- pattern/enablement alone never qualifies.
    return False


def score(query: ElementQuery, match: ElementMatch) -> float:
    """Additive ranking score for an eligible ``match`` against ``query``.

    Sums the signals that apply, per the v5 "Ranking score" table:

    * single highest name signal -- exact +0.5, else starts-with +0.4, else
      substring +0.3 (the three are mutually exclusive in practice because
      exact implies starts-with implies substring; only the strongest is
      awarded);
    * role matches query role +0.3 (requires a concrete ``query.role``; a
      ``None`` query role is not a match and earns no role points);
    * InvokePattern supported +0.1;
    * control enabled +0.1.

    This function does not gate on eligibility -- callers run ``is_eligible``
    first and only score eligible matches.
    """
    query_name = query.name.casefold()
    match_name = match.name.casefold()

    total = 0.0

    # An empty or whitespace-only query name is not a positive name signal,
    # so it earns no name bonus. Without this guard startswith("") would
    # wrongly credit the +0.4 starts-with bonus for a nameless query (codex
    # wh-9f3t.3.1). Mirrors the is_eligible guard so score stays correct even
    # if called directly on an ineligible match.
    if query_name.strip():
        if _name_exact(query_name, match_name):
            total += _SCORE_NAME_EXACT
        elif _name_starts_with(query_name, match_name):
            total += _SCORE_NAME_STARTS_WITH
        elif _name_substring(query_name, match_name):
            total += _SCORE_NAME_SUBSTRING

    if _role_matches(query, match):
        total += _SCORE_ROLE_MATCH

    if match.invoke_supported:
        total += _SCORE_INVOKE

    if match.is_enabled:
        total += _SCORE_ENABLED

    return total
