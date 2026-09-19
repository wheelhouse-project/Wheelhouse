"""Test whether a following command acts on exactly the selected phrase.

This is the second probe for spoken phrase commands. The first probe
(spoken_phrase_probe.py) proved that Wheelhouse can find a spoken phrase
and select it, in about 4 milliseconds in Notepad and about 17 in a
72-page Word document. This probe answers the question that one could not,
which the feasibility report names as its largest risk:

    Is the selected range still the same text when the next command runs?

WHY THIS EXISTS (2026-08-18, bd issue wh-spoken-phrase-target).
Selecting text is safe. Acting on a selection is where a mistake destroys
a document that somebody wrote. Between the moment Wheelhouse selects a
phrase and the moment it sends a Delete key, several things can change.
Focus can move to another control. The application can edit the document.
The range Wheelhouse is holding can stop describing the text it described
a moment ago. None of that was measured by the first probe.

TWO MODES.

The default mode changes nothing. It finds the phrase, selects it, waits,
and then asks the control whether the range still holds the same text and
whether focus is still the same control. This measures staleness without
editing anything, so it is safe to run against a document you care about.

The --destructive mode also sends a Delete key and then compares the whole
document before and after. It proves the key removed exactly the selected
phrase and nothing else. USE A SCRATCH DOCUMENT. The probe refuses to run
this mode when it cannot trust its own before-and-after comparison.

WHY THE DESTRUCTIVE MODE REFUSES A LARGE DOCUMENT. The comparison needs
the complete document text. Word truncates a whole-document read at 65000
characters while its search covers the whole document, measured
2026-08-18. A truncated read would make the comparison meaningless, so
this probe refuses to delete anything when the read looks truncated.

WORD REMOVES ONE MORE CHARACTER THAN IT SELECTS. Measured 2026-08-19 in
five positions in one sentence. Word selects the phrase correctly and the
Delete key acts on that selection, but Word also removes one adjoining
space. Deleting "brown fox" from "The quick brown fox jumps over the lazy
dog." leaves "The quick jumps over the lazy dog.", with one space between
"quick" and "jumps", not two. This happened in all five positions,
including the start of the document and a phrase before a full stop.

That is Word's smart cut and paste setting, under File, Options,
Advanced, Cut copy and paste. This probe reports it as a difference from
the exact deletion, which it is. For a spoken delete command the result
is better English than the exact deletion would be. For a spoken replace
command it is a difference the design must handle. Notepad does not do
this: it removes exactly the selected characters.

MANUAL VARIATIONS WORTH RUNNING.

1. Run with --delay-ms 10000 and click into another window during the
   wait. That tests what happens when focus moves after the selection.
2. Run with --delay-ms 10000 and type into the document during the wait.
   That tests what happens when the document changes underneath the
   range.
3. Run with --delay-ms 0 for the case Wheelhouse would actually produce.

USAGE. Both forms open their own scratch document and close it again.

    ... spoken_phrase_action_probe.py "brown fox" --focus-notepad
        --prepare "The quick brown fox jumps over the lazy dog."
    ... spoken_phrase_action_probe.py "brown fox"
        --focus-word "The quick brown fox jumps over the lazy dog."

--focus-word builds a small .docx on disk rather than typing into Word.
The key path drops characters (see notepad_typing_trials.py), and a file
built on disk holds exactly the text the probe asked for.

No emojis in output (Windows CP1252 consoles).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

import uiautomation as auto

import scratch_window

# Wheelhouse's own key sender lives in the wheelhouse service. Use it
# rather than the uiautomation library's key helper, so this probe tests
# the same mechanism the product would use.
WHEELHOUSE_DIR = Path(__file__).resolve().parents[2] / "services" / "wheelhouse"

COUNTDOWN_SECONDS = 5
DEFAULT_DELAY_MS = 0

# Above this, a whole-document read is not trustworthy enough to compare
# before and after. Word truncated at exactly 65000 characters.
MAX_DESTRUCTIVE_DOCUMENT = 50000

# Modern Windows 11 Notepad document control.
NOTEPAD_CLASS = "RichEditD2DPT"

# Seconds to wait after a key burst, before reading the document back.
READ_SETTLE_SECONDS = 0.6

# The Notepad key path drops and repeats characters. Measured 2026-08-19
# by scripts/benchmarks/notepad_typing_trials.py: 19 of 43 typing bursts
# arrived wrong while the key sender reported every event accepted. So
# --prepare types, reads back, and repeats until the text is exact.
PREPARE_ATTEMPTS = 12


def prepare_notepad_document(control, text_pattern, text: str) -> bool:
    """Put exactly ``text`` into an empty Notepad document.

    Returns True when the document reads back exactly. Refuses, and
    returns False, when the control is not Notepad or the document is not
    empty.
    """
    sys.path.insert(0, str(WHEELHOUSE_DIR))
    from utils.win_input_sender import type_string_verified, verified_press_keys

    class_name = getattr(control, "ClassName", "?")
    if class_name != NOTEPAD_CLASS:
        print(f"[x] --prepare needs a Notepad document control, not "
              f"{class_name!r}.")
        return False

    existing = text_pattern.DocumentRange.GetText(-1)
    if existing.strip():
        print(f"[x] --prepare refuses: the document already holds "
              f"{len(existing)} characters.")
        print(f"[x] It starts: {existing[:60]!r}")
        print("[x] Open an empty Notepad tab and run this again.")
        return False

    for attempt in range(1, PREPARE_ATTEMPTS + 1):
        type_string_verified(text)
        time.sleep(READ_SETTLE_SECONDS)
        arrived = text_pattern.DocumentRange.GetText(-1)
        if arrived.strip() == text:
            print(f"[+] prepared the document on attempt {attempt}")
            return True
        verified_press_keys("ctrl", "a")
        verified_press_keys("delete")
        time.sleep(READ_SETTLE_SECONDS)

    print(f"[x] --prepare could not type the text exactly in "
          f"{PREPARE_ATTEMPTS} attempts.")
    print("[x] The Notepad key path drops characters; see "
          "scripts/benchmarks/notepad_typing_trials.py.")
    return False


def countdown(seconds: int) -> None:
    print("[!] Focus the target window and click into the text.")
    for remaining in range(seconds, 0, -1):
        print(f"    grabbing the focused control in {remaining}...")
        time.sleep(1)


def control_identity(control) -> tuple:
    """A value that changes when focus moves to a different control."""
    try:
        runtime_id = tuple(control.GetRuntimeId())
    except Exception:
        runtime_id = ()
    try:
        class_name = control.ClassName
    except Exception:
        class_name = "?"
    try:
        name = control.Name
    except Exception:
        name = "?"
    return (runtime_id, class_name, name)


def read_document(text_pattern) -> str | None:
    try:
        return text_pattern.DocumentRange.GetText(-1)
    except Exception as exc:
        print(f"[!] could not read the document text: {exc}")
        return None


def looks_truncated(text: str) -> bool:
    """A length that is an exact multiple of 1000 is almost certainly a cap."""
    return len(text) > 0 and len(text) % 1000 == 0


def describe_difference(before: str, after: str, removed: str) -> None:
    """Say exactly how the document changed, in plain terms."""
    print(f"    document length before: {len(before)} characters")
    print(f"    document length after:  {len(after)} characters")
    print(f"    the selected phrase was {len(removed)} characters")

    expected = before.replace(removed, "", 1)
    if after == expected:
        print("[+] the document lost exactly the selected phrase, "
              "and nothing else changed")
        return

    print("[x] the document did NOT change in the expected way")
    print(f"    expected length after: {len(expected)} characters")

    # Find the first and last positions where the two differ, so the
    # report says where the damage is rather than only that it happened.
    limit = min(len(expected), len(after))
    first = None
    for index in range(limit):
        if expected[index] != after[index]:
            first = index
            break
    if first is None and len(expected) != len(after):
        first = limit
    if first is not None:
        start = max(0, first - 40)
        print(f"    first difference at character {first}")
        print(f"    expected around there: {expected[start:first + 40]!r}")
        print(f"    actually there:        {after[start:first + 40]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test whether a command acts on exactly the selected "
        "phrase."
    )
    parser.add_argument("phrase", help="the phrase to find, as if spoken")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=DEFAULT_DELAY_MS,
        help="milliseconds to wait between selecting and acting "
        f"(default {DEFAULT_DELAY_MS})",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=COUNTDOWN_SECONDS,
        help=f"seconds before grabbing focus (default {COUNTDOWN_SECONDS})",
    )
    parser.add_argument(
        "--destructive",
        action="store_true",
        help="also send a Delete key and verify what was removed. "
        "USE A SCRATCH DOCUMENT.",
    )
    parser.add_argument(
        "--focus-notepad",
        action="store_true",
        help="bring the running Notepad forward instead of counting down",
    )
    parser.add_argument(
        "--prepare",
        metavar="TEXT",
        help="type TEXT into an empty Notepad document first. Refuses "
        "when the document is not empty.",
    )
    parser.add_argument(
        "--focus-word",
        metavar="TEXT",
        help="build a scratch .docx holding TEXT, open it in Word, and "
        "probe that. Word never receives typed keys.",
    )
    args = parser.parse_args(argv)

    if args.focus_notepad and args.focus_word:
        print("[x] choose either --focus-notepad or --focus-word.")
        return 2

    if args.destructive and args.focus_notepad and not args.prepare:
        print("[x] --destructive with --focus-notepad also needs --prepare.")
        print("[x] Without it this would delete text from whatever Notepad")
        print("[x] tab happens to be in front, which may hold real work.")
        return 2

    if args.destructive:
        print("[!] DESTRUCTIVE MODE. This will delete text from the focused")
        print("[!] document. Use a scratch document you do not care about.")
        print()

    if not (args.focus_notepad or args.focus_word):
        countdown(args.countdown)

    with auto.UIAutomationInitializerInThread(debug=False):
        window_class = None
        before: set[int] = set()
        folder = None

        if args.focus_notepad:
            window_class = scratch_window.NOTEPAD_WINDOW_CLASS
            before = scratch_window.list_handles(window_class)
            if not scratch_window.open_notepad(before):
                print("[x] the launch opened no new Notepad window, so the")
                print("[x] focused document belongs to somebody else.")
                return 2

        if args.focus_word:
            window_class = scratch_window.WORD_WINDOW_CLASS
            before = scratch_window.list_handles(window_class)
            folder = Path(tempfile.mkdtemp(prefix="wh_word_probe_"))
            document = scratch_window.write_docx(
                folder / "scratch.docx", [args.focus_word]
            )
            print(f"[+] scratch document: {document}")
            if not scratch_window.open_word(document, before):
                print("[x] Word opened no new window.")
                shutil.rmtree(folder, ignore_errors=True)
                return 2

        try:
            return run_probe(args)
        finally:
            if window_class is not None:
                _, refused = scratch_window.close_new_windows(
                    window_class, before
                )
                if refused:
                    print(f"[!] {refused} window(s) would not close")
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)


def run_probe(args) -> int:
    control = auto.GetFocusedControl()
    if not control:
        print("[x] no focused control")
        return 1
    identity_before = control_identity(control)
    print(f"[+] focused control: {identity_before[1]} "
          f"name={identity_before[2]!r}")

    text_pattern = control.GetPattern(auto.PatternId.TextPattern)
    if not text_pattern:
        print("[x] this control does not expose TextPattern")
        return 1

    if args.prepare and not prepare_notepad_document(
        control, text_pattern, args.prepare
    ):
        return 2

    document_before = read_document(text_pattern)
    if document_before is None:
        return 1
    print(f"[+] document length: {len(document_before)} characters")

    if args.destructive:
        if looks_truncated(document_before):
            print("[x] the document read looks truncated (its length is "
                  "an exact multiple of 1000).")
            print("[x] Refusing to delete anything, because the before "
                  "and after comparison could not be trusted.")
            print("[x] Use a shorter scratch document.")
            return 2
        if len(document_before) > MAX_DESTRUCTIVE_DOCUMENT:
            print(f"[x] the document is longer than "
                  f"{MAX_DESTRUCTIVE_DOCUMENT} characters.")
            print("[x] Refusing to delete anything. Use a shorter "
                  "scratch document.")
            return 2

    found = text_pattern.DocumentRange.FindText(args.phrase, False, True)
    if found is None:
        print(f"[x] the phrase {args.phrase!r} was not found")
        return 1
    matched_text = found.GetText(-1)
    print(f"[+] matched text: {matched_text!r}")

    found.Select(waitTime=0)
    selection = text_pattern.GetSelection()
    if not selection:
        print("[x] Select ran but the control reports no selection")
        return 1
    selected_text = selection[0].GetText(-1)
    if selected_text != matched_text:
        print(f"[x] the selection does not hold the matched text: "
              f"{selected_text!r}")
        return 1
    print("[+] the selection holds exactly the matched text")

    if args.delay_ms > 0:
        print(f"[!] waiting {args.delay_ms} ms before acting. Move focus "
              "or type now if you are testing that.")
        time.sleep(args.delay_ms / 1000.0)

    print()
    print("[+] STATE AT THE MOMENT OF ACTING")

    # Is the range still describing the same text?
    try:
        range_text_now = found.GetText(-1)
    except Exception as exc:
        print(f"[x] the range is no longer readable: {exc}")
        print("[x] A command sent now would have no reliable target.")
        return 1
    if range_text_now == matched_text:
        print("[+] the range still holds the same text")
    else:
        print(f"[x] the range now holds different text: "
              f"{range_text_now!r}")

    # Is the selection still the phrase?
    try:
        selection_now = text_pattern.GetSelection()
        selection_text_now = (
            selection_now[0].GetText(-1) if selection_now else ""
        )
    except Exception as exc:
        print(f"[x] could not read the selection: {exc}")
        selection_text_now = ""
    if selection_text_now == matched_text:
        print("[+] the selection is still the phrase")
    else:
        print(f"[x] the selection is now: {selection_text_now!r}")

    # Is focus still the same control?
    control_now = auto.GetFocusedControl()
    identity_now = control_identity(control_now) if control_now else None
    if identity_now == identity_before:
        print("[+] focus is still the same control")
    else:
        print(f"[x] focus moved. It is now: {identity_now}")
        print("[x] A command sent now could act on the wrong window.")

    if not args.destructive:
        print()
        print("[+] safe mode finished. Nothing was changed. Add "
              "--destructive on a scratch document to send the key.")
        return 0

    print()
    print("[+] SENDING THE DELETE KEY")
    sys.path.insert(0, str(WHEELHOUSE_DIR))
    from utils.win_input_sender import verified_press_keys

    succeeded, accepted, expected_events = verified_press_keys("delete")
    print(f"    SendInput accepted {accepted} of {expected_events} events")
    if not succeeded:
        print("[x] the key was not fully delivered. The document may be "
              "in an unknown state. Stopping without further action.")
        return 1

    # Give the application a moment to apply the edit before reading.
    time.sleep(0.2)

    document_after = read_document(text_pattern)
    if document_after is None:
        print("[x] could not read the document back to check the result")
        return 1

    print()
    print("[+] WHAT ACTUALLY CHANGED")
    describe_difference(document_before, document_after, matched_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
