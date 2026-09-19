"""Mutation evidence for the Expand / Collapse default-action divert
(wh-treeitem-dda-wrong-action).

A voice click on an expandable tree item must not fire an MSAA default action
that means Expand or Collapse; the executor takes its guarded coordinate click
instead. This gate breaks the detection (the verb comparison and the loader
that builds the verb set), the executor branch that diverts, and the
coordinate-verification gate the diverted click passes through, and requires
the named tests to fail for each.

Run from services/wheelhouse:

    python tests/mutation_gate_treeitem_expand_collapse.py --check
    python tests/mutation_gate_treeitem_expand_collapse.py

The selection is whole-test node IDs, not the two test files, because
tests/test_uia_walker.py holds a live-desktop test that is skipped on a
headless run. The node IDs name tests without their parameter list, so every
case of a parametrized test runs; the runner strips "[...]" before it matches
an expected name.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

WALKER = "tests/test_uia_walker.py::"
EXECUTOR = "tests/test_click_executor.py::"
NOTICE = "tests/test_click_notice.py::"

# Walker tests
RAISES = "test_dda_via_pattern_expand_collapse_raises_without_pressing"
GENUINE = "test_dda_via_pattern_genuine_default_action_still_presses"
LOCALIZED_SET = "test_expand_collapse_verbs_include_localized_names"
LOCALIZED_RAISES = "test_dda_via_pattern_localized_expand_collapse_raises_without_pressing"
FLOOR = "test_expand_collapse_verbs_english_floor_when_nothing_loads"
LOAD_FAILURE = "test_load_string_resource_failure_returns_none_and_never_raises"
NULL_MODULE = "test_load_string_resource_missing_dll_never_reads_a_string"
FREES = "test_load_string_resource_frees_the_module_after_a_missing_resource"
API_UNAVAILABLE = "test_expand_collapse_verbs_fall_back_to_floor_when_api_unavailable"
REAL_STRINGS = "test_load_string_resource_reads_real_windows_strings"

# Executor tests
COORD = "test_tree_item_expand_collapse_takes_coordinate_path_not_dda"
GENUINE_EXEC = "test_genuine_default_action_still_fires_dda"
INELIGIBLE = "test_expand_collapse_ineligible_match_refuses_without_pressing"
CHAIN = "test_expand_collapse_coordinate_click_not_landing_reports_chain_reason"
CLICK_POINT = "test_expand_collapse_click_point_check_refuses_without_input"
REVERIFY = "test_expand_collapse_reverification_failure_refuses_without_input"

# Notice tests
NOTICE_PERMANENT = "test_wording_execution_failed_dda_expand_collapse"
NOTICE_TRANSIENT = "test_wording_execution_failed_dda_expand_collapse_then_sendinput_failed"

TESTS = (
    *(WALKER + t for t in (
        RAISES, GENUINE, LOCALIZED_SET, LOCALIZED_RAISES, FLOOR, LOAD_FAILURE,
        NULL_MODULE, FREES, API_UNAVAILABLE, REAL_STRINGS)),
    *(EXECUTOR + t for t in (
        COORD, GENUINE_EXEC, INELIGIBLE, CHAIN, CLICK_POINT, REVERIFY)),
    *(NOTICE + t for t in (NOTICE_PERMANENT, NOTICE_TRANSIENT)),
)

MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TESTS,
                          file=SERVICE / file, old=old, new=new, expect=list(expect)))


WALKER_SRC = "ui/uia_walker.py"
EXECUTOR_SRC = "ui/click_executor.py"
WORDING_SRC = "click_notice_toast_wording.py"

# --- the comparison itself --------------------------------------------------

COMPARE = ("    if default_action.casefold() in _expand_collapse_verbs():\n"
           "        raise DefaultActionIsExpandCollapse(default_action)\n")

add("compare-negated", WALKER_SRC, COMPARE,
    COMPARE.replace(" in _expand", " not in _expand"),
    RAISES, GENUINE, COORD, GENUINE_EXEC)
add("compare-without-casefold", WALKER_SRC, COMPARE,
    COMPARE.replace("default_action.casefold() in", "default_action in"),
    RAISES, LOCALIZED_RAISES, COORD)
add("detection-never-fires", WALKER_SRC, COMPARE,
    "    if False:\n"
    "        raise DefaultActionIsExpandCollapse(default_action)\n",
    RAISES, LOCALIZED_RAISES, FLOOR, COORD)
add("detection-always-fires", WALKER_SRC, COMPARE,
    "    if True:\n"
    "        raise DefaultActionIsExpandCollapse(default_action)\n",
    GENUINE, GENUINE_EXEC)
# A prefix match would also divert "Expand all", a genuine action.
add("compare-by-prefix", WALKER_SRC, COMPARE,
    "    if any(default_action.casefold().startswith(v) for v in _expand_collapse_verbs()):\n"
    "        raise DefaultActionIsExpandCollapse(default_action)\n",
    GENUINE)
add("compare-english-floor-only", WALKER_SRC, COMPARE,
    COMPARE.replace("_expand_collapse_verbs()", "_EXPAND_COLLAPSE_ENGLISH_FLOOR"),
    LOCALIZED_RAISES)
add("press-before-the-check", WALKER_SRC,
    COMPARE + "\n    pattern.DoDefaultAction()\n",
    "    pattern.DoDefaultAction()\n" + COMPARE + "\n",
    RAISES, LOCALIZED_RAISES, FLOOR, COORD)

# --- the verb-set loader ----------------------------------------------------

add("loader-drops-english-floor", WALKER_SRC,
    "    verbs = set(_EXPAND_COLLAPSE_ENGLISH_FLOOR)\n",
    "    verbs = set()\n",
    LOCALIZED_SET, FLOOR, API_UNAVAILABLE)
add("loader-keeps-case", WALKER_SRC,
    '        folded = (_load_string_resource(dll_name, string_id) or "").strip().casefold()\n',
    '        folded = (_load_string_resource(dll_name, string_id) or "").strip()\n',
    LOCALIZED_SET, LOCALIZED_RAISES)
add("loader-ignores-loaded-strings", WALKER_SRC,
    "        if folded:\n"
    "            verbs.add(folded)\n",
    "        if folded:\n"
    "            pass\n",
    LOCALIZED_SET, LOCALIZED_RAISES)
for dll, sid, wrong, verb in (("oleaccrc.dll", 305, 304, "Expand"),
                              ("oleaccrc.dll", 306, 307, "Collapse"),
                              ("UIAutomationCore.dll", 204, 203, "Expand"),
                              ("UIAutomationCore.dll", 205, 206, "Collapse")):
    add(f"loader-wrong-id-{dll.split('.')[0].lower()}-{sid}", WALKER_SRC,
        f'    ("{dll}", {sid}),  # {verb}\n',
        f'    ("{dll}", {wrong}),  # {verb}\n',
        LOCALIZED_SET)
add("loader-except-narrowed", WALKER_SRC,
    "    except Exception as exc:  # noqa: BLE001 -- the loader must never raise\n",
    "    except ZeroDivisionError as exc:  # noqa: BLE001 -- the loader must never raise\n",
    LOAD_FAILURE, API_UNAVAILABLE)
add("loader-accepts-zero-length", WALKER_SRC,
    "    if length <= 0:\n",
    "    if length < 0:\n",
    LOAD_FAILURE)
# A NULL module handle given to LoadStringW reads the executable's own string
# table, so the NULL check must stop the read.
add("loader-reads-through-null-module", WALKER_SRC,
    "        if not module:\n"
    "            logger.warning(\n"
    '                "Expand/Collapse verb load: LoadLibraryExW(%s) failed "\n',
    "        if False:\n"
    "            logger.warning(\n"
    '                "Expand/Collapse verb load: LoadLibraryExW(%s) failed "\n',
    NULL_MODULE)
add("loader-never-frees-module", WALKER_SRC,
    "        finally:\n"
    "            api.FreeLibrary(module)\n",
    "        finally:\n"
    "            pass\n",
    FREES)

# --- the executor branch that diverts ---------------------------------------

DIVERT = ("            return self._structural_absence_coordinate_fallback(\n"
          '                winner, snap, query, structural_reason="dda_expand_collapse"\n'
          "            )\n")

add("executor-divert-fails-closed", EXECUTOR_SRC, DIVERT,
    '            return self._fail(winner, "dda_expand_collapse")\n',
    COORD, CHAIN)
add("executor-divert-skips-eligibility", EXECUTOR_SRC, DIVERT,
    "            return self._coordinate_fallback(\n"
    '                winner, snap, fail_reason="dda_expand_collapse_then_sendinput_failed"\n'
    "            )\n",
    INELIGIBLE)
add("executor-divert-wrong-reason", EXECUTOR_SRC, DIVERT,
    DIVERT.replace('"dda_expand_collapse"', '"dda_no_default_action"'),
    COORD, INELIGIBLE, CHAIN)
add("executor-except-narrowed", EXECUTOR_SRC,
    "        except DefaultActionIsExpandCollapse as exc:\n",
    "        except ZeroDivisionError as exc:\n",
    COORD, INELIGIBLE, CHAIN)

# --- the coordinate-verification gate the diverted click passes -------------

add("gate-eligibility-skipped", EXECUTOR_SRC,
    "        if not self._coord_eligible(winner, query):\n"
    "            return self._fail(winner, structural_reason)\n",
    "        if False:\n"
    "            return self._fail(winner, structural_reason)\n",
    INELIGIBLE)
add("gate-reverify-ignored", EXECUTOR_SRC,
    "        verdict = self._verify(winner, snap)\n"
    "        if verdict is not None:\n",
    "        verdict = self._verify(winner, snap)\n"
    "        if False:\n",
    REVERIFY)
add("gate-root-window-check-skipped", EXECUTOR_SRC,
    "        if root_at_point != expected_root:\n",
    "        if False:\n",
    CLICK_POINT)
add("gate-element-at-point-check-skipped", EXECUTOR_SRC,
    "        if not point_hits_winner:\n",
    "        if False:\n",
    CLICK_POINT)

# --- the notice wording for the two new reasons -----------------------------

add("wording-permanent-reason-dropped", WORDING_SRC,
    '        "dda_expand_collapse",\n',
    "",
    NOTICE_PERMANENT)
add("wording-transient-alias-dropped", WORDING_SRC,
    '        "dda_expand_collapse_then_sendinput_failed",\n',
    "",
    NOTICE_TRANSIENT)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
