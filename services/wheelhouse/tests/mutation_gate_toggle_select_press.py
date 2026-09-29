"""Mutation evidence for the Toggle and Select press paths
(wh-mcp-repo-mining.3).

A by-name click whose Invoke pattern is structurally unavailable now tries
``TogglePattern.Toggle``, then ``SelectionItemPattern.Select`` (never for a
TreeItem), and only then the MSAA default action. This gate breaks each part
of that decision -- the placement before the default action, the order of the
two presses, the TreeItem exclusion, the badge-pick exclusion, the honesty
contract after a press call raised, the coordinate-click exception for a
no-side-effect HRESULT, the reason tags, the real seams in the walker, and the
notice wording -- and requires the named tests to fail for each.

Run from services/wheelhouse:

    python tests/mutation_gate_toggle_select_press.py --check
    python tests/mutation_gate_toggle_select_press.py

WHY THIS GATE REPLACES ``runner.LAUNCH``: the runner's default starts pytest
under ``uv run``, and ``uv run`` in a worktree builds a .venv inside the tree,
which makes the worktree undeletable (see
services/stt_providers/shared/tests/mutation_gate_lead_phrase.py). pytest runs
with an existing interpreter instead: this service's own .venv when it has
one, else the main checkout's services/wheelhouse/.venv (a worktree under
.claude/worktrees/ has none). The cache provider is off so the run writes no
.pytest_cache into the tree.

WHY THIS GATE PRINTS THE FAILURE REASONS: a catch counts only when the
expected assertion fired, not an unrelated exception upstream of it
(mutation-gate skill). The runner prints the failed names only.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner  # noqa: E402


def _interpreter() -> Path:
    """This service's .venv python, else the main checkout's."""
    own = SERVICE / ".venv" / "Scripts" / "python.exe"
    if own.is_file():
        return own
    # A worktree lives at <main>/.claude/worktrees/<name>.
    if ROOT.parent.name == "worktrees" and ROOT.parents[1].name == ".claude":
        return ROOT.parents[2] / "services/wheelhouse/.venv/Scripts/python.exe"
    return own


def _launch(service, test_file, report, collect):
    """Start pytest with an existing interpreter, never ``uv run``."""
    python = _interpreter()
    if not python.is_file():
        # Raised as OSError so the runner reports an error, never a verdict.
        raise OSError(f"no interpreter for {service.name}: {python}")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        "-p", "no:cacheprovider",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_plain_run_pytest = runner._run_pytest


def _run_pytest_showing_reasons(service, test_file):
    """The runner's _run_pytest, echoing each failure's reason line."""
    result = _plain_run_pytest(service, test_file)
    out = result.stdout + result.stderr
    summary = out.split("short test summary info", 1)
    if len(summary) == 2:
        for line in summary[1].splitlines():
            if line.startswith(("FAILED", "ERROR")):
                print(f"    reason: {line[:400]}")
    return result


runner._run_pytest = _run_pytest_showing_reasons

# One selection for every mutation: the three whole files are fast (about
# three seconds together) and double as the sanity suite.
TESTS = (
    "tests/test_click_executor.py",
    "tests/test_click_notice.py",
    "tests/test_uia_walker.py",
)

# Executor tests (tests/test_click_executor.py)
TOGGLE_OK = "test_toggle_capable_target_without_invoke_is_pressed_through_toggle"
SELECT_OK = "test_select_capable_target_without_invoke_is_pressed_through_select"
TREE = "test_tree_item_with_selection_item_and_no_invoke_never_calls_select"
TREE_TOGGLE = "test_tree_item_with_toggle_and_no_invoke_never_calls_toggle"
BOTH_ABSENT = "test_select_unavailable_falls_through_to_the_default_action"
BOTH_PATTERNS = "test_target_offering_toggle_and_selection_item_is_only_toggled"
COM_ERROR = "test_press_com_error_after_the_call_fails_closed"
KNOB = "test_toggle_com_error_fails_closed_even_with_the_com_error_knob_on"
NON_COM = "test_toggle_non_com_exception_with_an_allowlisted_hresult_fails_closed"
NO_SIDE_EFFECT = "test_press_no_side_effect_hresult_takes_one_coordinate_click"
INELIGIBLE = (
    "test_press_no_side_effect_hresult_on_an_ineligible_match_fails_closed"
)
NOT_LANDED = "test_press_coordinate_retry_that_does_not_land_has_its_own_tag"
BADGE_FIRST = (
    "test_badge_pick_on_a_toggle_capable_target_clicks_the_coordinate_first"
)
BADGE_NO_INPUT = (
    "test_badge_pick_refused_before_input_goes_to_the_default_action_not_toggle"
)
BADGE_INELIGIBLE = (
    "test_ineligible_badge_pick_goes_to_the_default_action_not_toggle"
)
SHELL_NO_INPUT = (
    "test_shell_badge_pick_refused_before_input_goes_to_the_default_action"
    "_not_toggle"
)
TREE_NO_READ = "test_tree_item_without_invoke_reads_no_toggle_or_select_pattern"

# Walker tests (tests/test_uia_walker.py)
W_TOGGLE = "test_toggle_press_toggles_through_the_live_toggle_pattern"
W_TOGGLE_ABSENT = "test_toggle_press_raises_unavailable_without_a_toggle_pattern"
W_TOGGLE_NULL = "test_toggle_press_treats_a_null_pointer_as_unavailable"
W_TOGGLE_RAISES = "test_toggle_press_lets_a_raising_toggle_propagate"
W_SELECT = "test_select_press_selects_through_the_live_selection_item_pattern"
W_SELECT_ABSENT = (
    "test_select_press_raises_unavailable_without_a_selection_item_pattern"
)
W_SELECT_NULL = "test_select_press_treats_a_null_pointer_as_unavailable"
W_SELECT_RAISES = "test_select_press_lets_a_raising_select_propagate"

# Notice tests (tests/test_click_notice.py)
NOTICE = "test_wording_execution_failed_toggle_and_select_reasons"

MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TESTS,
                          file=SERVICE / file, old=old, new=new,
                          expect=list(expect)))


EXECUTOR_SRC = "ui/click_executor.py"
WALKER_SRC = "ui/uia_walker.py"
WORDING_SRC = "click_notice_toast_wording.py"

TOGGLE_GUARD = (
    "        if winner.control_type_id not in "
    "_TOGGLE_PRESS_EXCLUDED_CONTROL_TYPES:\n"
)
TOGGLE_CALL = "                self._toggle_fn(winner.control_ref)\n"
SELECT_CALL = "                self._select_fn(winner.control_ref)\n"
FAIL_CLOSED = "        return self._fail(winner, com_error_reason)\n"

# --- placement: before the default action, for by-name clicks only ----------

# The default action runs first and its success ends the click, so a
# checkbox is pressed through "Check" and a list item through "Double
# Click" -- the order the Boss ruling on A3 rejected.
add("default-action-before-toggle", EXECUTOR_SRC,
    TOGGLE_GUARD,
    "        dda_first = self._attempt_do_default_action(winner, snap, query)\n"
    '        if dda_first.outcome == "ok":\n'
    "            return dda_first\n"
    + TOGGLE_GUARD,
    TOGGLE_OK, SELECT_OK, BOTH_PATTERNS)
add("by-name-click-skips-the-pattern-presses", EXECUTOR_SRC,
    "            return self._attempt_pattern_press(winner, snap, query)\n",
    "            return self._attempt_do_default_action(winner, snap, query)\n",
    TOGGLE_OK, SELECT_OK, BOTH_ABSENT, COM_ERROR, NO_SIDE_EFFECT)

# --- the two presses and their order ----------------------------------------

add("toggle-never-tried", EXECUTOR_SRC,
    TOGGLE_CALL,
    '                raise TogglePatternUnavailable("mutant")\n',
    TOGGLE_OK, BOTH_PATTERNS)
add("select-never-tried", EXECUTOR_SRC,
    SELECT_CALL,
    '                raise SelectionItemPatternUnavailable("mutant")\n',
    SELECT_OK, BOTH_ABSENT)
# A Toggle that worked does not end the click: Select (or the default
# action) presses the same target a second time.
add("toggle-success-falls-through", EXECUTOR_SRC,
    "            else:\n"
    "                logger.debug(\n"
    '                    "click_element Toggle succeeded (toggle_ok): matched=%r",\n'
    "                    winner.name,\n"
    "                )\n"
    "                return ClickResult(\n",
    "            else:\n"
    "                logger.debug(\n"
    '                    "click_element Toggle succeeded (toggle_ok): matched=%r",\n'
    "                    winner.name,\n"
    "                )\n"
    "            if False:\n"
    "                return ClickResult(\n",
    TOGGLE_OK, BOTH_PATTERNS)
add("select-success-falls-through", EXECUTOR_SRC,
    "            else:\n"
    "                logger.debug(\n"
    '                    "click_element Select succeeded (select_ok): matched=%r",\n'
    "                    winner.name,\n"
    "                )\n"
    "                return ClickResult(\n",
    "            else:\n"
    "                logger.debug(\n"
    '                    "click_element Select succeeded (select_ok): matched=%r",\n'
    "                    winner.name,\n"
    "                )\n"
    "            if False:\n"
    "                return ClickResult(\n",
    SELECT_OK)

# --- the TreeItem exclusion (Boss ruling R1) --------------------------------

add("treeitem-select-exclusion-emptied", EXECUTOR_SRC,
    "_SELECT_PRESS_EXCLUDED_CONTROL_TYPES = frozenset({UIA_TREEITEM})\n",
    "_SELECT_PRESS_EXCLUDED_CONTROL_TYPES = frozenset()\n",
    TREE, TREE_NO_READ)
add("treeitem-select-exclusion-check-removed", EXECUTOR_SRC,
    "        if winner.control_type_id not in "
    "_SELECT_PRESS_EXCLUDED_CONTROL_TYPES:\n",
    "        if True:\n",
    TREE, TREE_NO_READ)
add("treeitem-select-exclusion-inverted", EXECUTOR_SRC,
    "        if winner.control_type_id not in "
    "_SELECT_PRESS_EXCLUDED_CONTROL_TYPES:\n",
    "        if winner.control_type_id in "
    "_SELECT_PRESS_EXCLUDED_CONTROL_TYPES:\n",
    SELECT_OK, TREE, BOTH_ABSENT, TREE_NO_READ)

# --- the TreeItem Toggle exclusion (BOSS RULING 01:46) -----------------------

add("treeitem-toggle-exclusion-emptied", EXECUTOR_SRC,
    "_TOGGLE_PRESS_EXCLUDED_CONTROL_TYPES = frozenset({UIA_TREEITEM})\n",
    "_TOGGLE_PRESS_EXCLUDED_CONTROL_TYPES = frozenset()\n",
    TREE_TOGGLE, TREE_NO_READ)
add("treeitem-toggle-exclusion-check-removed", EXECUTOR_SRC,
    TOGGLE_GUARD,
    "        if True:\n",
    TREE_TOGGLE, TREE_NO_READ)
add("treeitem-toggle-exclusion-inverted", EXECUTOR_SRC,
    TOGGLE_GUARD,
    "        if winner.control_type_id in "
    "_TOGGLE_PRESS_EXCLUDED_CONTROL_TYPES:\n",
    TOGGLE_OK, BOTH_PATTERNS, TREE_TOGGLE, TREE_NO_READ)

# --- badge picks keep today's order (Boss ruling R2) ------------------------

BADGE_DDA_GUARD = (
    "            if badge_pick or skip_pattern_presses:\n"
    "                # A badge pick keeps today's order"
)
add("ineligible-badge-pick-takes-the-pattern-presses", EXECUTOR_SRC,
    BADGE_DDA_GUARD,
    BADGE_DDA_GUARD.replace("badge_pick or skip_pattern_presses",
                            "skip_pattern_presses"),
    BADGE_INELIGIBLE)
add("badge-no-input-fallback-takes-the-pattern-presses", EXECUTOR_SRC,
    "                    return self._attempt_do_default_action(winner, snap, query)\n",
    "                    return self._attempt_pattern_press(winner, snap, query)\n",
    BADGE_NO_INPUT)
# The shell-owned badge pick whose coordinate-first attempt sent no input
# (wh-tray-invoke-noop) reaches _handle_invoke_error with badge_pick False;
# each of these three lets it take the Toggle / Select presses.
add("shell-no-input-badge-pick-takes-the-pattern-presses", EXECUTOR_SRC,
    BADGE_DDA_GUARD,
    BADGE_DDA_GUARD.replace("badge_pick or skip_pattern_presses",
                            "badge_pick"),
    SHELL_NO_INPUT)
add("shell-no-input-flag-not-set", EXECUTOR_SRC,
    "                    skip_pattern_presses=True,\n",
    "                    skip_pattern_presses=False,\n",
    SHELL_NO_INPUT)
add("shell-no-input-flag-not-passed-through", EXECUTOR_SRC,
    "                skip_pattern_presses=skip_pattern_presses,\n",
    "",
    SHELL_NO_INPUT)
# An eligible badge pick tries the pattern presses before its coordinate
# click (wh-electron-dda-noop order broken).
add("pattern-press-ahead-of-badge-coordinate-first", EXECUTOR_SRC,
    "            if badge_pick and self._coord_eligible(winner, query):\n",
    "            if badge_pick:\n"
    "                pressed = self._attempt_pattern_press(winner, snap, query)\n"
    '                if pressed.outcome == "ok":\n'
    "                    return pressed\n"
    "            if badge_pick and self._coord_eligible(winner, query):\n",
    BADGE_FIRST, BADGE_NO_INPUT, BADGE_INELIGIBLE)

# --- the honesty contract after a press call raised (A4) --------------------

# A raise from Toggle() / Select() treated as "no pattern": the next press
# path runs on a target that may already have toggled.
add("toggle-error-treated-as-structural", EXECUTOR_SRC,
    "            except TogglePatternUnavailable:\n",
    "            except Exception:\n",
    COM_ERROR, KNOB, NON_COM, NO_SIDE_EFFECT, INELIGIBLE, NOT_LANDED)
# Toggle retried after its call raised: a Toggle that fired and then raised
# is toggled back by the retry.
add("toggle-retried-after-an-error", EXECUTOR_SRC,
    "            except Exception as exc:  # noqa: BLE001 -- COM raises broad exceptions\n"
    "                return self._pattern_press_error(\n"
    "                    exc,\n"
    "                    winner,\n"
    "                    snap,\n"
    "                    query,\n"
    '                    pattern_name="Toggle",\n',
    "            except Exception as exc:  # noqa: BLE001 -- COM raises broad exceptions\n"
    "                try:\n"
    "                    self._toggle_fn(winner.control_ref)\n"
    "                except Exception:  # noqa: BLE001\n"
    "                    pass\n"
    "                return self._pattern_press_error(\n"
    "                    exc,\n"
    "                    winner,\n"
    "                    snap,\n"
    "                    query,\n"
    '                    pattern_name="Toggle",\n',
    COM_ERROR, KNOB, NON_COM, NO_SIDE_EFFECT)
add("select-error-treated-as-structural", EXECUTOR_SRC,
    "            except SelectionItemPatternUnavailable:\n",
    "            except Exception:\n",
    COM_ERROR, NO_SIDE_EFFECT, INELIGIBLE, NOT_LANDED)
# The default action after a fired Toggle reverts the checkbox.
add("default-action-after-a-press-error", EXECUTOR_SRC,
    FAIL_CLOSED,
    "        return self._attempt_do_default_action(winner, snap, query)\n",
    COM_ERROR, KNOB, NON_COM, INELIGIBLE)
add("success-reported-when-the-press-raised", EXECUTOR_SRC,
    FAIL_CLOSED,
    "        return ClickResult(\n"
    '            outcome="ok", reason=None, matched_name=winner.name,\n'
    '            clicked_via="invoke",\n'
    "        )\n",
    COM_ERROR, KNOB, NON_COM, INELIGIBLE)
add("fail-closed-under-the-wrong-reason", EXECUTOR_SRC,
    FAIL_CLOSED,
    '        return self._fail(winner, "invoke_com_error")\n',
    COM_ERROR, KNOB, NON_COM, INELIGIBLE)

# --- the one coordinate-click exception -------------------------------------

NO_SIDE_EFFECT_TEST = (
    "        if self._com_error_predicate(exc):\n"
    "            hresult = _hresult_of(exc)\n"
    "            if is_no_side_effect_hresult(hresult) and self._coord_eligible("
    "  # type: ignore[arg-type]\n"
    "                winner, query\n"
    "            ):\n"
)
add("no-side-effect-exception-removed", EXECUTOR_SRC,
    NO_SIDE_EFFECT_TEST,
    NO_SIDE_EFFECT_TEST.replace(
        "            if is_no_side_effect_hresult(hresult) and",
        "            if False and"),
    NO_SIDE_EFFECT, NOT_LANDED)
add("gate-zero-removed", EXECUTOR_SRC,
    NO_SIDE_EFFECT_TEST,
    NO_SIDE_EFFECT_TEST.replace(
        "        if self._com_error_predicate(exc):\n",
        "        if True:\n"),
    NON_COM)
add("eligibility-gate-removed", EXECUTOR_SRC,
    NO_SIDE_EFFECT_TEST,
    NO_SIDE_EFFECT_TEST.replace(
        " and self._coord_eligible(  # type: ignore[arg-type]\n"
        "                winner, query\n"
        "            ):\n",
        ":  # type: ignore[arg-type]\n"),
    INELIGIBLE)
add("coordinate-click-honours-the-com-error-knob", EXECUTOR_SRC,
    NO_SIDE_EFFECT_TEST,
    NO_SIDE_EFFECT_TEST.replace(
        "            if is_no_side_effect_hresult(hresult) and",
        "            if (is_no_side_effect_hresult(hresult)"
        " or self._enable_coordinate_click_on_com_error) and"),
    KNOB)

# --- the reason tags ---------------------------------------------------------

add("delivery-tag-collapsed-onto-the-press-tag", EXECUTOR_SRC,
    "                    winner, snap, fail_reason=sendinput_reason\n",
    "                    winner, snap, fail_reason=com_error_reason\n",
    NOT_LANDED)
add("toggle-tag-renamed", EXECUTOR_SRC,
    '                    com_error_reason="toggle_com_error",\n',
    '                    com_error_reason="invoke_com_error",\n',
    COM_ERROR, KNOB, NON_COM, INELIGIBLE)
add("select-tag-renamed", EXECUTOR_SRC,
    '                    com_error_reason="select_com_error",\n',
    '                    com_error_reason="invoke_com_error",\n',
    COM_ERROR, INELIGIBLE)
add("toggle-delivery-tag-renamed", EXECUTOR_SRC,
    '                    sendinput_reason="toggle_then_sendinput_failed",\n',
    '                    sendinput_reason="invoke_then_sendinput_failed",\n',
    NOT_LANDED)
add("select-delivery-tag-renamed", EXECUTOR_SRC,
    '                    sendinput_reason="select_then_sendinput_failed",\n',
    '                    sendinput_reason="invoke_then_sendinput_failed",\n',
    NOT_LANDED)

# --- the real seams in the walker -------------------------------------------

add("toggle-seam-asks-for-the-wrong-pattern", WALKER_SRC,
    '    pattern_id = _uia_const("UIA_TogglePatternId", 10015)\n'
    "    pattern = _typed_pattern(\n"
    '        element, "GetCurrentPattern", pattern_id, _toggle_pattern_class\n',
    '    pattern_id = _uia_const("UIA_SelectionItemPatternId", 10010)\n'
    "    pattern = _typed_pattern(\n"
    '        element, "GetCurrentPattern", pattern_id, _toggle_pattern_class\n',
    W_TOGGLE, W_TOGGLE_ABSENT)
add("toggle-seam-silent-when-absent", WALKER_SRC,
    "    if pattern is None:\n"
    '        raise TogglePatternUnavailable("control exposes no UIA Toggle pattern")\n',
    "    if pattern is None:\n"
    "        return\n",
    W_TOGGLE_ABSENT, W_TOGGLE_NULL)
add("toggle-seam-maps-a-raise-to-unavailable", WALKER_SRC,
    "    pattern.Toggle()\n",
    "    try:\n"
    "        pattern.Toggle()\n"
    "    except Exception as exc:  # noqa: BLE001\n"
    '        raise TogglePatternUnavailable("mutant") from exc\n',
    W_TOGGLE_RAISES)
add("select-seam-asks-for-the-wrong-pattern", WALKER_SRC,
    '    pattern_id = _uia_const("UIA_SelectionItemPatternId", 10010)\n'
    "    pattern = _typed_pattern(\n"
    "        element, \"GetCurrentPattern\", pattern_id, "
    "_selection_item_pattern_class\n",
    '    pattern_id = _uia_const("UIA_TogglePatternId", 10015)\n'
    "    pattern = _typed_pattern(\n"
    "        element, \"GetCurrentPattern\", pattern_id, "
    "_selection_item_pattern_class\n",
    W_SELECT, W_SELECT_ABSENT)
add("select-seam-silent-when-absent", WALKER_SRC,
    "    if pattern is None:\n"
    "        raise SelectionItemPatternUnavailable(\n",
    "    if pattern is None:\n"
    "        return\n"
    "        raise SelectionItemPatternUnavailable(\n",
    W_SELECT_ABSENT, W_SELECT_NULL)
add("select-seam-maps-a-raise-to-unavailable", WALKER_SRC,
    "    pattern.Select()\n",
    "    try:\n"
    "        pattern.Select()\n"
    "    except Exception as exc:  # noqa: BLE001\n"
    '        raise SelectionItemPatternUnavailable("mutant") from exc\n',
    W_SELECT_RAISES)
add("toggle-seam-reads-the-cached-pattern", WALKER_SRC,
    '        element, "GetCurrentPattern", pattern_id, _toggle_pattern_class\n',
    '        element, "GetCachedPattern", pattern_id, _toggle_pattern_class\n',
    W_TOGGLE)

# --- the notice wording for the four new reasons ----------------------------

for _reason in ("toggle_com_error", "select_com_error",
                "toggle_then_sendinput_failed",
                "select_then_sendinput_failed"):
    add(f"wording-{_reason}-alias-dropped", WORDING_SRC,
        f'        "{_reason}",\n',
        "",
        NOTICE)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
