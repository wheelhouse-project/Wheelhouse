"""Measure whether Wheelhouse could find and select a spoken phrase.

Standalone probe. Changes nothing in Wheelhouse and writes nothing to any
document. It only searches and selects.

Run it, then focus a Notepad or Word window and put the cursor in the text
before the countdown ends:

    uv run python ../../scripts/benchmarks/spoken_phrase_probe.py "some phrase"

WHY THIS EXISTS (2026-08-18, bd issue wh-spoken-phrase-target.1).
docs/design/spoken-phrase-commands.md concludes that Wheelhouse could find
an arbitrary spoken phrase inside a focused text control and select it, but
every timing number in that report is an engineering judgment. Nobody has
run a single search against a real document. This script replaces the
judgments with measurements before anyone commits to building the feature.

It answers five questions:

1. Does the focused control expose TextPattern at all?
2. How long does FindText take over the whole document?
3. How long does FindText take when the phrase is absent?
4. Does Select actually leave the phrase selected?
5. How much of the cost is the library's fixed 0.5 second sleep?

Question 5 matters because the 500 ms caret read Wheelhouse measured
earlier came from that sleep, not from the COM call. TextRange.Select
takes a waitTime argument, so the probe measures it both ways.

No emojis in output (Windows CP1252 consoles).
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

import uiautomation as auto

COUNTDOWN_SECONDS = 5
SEARCH_REPEATS = 10

# A phrase no document should contain, used to time the absent case.
ABSENT_PHRASE = "zzq-absent-phrase-probe-zzq"


def countdown(seconds: int) -> None:
    print("[!] Focus the Notepad or Word window and click into the text.")
    for remaining in range(seconds, 0, -1):
        print(f"    grabbing the focused control in {remaining}...")
        time.sleep(1)


def describe(control) -> str:
    try:
        name = control.Name
    except Exception:
        name = "?"
    try:
        class_name = control.ClassName
    except Exception:
        class_name = "?"
    try:
        control_type = control.ControlTypeName
    except Exception:
        control_type = "?"
    return f"{control_type} class={class_name} name={name!r}"


def time_search(text_pattern, phrase: str, ignore_case: bool, repeats: int):
    """Return (timings_ms, last_result_range)."""
    timings: list[float] = []
    found = None
    for _ in range(repeats):
        document = text_pattern.DocumentRange
        start = time.perf_counter()
        found = document.FindText(phrase, False, ignore_case)
        timings.append((time.perf_counter() - start) * 1000.0)
    return timings, found


def report(label: str, timings: list[float]) -> None:
    if not timings:
        print(f"    {label}: no samples")
        return
    print(
        f"    {label}: min {min(timings):.2f} ms  "
        f"median {statistics.median(timings):.2f} ms  "
        f"max {max(timings):.2f} ms  n={len(timings)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure FindText and Select on the focused text control."
    )
    parser.add_argument("phrase", help="the phrase to find, as if spoken")
    parser.add_argument(
        "--repeats",
        type=int,
        default=SEARCH_REPEATS,
        help=f"how many times to run each search (default {SEARCH_REPEATS})",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=COUNTDOWN_SECONDS,
        help=f"seconds before grabbing focus (default {COUNTDOWN_SECONDS})",
    )
    args = parser.parse_args(argv)

    countdown(args.countdown)

    with auto.UIAutomationInitializerInThread(debug=False):
        control = auto.GetFocusedControl()
        if not control:
            print("[x] no focused control")
            return 1
        print(f"[+] focused control: {describe(control)}")

        text_pattern = control.GetPattern(auto.PatternId.TextPattern)
        if not text_pattern:
            print("[x] this control does not expose TextPattern.")
            print("[x] The direct phrase method cannot work here.")
            return 1
        print("[+] TextPattern is available")

        # How big is the document? Search cost may depend on it.
        #
        # GetText is asked for the whole document with maxLength=-1, which
        # the library passes straight to the provider. Some providers
        # truncate anyway. Word returned exactly 65000 characters for a
        # 21984-word document on 2026-08-18. A suspiciously round length is
        # therefore reported, because a truncated read breaks any plan that
        # normalizes the whole document locally.
        document_text = ""
        try:
            document_text = text_pattern.DocumentRange.GetText(-1)
            print(f"[+] document length: {len(document_text)} characters")
            if len(document_text) % 1000 == 0:
                print(
                    "[!] that length is an exact multiple of 1000, which "
                    "suggests the provider truncated the read rather than "
                    "returning the whole document"
                )
        except Exception as exc:
            print(f"[!] could not read the document text: {exc}")

        print()
        print("[+] SEARCH TIMING")

        exact_timings, exact_found = time_search(
            text_pattern, args.phrase, False, args.repeats
        )
        report("exact, case-sensitive", exact_timings)

        insensitive_timings, insensitive_found = time_search(
            text_pattern, args.phrase, True, args.repeats
        )
        report("exact, case-insensitive", insensitive_timings)

        absent_timings, absent_found = time_search(
            text_pattern, ABSENT_PHRASE, True, args.repeats
        )
        report("phrase absent", absent_timings)
        if absent_found is not None:
            print(
                "[!] the absent-phrase search returned a match; "
                "the provider may not honor FindText"
            )

        found = insensitive_found or exact_found
        if found is None:
            print()
            print(
                f"[x] the phrase {args.phrase!r} was not found in this document."
            )
            print(
                "[x] Put the phrase in the document and run the probe again "
                "to measure selection."
            )
            return 1

        try:
            matched_text = found.GetText(-1)
        except Exception as exc:
            print(f"[x] could not read the matched range: {exc}")
            return 1
        print(f"[+] matched text: {matched_text!r}")
        if matched_text.lower() != args.phrase.lower():
            print(
                "[!] the matched text is not the phrase that was asked for; "
                "the provider matched something else"
            )

        # Does the search reach further than the read?
        #
        # This decides whether tolerant matching is possible. Tolerant
        # matching means reading the whole document, normalizing it, and
        # mapping the result back to the original characters. That plan
        # needs a complete GetText. If FindText locates a phrase that
        # GetText never returned, the read is incomplete and only the
        # exact search can be trusted on this provider.
        if document_text:
            if matched_text in document_text:
                print(
                    "[+] the match lies inside the text that GetText "
                    "returned, so the read may be complete"
                )
            else:
                print(
                    "[x] FindText located a phrase that GetText never "
                    "returned. The provider truncates the read but not the "
                    "search. Whole-document normalization cannot work here."
                )

        print()
        print("[+] SELECTION")

        start = time.perf_counter()
        found.Select(waitTime=0)
        no_wait_ms = (time.perf_counter() - start) * 1000.0
        print(f"    Select with no settle wait: {no_wait_ms:.2f} ms")

        # Did the selection actually land on the phrase?
        try:
            selection = text_pattern.GetSelection()
        except Exception as exc:
            print(f"[x] could not read the selection back: {exc}")
            return 1
        if not selection:
            print("[x] Select ran but the control reports no selection")
            return 1
        selected_text = selection[0].GetText(-1)
        print(f"    selection reads back as: {selected_text!r}")
        if selected_text == matched_text:
            print("[+] the selection holds exactly the matched text")
        else:
            print("[x] the selection does NOT hold the matched text")

        # The library's default settle wait, for comparison.
        start = time.perf_counter()
        found.Select()
        default_ms = (time.perf_counter() - start) * 1000.0
        print(f"    Select with the library default wait: {default_ms:.2f} ms")
        print(
            f"    the fixed sleep accounts for "
            f"{default_ms - no_wait_ms:.2f} ms of that"
        )

    print()
    print(
        "[+] probe finished. The selection is left in place on purpose so "
        "you can see it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
