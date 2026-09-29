"""Mutation gate for the (\\d+) number rule (wh-number-capture-enforce).

WHAT THE CHANGE DOES. speech/number_capture_rule.py holds two pure checks
that the Advanced-mode pattern editor runs before it enables Save:

  Rule B  number_form_error: a command expression (leading '^') must not
          match a number with \\d outside a (\\d+) or (?P<name>\\d+) body, a
          quantified (\\d+), a digit character class, or a list of digits
          or number words. A group that a grid_number_command step reads
          is exempt, so the mouse grid patterns keep their 1-9 lists.
  Rule A  count_capture_error: a number-kind step parameter that reads a
          capture group g<N> must read a (\\d+) group, optionally with '?'.

create_pattern_dialog.py::_validate_advanced shows Rule B under the
expression and Rule A under the steps; pattern_help_dialog.py carries the
"Numbers in commands" help section.

WHAT IT COVERS. Every mutation names the tests that must fail. Rule and
walker mutations run tests/test_number_capture_rule.py; editor and help
mutations run the TestNumberCaptureValidation and TestAdvancedExpressionField
classes of tests/test_create_pattern_dialog.py plus tests/test_pattern_help.py.

Equivalent mutations left out, with the reason:

  hk bool guard removed        a bool last param is never a str, so
                               _group_ref returns None for it either way
  hk isdigit / isinstance int  a digit string or an int is never a g<N>
                               reference, so the index list changes nothing
                               count_capture_error can see
  help html.escape removed     the paragraphs hold no '<', '>', or '&';
                               Qt renders a raw '"' as it renders '&quot;'

Run from services/wheelhouse with the interpreter the whole worktree uses.
In a worktree that is the main checkout's interpreter, so <main> below is
the main checkout's root:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_number_capture_rule.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_number_capture_rule.py

pytest starts as ``<this interpreter> -m pytest`` in the service directory,
never through scripts/run_tests.py, whose ``uv run`` builds a .venv inside a
git worktree. The shared runner
(services/stt_providers/shared/tests/mutation_gate_runner.py) owns
everything else: exactly-one pattern matches, compiled mutants, the
expected-name check against real collection, the green baseline, the
per-run timeout, byte-exact restoration, and the short-summary verdicts.

This gate adds one thing on top: after each run it prints the short-summary
FAILED lines, reason included (the runner sets COLUMNS=1000), so a reader
can confirm the expected assertion fired rather than an unrelated error.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
RULE = SERVICE / "speech/number_capture_rule.py"
DIALOG = SERVICE / "create_pattern_dialog.py"
HELP = SERVICE / "pattern_help_dialog.py"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner  # noqa: E402


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_plain_run_pytest = runner._run_pytest


def _run_pytest_showing_reasons(service, test_file):
    """The runner's own run, then the short-summary FAILED lines printed.

    Verdicts still come from the runner. This only shows the reason each
    test failed, so an AttributeError or an ImportError upstream of the
    guarded assertion cannot pass as a catch unseen.
    """
    result = _plain_run_pytest(service, test_file)
    if result is not None and result.returncode:
        lines = (result.stdout + result.stderr).splitlines()
        for index, line in enumerate(lines):
            if "short test summary info" in line:
                for failed in lines[index + 1:]:
                    if failed.startswith("FAILED"):
                        print("    " + failed.split("::", 1)[-1][:300])
                break
    return result


runner._run_pytest = _run_pytest_showing_reasons

RULE_TESTS = "tests/test_number_capture_rule.py"
UI_TESTS = (
    "tests/test_create_pattern_dialog.py::TestNumberCaptureValidation",
    "tests/test_create_pattern_dialog.py::TestAdvancedExpressionField",
    "tests/test_pattern_help.py",
)

# --- expected catchers ------------------------------------------------------
DIGIT_REFUSED = "test_digit_form_refused"
LIST_REFUSED = "test_number_list_refused"
FIRST_PART = "test_first_digit_form_in_the_expression_is_reported"
B_ACCEPTS = "test_rule_b_accepts"
REPLACEMENT = "test_replacement_expression_is_not_checked"
SHIPPED = "test_every_shipped_pattern_passes_both_checks"
GRID_PASSES = "test_grid_list_referenced_by_grid_number_command_passes"
OTHER_FUNCTION = "test_same_expression_with_another_function_is_refused"
OUTSIDE_GROUP = "test_offending_part_outside_the_referenced_group_is_refused"
A_ACCEPTS = "test_rule_a_accepts"
HK_WORD = "test_hk_repeat_on_word_group"
PRESS_LIST = "test_press_repeat_on_number_list"
SCROLL_DIGIT = "test_scroll_clicks_on_bare_digit_group"
NEWLINES_WORD = "test_insert_newlines_count_on_word_group"
REPEATED = "test_repeated_group_is_refused"
REPLACEMENT_A = "test_applies_to_replacements_too"
DISAGREE = "test_walk_that_disagrees_with_re_is_not_trusted"
ESCAPE_IN_CLASS = "test_bracket_first_in_class_and_escape_in_class"
BACKREFERENCE = "test_named_group_is_capturing_backreference_is_not"

UI_BARE_DIGIT = "test_bare_digit_form_shows_expression_error_and_blocks_save"
UI_LIST = "test_number_list_shows_expression_error_and_blocks_save"
UI_COUNT = "test_count_param_on_word_group_shows_steps_error_and_blocks_save"
UI_HK = "test_hk_repeat_on_word_group_blocks_save"
UI_GRID = "test_grid_number_list_read_by_grid_number_command_keeps_save"
UI_TYPE = "test_rule_b_error_keeps_the_type_error"
UI_GROUP_REF = "test_rule_b_error_keeps_the_group_ref_error"
UI_RULE_A = "test_rule_b_error_keeps_the_rule_a_error"
UI_COMPILE = "test_invalid_expression_shows_error_and_disables_save"
HELP_TOC = "test_numbers_section_is_in_the_toc_after_advanced_mode"
HELP_TEXT = "test_numbers_section_renders_the_exact_paragraphs"


def rule(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=RULE_TESTS, file=RULE,
                old=old, new=new, expect=expect)


def ui(name, target, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=UI_TESTS, file=target,
                old=old, new=new, expect=expect)


MUTATIONS = [
    # --- Rule B -------------------------------------------------------------
    rule("b-commands-only-check-removed",
         '    if not expression.startswith("^"):\n        return None\n',
         '    if False:\n        return None\n',
         [REPLACEMENT]),
    rule("b-commands-only-check-inverted",
         '    if not expression.startswith("^"):\n        return None\n',
         '    if expression.startswith("^"):\n        return None\n',
         [REPLACEMENT, DIGIT_REFUSED, LIST_REFUSED]),
    rule("b-d-plus-body-not-skipped",
         "        if index in number_bodies:\n            continue\n",
         "        if False:\n            continue\n",
         [B_ACCEPTS, SHIPPED]),
    rule("b-bad-quantifier-check-removed",
         '        if quantifier[:1] in ("*", "+", "{"):\n'
         "            findings.append((group.start, group.end + len(quantifier)))\n",
         "        if False:\n"
         "            findings.append((group.start, group.end + len(quantifier)))\n",
         [DIGIT_REFUSED]),
    rule("b-digit-escape-part-drops-quantifier",
         "        findings.append((index, index + 2 + len(quantifier)))\n",
         "        findings.append((index, index + 2))\n",
         [DIGIT_REFUSED]),
    rule("b-class-finding-removed",
         "        if cls.has_digit and not cls.negated and not cls.accepts_letters:\n",
         "        if False:\n",
         [DIGIT_REFUSED, FIRST_PART]),
    rule("b-negated-class-not-skipped",
         "        if cls.has_digit and not cls.negated and not cls.accepts_letters:\n",
         "        if cls.has_digit and not cls.accepts_letters:\n",
         [B_ACCEPTS]),
    # A class that also accepts every letter a to z saves (boss ruling
    # 02:13 2026-09-28).
    rule("b-letter-class-not-skipped",
         "        if cls.has_digit and not cls.negated and not cls.accepts_letters:\n",
         "        if cls.has_digit and not cls.negated:\n",
         [B_ACCEPTS]),
    rule("b-some-letters-count-as-every-letter",
         "    return all(compiled.fullmatch(c) for c in string.ascii_lowercase)\n",
         "    return any(compiled.fullmatch(c) for c in string.ascii_lowercase)\n",
         [DIGIT_REFUSED]),
    rule("b-letter-check-case-sensitive",
         "        compiled = re.compile(class_text, re.IGNORECASE)\n",
         "        compiled = re.compile(class_text)\n",
         [B_ACCEPTS]),
    rule("b-findings-not-sorted",
         "    for start, end in sorted(findings):\n",
         "    for start, end in findings:\n",
         [FIRST_PART]),
    rule("b-noncapturing-d-plus-accepted",
         "    for group in scan.capturing:\n"
         "        if _body(expression, group) != _NUMBER_BODY:\n",
         "    for group in scan.groups:\n"
         "        if _body(expression, group) != _NUMBER_BODY:\n",
         [DIGIT_REFUSED]),
    rule("b-named-group-not-a-number-body",
         "    for group in scan.capturing:\n"
         "        if _body(expression, group) != _NUMBER_BODY:\n",
         "    for group in scan.capturing:\n"
         "        if (_body(expression, group) != _NUMBER_BODY\n"
         '                or expression[group.start + 1] == "?"):\n',
         [B_ACCEPTS]),
    rule("b-number-list-check-removed",
         "        if _is_number_list(expression, group) and not is_exempt(\n",
         "        if False and not is_exempt(\n",
         [LIST_REFUSED, OTHER_FUNCTION]),
    rule("b-real-number-condition-removed",
         "    return any(is_digits(w) or w in _REAL_NUMBER_WORDS\n"
         "               for ws in words for w in ws)\n",
         "    return True\n",
         [B_ACCEPTS]),
    rule("b-all-numbers-condition-removed",
         "    if not all(ws and all(is_digits(w) or w in PHRASE_WORDS for w in ws)\n"
         "               for ws in words):\n"
         "        return False\n",
         "    if False:\n"
         "        return False\n",
         [B_ACCEPTS]),
    # Multi-word number phrases (Codex round 1, wh-number-capture-enforce.2.3).
    rule("b-alternative-not-split-into-words",
         "        [w for w in _WORD_SPLIT_RE.split(a) if w] for a in alternatives\n",
         "        [a] for a in alternatives\n",
         [LIST_REFUSED]),
    rule("b-hyphen-not-a-word-separator",
         '_WORD_SPLIT_RE = re.compile(r"[\\s-]+")\n',
         '_WORD_SPLIT_RE = re.compile(r"\\s+")\n',
         [LIST_REFUSED]),
    rule("b-empty-alternative-counted-as-number",
         "    if not all(ws and all(is_digits(w) or w in PHRASE_WORDS for w in ws)\n",
         "    if not all(all(is_digits(w) or w in PHRASE_WORDS for w in ws)\n",
         [B_ACCEPTS]),
    rule("b-alias-words-counted-as-real",
         '_REAL_NUMBER_WORDS = PHRASE_WORDS - ALIAS_WORDS - {"and"}\n',
         '_REAL_NUMBER_WORDS = PHRASE_WORDS - {"and"}\n',
         [B_ACCEPTS]),
    rule("b-and-counted-as-real",
         '_REAL_NUMBER_WORDS = PHRASE_WORDS - ALIAS_WORDS - {"and"}\n',
         "_REAL_NUMBER_WORDS = PHRASE_WORDS - ALIAS_WORDS\n",
         [B_ACCEPTS]),
    rule("b-exemption-removed",
         "        return any(s <= start and end <= e for s, e in exempt)\n",
         "        return False\n",
         [GRID_PASSES, SHIPPED]),
    rule("b-exemption-for-every-function",
         "        if step.get(\"function\") != GRID_NUMBER_FUNCTION:\n"
         "            continue\n",
         "        if False:\n"
         "            continue\n",
         [OTHER_FUNCTION]),
    rule("b-exemption-end-bound-dropped",
         "        return any(s <= start and end <= e for s, e in exempt)\n",
         "        return any(s <= start for s, e in exempt)\n",
         [OUTSIDE_GROUP]),

    # --- Rule A -------------------------------------------------------------
    rule("a-always-none",
         "    for step in steps:\n"
         '        function = step.get("function")\n'
         "        entry = CATALOG_BY_NAME.get(function)\n",
         "    return None\n"
         "    for step in steps:\n"
         '        function = step.get("function")\n'
         "        entry = CATALOG_BY_NAME.get(function)\n",
         [HK_WORD, PRESS_LIST, SCROLL_DIGIT, NEWLINES_WORD, REPEATED,
          REPLACEMENT_A]),
    rule("a-kind-check-removed",
         '        if catalog_params[index].get("kind") == "number"\n',
         "        if True\n",
         [A_ACCEPTS]),
    rule("a-kind-index-off-by-one",
         '        if catalog_params[index].get("kind") == "number"\n',
         '        if catalog_params[index - 1].get("kind") == "number"\n',
         [PRESS_LIST, SCROLL_DIGIT]),
    rule("a-hk-rule-removed",
         '    if function == "hk":\n',
         "    if False:\n",
         [HK_WORD]),
    rule("a-hk-group-ref-repeat-ignored",
         "            and (last.isdigit() or _GROUP_REF_RE.fullmatch(last))\n",
         "            and last.isdigit()\n",
         [HK_WORD]),
    rule("a-run-capture-rule-removed",
         '    if function == "run_capture":\n',
         "    if False:\n",
         [A_ACCEPTS]),
    rule("a-number-group-quantifier-check-removed",
         '    return _quantifier_at(expression, group.end)[:1] not in ("*", "+", "{")\n',
         "    return True\n",
         [REPEATED]),
    rule("a-optional-group-refused",
         '    return _quantifier_at(expression, group.end)[:1] not in ("*", "+", "{")\n',
         '    return _quantifier_at(expression, group.end)[:1] not in ("*", "+", "{", "?")\n',
         [A_ACCEPTS]),
    rule("a-named-group-not-a-number",
         "    if not group.capturing or _body(expression, group) != _NUMBER_BODY:\n",
         "    if (not group.capturing or expression[group.start + 1] == \"?\"\n"
         "            or _body(expression, group) != _NUMBER_BODY):\n",
         [A_ACCEPTS]),
    # The bound exists so that a g<N> past the group count cannot index
    # past the capture list: without it the ref-beyond-group-count case
    # raises IndexError, which is the failure the bound prevents.
    rule("a-group-ref-bound-dropped",
         "    return number if number <= group_count else None\n",
         "    return number\n",
         [A_ACCEPTS]),
    rule("a-number-group-skip-removed",
         "            if _is_number_group(expression, group):\n"
         "                continue\n",
         "            if False:\n"
         "                continue\n",
         [A_ACCEPTS]),
    rule("a-message-drops-quantifier",
         '            if quantifier[:1] in ("*", "+", "{"):\n'
         "                group_text += quantifier\n",
         "            if False:\n"
         "                group_text += quantifier\n",
         [REPEATED]),

    # --- the group walker ---------------------------------------------------
    rule("w-group-count-check-removed",
         "    if len(capturing) != compiled.groups:\n        return None\n",
         "    if False:\n        return None\n",
         [DISAGREE]),
    rule("w-escape-advances-one",
         '            if i + 1 < n and expression[i + 1] == "d":\n'
         "                digit_escapes.append(i)\n"
         "            i += 2\n",
         '            if i + 1 < n and expression[i + 1] == "d":\n'
         "                digit_escapes.append(i)\n"
         "            i += 1\n",
         [B_ACCEPTS]),
    rule("w-digit-escape-not-recorded",
         "                digit_escapes.append(i)\n",
         "                pass\n",
         [DIGIT_REFUSED]),
    rule("w-class-escape-advances-one",
         "                    j += 2\n                    continue\n",
         "                    j += 1\n                    continue\n",
         [ESCAPE_IN_CLASS]),
    rule("w-class-digit-escape-not-seen",
         '                    if j + 1 < n and expression[j + 1] == "d":\n'
         "                        has_digit = True\n",
         '                    if j + 1 < n and expression[j + 1] == "d":\n'
         "                        pass\n",
         [DIGIT_REFUSED]),
    rule("w-class-literal-digit-not-seen",
         '                if expression[j] in "0123456789":\n',
         "                if False:\n",
         [DIGIT_REFUSED, FIRST_PART]),
    rule("w-negation-not-seen",
         "                negated = True\n                j += 1\n",
         "                negated = False\n                j += 1\n",
         [B_ACCEPTS]),
    rule("w-named-body-starts-at-the-paren",
         "                    group = _Group(i, close + 1, capturing=True)\n",
         "                    group = _Group(i, i + 1, capturing=True)\n",
         [B_ACCEPTS, A_ACCEPTS]),
    rule("w-backreference-not-an-atom",
         '                elif rest.startswith("P=") or rest.startswith("#"):\n',
         '                elif rest.startswith("#"):\n',
         [BACKREFERENCE]),

    # --- the editor ---------------------------------------------------------
    ui("d-rule-b-call-removed", DIALOG,
       "            expr_error = number_form_error(\n"
       "                self._expression_edit.text(), self._steps_editor.steps(),\n"
       "            )\n",
       "            expr_error = None\n",
       [UI_BARE_DIGIT, UI_LIST]),
    ui("d-rule-b-without-the-steps", DIALOG,
       "            expr_error = number_form_error(\n"
       "                self._expression_edit.text(), self._steps_editor.steps(),\n"
       "            )\n",
       "            expr_error = number_form_error(\n"
       "                self._expression_edit.text(), [],\n"
       "            )\n",
       [UI_GRID]),
    ui("d-rule-a-call-removed", DIALOG,
       "                steps_error = count_capture_error(\n"
       "                    self._expression_edit.text(), self._steps_editor.steps(),\n"
       "                )\n",
       "                steps_error = None\n",
       [UI_COUNT, UI_HK, UI_RULE_A]),
    ui("d-rule-b-runs-over-a-compile-error", DIALOG,
       "        if expr_error is None:\n"
       r"            # A command must capture every number with (\d+)" "\n",
       "        if True:\n"
       r"            # A command must capture every number with (\d+)" "\n",
       [UI_COMPILE]),
    ui("d-type-check-gated-on-rule-b", DIALOG,
       "        type_error = None if compile_error else self._advanced_type_error()\n",
       "        type_error = None if expr_error else self._advanced_type_error()\n",
       [UI_TYPE]),
    ui("d-group-ref-gated-on-rule-b", DIALOG,
       "            if steps_error is None and compile_error is None:\n"
       "                # Group-ref range check only once the expression compiles;\n",
       "            if steps_error is None and expr_error is None:\n"
       "                # Group-ref range check only once the expression compiles;\n",
       [UI_GROUP_REF]),
    ui("d-rule-a-gated-on-rule-b", DIALOG,
       "            if steps_error is None and compile_error is None:\n"
       r"                # A count parameter must read a (\d+) group" "\n",
       "            if steps_error is None and expr_error is None:\n"
       r"                # A count parameter must read a (\d+) group" "\n",
       [UI_RULE_A]),

    # --- the help page ------------------------------------------------------
    ui("h-numbers-section-removed", HELP,
       '    <h3><a name="numbers">Numbers in commands</a></h3>\n'
       "    {numbers_paragraphs}\n\n",
       "",
       [HELP_TOC, HELP_TEXT]),
    ui("h-toc-entry-removed", HELP,
       '    ("numbers", "Numbers in commands"),\n',
       "",
       [HELP_TOC]),
    ui("h-second-paragraph-dropped", HELP,
       '        f"<p>{html.escape(text)}</p>" for text in _NUMBERS_PARAGRAPHS\n',
       '        f"<p>{html.escape(text)}</p>" for text in _NUMBERS_PARAGRAPHS[:1]\n',
       [HELP_TEXT]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
