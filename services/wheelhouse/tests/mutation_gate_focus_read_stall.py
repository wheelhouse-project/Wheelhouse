"""Mutation evidence for wh-insert-focus-read-stall.

Each mutation removes one part of the repair for the focused-control read
that blocks and then raises COMError, and names the test that must fail when
it is gone. The shared runner in
services/stt_providers/shared/tests/mutation_gate_runner.py owns pattern
matching, the exactly-one-match check, the compile check, the per-mutation
timeout, and source restoration; this file only declares the table.

Full sweep:
  <the wheelhouse interpreter> tests/mutation_gate_focus_read_stall.py
Pattern check only (never a substitute for the sweep):
  <the wheelhouse interpreter> tests/mutation_gate_focus_read_stall.py --check
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

TEST = "tests/test_ui/test_context_focus_read_failure.py"
MUTATIONS = []


COMMAND_TEST = "tests/test_ui/test_command_paths_failed_focus_read.py"


def add(name, file, old, new, *expect, test_file=TEST):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=test_file,
                          file=SERVICE / file, old=old, new=new,
                          expect=list(expect)))


# A1b. capture_context reads the focused control exactly once and never
# retries a failure (boss ruling 2026-09-18 on finding
# wh-insert-focus-read-stall.1.1, which withdrew an earlier ruling that
# retried a failure returning in under half a second). This mutation puts the
# retry back, in the shape the withdrawn ruling had minus its entry test, so a
# later session cannot restore the retry unnoticed. Nothing can bound a second
# read once it has started: a first failure at 0.49 seconds and a second call
# blocking 5.5 seconds passes the 5.0 second request limit in app.py and fires
# its timeout ERROR, which is a user notification.
add("restore-a-second-focused-control-read", "ui/context.py",
    "        read_error = error\n"
    "        focused_control = None\n"
    "        focus_read_failed = True",
    "        read_error = error\n"
    "        try:\n"
    "            focused_control = auto.GetFocusedControl()\n"
    "            read_error = None\n"
    "        except Exception as second_error:\n"
    "            read_error = second_error\n"
    "        if read_error is not None:\n"
    "            focused_control = None\n"
    "            focus_read_failed = True",
    "test_a_failed_read_is_never_retried")

# A2. The identity on the failure path comes from the capture made BEFORE
# the read. Without that capture there is nothing to paste into.
add("drop-the-capture-made-before-the-read", "ui/context.py",
    "    pre_read_identity = capture_foreground_identity()",
    "    pre_read_identity = TargetIdentity()",
    "test_the_identity_is_the_capture_made_before_the_read")

# A2. The word is pasted when the foreground held: handing on the empty
# record instead would make ui/router.py take its silent drop.
add("drop-the-word-although-the-foreground-held", "ui/context.py",
    "            target_identity = pre_read_identity",
    "            target_identity = TargetIdentity()",
    "test_the_identity_is_the_capture_made_before_the_read")

# A2b. The word is dropped when the foreground moved during the read.
add("paste-into-whatever-the-foreground-became", "ui/context.py",
    "        elif current_foreground_root() != pre_read_identity.root:",
    "        elif False:",
    "test_a_moved_foreground_gives_the_empty_identity",
    "test_one_warning_says_the_target_window_changed")

# A2c. The numeric root compare above is not enough on its own: Windows
# reuses top-level handles, so the provenance markers of the pre-read
# identity are re-read before the paste branch is chosen. Without that
# re-read the stale identity reaches verified_paste, which refuses it at
# ERROR and fires a user-visible Windows notice (wh-insert-focus-read-
# stall.1.2).
add("hand-on-a-recycled-window-without-re-reading-its-marker", "ui/context.py",
    "        elif (\n"
    "            read_hwnd_provenance(pre_read_identity.root)",
    "        elif False and (\n"
    "            read_hwnd_provenance(pre_read_identity.root)",
    "test_a_lost_provenance_marker_gives_the_empty_identity",
    "test_one_warning_reports_the_recycled_window_and_no_error_is_logged")

# A2d. Branch 1 -- the read failed and nothing could be captured before it --
# is logged at WARNING for the same reason as the branch below
# (wh-insert-focus-read-stall.1.3).
add("report-an-uncapturable-target-at-error-level", "ui/context.py",
    '            logger.warning(\n'
    '                "Focused-control read failed and no target window identity "',
    '            logger.error(\n'
    '                "Focused-control read failed and no target window identity "',
    "test_an_empty_pre_read_capture_drops_the_word_with_one_warning")

# A2. The failure is logged at WARNING. An ERROR record reaches
# ErrorNotificationHandler and shows the user a Windows notice.
add("report-the-failed-read-at-error-level", "ui/context.py",
    '            logger.warning(\n'
    '                "Focused-control read failed; the target window is unchanged, "',
    '            logger.error(\n'
    '                "Focused-control read failed; the target window is unchanged, "',
    "test_one_warning_names_the_com_error_and_no_error_is_logged")

# A3. No code change was owed here, because the elevation check already
# resolves the target through the foreground window when the control gives
# no handle. This mutation is what keeps that true.
add("drop-the-elevated-foreground-fallback", "ui/elevation_check.py",
    "    if not hwnd:\n"
    "        try:\n"
    "            hwnd = int(win32gui.GetForegroundWindow() or 0)\n"
    "        except Exception:\n"
    "            hwnd = 0\n",
    "    if not hwnd:\n"
    "        pass\n",
    "test_the_foreground_fallback_still_reports_an_elevated_target",
    "test_the_router_refuses_a_failed_read_context")

# A4. The four COMMAND actions stop on a failed read
# (wh-insert-focus-read-stall.1.4). One mutation covers all four, because
# all four call the one shared guard. Neutering it puts back the state
# codex round 3 found: transform_selection and wrap_or_insert send Ctrl+C,
# press_key_action sends the key, and hotkey_action sends the chord, each
# into whatever window then holds the foreground. The guard re-raises the
# original read error, so the catchers below measure what a user meets --
# a key sent -- rather than the shape of the guard.
add("let-the-command-actions-run-after-a-failed-read",
    "ui/ui_action_handler.py",
    "    if not context.focus_read_failed:\n"
    "        return\n"
    "    if context.read_error is None:",
    "    if True:\n"
    "        return\n"
    "    if context.read_error is None:",
    "test_transform_selection_sends_nothing",
    "test_wrap_or_insert_sends_nothing",
    "test_press_key_action_sends_nothing",
    "test_hotkey_action_sends_nothing",
    "test_the_original_read_error_reaches_the_log",
    test_file=COMMAND_TEST)

# A5. The accepted limit of wh-insert-focus-read-stall.1.5 (boss ruling
# 2026-09-18, option A): a failed read leaves no control, and the paste must
# still proceed. This mutation removes the None guard in front of the SetFocus
# correction, so the failure path calls SetFocus on nothing. The except arm
# catches the AttributeError and refuses at ERROR, which both drops the word
# this bead exists to deliver and shows the user a Windows notice. The
# mutation runs to the assertion rather than crashing the test, because the
# production code catches the error itself.
add("call-setfocus-on-the-control-a-failed-read-never-produced",
    "ui/clipboard_operations.py",
    "        if resolved_control is not None:\n"
    "            try:\n"
    "                resolved_control.SetFocus()",
    "        if True:\n"
    "            try:\n"
    "                resolved_control.SetFocus()",
    "test_a_control_of_none_skips_setfocus_and_still_proves_the_window")


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
