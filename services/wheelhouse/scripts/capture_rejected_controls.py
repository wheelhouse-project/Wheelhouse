"""Capture soft-reject text controls for the starter list (wh-no-go-capture).

Interactive helper that walks a fixed list of priority apps. For each app
the script prints a prompt naming the app and a hint about where to
click, then waits on ENTER or 's'. While the user clicks into controls
inside the named app, a background thread polls the focused control via
the WheelHouse uiautomation dependency and runs the production
TextTargetPredicate on each newly focused control. Every control whose
predicate reason is default_reject_paste_capable_class is appended to
/tmp/starter-candidates.toml as a candidate for
soft_allow_starter_tuples.toml.

Decision rules are imported directly from
services/wheelhouse/ui/text_target.py so the script's filter agrees
with the runtime predicate. The predicate is constructed with NO
soft_allow data (no user file, no starter file), so the script's
filter answers "would this control soft-reject on a fresh WheelHouse
install?" instead of "does this control still soft-reject given the
user's current data".

Output schema matches soft_allow_starter_tuples.toml: each [[entries]]
block has process_name, class_name, control_type, and added_at. The
existing soft_allow_writer.append_soft_allow_tuple call serialises the
file atomically (temp + fsync + os.replace) and dedups on the
(process, class, control_type) triple, so the script is safe to
Ctrl-C: the output file is always either the previous version or the
new version, never partial.

Approach note: the python-uiautomation library does not expose a clean
global focus-changed event subscription -- the underlying COM API is
IUIAutomation::AddFocusChangedEventHandler, accessible only through
comtypes plumbing that the library does not wrap. This one-off helper
polls auto.GetFocusedControl() at a short interval instead. Human
clicks are slow enough that polling at ~150 ms catches every focus
change without missing any in practice.

Usage from services/wheelhouse:

    uv run python scripts/capture_rejected_controls.py

References: wh-no-go-capture (this script), wh-k535r (starter-list
shipping infrastructure), wh-9weum (text-target predicate design).
"""
from __future__ import annotations

import _ctypes
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Resolve the script's package roots. The script lives at
# services/wheelhouse/scripts/capture_rejected_controls.py. The
# production text-target module imports `from shared.soft_allow_schema`,
# so services/wheelhouse must be on sys.path for the import chain to
# work the same way it does in the running WheelHouse process.
SCRIPT_DIR = Path(__file__).resolve().parent
WHEELHOUSE_ROOT = SCRIPT_DIR.parent  # services/wheelhouse
if str(WHEELHOUSE_ROOT) not in sys.path:
    sys.path.insert(0, str(WHEELHOUSE_ROOT))

import psutil  # noqa: E402
import uiautomation as auto  # noqa: E402

from shared.soft_allow_schema import parse_soft_allow_file  # noqa: E402
from ui.text_target import TextTargetPredicate, TextTargetVerdict  # noqa: E402
from utils.soft_allow_writer import append_soft_allow_tuple  # noqa: E402


OUTPUT_PATH = Path("/tmp/starter-candidates.toml")
POLL_INTERVAL_S = 0.15
CAPTURE_REASON = "default_reject_paste_capable_class"

# Priority apps in capture order. Each entry: (display_name, hint). The
# hint names a few sub-controls inside the app that the WheelHouse user
# is likely to dictate into; the script captures whichever ones happen
# to soft-reject and skips the ones that already accept.
PRIORITY_APPS: list[tuple[str, str]] = [
    (
        "Visual Studio Code",
        "Click the editor pane, the integrated terminal, then the Source "
        "Control commit-message box.",
    ),
    (
        "ChatGPT desktop",
        "Click the prompt textarea and any Custom-GPT or Project "
        "description fields.",
    ),
    (
        "ChatGPT-Wheelhouse Custom GPT desktop",
        "Click the prompt textarea.",
    ),
    (
        "Claude desktop",
        "Click the prompt textarea, the project description field, and "
        "any settings text inputs.",
    ),
    (
        "Codex desktop",
        "Click the prompt textarea and any in-conversation text input.",
    ),
    (
        "Google Gemini desktop",
        "Click the prompt textarea.",
    ),
    (
        "Microsoft Copilot desktop",
        "Click the prompt textarea.",
    ),
    (
        "Perplexity desktop",
        "Click the prompt textarea.",
    ),
    (
        "Discord",
        "Click the message-compose box and the search box at the top.",
    ),
    (
        "Zoom",
        "Click the in-meeting chat box and the meeting-name field on the "
        "home screen.",
    ),
    (
        "Zed secondary panes",
        "Open the project-search panel, the in-pane filter box, or the "
        "Git commit textarea. The main editor already accepts -- skip it.",
    ),
    (
        "Typora",
        "Click the editor pane.",
    ),
    (
        "Sticky Notes new",
        "Click a sticky note's body.",
    ),
]


# Module-level logger so the script can be imported from a test without
# spawning the background thread or initialising COM.
logger = logging.getLogger("capture_rejected_controls")


def build_capture_predicate() -> TextTargetPredicate:
    """Construct the predicate the script uses for the capture decision.

    No soft_allow_path, no soft_allow_starter_path. The starter-list
    candidate file should reflect "this control would soft-reject on a
    fresh install", not "this control still soft-rejects on the user's
    current machine". A user who has already granted a tuple via the
    three-strikes flow is still a useful starter-list signal -- the
    target soft-rejected for them at first, and other users would hit
    the same first-time experience.
    """
    return TextTargetPredicate()


def should_capture(verdict: TextTargetVerdict) -> bool:
    """Return True if the verdict means we should add the triple to the output."""
    return verdict.reason == CAPTURE_REASON


def _now_iso() -> str:
    """ISO 8601 UTC timestamp matching the soft_allow_writer convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_process_name(focused_control) -> str | None:
    """Resolve the process name for the focused control (matches ui/context.py).

    Returns None on any failure. Callers skip the capture when None is
    returned. An empty process_name in the output file would never match
    a real runtime decision -- the runtime captures the lowercase exe
    name, never the empty string -- so a starter entry with an empty
    process_name would be inert noise (codex review wh-no-go-capture.1.1).

    The except list includes ``_ctypes.COMError`` so a stale UIA control
    cannot leak the COM error out of this helper into the polling loop
    (codex review wh-no-go-capture.1.2). The production text-target
    check at services/wheelhouse/ui/text_target.py uses the same
    exception set for its identity reads.
    """
    try:
        pid = int(focused_control.ProcessId)
    except (_ctypes.COMError, AttributeError, OSError, ValueError, TypeError):
        return None
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
        return None


def _load_captured_triples(
    output_path: Path,
) -> set[tuple[str, str, str]]:
    """Read existing entries from the output file into an in-memory set.

    Used to seed the polling thread's dedup so a re-run of the script
    against an existing output file does not rewrite the same row on
    every poll while the user lingers on a previously-captured control.

    A missing file returns an empty set (the documented initial state
    of the writer). Malformed entries are dropped silently by the
    schema parser.
    """
    entries = parse_soft_allow_file(
        output_path,
        log_skipped_entries=False,
        caller="capture_rejected_controls bootstrap",
    )
    return {
        (e.process_name, e.class_name, e.control_type) for e in entries
    }


def _capture_loop(
    predicate: TextTargetPredicate,
    output_path: Path,
    shutdown: threading.Event,
    captured: set[tuple[str, str, str]] | None = None,
    on_capture: Callable[[tuple[str, str, str]], None] | None = None,
) -> None:
    """Background polling loop. Runs until ``shutdown`` is set.

    Wrapped in UIAutomationInitializerInThread so COM is initialised on
    the polling thread regardless of how the main thread set things up.

    ``captured`` is the in-memory dedup set keyed on
    (process, class, control_type). The polling thread mutates it on
    every successful append. The caller seeds it from the existing
    output file via ``_load_captured_triples`` so the writer is not
    invoked for triples already in the file. Only the polling thread
    reads or writes ``captured``, so no lock is needed.

    Each iteration runs the predicate on the currently focused control.
    The script does NOT skip evaluation when the previous iteration saw
    the same identity tuple -- two distinct controls in the same app
    can share the same (process, class, control_type) and produce
    different decisions, so an identity-tuple skip would silently drop
    the soft-reject case (codex review wh-no-go-capture.1.3). Output
    log lines and disk writes are dedup'd separately so the
    every-iteration evaluation does not produce console spam or
    repeated rewrites.

    The whole iteration body is wrapped in a top-level catch so a
    stale UIA control read or an unexpected library error cannot kill
    the daemon thread silently (codex review wh-no-go-capture.1.2).
    The user sees the error on stderr and the next poll runs as
    normal.

    ``on_capture`` is a test hook fired with the (process, class,
    control_type) triple after a successful append.
    """
    if captured is None:
        captured = set()

    with auto.UIAutomationInitializerInThread(debug=False):
        last_logged: tuple[str, str, str, str] | None = None
        while not shutdown.is_set():
            try:
                try:
                    focused = auto.GetFocusedControl()
                except (_ctypes.COMError, AttributeError, OSError) as exc:
                    logger.debug("GetFocusedControl raised: %s", exc)
                    shutdown.wait(POLL_INTERVAL_S)
                    continue
                if focused is None:
                    shutdown.wait(POLL_INTERVAL_S)
                    continue

                resolved = _resolve_process_name(focused)
                if resolved is None:
                    # Cannot identify the process. An empty
                    # process_name would never match a runtime decision
                    # so the starter entry would be inert noise;
                    # skip the capture and the evaluation entirely
                    # (codex review wh-no-go-capture.1.1).
                    shutdown.wait(POLL_INTERVAL_S)
                    continue
                process_name: str = resolved

                try:
                    verdict = predicate.evaluate(
                        focused, process_name=process_name,
                    )
                except (_ctypes.COMError, AttributeError, OSError) as exc:
                    logger.debug("predicate.evaluate raised: %s", exc)
                    shutdown.wait(POLL_INTERVAL_S)
                    continue

                logged_key = (
                    verdict.process_name,
                    verdict.class_name,
                    verdict.control_type,
                    verdict.reason,
                )
                if logged_key != last_logged:
                    last_logged = logged_key
                    print(
                        f"  focus: process={verdict.process_name or '?':<20} "
                        f"class={verdict.class_name or '?':<28} "
                        f"type={verdict.control_type or '?':<20} "
                        f"-> {verdict.reason}"
                    )

                if should_capture(verdict):
                    triple = (
                        verdict.process_name,
                        verdict.class_name,
                        verdict.control_type,
                    )
                    if triple in captured:
                        # Already in the output file. The writer would
                        # dedup, but only after rewriting the file --
                        # skip the call entirely so the poller does
                        # not hit the disk every 150 ms while the user
                        # lingers on the same control.
                        shutdown.wait(POLL_INTERVAL_S)
                        continue
                    ok = append_soft_allow_tuple(
                        (triple[0], triple[1], triple[2], _now_iso()),
                        output_path,
                    )
                    if ok:
                        captured.add(triple)
                        print(
                            f"  CAPTURED -> {output_path}: "
                            f"({triple[0]}, {triple[1]}, {triple[2]})"
                        )
                        if on_capture is not None:
                            on_capture(triple)
                    else:
                        print(
                            f"  WRITE FAILED -> {output_path}",
                            file=sys.stderr,
                        )
            except Exception as exc:  # noqa: BLE001 -- fail-closed catch
                # Top-level catch so an unexpected error in any branch
                # does not kill the daemon polling thread silently. The
                # user sees the error on stderr and Ctrl-C remains
                # the way to stop the script.
                print(
                    f"  ERROR in focus poller: {exc!r} -- continuing",
                    file=sys.stderr,
                )

            shutdown.wait(POLL_INTERVAL_S)


def _walk_priority_apps(shutdown: threading.Event) -> None:
    """Main-thread interactive walk through the priority apps.

    For each app, print the prompt, wait on ENTER or 's'. Setting
    ``shutdown`` exits the loop early (the polling thread also stops).
    """
    print(
        "Capture script started. The polling thread is now watching focus "
        "changes."
    )
    print(f"Output file: {OUTPUT_PATH}")
    print(
        "For each app below, switch to the app, click into the listed "
        "sub-controls, then press ENTER to advance. Type 's' + ENTER to "
        "skip an app, or Ctrl-C at any time to exit."
    )
    print()

    for index, (name, hint) in enumerate(PRIORITY_APPS, start=1):
        if shutdown.is_set():
            return
        print(f"[{index}/{len(PRIORITY_APPS)}] {name}")
        print(f"    {hint}")
        try:
            response = input("    ENTER to continue, 's' to skip: ").strip()
        except EOFError:
            return
        if response.lower() == "s":
            print(f"    skipped: {name}")
        print()

    print("Walked through all priority apps. Polling continues until Ctrl-C.")
    while not shutdown.is_set():
        shutdown.wait(1.0)


def main() -> int:
    """Entry point. Returns a shell exit code."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    predicate = build_capture_predicate()
    captured = _load_captured_triples(OUTPUT_PATH)
    if captured:
        print(
            f"Loaded {len(captured)} existing entries from {OUTPUT_PATH} "
            "-- the script will only append novel triples."
        )
    shutdown = threading.Event()
    poller = threading.Thread(
        target=_capture_loop,
        args=(predicate, OUTPUT_PATH, shutdown, captured),
        name="focus-poller",
        daemon=True,
    )
    poller.start()
    try:
        _walk_priority_apps(shutdown)
    except KeyboardInterrupt:
        print("\nCtrl-C received -- shutting down.")
    finally:
        shutdown.set()
        poller.join(timeout=POLL_INTERVAL_S * 4)
    print(f"Done. Captured entries are in {OUTPUT_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
