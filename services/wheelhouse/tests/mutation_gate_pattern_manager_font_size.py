"""Mutation evidence for the Pattern Manager font-size persistence
(wh-pattern-font-size).

The dialog reports a user zoom through ``font_size_changed``; the GuiManager
saves it as ``PATTERN_MANAGER_FONT_SIZE`` through the acknowledged
``set_config_value`` route, validates the stored value when it starts, applies
it when the dialog is built, and puts the confirmed size back when a save is
refused by the queue, fails, or is settled by a reconcile reply. This gate
breaks each of those shipped behaviours in ``gui.py`` and
``pattern_manager_dialog.py`` and requires the named tests to fail for each.

Run from services/wheelhouse with the interpreter of this checkout
(``sys.executable`` becomes the pytest launcher, see LAUNCH below):

    .venv/Scripts/python.exe tests/mutation_gate_pattern_manager_font_size.py --check
    .venv/Scripts/python.exe tests/mutation_gate_pattern_manager_font_size.py
    .venv/Scripts/python.exe tests/mutation_gate_pattern_manager_font_size.py \
        --only result-restore-removed,apply-emits-font-size-changed

The runner is the shared one (services/stt_providers/shared/tests/
mutation_gate_runner.py), launched the way
tests/mutation_gate_pattern_manager_tree_changed.py launches it: ``<python>
-m pytest`` in the service directory, never ``uv run``, so a worktree gets no
``.venv`` built by the gate. This module adds two things on top of the
runner, both without editing it:

  - ``--only NAME[,NAME...]``. Every name must equal a mutation name exactly.
    The runner's own filter matches substrings, so this module also refuses
    to load when one mutation name is a substring of another; with that
    rule, the substring filter selects exactly the names given.
  - A reason check on every catch. The runner confirms that each expected
    catcher is in pytest's short-summary FAILED set; it does not read why.
    A catcher that failed on an unrelated crash (a TypeError upstream of
    the assertion) proves nothing about the behaviour, so each expected
    catcher must have at least one short-summary line whose reason is an
    assertion. A catch without one is reported as an error
    (``catch-not-from-an-assertion``) and the gate exits nonzero.

ONE SELECTION FOR EVERY MUTATION. The persistence test file (13 test
functions) holds every expected catcher, and four zoom tests from
tests/test_pattern_manager_font_size.py are the sanity set. One shared
selection costs one collection and one baseline run instead of one of each
per mutation.

BEHAVIOURS WITH NO MUTATION HERE, and why.

  - A successful save updating the confirmed baseline for the font key.
    That is the generic loop in ``_handle_settings_result``
    (``self._settings_confirmed[key] = ...``), which predates this branch
    (``git diff 6dbce317..77f45753 -- services/wheelhouse/gui.py`` does not
    touch it), so it is out of this gate's scope.
  - ``emit(point_size)`` in place of ``emit(self._current_font_point_size)``
    is an equivalent mutant. The signal fires only when the size changed,
    the step is 1, and the reset target is the dialog's own default, which
    is inside the 7-24 bounds; so a request that changes the size is never
    clamped, and the two values are always equal.
  - Routing the save through ``_send_pm_command`` instead of
    ``send_command``. The brief allowed either that or a different key; the
    ``_send_pm_command`` mutant sends no ``request_id``, so every catcher
    dies on a ``KeyError`` reading it -- a crash, not an assertion. The
    different-key mutation (``save-sends-a-different-key``) runs through to
    the assertions instead.
  - The ``dialog is None`` guard in ``_restore_pattern_manager_font_size``.
    Every test opens the dialog before a reply arrives, so removing it
    would only show as an AttributeError in a path no test builds.
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

PERSIST = "tests/test_pattern_manager_font_size_persistence.py"
ZOOM = "tests/test_pattern_manager_font_size.py"
SELECTION = (
    PERSIST,
    f"{ZOOM}::test_zoom_in_increases_the_font_size",
    f"{ZOOM}::test_zoom_reset_returns_to_the_starting_size",
    f"{ZOOM}::test_zoom_in_is_clamped_to_the_maximum",
    f"{ZOOM}::test_zoom_out_is_clamped_to_the_minimum",
)

# --- expected catchers, by the name pytest reports (parameters stripped) ----

ROUND_TRIP = "test_zoom_persists_through_the_settings_route_and_a_restart"
STORED_VALIDATED = "test_stored_size_is_validated_when_the_dialog_is_built"
FAILED_RESTORES = "test_failed_save_restores_the_confirmed_size"
REFUSED_RESTORES = "test_refused_queue_restores_the_confirmed_size"
REFUSED_BUTTON_KEY = (
    "test_refused_queue_for_a_button_key_leaves_the_font_size_alone"
)
FAILED_INVALID = (
    "test_failed_save_with_an_invalid_stored_value_restores_a_valid_size"
)
RECONCILE = "test_reconcile_reply_applies_the_stored_size_to_the_dialog"
FAILED_BUTTON_KEY = "test_failed_save_of_a_button_key_leaves_the_font_size_alone"
APPLY_SILENT = "test_apply_font_point_size_emits_nothing"
APPLY_NO_WRITE = "test_apply_font_point_size_through_the_manager_sends_no_write"
USER_ZOOM_EMITS = "test_user_zoom_emits_only_when_the_size_changes"

MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=SELECTION,
                          file=SERVICE / file, old=old, new=new,
                          expect=list(expect)))


DIALOG_SRC = "pattern_manager_dialog.py"
GUI_SRC = "gui.py"

# --- the dialog: which size changes are reported ----------------------------

USER_SET = (
    "        if self._set_font_point_size(point_size):\n"
    "            self.font_size_changed.emit(self._current_font_point_size)\n"
)

add("user-zoom-emits-nothing", DIALOG_SRC,
    USER_SET,
    "        if self._set_font_point_size(point_size):\n"
    "            pass\n",
    USER_ZOOM_EMITS, ROUND_TRIP, FAILED_RESTORES, REFUSED_RESTORES)
# The saved-settings apply reported as a user choice: every restore and every
# start then writes the size back.
add("apply-emits-font-size-changed", DIALOG_SRC,
    "            point_size = self._default_font_point_size\n"
    "        self._set_font_point_size(point_size)\n",
    "            point_size = self._default_font_point_size\n"
    "        self._user_set_font_point_size(point_size)\n",
    APPLY_SILENT, APPLY_NO_WRITE, FAILED_RESTORES, RECONCILE)
# ROUND_TRIP is not a catcher here: _open_pattern_manager applies the stored
# size before it connects font_size_changed, so the mutant's emit at open
# reaches no slot and the restart leg cannot see it.
# The emit fires whether or not the size changed (at a bound, or a reset at
# the default).
add("emit-fires-without-a-change", DIALOG_SRC,
    USER_SET,
    "        if self._set_font_point_size(point_size) or True:\n"
    "            self.font_size_changed.emit(self._current_font_point_size)\n",
    USER_ZOOM_EMITS)
# The same defect one level down: the setter claims a change it did not make.
add("setter-reports-a-change-when-none", DIALOG_SRC,
    "        if clamped == self._current_font_point_size:\n"
    "            return False\n",
    "        if clamped == self._current_font_point_size:\n"
    "            return True\n",
    USER_ZOOM_EMITS)
# None must mean the dialog's default size, not "leave it where it is".
add("apply-none-keeps-the-current-size", DIALOG_SRC,
    "        if point_size is None:\n"
    "            point_size = self._default_font_point_size\n",
    "        if point_size is None:\n"
    "            point_size = self._current_font_point_size\n",
    APPLY_SILENT, FAILED_RESTORES, FAILED_INVALID)

# --- gui.py: validation of a stored value ------------------------------------

VALIDATE = (
    "    if not isinstance(value, int) or isinstance(value, bool):\n"
    "        return None\n"
    "    return max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, value))\n"
)

add("validation-accepts-a-bool", GUI_SRC,
    VALIDATE,
    VALIDATE.replace(" or isinstance(value, bool)", ""),
    STORED_VALIDATED, FAILED_INVALID)
# A str would crash inside min(); a float runs through: it is accepted and
# converted, so the stored 12.0 becomes a confirmed 12 instead of None.
add("validation-accepts-a-float", GUI_SRC,
    VALIDATE,
    "    if not isinstance(value, (int, float)) or isinstance(value, bool):\n"
    "        return None\n"
    "    return max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, int(value)))\n",
    STORED_VALIDATED)
# The dialog clamps again, so only the confirmed baseline shows these two.
add("validation-high-clamp-removed", GUI_SRC,
    "    return max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, value))\n",
    "    return max(_FONT_SIZE_MIN, value)\n",
    STORED_VALIDATED)
add("validation-low-clamp-removed", GUI_SRC,
    "    return max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, value))\n",
    "    return min(_FONT_SIZE_MAX, value)\n",
    STORED_VALIDATED)

# --- gui.py: the stored size at start and at open ----------------------------

add("constructor-ignores-the-stored-size", GUI_SRC,
    "            _valid_pattern_manager_font_size(\n"
    "                (config or {}).get(PATTERN_MANAGER_FONT_SIZE_KEY)))\n",
    "            _valid_pattern_manager_font_size(\n"
    "                None))\n",
    STORED_VALIDATED, ROUND_TRIP)

OPEN_WIRING = (
    "            self._restore_pattern_manager_font_size()\n"
    "            self._pm_dialog.font_size_changed.connect(\n"
    "                self._send_pattern_manager_font_size\n"
    "            )\n"
)

add("open-does-not-apply-the-stored-size", GUI_SRC,
    OPEN_WIRING,
    OPEN_WIRING.replace(
        "            self._restore_pattern_manager_font_size()\n",
        "            pass\n"),
    STORED_VALIDATED, ROUND_TRIP, APPLY_NO_WRITE)
add("font-size-changed-not-connected", GUI_SRC,
    OPEN_WIRING,
    "            self._restore_pattern_manager_font_size()\n"
    "            pass\n",
    ROUND_TRIP, FAILED_RESTORES, REFUSED_RESTORES)

# --- gui.py: the save ---------------------------------------------------------

add("save-sends-a-different-key", GUI_SRC,
    "                           'key': PATTERN_MANAGER_FONT_SIZE_KEY, 'value': size})\n",
    "                           'key': 'PATTERN_MANAGER_FONT', 'value': size})\n",
    ROUND_TRIP, FAILED_RESTORES, REFUSED_RESTORES)

# --- gui.py: the queue refused the write --------------------------------------

QUEUE_FULL = (
    "            if PATTERN_MANAGER_FONT_SIZE_KEY in values:\n"
    "                self._restore_pattern_manager_font_size()\n"
)

add("queue-full-restore-removed", GUI_SRC,
    QUEUE_FULL,
    "            if PATTERN_MANAGER_FONT_SIZE_KEY in values:\n"
    "                pass\n",
    REFUSED_RESTORES)
add("queue-full-restores-for-every-key", GUI_SRC,
    QUEUE_FULL,
    QUEUE_FULL.replace("PATTERN_MANAGER_FONT_SIZE_KEY in values", "True"),
    REFUSED_BUTTON_KEY)

# --- gui.py: a reply settled the newest font-size write -----------------------

RESULT = (
    "        if PATTERN_MANAGER_FONT_SIZE_KEY in keys:\n"
    "            self._restore_pattern_manager_font_size()\n"
)

add("result-restore-removed", GUI_SRC,
    RESULT,
    "        if PATTERN_MANAGER_FONT_SIZE_KEY in keys:\n"
    "            pass\n",
    FAILED_RESTORES, FAILED_INVALID, RECONCILE)
add("result-restores-for-every-key", GUI_SRC,
    RESULT,
    RESULT.replace("PATTERN_MANAGER_FONT_SIZE_KEY in keys", "True"),
    FAILED_BUTTON_KEY)
# Reconcile gap 3: a reconcile reply after a lost result is not a failure,
# and the dialog must still follow it.
add("result-restores-only-on-failure", GUI_SRC,
    RESULT,
    RESULT.replace("PATTERN_MANAGER_FONT_SIZE_KEY in keys",
                   "failed and PATTERN_MANAGER_FONT_SIZE_KEY in keys"),
    RECONCILE)

# --- gui.py: the restore helper validates what it applies ---------------------

add("restore-skips-validation", GUI_SRC,
    "        dialog.apply_font_point_size(_valid_pattern_manager_font_size(\n"
    "            self._settings_confirmed.get(PATTERN_MANAGER_FONT_SIZE_KEY)))\n",
    "        dialog.apply_font_point_size(\n"
    "            self._settings_confirmed.get(PATTERN_MANAGER_FONT_SIZE_KEY))\n",
    FAILED_INVALID)


# --- names: exact --only, and the runner's substring filter stays exact -------

_NAMES = [m["name"] for m in MUTATIONS]
assert len(set(_NAMES)) == len(_NAMES), "duplicate mutation name"
for _a in _NAMES:
    for _b in _NAMES:
        assert _a == _b or _a not in _b, (
            f"mutation name {_a!r} is a substring of {_b!r}; the runner's "
            "name filter would select both")


def _argv(args):
    """Translate ``--only a,b`` into the runner's positional name filters."""
    out, only = [], []
    it = iter(args)
    for arg in it:
        if arg == "--only":
            value = next(it, None)
            if value is None:
                raise SystemExit("ERROR --only needs a name")
            only.extend(n for n in value.split(",") if n)
        elif arg.startswith("--only="):
            only.extend(n for n in arg[len("--only="):].split(",") if n)
        else:
            out.append(arg)
    unknown = [n for n in only if n not in _NAMES]
    if unknown:
        raise SystemExit(f"ERROR --only names no mutation: {unknown}")
    return out + only


# --- the reason check on each catch ------------------------------------------

_CURRENT = [None]
_SUMMARIES = {}   # mutation name -> [(test base name, reason)]
_ASSERTION = re.compile(r"^(?:AssertionError\b|assert\b|Failed: DID NOT RAISE)")

_orig_save_recovery = runner._save_recovery
_orig_failed_names = runner._failed_names


def _save_recovery(path, original, mutated, name):
    # Called once per mutation, before the mutant is written: it names the
    # mutation whose pytest output _failed_names reads next.
    _CURRENT[0] = name
    return _orig_save_recovery(path, original, mutated, name)


def _failed_names_with_reasons(output):
    failed = _orig_failed_names(output)
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "short test summary info" in line:
            lines = lines[index + 1:]
            break
    records = []
    for line in lines:
        if not line.startswith("FAILED "):
            continue
        nodeid, _, reason = line[len("FAILED "):].partition(" - ")
        base = re.sub(r"\[.*\]$", "", nodeid.split("::")[-1].strip())
        records.append((base, reason.strip()))
    if _CURRENT[0] is not None:
        _SUMMARIES[_CURRENT[0]] = records
    return failed


runner._save_recovery = _save_recovery
runner._failed_names = _failed_names_with_reasons


def _reason_errors():
    """Every catch whose expected catcher failed on no assertion at all."""
    errors = []
    for m in MUTATIONS:
        records = _SUMMARIES.get(m["name"])
        if records is None:
            continue
        failed = {name for name, _ in records}
        if not set(m["expect"]) <= failed:
            continue  # a survivor; the runner already reported it
        for test in m["expect"]:
            reasons = [r for name, r in records if name == test]
            good = [r for r in reasons if _ASSERTION.match(r)]
            if good:
                print(f"assertion {m['name']} / {test}: {good[0][:160]}")
            else:
                errors.append(f"{m['name']}: catch-not-from-an-assertion by "
                              f"{test}: {reasons}")
    return errors


def main(args):
    argv = _argv(args)
    code = runner.run(MUTATIONS, argv)
    if "--check" in argv:
        return code
    errors = _reason_errors()
    for e in errors:
        print(f"ERROR {e}")
    print(f"reason check: {len(errors)} catches not from an assertion")
    return 1 if (code or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
