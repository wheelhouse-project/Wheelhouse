"""Mutation gate for the spaced equal sign (wh-voice-access-parity.1.15.5).

WHAT THE FIX DOES. TextPerfector._determine_spacing_prefix (ui/text_perfector.py)
gives no leading space to an insertion made only of punctuation. An insertion
that strips to a member of SPACED_PUNCTUATION ("=", "+", "<", ">", "|") is not
treated as punctuation-only, so it follows the word rules: a space after a
word, and no space in an empty field, after whitespace, after an opening
bracket, or over a selection. "x equals sign y" then types "x = y" instead of
"x= y". A spaced symbol also gets no space after a character in
OPERATOR_CHARACTERS, so "<" then "=" types "<=" and "!" then "=" types "!="
(Boss e8 rulings 11:47 and 11:49).

WHAT IT COVERS (Boss e8 ruling 2, 10:02, and the rulings above):

  equals-case-removed                  the spaced symbols are punctuation-only
                                       again ("x= y", "a+ b")
  widened-to-all-punctuation           every symbol gets a leading space
  widened-to-a-leading-equals          "==" gets a leading space too
  star-and-minus-spaced                "*" and "-" get a leading space
  <symbol>-not-spaced (4)              one of + < > | loses its leading space
  operator-rule-removed                "<" then "=" types "< ="
  operator-rule-after-any-punctuation  "f(x)" then "=" types "f(x)="
  operator-rule-for-words-too          the word after "||" loses its space
  operator-character-<name>-dropped    one operator character no longer
                         (14)          joins the "=" or symbol after it
  equals-ignores-empty-field           "=" in an empty field gets a space
  equals-ignores-whitespace-before     "=" after a space gets a second space
  equals-ignores-opening-bracket       "=" after "(" gets a space
  equals-ignores-selection             "=" over a selection gets a space
  leading-space-doubled                a pattern output " = " gets a second
                                       space (wh-voice-access-parity.1.15.6.3)
  leading-space-read-at-the-end        "= " is no longer a spaced symbol
  help-plus-says-another-symbol        the "+" help note says "another
                                       symbol" again (.1.15.6.4)

The four "ignores" mutations model a fix that sends "=" through its own
branch ahead of the shared rules and forgets one exception. Removing an
exception from the shared rule itself would change every word as well, and
the older word tests already guard that.

Run from services/wheelhouse with the interpreter the whole worktree uses.
In a worktree that is the main checkout's interpreter, so <main> below is the
main checkout's root:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_equals_spacing.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_equals_spacing.py

pytest starts as ``<this interpreter> -m pytest`` in the service directory,
never through scripts/run_tests.py, whose ``uv run`` builds a .venv inside a
git worktree. The launcher is the one in
tests/mutation_gate_overlay_interior_row.py. The shared runner
(services/stt_providers/shared/tests/mutation_gate_runner.py) owns everything
else: exactly-one pattern matches, compiled mutants, the expected-name check
against real collection, the green baseline, the per-run timeout, byte-exact
restoration, and the short-summary verdicts.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TARGET = SERVICE / "ui/text_perfector.py"
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

TESTS = (
    "tests/test_text_perfector_equals_spacing.py",
    "tests/e2e/test_e2e_workflows.py::TestDictationWithPunctuation",
    # Sanity: the other spacing rules stay green under every mutant.
    "tests/test_text_perfector_line_separators.py",
    "tests/test_terminal_editor_insert_spacing.py",
)

SPACED_SET = 'SPACED_PUNCTUATION = frozenset({"=", "+", "<", ">", "|"})\n'
OPERATOR_SET = 'OPERATOR_CHARACTERS = frozenset("=+<>|!*-%^&:~?")\n'
IS_SPACED = "            insertion_string.strip() in SPACED_PUNCTUATION\n"
LEADING_SPACE = "            and not insertion_string[:1].isspace()\n"
HELP = SERVICE / "knowledge/helpdoc/command_descriptions.toml"
PLUS_NOTE = ("No space goes before + at the start of a line, after a space, or "
             "right after one of = + < > | ! * - % ^ & : ~ ?, so")
EQUALS_CASE = "        ) and not is_spaced_symbol\n"
OPERATOR_RULE = (
    "            is_spaced_symbol and preceding_chars[-1:] in OPERATOR_CHARACTERS\n"
)
DECISION = (
    "        if (is_punctuation_only or builds_operator or ends_with_whitespace\n"
    "                or has_selection):\n"
)


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=TARGET,
                old=old, new=new, expect=expect)


def equals_first(condition):
    """A separate "=" branch ahead of the shared decision, with one exception lost."""
    return ("        if insertion_string.strip() in SPACED_PUNCTUATION and "
            f"{condition}:\n"
            "            return ' '\n" + DECISION)


MUTATIONS = [
    mutation("equals-case-removed",
             EQUALS_CASE, "        )\n",
             ["test_equal_sign_after_a_word_gets_a_leading_space",
              "test_spaced_symbol_after_a_word_gets_a_leading_space",
              "test_equals_sign", "test_other_spaced_symbols"]),
    mutation("widened-to-all-punctuation",
             EQUALS_CASE, "        ) and False\n",
             ["test_other_punctuation_after_a_word_gets_no_space",
              "test_hello_comma_world"]),
    mutation("widened-to-a-leading-equals",
             IS_SPACED,
             "            insertion_string.strip()[:1] in SPACED_PUNCTUATION\n",
             ["test_other_punctuation_after_a_word_gets_no_space"]),
    mutation("star-and-minus-spaced",
             SPACED_SET,
             'SPACED_PUNCTUATION = frozenset({"=", "+", "<", ">", "|", "*", "-"})\n',
             ["test_other_punctuation_after_a_word_gets_no_space",
              "test_hyphen"]),
    *[mutation(f"{name}-not-spaced",
               SPACED_SET,
               "SPACED_PUNCTUATION = frozenset({%s})\n" % ", ".join(
                   f'"{s}"' for s in "=+<>|" if s != symbol),
               ["test_spaced_symbol_after_a_word_gets_a_leading_space",
                "test_other_spaced_symbols"])
      for name, symbol in (("plus", "+"), ("less", "<"),
                           ("greater", ">"), ("bar", "|"))],
    mutation("operator-rule-removed",
             OPERATOR_RULE, "            False\n",
             ["test_spaced_symbol_after_a_spaced_symbol_gets_no_space",
              "test_equal_sign_after_an_operator_character_gets_no_space",
              "test_spaced_symbol_after_an_operator_character_gets_no_space",
              "test_symbol_then_equals_sign_builds_an_operator"]),
    mutation("operator-rule-after-any-punctuation",
             OPERATOR_RULE,
             "            is_spaced_symbol and preceding_chars[-1:] in string.punctuation\n",
             ["test_equal_sign_after_other_punctuation_gets_a_leading_space"]),
    mutation("operator-rule-for-words-too",
             OPERATOR_RULE,
             "            preceding_chars[-1:] in OPERATOR_CHARACTERS\n",
             ["test_word_after_a_built_operator_gets_a_leading_space",
              "test_word_after_the_equal_sign_gets_a_leading_space"]),
    *[mutation(f"operator-character-{name}-dropped",
               OPERATOR_SET,
               'OPERATOR_CHARACTERS = frozenset("%s")\n'
               % "".join(c for c in "=+<>|!*-%^&:~?" if c != char),
               # The spaced symbols are caught by the symbol-pair test; the
               # other characters by the "=" after an operator test.
               (["test_equal_sign_after_an_operator_character_gets_no_space"]
                if char not in "=+<>|" else [])
               + (["test_symbol_then_equals_sign_builds_an_operator"]
                  if char in "<!" else [])
               + (["test_spaced_symbol_after_a_spaced_symbol_gets_no_space"]
                  if char in "=+<>|" else [])
               + (["test_spaced_symbol_after_an_operator_character_gets_no_space"]
                  if char == "-" else []))
      for name, char in (("equals", "="), ("plus", "+"), ("less", "<"),
                         ("greater", ">"), ("bar", "|"), ("bang", "!"),
                         ("star", "*"), ("minus", "-"), ("percent", "%"),
                         ("caret", "^"), ("ampersand", "&"), ("colon", ":"),
                         ("tilde", "~"), ("question", "?"))],
    mutation("equals-ignores-empty-field",
             DECISION, equals_first("not preceding_chars"),
             ["test_equal_sign_in_an_empty_field_gets_no_space"]),
    mutation("equals-ignores-whitespace-before",
             DECISION, equals_first("preceding_chars[-1:].isspace()"),
             ["test_equal_sign_after_a_space_gets_no_second_space"]),
    mutation("equals-ignores-opening-bracket",
             DECISION, equals_first("preceding_chars.endswith('(')"),
             ["test_equal_sign_after_an_opening_bracket_gets_no_space"]),
    mutation("equals-ignores-selection",
             DECISION, equals_first("has_selection"),
             ["test_equal_sign_over_a_selection_gets_no_space"]),
    mutation("leading-space-doubled",
             LEADING_SPACE, "            and True\n",
             ["test_a_leading_space_in_the_output_is_not_doubled"]),
    mutation("leading-space-read-at-the-end",
             LEADING_SPACE, "            and not insertion_string[-1:].isspace()\n",
             ["test_a_trailing_space_alone_still_gets_the_leading_space"]),
    dict(name="help-plus-says-another-symbol", service=SERVICE, test_file=TESTS,
         file=HELP, old=PLUS_NOTE,
         new=PLUS_NOTE.replace("one of = + < > | ! * - % ^ & : ~ ?,",
                               "another symbol,"),
         expect=["test_the_note_lists_the_operator_characters"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
