"""Test whether the Wheelhouse key sender types the wrong text in Notepad.

WHY THIS EXISTS (2026-08-19, bd issue wh-startup-trailing-corruption).
That bead is CLOSED. It recorded trailing-word character corruption on the
first dictation after Wheelhouse starts, and it was root-caused to the
Notepad RichEditD2DPT input pipeline, outside Wheelhouse. A clipboard
paste path shipped as the workaround, and David confirmed no recurrence
after 2026-07-04.

On 2026-08-19 a feasibility script reproduced the same signature by
accident. The key sender reported 45 of 45 events accepted with no error,
and the document showed "The The             ..........................."
instead of the sentence. Two things confounded that result. The text
replaced a selection instead of typing at a plain cursor, and it came
from a bare Python script rather than from Wheelhouse.

This script removes both confounds and repeats the test. Each trial runs
in its own new process, types into an empty document with no selection,
reads the text back through UI Automation, and compares it.

SAFETY. Each trial opens its OWN Notepad window and closes it again
(scripts/benchmarks/scratch_window.py). It also refuses to type unless
two things are true: the focused control must be a Notepad document
control, and that document must be empty. Both guards matter. On
2026-08-19 a feasibility script typed into a document David was using,
because a launch of notepad.exe can land on an already-open document.

USAGE. Nothing to prepare; the script opens its own window.

    uv run python ../../scripts/benchmarks/notepad_typing_trials.py --trials 10

No emojis in output (Windows CP1252 consoles).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

WHEELHOUSE_DIR = Path(__file__).resolve().parents[2] / "services" / "wheelhouse"

# Every letter of the alphabet, plus digits and punctuation, so any
# character class that arrives wrong shows up.
SAMPLE_TEXT = "The quick brown fox jumps over the lazy dog 0123456789."

# Modern Windows 11 Notepad. The 2026-08-19 observation and the closed
# bead both name this control class.
NOTEPAD_CLASS = "RichEditD2DPT"

# Seconds to wait after typing, before reading the document back.
READ_SETTLE_SECONDS = 0.6


def run_one_trial(bursts: int) -> dict:
    """Type the sample text ``bursts`` times in this one new process.

    Two or more bursts separate a settle race from a sender defect. If
    only the first burst loses characters, the control was not ready to
    receive input; the sender is not at fault.
    """
    import uiautomation as auto

    import scratch_window

    sys.path.insert(0, str(WHEELHOUSE_DIR))
    from utils.win_input_sender import type_string_verified, verified_press_keys

    with auto.UIAutomationInitializerInThread(debug=False):
        window_class = scratch_window.NOTEPAD_WINDOW_CLASS
        before = scratch_window.list_handles(window_class)
        if not scratch_window.open_notepad(before):
            return {
                "status": "refused",
                "reason": "the launch opened no new window, so the focused "
                "document belongs to somebody else",
            }
        try:
            return measure(
                auto, type_string_verified, verified_press_keys, bursts
            )
        finally:
            _, refused = scratch_window.close_new_windows(
                window_class, before
            )
            if refused:
                print(
                    f"[!] {refused} Notepad window(s) would not close",
                    file=sys.stderr,
                )


def measure(auto, type_string_verified, verified_press_keys, bursts) -> dict:
    """Type and read back inside an already-open scratch window."""
    control = auto.GetFocusedControl()
    if control is None:
        return {"status": "refused", "reason": "no focused control"}

    class_name = getattr(control, "ClassName", "?")
    if class_name != NOTEPAD_CLASS:
        return {
            "status": "refused",
            "reason": f"focused control is {class_name!r}, "
            f"not {NOTEPAD_CLASS!r}",
        }

    pattern = control.GetPattern(auto.PatternId.TextPattern)
    if pattern is None:
        return {"status": "refused", "reason": "no TextPattern"}

    before = pattern.DocumentRange.GetText(-1)
    if before.strip():
        return {
            "status": "refused",
            "reason": f"document is not empty ({len(before)} characters)",
            "document_start": before[:60],
        }

    records = []
    for _ in range(bursts):
        sent_ok, accepted, expected = type_string_verified(SAMPLE_TEXT)
        time.sleep(READ_SETTLE_SECONDS)
        after = pattern.DocumentRange.GetText(-1)

        # Clear before the next burst. Ctrl+A then Delete.
        select_ok, _, _ = verified_press_keys("ctrl", "a")
        delete_ok, _, _ = verified_press_keys("delete")
        time.sleep(READ_SETTLE_SECONDS)
        cleared = pattern.DocumentRange.GetText(-1)

        records.append(
            {
                "sender_ok": bool(sent_ok),
                "sender_accepted": accepted,
                "sender_expected": expected,
                "expected_text": SAMPLE_TEXT,
                "actual_text": after,
                "matched": after.strip() == SAMPLE_TEXT,
                "clear_keys_ok": bool(select_ok and delete_ok),
                "document_after_clear": cleared,
            }
        )
        if cleared.strip():
            break

    return {"status": "measured", "bursts": records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repeat a Notepad typing burst and check the result."
    )
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument(
        "--bursts",
        type=int,
        default=1,
        help="typing bursts per process; 2 or more separate a settle "
        "race from a sender defect",
    )
    parser.add_argument(
        "--child",
        action="store_true",
        help="internal: run one trial and print the result as JSON",
    )
    args = parser.parse_args(argv)

    if args.child:
        print(json.dumps(run_one_trial(args.bursts)))
        return 0

    print(f"[+] {args.trials} trials, one new process each")
    print(f"[+] {args.bursts} typing burst(s) per process")
    print(f"[+] sample text: {SAMPLE_TEXT!r}")
    print("[+] each trial opens and closes its own Notepad window")
    print()

    results: list[dict] = []
    for index in range(1, args.trials + 1):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                "--bursts",
                str(args.bursts),
            ],
            capture_output=True,
            text=True,
        )
        line = completed.stdout.strip().splitlines()
        if not line:
            print(f"[x] trial {index}: child produced no output")
            print(f"    stderr: {completed.stderr.strip()[:400]}")
            return 1
        try:
            record = json.loads(line[-1])
        except json.JSONDecodeError:
            print(f"[x] trial {index}: could not read the child result")
            print(f"    stdout: {completed.stdout.strip()[:400]}")
            return 1

        results.append(record)
        if record["status"] == "refused":
            print(f"[x] trial {index}: refused -- {record['reason']}")
            if "document_start" in record:
                print(f"    document starts: {record['document_start']!r}")
            print("[x] Stopping. Put Notepad in front on an empty tab.")
            return 2

        for position, burst in enumerate(record["bursts"], start=1):
            mark = "[+]" if burst["matched"] else "[x]"
            print(
                f"{mark} trial {index} burst {position}: "
                f"sender_ok={burst['sender_ok']} "
                f"accepted={burst['sender_accepted']} "
                f"matched={burst['matched']}"
            )
            if not burst["matched"]:
                print(f"    expected: {burst['expected_text']!r}")
                print(f"    actual:   {burst['actual_text']!r}")
            if not burst["clear_keys_ok"]:
                print("    [!] the clear keystrokes were not fully delivered")
            if burst["document_after_clear"].strip():
                print(
                    "    [!] the document was not empty after the clear: "
                    f"{burst['document_after_clear'][:60]!r}"
                )

    print()
    for position in range(1, args.bursts + 1):
        at_position = [
            r["bursts"][position - 1]
            for r in results
            if r["status"] == "measured" and len(r["bursts"]) >= position
        ]
        wrong = [b for b in at_position if not b["matched"]]
        print(
            f"[+] burst {position}: {len(at_position)} measured, "
            f"{len(wrong)} typed wrong text"
        )
        if wrong:
            clean = [b for b in wrong if b["sender_ok"]]
            print(
                f"[x]   {len(clean)} of those {len(wrong)} reported "
                "complete success from the key sender"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
