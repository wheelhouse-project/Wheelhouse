"""Mutation evidence for the tree-item Invoke outcome check
(wh-pattern-manager-tree-click).

A Qt tree row answers ``InvokePattern.Invoke()`` with S_OK and does nothing,
so the executor reads ``SelectionItem.IsSelected`` around the press and falls
through to its already-gated coordinate click when the state did not move.
This gate breaks each part of that decision -- the TreeItem scope, the
pre-press read, the post-press read, the two bound (b) escapes that must
press nothing, the eligibility gate, the two reason tags, and the notice
wording for them -- and requires the named tests to fail for each.

Run from services/wheelhouse:

    python tests/mutation_gate_treeitem_invoke_outcome.py --check
    python tests/mutation_gate_treeitem_invoke_outcome.py

The selection is whole-test node IDs, not the test files, because
tests/test_uia_walker.py holds a live-desktop test that is skipped on a
headless run.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

EXECUTOR = "tests/test_click_executor.py::"
NOTICE = "tests/test_click_notice.py::"
WALKER = "tests/test_uia_walker.py::"

# Walker tests (the selection reader itself)
READER_CACHED = "test_selection_state_uses_the_cached_pattern"
READER_CURRENT = "test_selection_state_falls_back_to_the_current_pattern"
READER_ABSENT = "test_selection_state_is_none_when_the_control_has_no_pattern"
READER_NULL = "test_selection_state_treats_a_null_pointer_as_no_pattern"
READER_RAISES = "test_selection_state_lets_a_raising_read_propagate"
READER_BOOL = "test_selection_state_coerces_the_com_value_to_a_bool"

# Executor tests
COORD = "test_treeitem_invoke_that_does_not_select_falls_back_to_coordinate"
SELECTED = "test_treeitem_invoke_that_selects_sends_no_coordinate_click"
OTHER_TYPE = "test_non_treeitem_never_reads_the_selection_state"
HELPER_SCOPE = (
    "test_selection_state_helper_returns_none_for_a_non_treeitem"
)
PRESELECTED = "test_treeitem_selected_before_the_invoke_sends_no_coordinate_click"
NO_PATTERN = "test_treeitem_without_selectionitem_sends_no_coordinate_click"
PRE_RAISE = "test_selection_read_raising_before_the_invoke_presses_once"
POST_RAISE = "test_selection_read_raising_after_the_invoke_reports_ok"
PATTERN_GONE = "test_selection_pattern_gone_after_the_invoke_reports_ok"
FOREGROUND = "test_foreground_change_after_the_invoke_reports_ok"
INELIGIBLE = "test_ineligible_treeitem_match_sends_no_coordinate_click"
INERT = "test_treeitem_unselected_after_the_coordinate_click_fails_with_its_tag"
NOT_LANDED = "test_coordinate_click_that_does_not_land_after_the_invoke_has_its_tag"
REVERIFY = "test_verification_failure_between_the_presses_returns_its_own_reason"
BUDGET_OVERRUN = (
    "test_treeitem_selection_pre_read_that_overruns_the_budget_sends_nothing"
)
BUDGET_RAISE = (
    "test_treeitem_selection_pre_read_that_raises_past_the_budget_sends_nothing"
)
BUDGET_OK = "test_treeitem_selection_pre_read_inside_the_budget_still_invokes"
NAVPANE = "test_treeitem_without_cached_invoke_skips_the_selection_pre_read"
CACHED_ARM = (
    "test_treeitem_with_cached_invoke_still_gets_the_selection_pre_read"
)

# Notice tests
NOTICE_INERT = "test_wording_execution_failed_invoke_then_coordinate_no_effect"
NOTICE_NOT_LANDED = (
    "test_wording_execution_failed_invoke_no_effect_then_sendinput_failed"
)

TESTS = (
    *(EXECUTOR + t for t in (
        COORD, SELECTED, OTHER_TYPE, PRESELECTED, NO_PATTERN, PRE_RAISE,
        POST_RAISE, PATTERN_GONE, FOREGROUND, INELIGIBLE, INERT, NOT_LANDED,
        REVERIFY, BUDGET_OVERRUN, BUDGET_RAISE, BUDGET_OK, NAVPANE,
        CACHED_ARM, HELPER_SCOPE)),
    *(NOTICE + t for t in (NOTICE_INERT, NOTICE_NOT_LANDED)),
    *(WALKER + t for t in (
        READER_CACHED, READER_CURRENT, READER_ABSENT, READER_NULL,
        READER_RAISES, READER_BOOL)),
)

MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TESTS,
                          file=SERVICE / file, old=old, new=new,
                          expect=list(expect)))


EXECUTOR_SRC = "ui/click_executor.py"
WALKER_SRC = "ui/uia_walker.py"
WORDING_SRC = "click_notice_toast_wording.py"

# The arm that decides whether a TreeItem gets the pre-read at all, and
# the budget check that sits inside it (hard gate 2026-09-22, delta D1).
ARM = (
    "        if winner.control_type_id == UIA_TREEITEM and "
    "winner.invoke_supported:\n"
)
BUDGET_CHECK = (
    '            if self._verification_budget_expired'
    '("tree-item selection pre-read"):\n'
)

# --- the outcome check runs at all ------------------------------------------

add("outcome-check-never-arms", EXECUTOR_SRC,
    "        if selected_before is False:\n",
    "        if False:\n",
    COORD, INERT, NOT_LANDED, REVERIFY)
# The whole point of the pre-read: a row ALREADY selected has no signal, so
# arming on any answer would press a second time on a working Invoke.
# NO_PATTERN and PRE_RAISE are deliberately NOT expected here: when the
# pre-read could not answer, the post-read cannot either, so arming the check
# changes nothing observable for them. The two mutations below cover that
# decision directly, with post-reads that DO answer.
add("outcome-check-arms-whatever-the-pre-read-said", EXECUTOR_SRC,
    "        if selected_before is False:\n",
    "        if True:\n",
    PRESELECTED, OTHER_TYPE)

# --- the TreeItem scope (bound a) ---
# The catcher is HELPER_SCOPE, not OTHER_TYPE: _invoke_path's arm (hard
# gate D1) tests the control type too, so a non-TreeItem no longer reaches
# this guard through the public click path and OTHER_TYPE passes under both
# mutations below. The helper test calls the guard directly.
# -------------------------------------------

add("scope-open-to-every-control-type", EXECUTOR_SRC,
    "        if winner.control_type_id != UIA_TREEITEM:\n"
    "            return None\n",
    "        if False:\n"
    "            return None\n",
    HELPER_SCOPE)
add("scope-inverted", EXECUTOR_SRC,
    "        if winner.control_type_id != UIA_TREEITEM:\n",
    "        if winner.control_type_id == UIA_TREEITEM:\n",
    COORD, HELPER_SCOPE, INERT)

# --- the pre-press read (bound a) -------------------------------------------

add("pre-read-raise-arms-the-check", EXECUTOR_SRC,
    '''        except Exception as exc:  # noqa: BLE001 -- COM property read can raise
            logger.debug(
                "click_element pre-invoke selection read failed, leaving the "
                "tree-item outcome check off: matched=%r repr=%.200s",
                winner.name,
                repr(exc),
            )
            return None
        if state is None:
            return None
        return bool(state)
''',
    '''        except Exception:  # noqa: BLE001 -- COM property read can raise
            return False
        if state is None:
            return None
        return bool(state)
''',
    PRE_RAISE)
add("pre-read-missing-pattern-arms-the-check", EXECUTOR_SRC,
    "        if state is None:\n"
    "            return None\n"
    "        return bool(state)\n"
    "\n"
    "    def _invoke_outcome_retry(",
    "        if state is None:\n"
    "            return False\n"
    "        return bool(state)\n"
    "\n"
    "    def _invoke_outcome_retry(",
    NO_PATTERN)

# --- the post-press read (bounds b and d) -----------------------------------

add("post-read-ignored", EXECUTOR_SRC,
    "        if after_invoke is not False:\n",
    "        if False:\n",
    SELECTED)
add("post-read-raise-treated-as-unselected", EXECUTOR_SRC,
    '''        except Exception as exc:  # noqa: BLE001 -- COM property read can raise
            logger.info(
                "click_element selection re-read after %s failed for %r; "
                "treating the press as having acted: repr=%.200s",
                after_what,
                winner.name,
                repr(exc),
            )
            return None
        if state is None:
            return None
        return bool(state)
''',
    '''        except Exception:  # noqa: BLE001 -- COM property read can raise
            return False
        if state is None:
            return None
        return bool(state)
''',
    POST_RAISE)
add("post-read-missing-pattern-treated-as-unselected", EXECUTOR_SRC,
    "        try:\n"
    "            state = self._selection_state_fn(winner.control_ref)\n"
    "        except Exception as exc:  # noqa: BLE001 -- COM property read can raise\n"
    "            logger.info(\n",
    "        try:\n"
    "            state = self._selection_state_fn(winner.control_ref)\n"
    "            if state is None:\n"
    "                return False\n"
    "        except Exception as exc:  # noqa: BLE001 -- COM property read can raise\n"
    "            logger.info(\n",
    PATTERN_GONE)

# --- bound (b): the foreground escape ---------------------------------------

add("foreground-escape-removed", EXECUTOR_SRC,
    "        if self._foreground_identity_reason(probe, snap) is not None:\n",
    "        if False:\n",
    FOREGROUND)

# --- bound (c): the eligibility gate ----------------------------------------

# The gate call appears at four sites, so this pattern carries the first line
# of the tree-item site's own log message to pin it.
GATE_SITE = (
    "        if not self._coord_eligible(winner, query):\n"
    "            logger.info(\n"
    '                "click_element tree item %r did not select on Invoke and the "\n'
)

add("eligibility-gate-removed", EXECUTOR_SRC,
    GATE_SITE,
    GATE_SITE.replace(
        "        if not self._coord_eligible(winner, query):\n",
        "        if False:\n",
    ),
    INELIGIBLE)

# --- the two reason tags ----------------------------------------------------

add("inert-tag-collapsed-onto-the-delivery-tag", EXECUTOR_SRC,
    '            return self._fail(winner, "invoke_then_coordinate_no_effect")\n',
    '            return self._fail(winner, "invoke_no_effect_then_sendinput_failed")\n',
    INERT)
add("delivery-tag-collapsed-onto-invoke-com-error", EXECUTOR_SRC,
    '            fail_reason="invoke_no_effect_then_sendinput_failed",\n',
    '            fail_reason="invoke_com_error",\n',
    NOT_LANDED)
add("coordinate-failure-reported-as-ok", EXECUTOR_SRC,
    '        if result.outcome != "ok":\n'
    "            return result\n",
    "        if False:\n"
    "            return result\n",
    NOT_LANDED, REVERIFY)

# --- the selection reader itself --------------------------------------------

add("reader-reports-a-missing-pattern-as-unselected", WALKER_SRC,
    "    if pattern is None:\n"
    "        return None\n"
    "\n"
    "    return bool(pattern.CurrentIsSelected)\n",
    "    if pattern is None:\n"
    "        return False\n"
    "\n"
    "    return bool(pattern.CurrentIsSelected)\n",
    READER_ABSENT, READER_NULL)
add("reader-never-consults-the-live-pattern", WALKER_SRC,
    "    if pattern is None:\n"
    "        pattern = _typed_pattern(\n"
    "            element,\n"
    '            "GetCurrentPattern",\n'
    "            pattern_id,\n"
    "            _selection_item_pattern_class,\n"
    "        )\n",
    "",
    READER_CURRENT)
add("reader-swallows-a-raising-read", WALKER_SRC,
    "    return bool(pattern.CurrentIsSelected)\n",
    "    try:\n"
    "        return bool(pattern.CurrentIsSelected)\n"
    "    except Exception:  # noqa: BLE001\n"
    "        return False\n",
    READER_RAISES)
add("reader-returns-the-raw-com-value", WALKER_SRC,
    "    return bool(pattern.CurrentIsSelected)\n",
    "    return pattern.CurrentIsSelected\n",
    READER_BOOL)

# --- the pre-read stays inside the verification budget (round 1 finding) ----
# The selection read is a live COM property read between _verify's last budget
# check and the press, so the press needs its own check after it
# (wh-pattern-manager-tree-click.2.1).

add("budget-check-after-the-pre-read-never-runs", EXECUTOR_SRC,
    BUDGET_CHECK,
    "            if False:\n",
    BUDGET_OVERRUN, BUDGET_RAISE)
add("budget-check-refuses-whatever-the-clock-says", EXECUTOR_SRC,
    BUDGET_CHECK,
    "            if True:\n",
    BUDGET_OK, COORD, SELECTED)

# --- the arm: control type AND a cached Invoke pattern (hard gate D1) ---
# A File Explorer navigation-pane folder is a TreeItem with no cached
# Invoke pattern. Arming on the control type alone made it pay a live
# cross-process SelectionItem read and let it return verification_timeout,
# where the base went straight to the InvokePatternUnavailable / DDA /
# coordinate path (wh-explorer-navpane-click).

add("arm-ignores-the-cached-invoke-pattern", EXECUTOR_SRC,
    ARM,
    "        if winner.control_type_id == UIA_TREEITEM:\n",
    NAVPANE)
add("arm-uses-the-wrong-control-type", EXECUTOR_SRC,
    ARM,
    ARM.replace("== UIA_TREEITEM", "!= UIA_TREEITEM"),
    BUDGET_OVERRUN, BUDGET_RAISE, COORD, CACHED_ARM)
add("arm-inverts-the-cached-invoke-test", EXECUTOR_SRC,
    ARM,
    ARM.replace("and winner.invoke_supported",
                "and not winner.invoke_supported"),
    NAVPANE, COORD, CACHED_ARM)


# --- the notice wording for the two new reasons -----------------------------

add("wording-inert-alias-dropped", WORDING_SRC,
    '        "invoke_then_coordinate_no_effect",\n',
    "",
    NOTICE_INERT)
add("wording-delivery-alias-dropped", WORDING_SRC,
    '        "invoke_no_effect_then_sendinput_failed",\n',
    "",
    NOTICE_NOT_LANDED)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
