"""Mutation gate for wh-clicks-spoken-click: "clicks" in the click-element row.

Each mutation breaks one thing the tests in
tests/test_click_element_clicks_form.py protect, and names the tests that
must fail for it:

  - clicks-dropped: the row goes back to '^(?:click|tap)\\s+(.+)$', the
    text before the fix. Every "clicks" test must fail.
  - clicks-optional-s: the row is spelled '^(?:clicks?|tap)\\s+(.+)$'. It
    matches the same words, so only the Pattern Manager display test and
    the exact-text test can see it.
  - hotword-dropped: the row stops requiring the hotword, so "clicks" with
    no hotword clicks instead of typing (acceptance A2).
  - grid-click-gains-clicks: the bare grid click row also accepts
    "clicks", the over-broad fix acceptance A3 rules out.

Run from services/wheelhouse with that service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_clicks_spoken_click.py
    .venv/Scripts/python.exe tests/mutation_gate_clicks_spoken_click.py --check

pytest is started directly with ``sys.executable``, never through
``uv run``, so the gate cannot build a .venv inside a worktree.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner  # noqa: E402


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

SELECTION = "tests/test_click_element_clicks_form.py"
PATTERNS = "speech/config/patterns.toml"

# --- expected catchers, by the name pytest reports (parameters stripped) ----

AFTER_HOTWORD = "test_verb_after_hotword_clicks_the_named_control"
SAME_QUERY = "test_clicks_query_equals_the_click_query"
REVISION = "test_revision_from_click_to_clicks_replays_as_a_click"
NO_HOTWORD = "test_verb_without_hotword_types_the_words"
FIRST_MATCH = "test_first_matching_row_is_unchanged_except_for_clicks"
BARE_CLICKS = "test_bare_clicks_matches_no_row"
DISPLAY = "test_pattern_manager_shows_click_clicks_and_tap"
ROW_TEXT = "test_click_element_row_is_the_new_text"

MUTATIONS = []


def add(name, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=SELECTION,
                          file=SERVICE / PATTERNS, old=old, new=new,
                          expect=list(expect)))


ROW = (
    "pattern = '''^(?:click|clicks|tap)\\s+(.+)$'''\n"
    "doc_id = \"click-element\"\n"
    "requires_hotword = true\n"
)

add(
    "clicks-dropped",
    ROW,
    ROW.replace("(?:click|clicks|tap)", "(?:click|tap)"),
    AFTER_HOTWORD, SAME_QUERY, REVISION, FIRST_MATCH, DISPLAY, ROW_TEXT,
)

add(
    "clicks-optional-s",
    ROW,
    ROW.replace("(?:click|clicks|tap)", "(?:clicks?|tap)"),
    DISPLAY, ROW_TEXT,
)

add(
    "hotword-dropped",
    ROW,
    ROW.replace("requires_hotword = true", "requires_hotword = false"),
    NO_HOTWORD, ROW_TEXT,
)

add(
    "grid-click-gains-clicks",
    "pattern = '''^((click|tap)[.!?]?)$'''\n",
    "pattern = '''^((click|clicks|tap)[.!?]?)$'''\n",
    BARE_CLICKS,
)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS, sys.argv[1:]))
