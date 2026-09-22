"""Mutation gate for the Wheelhouse Assistant explanation window (wh-assistant-button-explainer).

Run it from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_help_explainer.py --check
    .venv/Scripts/python.exe tests/mutation_gate_help_explainer.py [--only NAME ...] [--log PATH]

It proves that the guard tests for criteria W1-W8 really fail when the
behaviour they protect breaks. Seven behaviours are guarded, across four
files:

  1. The ``explained`` check in LogicController.start_help_online. The
     window is shown only when the caller has not already explained AND
     ai.help.explain_before_open is true. Each half is mutated separately,
     the default of that setting is mutated, and the command table's
     ``explained`` argument -- which carries the flag back from the GUI
     process -- is mutated too.
  2. The GUI process saves the setting only when the Assistant button was
     chosen AND the box is ticked. Cancel and the Escape key save nothing
     and open nothing, because HelpExplainerWindow emits assistant_chosen
     only from _on_assistant.
  3. The blank ai.help.gem_url branch: the notice, and nothing else, with
     no window. The check itself is mutated, and separately the order of
     the two branches is MOVED so the window request comes first.
  4. The single-window guard in GuiManager._open_help_explainer.
  5. The strict ``command.get("explained") is True``. The command crosses a
     process boundary, so bool("false") being True was a real defect.
  6. The single INFO line naming the source, the explained value, and the
     outcome.
  7. The ``source`` argument travelling from the command into the log.

Each mutation names the tests that must fail and the start of the assertion
text each must fail with, as pytest prints it in the short test summary. A
catch requires every named catcher to fail AT ITS OWN ASSERTION: a failure
whose message is not assertion-shaped is reported as an error, never as a
catch, because an exception upstream of the assertion proves nothing.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run this concurrently with a test suite or with edits in this
worktree: it rewrites main.py, gui.py, help_explainer_window.py, and
speech/actions.py while each mutation runs.

Line endings: all four target files are LF today (measured 2026-09-20,
crlf=0 for each). The runner still detects each file's own ending and
translates every pattern to it, because the mutation-gate skill records a
2026-08-27 run in which speech/actions.py was stored with CRLF and all four
multi-line patterns of another gate matched zero times. Detection costs
nothing and the next file may differ. The files are never normalised.

Two facts from the first run of this gate, kept because both would
otherwise be rediscovered the hard way:

  * pytest does NOT render every failed assertion the same way in the short
    summary. Some reasons carry an "AssertionError: " prefix and some do
    not, in the same file and the same test class. Every marker below was
    therefore read off a real failure rather than predicted. A first sweep
    with predicted markers produced ten "catcher failed at an unexpected
    assertion" errors, all of them the gate's fault and none the tests'.
  * One mutation (the-window-is-not-raised) died with a Windows fatal
    exception, pytest exit 3221227274, and a faulthandler stack dump inside
    _pytest.main.wrap_session. The gate reported it as an error and not as
    a verdict, the finally block restored gui.py, and `git status` was
    clean afterwards. It did not recur on the next sweep, where the same
    mutation was caught. Treat this exit code as the project's known
    intermittent pytest crash: re-run the one mutation with --only rather
    than reading anything into it.

Mutations left out on purpose, so a reader does not mistake the omission
for an oversight:

  * "the spoken command opens a browser of its own", the second half of
    criterion W4. The catcher assertion exists
    (test_it_hands_the_decision_to_the_logic_controller asserts
    mock_open.assert_not_called()), but a mutant that really calls
    webbrowser.open would launch a real browser from
    tests/test_ai/test_silent_actions.py::test_unconfigured_help_is_silent,
    which does not patch webbrowser. The first half of W4 is covered by
    the-spoken-command-does-not-name-itself.
  * The GUI queue's "open_help_explainer" dispatch arm in gui.py. The
    string appears three times in that file (the elif, the method name,
    and the call), so a pattern for the arm alone would need the
    surrounding arms to stay unique as they are edited. The Logic side of
    the same contract is mutated instead, in
    the-explainer-request-uses-another-action-name.
"""
# crewcut: the runner below is copied from
# tests/mutation_gate_audio_pause_notice.py, which copied it from
# tests/mutation_gate_bravia_error_envelope.py, extended here to four target
# files and to parametrized catcher names. A shared runner module imported by
# all three gates is the way to remove the duplication later.
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
MAIN = SERVICE / "main.py"
GUI = SERVICE / "gui.py"
WINDOW = SERVICE / "help_explainer_window.py"
ACTIONS = SERVICE / "speech" / "actions.py"
TARGETS = (MAIN, GUI, WINDOW, ACTIONS)

_VENV_PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable))

LOGIC_TESTS = "tests/test_logic_open_help_online.py"
GUI_TESTS = "tests/test_gui_help_explainer.py"
WINDOW_TESTS = "tests/test_help_explainer_window.py"
ACTION_TESTS = "tests/test_ai/test_actions.py"
SILENT_TESTS = "tests/test_ai/test_silent_actions.py"
SELECTION = [LOGIC_TESTS, GUI_TESTS, WINDOW_TESTS, ACTION_TESTS, SILENT_TESTS]
RUN_TIMEOUT = 180  # A clean run of the selection takes about 2.5 seconds.

# --- catcher test names, so a rename breaks one line rather than many ---
SKIPS_WINDOW = "test_the_assistant_button_skips_the_window"
OPENS_ADDRESS = "test_it_opens_the_configured_address"
MISSING_SETTING = "test_a_missing_setting_shows_the_window"
ASKS_FOR_WINDOW = "test_it_asks_for_the_window_and_opens_no_browser"
FIELD_TRAVELS = "test_the_explained_field_travels_with_the_command"
TRUE_SKIPS = "test_the_boolean_true_still_skips_the_window"
CARRIES_SOURCE = "test_the_command_carries_the_source_it_was_given"
BLANK_SAYS_SO = "test_a_blank_address_opens_nothing_and_says_so"
BLANK_WHEN_EXPLAINED = "test_a_blank_address_gives_the_notice_when_explained_is_true"
LOG_WINDOW = "test_asking_for_the_window_is_logged_with_its_source"
LOG_BROWSER = "test_opening_the_browser_is_logged_with_its_source"
LOG_NOTICE = "test_the_unconfigured_notice_is_logged_with_its_source"

_NON_BOOL = "test_a_non_boolean_explained_field_takes_the_full_decision"
# Only these four ids fail under the bool() form. The other four cases
# (number-zero, none, empty-list, empty-dict) pass under BOTH forms, because
# bool() already answers False for them; listing them as catchers would make
# the mutation look caught for the wrong reason.
NON_BOOL_CATCHERS = [
    f"{_NON_BOOL}[string-false]",
    f"{_NON_BOOL}[string-true]",
    f"{_NON_BOOL}[string-True-capital]",
    f"{_NON_BOOL}[number-one]",
]

SECOND_RAISES = "test_a_second_request_raises_the_open_window"
BOX_CLEARED = "test_the_check_box_is_cleared_before_each_showing"
ASSISTANT_OPENS = "test_assistant_asks_logic_to_open_the_page"
CHECKED_SAVES = "test_the_checked_box_also_saves_the_setting"
CLEAR_SAVES_NOTHING = "test_a_clear_box_saves_nothing"

CANCEL_SILENT = "test_cancel_reports_nothing_and_closes"
CANCEL_SILENT_TICKED = "test_cancel_reports_nothing_even_when_the_box_is_checked"
ESCAPE_SILENT = "test_escape_reports_nothing_and_closes"
REPORTS_TICKED = "test_assistant_reports_the_checked_box"

HANDS_OVER = "test_it_hands_the_decision_to_the_logic_controller"

# Assertion-text markers, every one MEASURED from a run of this gate rather
# than predicted. pytest prefixes "AssertionError: " to a rewritten assert
# whose explanation runs past one line and omits it otherwise, so which form
# a given assertion produces cannot be reasoned about and has to be read off
# a real failure.
#
# Where pytest's assertion rewriting truncates a long dict or list repr with
# an ellipsis, the marker stops before the truncation: the ellipsis position
# depends on the repr length, which an edit to an unrelated field of the same
# dict would move. The global non-assertion check in _judge is what rules out
# an upstream exception for those catchers, and the marker still proves the
# failing comparison was the intended one.
NO_CALL = "AssertionError: expected call not found"
NO_AWAIT = "AssertionError: expected await not found"
NOT_CALLED = "AssertionError: Expected 'open' to not have been called"
CALLED_ZERO = "AssertionError: Expected 'open' to be called once. Called 0 times."
QUEUED_LIST = "AssertionError: assert [{'action': '"
COMMAND_IN_LIST = (
    "AssertionError: assert {'action': 'open_help_online', "
    "'explained': True, 'source': 'window'} in ["
)

# The command table's explained/source arguments, as one unique substring.
TABLE_ARGS = (
    'explained=command.get("explained") is True, '
    'source=str(command.get("source", "menu"))'
)

# The two branches whose ORDER the move mutation swaps.
BLANK_BRANCH = (
    '        gem_url = config.get("ai.help.gem_url", "")\n'
    "        if not gem_url:\n"
    "            # Blanking the setting is how a user turns online help off, so\n"
    "            # this is a plain statement of fact rather than an error. The\n"
    "            # explanation window stays shut: its one button would have\n"
    "            # nothing to open. This check comes first on purpose: the\n"
    "            # window is modeless, so the address can be blanked while it is\n"
    "            # open, and the Assistant button must then say so rather than\n"
    "            # open an empty page.\n"
    "            logger.info(\n"
    '                "Help: source=%s explained=%s, online help is not configured.",\n'
    "                source,\n"
    "                explained,\n"
    "            )\n"
    "            self._send_gui_notification(\n"
    '                "Online help is not configured. Set gem_url under [ai.help]."\n'
    "            )\n"
    "            return\n"
)
WINDOW_BRANCH = (
    '        if not explained and config.get("ai.help.explain_before_open", True):\n'
    "            logger.info(\n"
    '                "Help: source=%s explained=%s, asking for the explanation window.",\n'
    "                source,\n"
    "                explained,\n"
    "            )\n"
    "            self._request_help_explainer()\n"
    "            return\n"
)

BROWSER_LOG = (
    "        logger.info(\n"
    '            "Help: source=%s explained=%s, opening the browser.",\n'
    "            source,\n"
    "            explained,\n"
    "        )\n"
    "        try:\n"
)

ON_ASSISTANT = (
    "    def _on_assistant(self) -> None:\n"
    "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())\n"
    "        self.accept()\n"
)

MUTATIONS = [
    # ---------------------------------------------------------------
    # Behaviour 1: the explained check, and the flag that feeds it.
    # ---------------------------------------------------------------
    {
        # Without the explained half the window answers its own Assistant
        # button and the browser never opens.
        "name": "the-explained-half-is-dropped",
        "file": MAIN,
        "old": '        if not explained and config.get("ai.help.explain_before_open", True):',
        "new": '        if config.get("ai.help.explain_before_open", True):',
        "catchers": {SKIPS_WINDOW: CALLED_ZERO},
    },
    {
        # Without the config read the setting does nothing: the window
        # appears however ai.help.explain_before_open is set.
        "name": "the-config-read-is-dropped",
        "file": MAIN,
        "old": '        if not explained and config.get("ai.help.explain_before_open", True):',
        "new": "        if not explained:",
        "catchers": {OPENS_ADDRESS: CALLED_ZERO},
    },
    {
        # W3: an absent key must read as true, so a user who has never
        # touched the setting still gets the explanation.
        "name": "the-default-becomes-false",
        "file": MAIN,
        "old": '        if not explained and config.get("ai.help.explain_before_open", True):',
        "new": '        if not explained and config.get("ai.help.explain_before_open", False):',
        "catchers": {MISSING_SETTING: NOT_CALLED},
    },
    {
        # The command table stops carrying the flag back from the GUI
        # process, so the Assistant button produces the window again.
        "name": "the-explained-field-is-ignored",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": 'explained=False, source=str(command.get("source", "menu"))',
        "catchers": {
            FIELD_TRAVELS: NO_CALL,
            TRUE_SKIPS: "assert False is True",
        },
    },
    {
        # Behaviour 5, the boss ruling's defect. bool("false") is True, so
        # under this form any non-empty string skips the window.
        "name": "the-strict-check-becomes-bool",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": (
            'explained=bool(command.get("explained", False)), '
            'source=str(command.get("source", "menu"))'
        ),
        "catchers": {name: "assert True is False" for name in NON_BOOL_CATCHERS},
    },
    # ---------------------------------------------------------------
    # Behaviour 7: the source argument.
    # ---------------------------------------------------------------
    {
        # Every run then claims the menu, so the log cannot tell the
        # Assistant button from the menu entry or the spoken command.
        "name": "the-source-is-fixed-to-menu",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": 'explained=command.get("explained") is True, source="menu"',
        "catchers": {CARRIES_SOURCE: "AssertionError: assert 'menu' == 'window'"},
    },
    # ---------------------------------------------------------------
    # Behaviour 3: the blank gem_url branch.
    # ---------------------------------------------------------------
    {
        # A blank address no longer stops the run, so the window opens on
        # an address its one button could not use.
        "name": "the-blank-check-is-dropped",
        "file": MAIN,
        "old": "        if not gem_url:",
        "new": "        if False:",
        "catchers": {
            BLANK_SAYS_SO: (
                "AssertionError: assert 'open_help_explainer' "
                "== 'show_notification'"
            ),
            BLANK_WHEN_EXPLAINED: NOT_CALLED,
            LOG_NOTICE: "AssertionError: assert 'not configured' in",
        },
    },
    {
        # A MOVE, not a copy: the window branch is deleted from its place
        # and inserted above the blank-address branch. A copy would leave
        # the original in place and the order assertion would still pass.
        "name": "the-window-request-comes-before-the-blank-check",
        "file": MAIN,
        "old": BLANK_BRANCH + "\n" + WINDOW_BRANCH,
        "new": WINDOW_BRANCH + "\n" + BLANK_BRANCH,
        "catchers": {
            BLANK_SAYS_SO: (
                "AssertionError: assert 'open_help_explainer' "
                "== 'show_notification'"
            ),
            LOG_NOTICE: "AssertionError: assert 'not configured' in",
        },
    },
    {
        # The Logic side of the cross-process contract: the GUI process
        # matches this exact action name and nothing else.
        "name": "the-explainer-request-uses-another-action-name",
        "file": MAIN,
        "old": 'queue.put_nowait({"action": "open_help_explainer"})',
        "new": 'queue.put_nowait({"action": "open_help_explainer_disabled"})',
        "catchers": {
            ASKS_FOR_WINDOW: QUEUED_LIST,
            MISSING_SETTING: QUEUED_LIST,
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 6: one INFO line per run, naming the path taken.
    # ---------------------------------------------------------------
    {
        # The window branch claims the browser opened. One line still, but
        # it names the wrong outcome.
        "name": "the-window-branch-logs-the-browser-text",
        "file": MAIN,
        "old": '                "Help: source=%s explained=%s, asking for the explanation window.",',
        "new": '                "Help: source=%s explained=%s, opening the browser.",',
        "catchers": {
            LOG_WINDOW: "AssertionError: assert 'explanation window' in",
        },
    },
    {
        # The browser branch logs nothing at all, so the one path a user
        # reaches most often leaves no trace.
        "name": "the-browser-branch-logs-nothing",
        "file": MAIN,
        "old": BROWSER_LOG,
        "new": "        try:\n",
        "catchers": {LOG_BROWSER: "assert 0 == 1"},
    },
    # ---------------------------------------------------------------
    # Behaviour 4: the single-window guard.
    # ---------------------------------------------------------------
    {
        # A second Help builds a second window instead of raising the one
        # already on screen.
        "name": "a-new-window-every-time",
        "file": GUI,
        "old": "        if getattr(self, '_help_explainer', None) is None:",
        "new": "        if True:",
        "catchers": {
            SECOND_RAISES: "AssertionError: Expected 'HelpExplainerWindow' to have been called once",
        },
    },
    {
        # The window is never brought to the front, so a second Help looks
        # like nothing happened.
        "name": "the-window-is-not-raised",
        "file": GUI,
        "old": "        self._help_explainer.raise_()\n",
        "new": "        pass\n",
        "catchers": {SECOND_RAISES: "AssertionError: assert 0 == 2"},
    },
    {
        "name": "the-window-is-not-activated",
        "file": GUI,
        "old": "        self._help_explainer.activateWindow()\n",
        "new": "        pass\n",
        "catchers": {SECOND_RAISES: "AssertionError: assert 0 == 2"},
    },
    {
        # The same instance comes back, so a box ticked in an earlier
        # reading would travel with a later Assistant press.
        "name": "the-check-box-is-not-cleared-before-showing",
        "file": GUI,
        "old": "        self._help_explainer.prepare_to_show()\n",
        "new": "        pass\n",
        "catchers": {
            BOX_CLEARED: "AssertionError: Expected 'prepare_to_show' to have been called once",
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 2: save only when Assistant was chosen AND the box is
    # ticked.
    # ---------------------------------------------------------------
    {
        # The tick is ignored and the setting is written every time, so one
        # Assistant press turns the explanation off for good.
        "name": "the-tick-is-ignored-and-the-setting-always-saves",
        "file": GUI,
        "old": "        if do_not_show_again:",
        "new": "        if True:",
        "catchers": {
            CLEAR_SAVES_NOTHING: QUEUED_LIST,
            ASSISTANT_OPENS: QUEUED_LIST,
        },
    },
    {
        # The tick never writes anything, so the check box does nothing.
        "name": "the-tick-never-saves-the-setting",
        "file": GUI,
        "old": "        if do_not_show_again:",
        "new": "        if False:",
        "catchers": {CHECKED_SAVES: "assert 0 == 1"},
    },
    {
        # The write goes the wrong way: ticking the box turns the
        # explanation ON rather than off.
        "name": "the-saved-value-becomes-true",
        "file": GUI,
        "old": "                'value': False,",
        "new": "                'value': True,",
        "catchers": {CHECKED_SAVES: "assert True is False"},
    },
    {
        # The Assistant button's command claims it came from the menu.
        "name": "the-window-does-not-name-itself-as-the-source",
        "file": GUI,
        "old": '            "source": "window",',
        "new": '            "source": "menu",',
        "catchers": {
            ASSISTANT_OPENS: QUEUED_LIST,
            CHECKED_SAVES: COMMAND_IN_LIST,
        },
    },
    {
        # Cancel reaches the Assistant handler, so backing out of the
        # window opens the page and can write the setting.
        "name": "cancel-emits-the-choice",
        "file": WINDOW,
        "old": "        cancel_button.clicked.connect(self.reject)",
        "new": "        cancel_button.clicked.connect(self._on_assistant)",
        "catchers": {
            CANCEL_SILENT: "assert [False] == []",
            CANCEL_SILENT_TICKED: "assert [True] == []",
        },
    },
    {
        # Every way out of the window reports a choice, including the
        # Escape key and the title-bar X.
        "name": "escape-and-cancel-emit-the-choice",
        "file": WINDOW,
        "old": ON_ASSISTANT,
        "new": (
            "    def reject(self) -> None:\n"
            "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())\n"
            "        super().reject()\n"
            "\n" + ON_ASSISTANT
        ),
        "catchers": {
            ESCAPE_SILENT: "assert [False] == []",
            CANCEL_SILENT: "assert [False] == []",
            CANCEL_SILENT_TICKED: "assert [True] == []",
        },
    },
    {
        # The choice is reported without the box's state, so a ticked box
        # never reaches the process that writes the setting.
        "name": "the-check-box-state-is-not-reported",
        "file": WINDOW,
        "old": "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())",
        "new": "        self.assistant_chosen.emit(False)",
        "catchers": {REPORTS_TICKED: "assert [False] == [True]"},
    },
    # ---------------------------------------------------------------
    # The spoken command's half of criterion W4.
    # ---------------------------------------------------------------
    {
        # The spoken command stops naming itself, so the log cannot tell a
        # spoken "help" from the Help menu entry.
        "name": "the-spoken-command-does-not-name-itself",
        "file": ACTIONS,
        "old": '        await lc.start_help_online(source="spoken")',
        "new": "        await lc.start_help_online()",
        "catchers": {HANDS_OVER: NO_AWAIT},
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(originals, names):
    """Return (prepared mutants, error lines). Never touches the files."""
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        target = mutation["file"]
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            errors.append(f"ERROR {mutation['name']}: pattern-not-found")
            continue
        if count > 1:
            errors.append(
                f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)"
            )
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(target), "exec")
        except SyntaxError as exc:
            errors.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, mutant.encode("utf-8")))
    return prepared, errors


def _clear_bytecode():
    for directory in SERVICE.rglob("__pycache__"):
        if ".venv" not in directory.parts:
            shutil.rmtree(directory, ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # pytest cuts the " - <reason>" suffix off a short-summary line wider
    # than the terminal, and a captured run reports 80 columns. The node ids
    # in this selection pass 100 characters routinely.
    env["COLUMNS"] = "1000"
    return env


def _pytest(extra, timeout):
    command = [
        PYTHON, "-m", "pytest", *SELECTION,
        "-p", "no:randomly", "-p", "no:cacheprovider",
        *extra,
    ]
    return subprocess.run(
        command, cwd=SERVICE, env=_env(), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _collected_names():
    """Every collected test name, with and without its parameter list."""
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            full = line.split("::")[-1].split(" ")[0].strip()
            names.add(full)
            names.add(full.split("[")[0])
    return result.returncode, names


def _summary(output: str):
    """Map test name -> (kind, message) from pytest's short summary only.

    Reading anywhere else would let a captured log record at ERROR level
    read as a test record.
    """
    records, inside = {}, False
    for line in output.splitlines():
        if SUMMARY_HEADER.match(line):
            inside = True
            continue
        if inside and SECTION_RULE.match(line):
            break
        if inside:
            match = SUMMARY_LINE.match(line)
            if match:
                name = match.group(2).split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    # Key on the exit code first: a green run prints no summary section at
    # all, and that is a survivor rather than an unreadable run.
    if result.returncode == 1 and not records:
        raise RuntimeError(
            f"pytest exit 1 with no short-summary records: {output[-1500:]}"
        )
    return records


def _write_with_retry(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    last = None
    try:
        for _ in range(10):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(target: Path, original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(target, original)
            os.utime(target, ns=stat)
            if target.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {target}: {exc}", flush=True)
    restored = False
    try:
        restored = target.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(
            f"RESTORE FAILED: {target} may still hold a mutant; check git diff",
            flush=True,
        )
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


def _judge(mutation, records):
    unexpected = [
        f"{name}: {message}"
        for name, (kind, message) in records.items()
        if kind == "ERROR"
        or not (message.startswith("assert") or message.startswith("AssertionError"))
    ]
    if unexpected:
        return "ERROR", "non-assertion failure: " + "; ".join(unexpected)
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not records[name][1].startswith(marker):
            wrong.append(f"{name}: {records[name][1]} (expected {marker!r})")
        else:
            fired.append(f"{name}: {records[name][1]}")
    if wrong:
        return "ERROR", "catcher failed at an unexpected assertion: " + "; ".join(wrong)
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append(
            "other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others)
        )
    if not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true",
                        help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME",
                        help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    known = {m["name"] for m in MUTATIONS}
    unknown = [n for n in args.only if n not in known]
    if unknown:
        say(f"ERROR unknown mutation names: {', '.join(unknown)}")
        return 1

    originals = {target: target.read_bytes() for target in TARGETS}
    stats = {
        target: (target.stat().st_atime_ns, target.stat().st_mtime_ns)
        for target in TARGETS
    }
    for target in TARGETS:
        ending = "CRLF" if _line_ending(originals[target]) == "\r\n" else "LF"
        say(f"Target {target.name}: {ending}")

    prepared, errors = _prepare(originals, set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, "
        f"{broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted(
        {name for mutation, _ in prepared for name in mutation["catchers"]}
        - collected
    )
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): "
            f"{', '.join(missing) or 'none collected'}")
        return 1

    _clear_bytecode()
    try:
        baseline = _run_selection()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        say(f"ERROR baseline: {exc}")
        return 1
    if baseline:
        say(f"ERROR baseline not green: {sorted(baseline)}")
        return 1
    say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    survivors = 0
    error_count = len(errors)
    for index, (mutation, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        target = mutation["file"]
        stat = stats[target]
        try:
            _write_with_retry(target, mutant)
            os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection()
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(target, originals[target], stat)
        if verdict == "SURVIVED":
            survivors += 1
        elif verdict == "ERROR":
            error_count += 1
        say(f"{verdict} {name}: {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{len(prepared) - survivors - (error_count - len(errors))} caught, "
        f"{survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
