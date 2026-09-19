"""Mutation gate for the removed tray items (wh-remove-restart-credentials-items).

David ordered both "Restart Transcription Service" and "Google Cloud
Credentials" off the menu, and their code paths deleted with them
(QUESTIONS-2026-09-04.md item 20).  Nothing in the running program now
prevents somebody from adding either label back: the only thing that does is
the guard class

    tests/test_gui.py::TestRemovedMenuItems

whose four absence tests for those two labels read them out of both menu
builders.  An
absence assertion is the kind that can pass for the wrong reason -- a menu
that failed to build its gated items at all would satisfy every one of them --
so the class also carries two tests that must stay GREEN under every mutation
here, proving the fixture opens the STT-provider gate the credentials item
used to sit behind.

The gate holds six mutations today. The first four are the original set,
one per label per builder, because gui.py builds the tray menu (pystray) and
the floating-button menu (Qt) from separate code:

    tray-restart-item-restored     -> the tray test must fail
    button-restart-item-restored   -> the button test must fail
    tray-credentials-item-restored -> the tray test must fail
    button-credentials-item-restored -> the button test must fail

Two more came with the move of "Teach WheelHouse your voice..." into the STT
Provider submenu (wh-voice-teaching-stt-submenu). One per branch, because
each branch holds its own copy of the entry, and a move done in one branch
only is the mistake a two-branch builder invites:

    qt-voice-teaching-not-in-submenu       -> the Qt submenu loses the entry
    tray-voice-teaching-callback-does-nothing -> the tray entry stops asking
                                                 the Qt thread to open the
                                                 session

Each mutation restores only the LABEL, with an inert callback.  Wiring the
old callbacks back would be a false catch in the language of the
mutation-gate skill: self.request_stt_restart and
self._pick_google_credentials_file are deleted, so naming either one raises
AttributeError while the menu is being built, and the test would then fail on
the crash instead of on the label it claims to pin.

Run it from services/wheelhouse:

    uv run python tests/mutation_gate_removed_menu_items.py
    uv run python tests/mutation_gate_removed_menu_items.py --check
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

SERVICE_ROOT = Path(__file__).resolve().parent.parent
TARGET = SERVICE_ROOT / "gui.py"
# The two voice-teaching mutations below have catchers in two classes beside
# TestRemovedMenuItems, and the expected-name check refuses to start on a name
# pytest never collects.
SELECTION = [
    "tests/test_gui.py",
    "-k",
    "TestRemovedMenuItems or TestTheTwoMenusOfferTheSameEntries"
    " or TestVoiceTeachingSitsInTheSttProviderSubmenu",
]

# Every mutation must leave these two passing.  They are the evidence that the
# absence assertions are not passing vacuously.
MUST_STAY_GREEN = [
    "test_the_tray_menu_still_builds_its_gated_items",
    "test_the_button_menu_still_builds_its_gated_items",
]

_TRAY_RESTART_ANCHOR = (
    '                pystray.MenuItem("Restart Wheelhouse", '
    "self.request_restart, enabled=is_ready),"
)

_TRAY_PROVIDER_ANCHOR = (
    "                menu_items.append(\n"
    '                    pystray.MenuItem("STT Provider", pystray.Menu(*provider_items))\n'
    "                )"
)

_BUTTON_RESTART_ANCHOR = '            restart_action = QAction("Restart Wheelhouse", self)'

_BUTTON_PROVIDER_ANCHOR = "                menu.addMenu(stt_submenu)"

# The whole Qt voice-teaching block, separator included, and it is replaced
# by ``pass`` rather than by nothing, for two reasons. The block is the only
# statement after the provider loop inside ``if self.stt_providers_available:``
# that is not the ``menu.addMenu`` call, so deleting it alone would still
# compile -- but removing the first line alone would not: the four lines
# below it name ``cal_action``, and a NameError while the menu is built would
# fail every test that builds a menu, which is a false catch in the language
# of the mutation-gate skill. Taking the whole block keeps the mutant honest:
# the Qt submenu loses the entry and nothing else changes. The identifier
# ``cal_action`` appears nowhere else in gui.py, so the anchor cannot match a
# second place.
_BUTTON_VOICE_TEACHING_ANCHOR = (
    "                stt_submenu.addSeparator()\n"
    '                cal_action = stt_submenu.addAction("Teach WheelHouse your voice...")\n'
    "                cal_action.setEnabled(is_ready)\n"
    "                cal_action.setToolTip(\n"
    '                    "Open the voice-teaching session for the Distil-Whisper engine."\n'
    "                )\n"
    "                cal_action.triggered.connect(self._open_calibration)"
)

# The tray callback, and only the callback. The label, the readiness gate and
# the separator above it all stay, so the mutant builds a menu that looks
# right and does nothing, which is what a disconnected handler is. The
# replacement is a lambda of the same shape, so pystray still has something
# callable to hold and the catcher fails on the queue it never received
# rather than on a crash.
_TRAY_VOICE_TEACHING_CALLBACK_ANCHOR = (
    '                        lambda: self.state_from_logic_queue.put('
    '{"action": "open_calibration"}),'
)

MUTATIONS = [
    {
        "name": "tray-restart-item-restored",
        "old": _TRAY_RESTART_ANCHOR,
        "new": (
            '                pystray.MenuItem("Restart Transcription Service", '
            "lambda: None, enabled=is_ready),\n"
            + _TRAY_RESTART_ANCHOR
        ),
        "expect": ["test_the_tray_menu_has_no_restart_transcription_service"],
    },
    {
        "name": "button-restart-item-restored",
        "old": _BUTTON_RESTART_ANCHOR,
        "new": (
            '            stt_restart_action = QAction("Restart Transcription Service", self)\n'
            "            stt_restart_action.setEnabled(is_ready)\n"
            "            menu.addAction(stt_restart_action)\n"
            "\n"
            + _BUTTON_RESTART_ANCHOR
        ),
        "expect": ["test_the_button_menu_has_no_restart_transcription_service"],
    },
    {
        "name": "tray-credentials-item-restored",
        "old": _TRAY_PROVIDER_ANCHOR,
        "new": (
            _TRAY_PROVIDER_ANCHOR
            + "\n"
            '                if "google_stt" in self.stt_providers_available:\n'
            "                    menu_items.append(\n"
            '                        pystray.MenuItem("Google Cloud Credentials",\n'
            "                                         lambda: None, enabled=is_ready)\n"
            "                    )"
        ),
        "expect": ["test_the_tray_menu_has_no_google_cloud_credentials"],
    },
    {
        "name": "button-credentials-item-restored",
        "old": _BUTTON_PROVIDER_ANCHOR,
        "new": (
            _BUTTON_PROVIDER_ANCHOR
            + "\n"
            '                if "google_stt" in self.stt_providers_available:\n'
            '                    creds_action = menu.addAction("Google Cloud Credentials")\n'
            "                    creds_action.setEnabled(is_ready)"
        ),
        "expect": ["test_the_button_menu_has_no_google_cloud_credentials"],
    },
    {
        "name": "qt-voice-teaching-not-in-submenu",
        "old": _BUTTON_VOICE_TEACHING_ANCHOR,
        "new": "                pass",
        # The second name is the point of the mutation. The move has to
        # happen in both branches, and the shared comparison is what refuses
        # a move done in one of them; the first name only proves the Qt
        # branch itself lost the entry.
        "expect": [
            "test_the_window_submenu_lists_the_engines_then_the_entry",
            "test_the_two_menus_list_the_same_submenu_entries",
        ],
    },
    {
        "name": "tray-voice-teaching-callback-does-nothing",
        "old": _TRAY_VOICE_TEACHING_CALLBACK_ANCHOR,
        "new": "                        lambda: None,",
        "expect": [
            "test_the_tray_entry_asks_the_qt_thread_to_open_the_session",
        ],
    },
]

TIMEOUT_SECONDS = 300


def _file_newline(path: Path) -> bytes:
    raw = path.read_bytes()
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _to_file_endings(text: str, newline: bytes) -> str:
    if newline == b"\r\n":
        return text.replace("\n", "\r\n")
    return text


def _clear_pycache() -> None:
    for cache in SERVICE_ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _collected_test_names() -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"] + SELECTION,
        cwd=SERVICE_ROOT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    names = set()
    for line in proc.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split(" ")[0])
    return names


def _run_selection() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:randomly"] + SELECTION,
        cwd=SERVICE_ROOT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _restore(original: bytes) -> bool:
    """Put the real source back, prove it is back, and say whether it is.

    The verdict travels back to the caller because a gate that cannot
    restore has to stop: every later mutation would judge code nobody
    meant to run, and a run ending "0 survivors" would leave the mutant
    in a tracked file.  A second Ctrl+C landing inside the restore must
    not escape with the mutant still there, and the bytecode caches have
    to go before the interrupt is re-raised: a same-size mutant leaves
    current-looking bytecode behind.
    """
    held: KeyboardInterrupt | None = None
    for attempt in (0, 1):
        try:
            TARGET.write_bytes(original)
            _clear_pycache()
        except KeyboardInterrupt as interrupt:
            if attempt:
                print("ERROR could not restore %s -- the mutant is still in the file"
                      % TARGET)
                raise
            held = interrupt
            continue
        except OSError as exc:
            if attempt:
                print("ERROR could not restore %s (%s) -- the mutant is still in the file"
                      % (TARGET, exc))
                return False
            continue
        try:
            written = TARGET.read_bytes()
        except OSError as exc:
            print("ERROR could not read %s back (%s) -- treating it as unrestored"
                  % (TARGET, exc))
            return False
        if written == original:
            if held is not None:
                raise held
            return True
        if attempt:
            print("ERROR %s still differs from the original after the restore" % TARGET)
            return False
    return False


def _apply_and_run(mutated: str, original: bytes) -> tuple[int | None, str, str, bool]:
    """Write the mutant, run the selection, and always put the source back.

    The write sits INSIDE the try, so a write that opens the file and
    then fails still reaches the restore; the old shape wrote first and
    left the restore to a finally the write could never enter.  The
    restore's own verdict comes back with the run's, because the caller
    has to stop when the source is not provably back.

    Returns (returncode, output, status, restored); status is "ran",
    "timeout" or "write-error".
    """
    rc: int | None = None
    out = ""
    status = "ran"
    try:
        TARGET.write_bytes(mutated.encode("utf-8"))
        _clear_pycache()
        rc, out = _run_selection()
    except subprocess.TimeoutExpired:
        status = "timeout"
    except OSError as exc:
        status = "write-error"
        out = str(exc)
    finally:
        restored = _restore(original)
    return rc, out, status, restored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="verify each pattern matches exactly once and compiles; run nothing")
    args = parser.parse_args()

    original = TARGET.read_bytes()
    source = original.decode("utf-8")
    newline = _file_newline(TARGET)

    errors: list[str] = []
    prepared = []
    for mutation in MUTATIONS:
        old = _to_file_endings(mutation["old"], newline)
        new = _to_file_endings(mutation["new"], newline)
        count = source.count(old)
        if count == 0:
            errors.append("%s: pattern-not-found" % mutation["name"])
            continue
        if count > 1:
            errors.append("%s: pattern-ambiguous (%d matches)" % (mutation["name"], count))
            continue
        mutated = source.replace(old, new, 1)
        try:
            compile(mutated, str(TARGET), "exec")
        except SyntaxError as exc:
            errors.append("%s: does-not-compile (%s)" % (mutation["name"], exc))
            continue
        prepared.append((mutation, mutated))

    if args.check:
        print("checked %d patterns, %d stale or broken" % (len(MUTATIONS), len(errors)))
        for line in errors:
            print("  ERROR " + line)
        return 1 if errors else 0

    if errors:
        for line in errors:
            print("ERROR " + line)
        return 1

    collected = _collected_test_names()
    for name in MUST_STAY_GREEN:
        if name not in collected:
            print("ERROR must-stay-green test not collected: %s" % name)
            return 1
    for mutation, _ in prepared:
        for name in mutation["expect"]:
            if name not in collected:
                print("ERROR %s: expected test name not collected: %s"
                      % (mutation["name"], name))
                return 1

    _clear_pycache()
    rc, out = _run_selection()
    if rc != 0:
        print("ERROR baseline is not green; refusing to mutate")
        print(out[-3000:])
        return 1
    print("baseline green")

    survivors = []
    try:
        for mutation, mutated in prepared:
            rc, out, status, restored = _apply_and_run(mutated, original)
            if not restored:
                print("ERROR %s: %s was not restored -- stopping the gate"
                      % (mutation["name"], TARGET))
                return 1
            if status == "write-error":
                print("ERROR %s: could not write the mutant (%s)"
                      % (mutation["name"], out))
                return 1
            if status == "timeout":
                print("ERROR %s: never-terminates" % mutation["name"])
                survivors.append(mutation["name"])
                continue

            if "+++ Timeout +++" in out:
                print("ERROR %s: suite-timeout-abort, no verdict" % mutation["name"])
                survivors.append(mutation["name"])
                continue

            failed = {line.split("::")[-1].split(" ")[0]
                      for line in out.splitlines() if line.startswith("FAILED")}
            collapsed = [n for n in MUST_STAY_GREEN if n in failed]
            if collapsed:
                print("ERROR %s: the menu stopped building (%s failed) -- no verdict"
                      % (mutation["name"], ", ".join(collapsed)))
                survivors.append(mutation["name"])
                continue

            missing = [n for n in mutation["expect"] if n not in failed]
            if rc == 0 or missing:
                print("SURVIVED %s (expected failures not seen: %s)"
                      % (mutation["name"], ", ".join(missing) or "none"))
                survivors.append(mutation["name"])
            else:
                print("caught   %s by %s" % (mutation["name"], ", ".join(sorted(failed))))
    except KeyboardInterrupt:
        _restore(original)
        raise

    print("")
    print("ran %d of %d mutations, %d survivors" % (len(prepared), len(MUTATIONS), len(survivors)))
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
