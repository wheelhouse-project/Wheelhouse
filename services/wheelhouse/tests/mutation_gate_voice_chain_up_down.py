r"""Mutation gate for "go up" and "go down" in "then" chains
(wh-voice-chain-up-down, change e9032874..b913345d).

A "then" chain reaches only the cursor-navigate pattern, which gives the
words to NavigationParser. The change makes the parser read an up or down
part with the words of the single forms (nav-up-times, nav-up-lines,
nav-up-paragraphs and their down twins) and makes the executor press the
same keys. This gate breaks each changed behavior once:

  Parser (speech/navigation/parser.py)
    up-down-directions-removed    the "up" and "down" entries of _DIRECTIONS
                                  are gone, so a chain with an up or down
                                  part is typed as text again
    up-reads-as-down              "up" maps to the direction "down"
    down-reads-as-up              "down" maps to the direction "up"
    vertical-default-character    an up or down part with no unit word gets
                                  the unit "character" instead of "line"
    times-without-count-accepted  "go up times" is accepted, with no count
                                  before the word "times"
    times-ignored                 the word "times" is never a unit word, so
                                  "go up three times" is refused
    vertical-units-for-all        every direction reads the up and down unit
                                  words, so "go left three times" is a move
    words-for-vertical            up and down also read the "words" and
                                  "characters" unit words
    grab-vertical-accepted        "grab up" and "grab down" are accepted
    lines-removed                 the word "lines" is not a unit word
    go-to-up-not-stripped         "to" after "go" stays when up or down
                                  follows, so "go to up three" said alone
                                  is typed text again

  Executor (speech/navigation/executor.py, _RELATIVE_KEYS)
    up-line-key-down              ("up", "line") presses Down
    down-line-key-up              ("down", "line") presses Up
    up-paragraph-no-ctrl          ("up", "paragraph") presses Up, not Ctrl+Up
    down-paragraph-ctrl-up        ("down", "paragraph") presses Ctrl+Up

  Routing (speech/config/patterns.toml)
    single-up-reaches-chain-parser  the nav-up-times pattern no longer
                                  matches "go up three", so the single
                                  form reaches cursor-navigate and the
                                  chain parser

patterns.toml is not Python, so --check skips the compile check for it.

Known limit: pytest names are compared without the parameter list, so the
name check cannot tell one parameter of a catcher from another. Read the
caught line of a full sweep with that in mind. The up-line-key-down
mutation also runs test_a_count_reads_the_same_in_a_chain_and_alone (240
cases, about 3 minutes); the other mutations leave it out.

Run from services/wheelhouse with the interpreter the whole worktree uses
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_voice_chain_up_down.py --check
    .venv/Scripts/python.exe tests/mutation_gate_voice_chain_up_down.py <name> ...
    .venv/Scripts/python.exe tests/mutation_gate_voice_chain_up_down.py

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
PARSER = SERVICE / "speech/navigation/parser.py"
EXECUTOR = SERVICE / "speech/navigation/executor.py"
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
# The up-line-key-down mutation runs the 240 cases of COUNT_SAME and fails
# many of them. Measured 2026-10-20: that run exceeded the shared runner's
# 300 s limit and was reported as "pytest timed out" (an error, not a
# verdict). 900 s is far above a legitimate run and below a hang.
runner.RUN_TIMEOUT = 900


def _failed_names(output: str):
    """The test names in pytest's short summary, parameter list removed.

    The shared runner cuts a summary line at its first space, so a
    parametrized case whose id holds a space ("test_x[go right 23]") is read
    as "test_x[go", which no expected name can match, and the mutation is
    reported as a survivor although the test failed. This version drops
    everything from the first "[", which is also the form _collected_names
    produces.
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

PARSER_TESTS = "tests/test_navigation_parser.py"
CHAIN_FILE = "tests/test_navigation_chain_up_down.py"

# Parser catchers (class TestUpAndDownInChains in test_navigation_parser.py).
READ = "test_an_up_or_down_part_is_read"
ORDER = "test_the_parts_keep_the_spoken_order"
REFUSED = "test_an_up_or_down_part_without_a_single_form_is_refused"
RL = "test_right_and_left_still_refuse_lines_and_times"
KEYS = "test_the_keys_are_the_keys_of_the_single_form"

# Pipeline catchers (test_navigation_chain_up_down.py).
EACH = "test_each_part_moves_as_the_same_words_alone"
LITERAL = "test_the_chain_presses_these_keys"
COUNT_SAME = "test_a_count_reads_the_same_in_a_chain_and_alone"
ROUTE = "test_a_single_up_or_down_still_goes_through_its_pattern"
TYPED = "test_a_chain_with_a_part_that_has_no_single_form_is_typed"
UNCHANGED = "test_a_chain_without_up_or_down_is_unchanged"
GO_TO = "test_go_to_up_moves_through_cursor_navigate_as_go_to_left"

# The two files of this change. The 240 cases of COUNT_SAME run only for the
# mutation that needs them.
TESTS = (
    PARSER_TESTS,
    f"{CHAIN_FILE}::{EACH}",
    f"{CHAIN_FILE}::{LITERAL}",
    f"{CHAIN_FILE}::{ROUTE}",
    f"{CHAIN_FILE}::{TYPED}",
    f"{CHAIN_FILE}::{UNCHANGED}",
    f"{CHAIN_FILE}::{GO_TO}",
)
TESTS_WITH_COUNT = TESTS + (f"{CHAIN_FILE}::{COUNT_SAME}",)

# Source text. Each is one exact match in the current file.
DIRECTIONS_TAIL = '               "up": "up", "down": "down"}\n'
DEFAULT_UNIT = '        unit = "line" if vertical else "character"\n'
TIMES_BRANCH = (
    "                if u is None and last in _TIMES and end - 1 > pos:\n"
)
VERTICAL_BRANCH = (
    "            if vertical:\n"
    "                u = _VERTICAL_UNITS.get(last)\n"
)
VERTICAL_LOOKUP = "                u = _VERTICAL_UNITS.get(last)\n"
GRAB_REFUSAL = (
    '        if vertical and verb == "grab":\n'
    "            return None\n"
)
LINE_UNITS = '    "line": "line", "lines": "line",\n'
TO_STRIP = '        if len(tokens) > 1 and tokens[0] == "to":\n'

UP_LINE = '    ("up", "line"): ["up"],\n'
DOWN_LINE = '    ("down", "line"): ["down"],\n'
UP_PARAGRAPH = '    ("up", "paragraph"): ["ctrl", "up"],\n'
DOWN_PARAGRAPH = '    ("down", "paragraph"): ["ctrl", "down"],\n'

UP_ROW = r"pattern = '''^(?:go|move) up(?: (\d+)(?: times?)?)?$'''" + "\n"


def mutation(name, file, old, new, expect, tests=TESTS):
    return dict(name=name, service=SERVICE, test_file=tests, file=file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    # ---- Parser ------------------------------------------------------
    mutation("up-down-directions-removed", PARSER, DIRECTIONS_TAIL,
             "               }\n",
             # KEYS is not listed: with the directions gone the parser
             # returns None, and KEYS fails on "TypeError: 'NoneType' object
             # is not iterable" inside to_actions, not on its assertion.
             [READ, ORDER, EACH, LITERAL]),
    mutation("up-reads-as-down", PARSER, DIRECTIONS_TAIL,
             '               "up": "down", "down": "down"}\n',
             [READ, KEYS, LITERAL, EACH]),
    mutation("down-reads-as-up", PARSER, DIRECTIONS_TAIL,
             '               "up": "up", "down": "up"}\n',
             [READ, KEYS, LITERAL, EACH]),
    mutation("vertical-default-character", PARSER, DEFAULT_UNIT,
             '        unit = "character"\n',
             [READ, ORDER]),
    mutation("times-without-count-accepted", PARSER, TIMES_BRANCH,
             "                if u is None and last in _TIMES:\n",
             [REFUSED]),
    mutation("times-ignored", PARSER, TIMES_BRANCH,
             "                if False:\n",
             [READ, ORDER, EACH, LITERAL]),
    mutation("vertical-units-for-all", PARSER, VERTICAL_BRANCH,
             "            if True:\n"
             "                u = _VERTICAL_UNITS.get(last)\n",
             [RL]),
    mutation("words-for-vertical", PARSER, VERTICAL_LOOKUP,
             "                u = _VERTICAL_UNITS.get(last) or _UNITS.get(last)\n",
             [REFUSED]),
    mutation("grab-vertical-accepted", PARSER, GRAB_REFUSAL,
             '        if vertical and verb == "grab":\n'
             "            pass\n",
             [REFUSED]),
    mutation("lines-removed", PARSER, LINE_UNITS,
             '    "line": "line",\n',
             # KEYS is not listed: "go down four lines" parses to None, and
             # KEYS fails on the same TypeError in to_actions as in
             # up-down-directions-removed.
             [READ, LITERAL, EACH]),
    mutation("go-to-up-not-stripped", PARSER, TO_STRIP,
             '        if len(tokens) > 1 and tokens[0] == "to"'
             ' and tokens[1] not in _VERTICAL:\n',
             [GO_TO]),

    # ---- Executor ----------------------------------------------------
    mutation("up-line-key-down", EXECUTOR, UP_LINE,
             '    ("up", "line"): ["down"],\n',
             [KEYS, EACH, LITERAL, COUNT_SAME], TESTS_WITH_COUNT),
    mutation("down-line-key-up", EXECUTOR, DOWN_LINE,
             '    ("down", "line"): ["up"],\n',
             [KEYS, EACH, LITERAL]),
    mutation("up-paragraph-no-ctrl", EXECUTOR, UP_PARAGRAPH,
             '    ("up", "paragraph"): ["up"],\n',
             [KEYS, EACH, LITERAL]),
    mutation("down-paragraph-ctrl-up", EXECUTOR, DOWN_PARAGRAPH,
             '    ("down", "paragraph"): ["ctrl", "up"],\n',
             [KEYS, EACH]),

    # ---- Routing -----------------------------------------------------
    # The single "go up three" reaches cursor-navigate, so the chain parser
    # reads it and the recording parse in ROUTE is called.
    mutation("single-up-reaches-chain-parser", PATTERNS, UP_ROW,
             r"pattern = '''^(?:go|move) upward(?: (\d+)(?: times?)?)?$'''"
             + "\n",
             [ROUTE]),
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
