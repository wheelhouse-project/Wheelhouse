r"""Mutation gate for number words in four commands
(wh-number-words-command-failures, parts A to D).

The user cannot choose whether the speech engine writes "5" or "five", so a
command must work with both forms. The change (bb878d46..706b049b) has four
parts, and this gate breaks each changed behavior once:

  Part A, key names (speech/actions.py, SPOKEN_KEY_MAP)
    key-two-removed              the "two" entry is gone, so "press control
                                 two" no longer presses Ctrl+2
    key-zero-wrong-value         "zero" maps to "1", so "press f zero" presses
                                 F and 1
    sound-alike-for-added        "for" joins the map as "4", so "press control
                                 for" would press Ctrl+4

  Part B, go counts (speech/navigation/parser.py, _try_relative)
    count-reads-one-token        only the first token after the direction is
                                 the count, so "twenty three" reads as 20
    unit-strip-stopped           the last token is never a unit, so "go left
                                 three words" is unparseable
    unit-ignored                 a unit is removed but the unit stays
                                 "character"
    unparseable-count-accepted   a segment whose count is not one number is
                                 accepted with a count of 1

  Part C, click a control named by a number (ui/confidence_scorer.py,
  ui/click_executor.py)
    name-exact-plain-equality    _name_exact is plain equality again
    same-number-guard-dropped    two names that are not numbers read as the
                                 same number (None == None)
    number-refusal-dropped       a name that starts with "number" is a number
    aliases-on                   "for" and "to" read as 4 and 2
    zero-off                     "zero" is no number
    digit-path-removed           digits do not take the int() path, so "0"
                                 is no number
    digit-path-returns-none      a digit name is no number
    coord-eligible-plain-equality  _coord_eligible compares names with plain
                                 equality again
    oh-refusal-dropped           a name with the word "oh" is a number again,
                                 so "one oh" is 10
    oh-refusal-ignores-hyphens   the "oh" check splits on spaces only, so
                                 "one-oh" is 10

  Part D, go up and go down (speech/config/patterns.toml)
    nav-up-times-old-pattern     the "go up" row needs "times" again
    nav-down-times-old-pattern   the "go down" row needs "times" again

patterns.toml is not Python, so --check skips the compile check for it.

Known limit: pytest names are compared without the parameter list, so the
name check cannot tell a mutation of the "go up" row from a mutation of the
"go down" row, or one parameter of a catcher from another. Read the
SURVIVED or caught line of a full sweep with that in mind.

Run from services/wheelhouse with the interpreter the whole worktree uses
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_number_words_commands.py --check
    .venv/Scripts/python.exe tests/mutation_gate_number_words_commands.py <name> ...
    .venv/Scripts/python.exe tests/mutation_gate_number_words_commands.py

``--check`` verifies, without running a mutant, that every pattern matches
exactly once, that every Python mutant compiles, and that every expected
catcher name is collected by pytest. Name words on the command line select
the mutations whose name contains the word (the shared runner's filter;
there is no --only flag), with or without --check.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
ACTIONS = SERVICE / "speech/actions.py"
PARSER = SERVICE / "speech/navigation/parser.py"
SCORER = SERVICE / "ui/confidence_scorer.py"
EXECUTOR = SERVICE / "ui/click_executor.py"
PATTERNS = SERVICE / "speech/config/patterns.toml"
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


def _failed_names(output: str):
    """The test names in pytest's short summary, parameter list removed.

    The shared runner cuts a summary line at its first space, so a
    parametrized case whose id holds a space ("test_x[go right 23]") is read
    as "test_x[go", which no expected name can match, and the mutation is
    reported as a survivor although the test failed. This version drops
    everything from the first "[", which is also the form _collected_names
    produces. Measured 2026-10-19 on the first --only run of this gate:
    test_words_and_digits_give_the_same_move failed on its "go ..." and
    "grab ..." cases and the runner listed it as "...[go" and "...[grab".
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "short test summary info" in line:
            lines = lines[index + 1:]
            break
    return {
        line.split("::")[-1].split()[0].split("[")[0]
        for line in lines
        if line.startswith("FAILED")
    }


runner._failed_names = _failed_names

TESTS_A = ("tests/test_actions.py", "tests/test_press_repeat_count.py")
TESTS_B = "tests/test_navigation_parser.py"
# test_click_executor.py is thousands of lines, so only the two tests of this
# change run from it.
TESTS_C = (
    "tests/test_confidence_scorer.py",
    "tests/test_click_executor.py::test_coord_eligible_number_word_matches_digit_name",
    "tests/test_click_executor.py::test_coord_eligible_sound_alike_is_not_a_number_match",
    "tests/test_click_executor.py::test_coord_eligible_oh_is_not_a_number_match",
)
TESTS_D = "tests/test_voice_access_aliases_1_14.py"

# Part A catchers.
KEYS_ARE_DIGITS = "test_number_words_are_digit_keys"
NOT_KEYS = "test_sound_alikes_and_ten_are_not_keys"
SEQUENCES = "test_other_sequences_are_unchanged"
SHIPPED_PATTERN = "test_through_the_shipped_pattern"

# Part B catchers.
SAME_MOVE = "test_words_and_digits_give_the_same_move"
CHAIN = "test_a_chain_with_multi_word_counts"
NOT_ONE_COUNT = "test_words_that_are_not_one_count_are_still_unparseable"
LEFT_UNIT = "test_go_left_with_count_and_unit"
ZERO_COUNT = "test_zero_is_not_a_cursor_count"
ABOVE_999 = "test_a_digit_count_above_nine_hundred_ninety_nine_is_dictation"

# Part C catchers.
EXACT_MATCH = "test_a_number_word_and_a_digit_name_are_an_exact_match"
NOT_A_MATCH = "test_a_sound_alike_or_another_number_is_not_a_match"
SCORE_SAME = "test_a_7_control_and_a_seven_control_score_the_same"
ROW_E = "test_row_e_substring_alone_ineligible"
SCORE_SUBSTRING = "test_score_name_substring"
SCORE_STARTS_WITH = "test_score_name_starts_with"
COORD_MATCH = "test_coord_eligible_number_word_matches_digit_name"
COORD_NO_SOUND_ALIKE = "test_coord_eligible_sound_alike_is_not_a_number_match"
NOT_OH = "test_the_word_oh_is_not_a_number_match"
COORD_NOT_OH = "test_coord_eligible_oh_is_not_a_number_match"

# Part D catchers.
PIPELINE = "test_go_up_or_down_and_a_count_presses_the_key_that_many_times"
ROW_OWNS_FORM = "test_the_named_row_owns_the_form"
LIVE_INDEX = "test_the_live_first_word_index_reaches_the_row"

# Source text. Each is one exact match in the current file.
TWO_KEY = '    "two": "2",\n'
ZERO_KEY = '    "zero": "0",\n'
NINE_KEY = '    "nine": "9",\n'

JOIN_COUNT = '            n = NavigationParser._parse_count(" ".join(tokens[pos:end]))\n'
UNIT_LOOKUP = "                u = _UNITS.get(last)\n"
UNIT_SET = "                unit = u\n"
COUNT_REFUSAL = (
    "            if n is None:\n"
    "                return None\n"
    "            count = n\n"
)

NAME_EXACT = (
    "    return query_name == match_name or names_are_same_number(\n"
    "        query_name, match_name\n"
    "    )\n"
)
SAME_NUMBER_GUARD = (
    "    if query_number is None:\n"
    "        return False\n"
)
NUMBER_REFUSAL = '    if not words or words[0] in ("number", "numbers"):\n'
OH_REFUSAL = (
    '    if "oh" in text.casefold().replace("-", " ").split():\n'
    "        return None\n"
)
NUMBER_READ = "    return parse_number_word(text, zero=True)\n"
DIGIT_TEST = "    if text.isascii() and text.isdigit():\n"
DIGIT_READ = "        return int(text)\n"
COORD_NAME_LINE = (
    "            if m_name == q_name or names_are_same_number(q_name, m_name):\n"
)

UP_ROW = r"pattern = '''^(?:go|move) up(?: (\d+)(?: times?)?)?$'''" + "\n"
DOWN_ROW = r"pattern = '''^(?:go|move) down(?: (\d+)(?: times?)?)?$'''" + "\n"


def mutation(name, file, old, new, expect, tests):
    return dict(name=name, service=SERVICE, test_file=tests, file=file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    # ---- Part A ------------------------------------------------------
    mutation("key-two-removed", ACTIONS, TWO_KEY, '    # "two": "2",\n',
             [KEYS_ARE_DIGITS, SEQUENCES, SHIPPED_PATTERN], TESTS_A),
    mutation("key-zero-wrong-value", ACTIONS, ZERO_KEY,
             ZERO_KEY.replace('"0"', '"1"'),
             [KEYS_ARE_DIGITS, SEQUENCES, SHIPPED_PATTERN], TESTS_A),
    mutation("sound-alike-for-added", ACTIONS, NINE_KEY,
             NINE_KEY + '    "for": "4",\n',
             [NOT_KEYS], TESTS_A),

    # ---- Part B ------------------------------------------------------
    mutation("count-reads-one-token", PARSER, JOIN_COUNT,
             "            n = NavigationParser._parse_count(tokens[pos])\n",
             [SAME_MOVE, CHAIN, NOT_ONE_COUNT], TESTS_B),
    # With the unit never stripped, the digit form of SAME_MOVE ("go right
    # 23 characters") is unparseable too, so that test fails on its
    # precondition line; CHAIN and LEFT_UNIT fail on the compared value.
    # The right and left unit lookup; wh-voice-chain-up-down moved it into
    # the else branch of the up and down unit lookup.
    mutation("unit-strip-stopped", PARSER, UNIT_LOOKUP,
             "                u = None\n",
             [CHAIN, LEFT_UNIT, SAME_MOVE], TESTS_B),
    mutation("unit-ignored", PARSER, UNIT_SET,
             '                unit = "character"\n',
             [CHAIN, LEFT_UNIT], TESTS_B),
    mutation("unparseable-count-accepted", PARSER, COUNT_REFUSAL,
             "            if n is not None:\n"
             "                count = n\n",
             [NOT_ONE_COUNT, ZERO_COUNT, ABOVE_999], TESTS_B),

    # ---- Part C ------------------------------------------------------
    mutation("name-exact-plain-equality", SCORER, NAME_EXACT,
             "    return query_name == match_name\n",
             [EXACT_MATCH, SCORE_SAME], TESTS_C),
    # Without the guard, two names that are not numbers are both None, so
    # every substring or starts-with pair reads as an exact match.
    mutation("same-number-guard-dropped", SCORER, SAME_NUMBER_GUARD,
             "    pass\n",
             [ROW_E, SCORE_SUBSTRING, SCORE_STARTS_WITH], TESTS_C),
    mutation("number-refusal-dropped", SCORER, NUMBER_REFUSAL,
             "    if not words:\n",
             [NOT_A_MATCH], TESTS_C),
    mutation("oh-refusal-dropped", SCORER, OH_REFUSAL,
             "    pass\n",
             [NOT_OH, COORD_NOT_OH], TESTS_C),
    mutation("oh-refusal-ignores-hyphens", SCORER, OH_REFUSAL,
             '    if "oh" in words:\n'
             "        return None\n",
             [NOT_OH, COORD_NOT_OH], TESTS_C),
    mutation("aliases-on", SCORER, NUMBER_READ,
             "    return parse_number_word(text, zero=True, aliases=True)\n",
             [NOT_A_MATCH, COORD_NO_SOUND_ALIKE], TESTS_C),
    mutation("zero-off", SCORER, NUMBER_READ,
             "    return parse_number_word(text, zero=False)\n",
             [EXACT_MATCH], TESTS_C),
    # parse_number_word("0") is None even with zero=True, so only the zero
    # cases need the int() path.
    mutation("digit-path-removed", SCORER, DIGIT_TEST,
             "    if False:\n",
             [EXACT_MATCH], TESTS_C),
    mutation("digit-path-returns-none", SCORER, DIGIT_READ,
             "        return None\n",
             [EXACT_MATCH, COORD_MATCH], TESTS_C),
    mutation("coord-eligible-plain-equality", EXECUTOR, COORD_NAME_LINE,
             "            if m_name == q_name:\n",
             [COORD_MATCH], TESTS_C),

    # ---- Part D ------------------------------------------------------
    mutation("nav-up-times-old-pattern", PATTERNS, UP_ROW,
             r"pattern = '''^(?:go|move) up(?: (\d+) times?)?$'''" + "\n",
             [PIPELINE, ROW_OWNS_FORM], TESTS_D),
    # Since wh-voice-chain-up-down the chain parser also reads "go down 4"
    # and presses Down 4 times, so with the old row the key-and-repeat test
    # PIPELINE stays green (the masking case of the mutation-gate skill).
    # The two route tests still see that cursor-navigate took the form.
    # The up mutation above keeps PIPELINE: its case "move up twenty three"
    # fails, because the chain parser reads only "go" and "grab". No PIPELINE
    # case says "move down".
    mutation("nav-down-times-old-pattern", PATTERNS, DOWN_ROW,
             r"pattern = '''^(?:go|move) down(?: (\d+) times?)?$'''" + "\n",
             [ROW_OWNS_FORM, LIVE_INDEX], TESTS_D),
]


def _missing_catchers(mutations):
    """Expected catcher names that pytest does not collect, as messages.

    The shared runner makes this check only before a sweep. --check owes it
    too, because a renamed catcher turns a genuine catch into a survivor.
    """
    suites = {}
    for m in mutations:
        suites.setdefault((m["service"], m["test_file"]), []).append(m)
    problems = []
    for (service, test_file), group in suites.items():
        real = runner._collected_names(service, test_file)
        for m in group:
            for name in m["expect"]:
                if name not in real:
                    problems.append(
                        f"{m['name']}: expected test {name} is not collected "
                        f"from {test_file}")
    return problems


def main(argv):
    code = runner.run(MUTATIONS, argv)
    if "--check" not in argv:
        return code
    names = [a for a in argv if a != "--check"]
    selected = [m for m in MUTATIONS
                if not names or any(a in m["name"] for a in names)]
    problems = _missing_catchers(selected)
    for problem in problems:
        print(f"ERROR {problem}")
    print(f"checked catcher names of {len(selected)} mutations, "
          f"{len(problems)} not collected")
    return 1 if (code or problems) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
