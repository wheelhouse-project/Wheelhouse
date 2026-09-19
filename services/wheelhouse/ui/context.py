"""UI Context detection and encapsulation.

This module handles the "sniffing" of the current UI state to determine
the appropriate insertion strategy.
"""
import logging
import threading
import psutil
import uiautomation as auto
from dataclasses import dataclass
from typing import Any, Optional
from ui.hwnd_utils import read_hwnd_provenance
from ui.target_identity import (
    TargetIdentity,
    capture_foreground_identity,
    capture_target_identity,
    current_foreground_root,
)

logger = logging.getLogger(__name__)

# capture_context makes exactly ONE focused-control read per call, and never
# retries a failed one (wh-insert-focus-read-stall.1.1, boss ruling
# 2026-09-18, which withdrew an earlier ruling that retried a fast failure).
# The read runs in the Input process against the target application and a
# hostile answer can block: the recorded failure blocked 5.5 seconds inside
# Brave before raising COMError (-2147220991). The Logic process gives up on a
# request after 5.0 seconds (app.py, the "timed out after" log), and that ERROR
# record shows the user a Windows notice through ErrorNotificationHandler.
#
# A retry could not be bounded. The entry test can only measure how long the
# FIRST call took; nothing can bound the second call once it has started, so a
# first failure returning in 0.49 seconds followed by a second call blocking
# 5.5 seconds passes the 5.0 second limit and fires that notice. It would also
# hold the Input process's single command loop for the whole second call, which
# delays the end-of-utterance clipboard restore and every later word.
#
# The retry also bought nothing measurable. A search of wheelhouse.log, its
# five rotated copies, two older rotate files and wheelhouse-watchdog.log found
# no focused-control read failure of any speed, so no record shows a second
# read ever succeeding after a first failed. The word is saved instead by the
# paste to an unchanged foreground window below.
#
# A gate mutation restores a second read, so adding the retry back cannot pass
# unnoticed.

# Per-word de-duplication for the terminal-detection DEBUG logging below.
# capture_context() runs once per dictated word in the Input process, so an
# unchanged focused target would otherwise repeat the same detection line for
# every word (one 2026-07-16 session logged 30 identical "Not detected as
# terminal: _DictationTextEdit" lines for 30 words). The Input process calls
# capture_context() from more than one thread, so this state is guarded by a
# lock. wh-context-terminal-log-dedup.
_last_target_log: Optional[str] = None
_last_target_log_lock = threading.Lock()


def _log_target_detection(message: str) -> None:
    """Log a per-call target-detection result at DEBUG, de-duplicated.

    Logs ``message`` only when it differs from the previous call. That removes
    the per-word repetition while keeping every distinct detection -- and every
    transition between targets -- visible. ``stacklevel=2`` makes the log record
    report the branch in capture_context() that produced the message (for
    example ``context.py:85`` for a console host), not this helper.

    The last-message state is read and updated under the lock; the
    ``logger.debug`` call runs outside it. The worst case of a race is one extra
    duplicate DEBUG line, which is harmless.
    """
    global _last_target_log
    with _last_target_log_lock:
        if message == _last_target_log:
            return
        _last_target_log = message
    logger.debug(message, stacklevel=2)

@dataclass
class UIContext:
    """Snapshot of the current UI state.

    Production capture always supplies target_identity, including an empty
    identity on failure. None only supports older explicitly built contexts;
    strategies must never replace a failed production capture with new focus.
    """
    focused_control: Any  # uiautomation control
    is_flutter: bool
    is_terminal: bool
    process_name: str
    class_name: str
    process_id: int = 0
    target_identity: Optional[TargetIdentity] = None
    # True when the focused-control read itself failed, as opposed to there
    # being no focused control to read (wh-insert-focus-read-stall). The
    # router cannot tell those apart from focused_control alone, and they
    # deserve different treatment: a failed read still knows which window the
    # user was in a moment ago. Defaults False so every context built
    # elsewhere, including the older explicitly built ones above, is unchanged.
    focus_read_failed: bool = False
    # The exception the failed read raised, kept so a caller can re-raise it
    # (wh-insert-focus-read-stall.1.4). Before this branch the error escaped
    # capture_context and each command action's own except arm formatted it.
    # Keeping the object is what lets those actions produce the same log line
    # and the same notice as before. None whenever focus_read_failed is False.
    read_error: Optional[BaseException] = None

def capture_context() -> UIContext:
    """Capture the current UI context.
    
    Determines the focused control and checks for specific application types
    like Flutter or Windows Terminal.
    
    Returns:
        UIContext object containing the state.
    """
    # Ruling 1 (boss, 2026-09-18): capture the foreground identity BEFORE the
    # read, so a read that blocks and then fails still carries an identity
    # taken while the user was demonstrably still in that window. The failure
    # path may use only this capture. A capture made after the failure would
    # bind whatever window the user moved to, and it would break the rule in
    # capture_target_identity that an empty identity forbids recapture.
    pre_read_identity = capture_foreground_identity()

    focused_control = None
    focus_read_failed = False
    read_error = None
    try:
        focused_control = auto.GetFocusedControl()
    except Exception as error:
        # Exactly one read, no retry. The reasoning is at the top of this
        # module, next to where the retry limit used to live.
        read_error = error
        focused_control = None
        focus_read_failed = True

    if focus_read_failed:
        if pre_read_identity == TargetIdentity():
            # No usable identity existed even before the read, so there is
            # nothing to paste into. The empty record makes ui/router.py take
            # its existing silent drop.
            target_identity = TargetIdentity()
            logger.warning(
                "Focused-control read failed and no target window identity "
                f"was captured before it; dropping this word: {read_error}"
            )
        elif current_foreground_root() != pre_read_identity.root:
            # The user moved while the read was blocked. A sentence pasted
            # into a window nobody dictated into is worse than one lost word.
            target_identity = TargetIdentity()
            logger.warning(
                "Focused-control read failed and the target window changed "
                "during the read; dropping this word rather than inserting "
                f"into the new foreground window: {read_error}"
            )
        elif (
            read_hwnd_provenance(pre_read_identity.root)
            != pre_read_identity.root_tag
            or read_hwnd_provenance(pre_read_identity.hwnd)
            != pre_read_identity.tag
        ):
            # wh-insert-focus-read-stall.1.2: the compare above is numeric
            # only, and Windows reuses top-level handles aggressively. The
            # original window can be destroyed during the blocked read and a
            # NEW window born on the same root handle can hold the foreground
            # at the instant of that compare, so it passes on a window nobody
            # dictated into. The SetProp provenance marker is stored on the
            # window OBJECT and dies with it, which makes it the one probe a
            # same-run handle reuse cannot alias (tag_hwnd_provenance in
            # ui/hwnd_utils.py). Re-read it here, before the paste branch is
            # chosen: handing the stale identity on instead sends the word to
            # verified_paste, which refuses it at ERROR
            # (ui/clipboard_operations.py) and fires a user-visible Windows
            # notice through ErrorNotificationHandler -- on a path that
            # promises no notice.
            #
            # TargetIdentity.is_current() is not used whole here: it re-reads
            # the foreground itself through win32gui, repeating the probe the
            # branch above already made through current_foreground_root(), and
            # its remaining checks (PID, GA_ROOT normalization) are the numeric
            # ones the handle reuse defeats. The two marker re-reads are the
            # part that adds information, so only they run.
            target_identity = TargetIdentity()
            logger.warning(
                "Focused-control read failed and the target window object was "
                "replaced on the same window handle during the read; dropping "
                "this word rather than pasting into the new window: "
                f"{read_error}"
            )
        else:
            # crewcut: this paste proves the top-level window and nothing
            # inside it. A failed read leaves no control object, so the
            # SetFocus correction at ui/clipboard_operations.py:526-528 and
            # :857-859 is skipped, and a person who moves to another field of
            # this SAME window during the stalled read gets the word in the
            # new field. Remove this limit with a field-level focus probe:
            # record which field holds focus before the read and compare it
            # after, through a Win32 call such as GetGUIThreadInfo rather than
            # UI Automation, which is the thing that just failed. Two fields
            # inside one window handle share that handle, so such a probe
            # would need more than hwndFocus to see a change between two
            # fields of one Chromium window. Accepted by the boss on
            # 2026-09-18 as option A on wh-insert-focus-read-stall.1.5, over
            # the reviewer's proposal to drop the word, which reverses this
            # bead. Note what is NOT lost: neither path ever proved which
            # field held focus, because capture_target_identity records the
            # control's TOP-LEVEL window, and the ordinary path captures the
            # control focused when the read RETURNS, not when the word was
            # spoken. tests/test_ui/test_context_focus_read_failure.py pins
            # the accepted behaviour.
            target_identity = pre_read_identity
            logger.warning(
                "Focused-control read failed; the target window is unchanged, "
                f"so this word is pasted into it instead: {read_error}"
            )
    else:
        target_identity = capture_target_identity(focused_control)

    # crewcut: a failed focused-control read leaves is_flutter False, so a
    # Flutter window takes the paste path instead of FlutterStrategy
    # (ui/router.py returns FlutterStrategy on is_flutter). Detecting Flutter
    # needs the top-level control of the focused control this branch just
    # failed to read. Remove this limit by deriving the top-level window from
    # pre_read_identity.root and reading its class name through Win32 rather
    # than UI Automation. Accepted by the boss on 2026-09-18:
    # wh-insert-focus-read-stall.
    is_flutter = False
    is_terminal = False
    process_name = ""
    class_name = ""
    process_id = 0

    if focused_control:
        try:
            # Get basic control info
            class_name = focused_control.ClassName

            # Get process info
            try:
                process_id = focused_control.ProcessId
                proc = psutil.Process(process_id)
                process_name = proc.name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Flutter Detection
            # Logic: Top-level window ClassName starts with 'FLUTTER'
            try:
                top_level = focused_control.GetTopLevelControl()
                if top_level and top_level.ClassName:
                    top_class = top_level.ClassName.upper()
                    if top_class.startswith('FLUTTER'):
                        is_flutter = True
                        logger.debug(f"Flutter detection: ClassName='{top_class}'")
            except Exception as e:
                logger.debug(f"Flutter detection failed: {e}")

            # Terminal Detection
            # 1. Modern Windows Terminal
            if class_name == 'TermControl' and process_name == 'windowsterminal.exe':
                is_terminal = True
                _log_target_detection(f"Target is Windows Terminal (class={class_name}, process={process_name})")

            # 2. Legacy Console (Task Scheduler, CMD, or Direct Python execution)
            elif class_name == 'ConsoleWindowClass':
                # This captures:
                # - Task Scheduler launching python.exe directly
                # - Task Scheduler launching cmd.exe
                # - You manually running 'cmd.exe' or 'powershell.exe' (classic)
                is_terminal = True
                _log_target_detection(f"Target is Legacy Console (class={class_name}, process={process_name})")
            
            # 3. Console Window Host (conhost.exe) - Task Scheduler execution
            elif process_name == 'conhost.exe':
                # When WheelHouse runs from Task Scheduler, the focused control
                # is managed by conhost.exe (Console Window Host) with empty class name
                is_terminal = True
                _log_target_detection(f"Target is Console Host (class={class_name}, process={process_name})")

            # 4. Debug logging for undetected cases
            else:
                _log_target_detection(f"Not detected as terminal: class={class_name}, process={process_name}")

        except Exception as e:
            logger.error(f"Error capturing UI context: {e}")

    return UIContext(
        focused_control=focused_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name=class_name,
        process_id=process_id,
        target_identity=target_identity,
        focus_read_failed=focus_read_failed,
        read_error=read_error,
    )
