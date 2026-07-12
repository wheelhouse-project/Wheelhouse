"""UIA-based text context reading for fast-path insertion.

Provides alternatives to clipboard-based context gathering using Windows
UI Automation TextPattern and ValuePattern. Falls back to None when
patterns are unavailable, allowing callers to use clipboard path instead.

Performance (from bench_uia_vs_clipboard.py):
    TextPattern.GetText():  ~378us (350x faster than clipboard)
    ValuePattern.Value:     ~284us (486x faster than clipboard)
    Clipboard round-trip:   ~56-139ms (baseline)

TextPattern2 caret source (wh-pkhrp Phase 3, design constraint A):
The original implementation used ``TextPattern.GetSelection`` as the
caret source, then walked ``DocumentRange`` backwards via
``MoveEndpointByRange`` to read preceding text. Qt's UIA backend
serves ``MoveEndpointByRange(End -> selection.Start)`` in a flat
500 ms on a QPlainTextEdit (see
docs/design/benchmarks/2026-05-02-155250-qpte-uia-fidelity.md). The
same call against ``TextPattern2.GetCaretRange`` returns in 0.013 ms
p99 -- a 38,000x speedup that brings the read inside the 5 ms
acceptance budget.

This module prefers TextPattern2.GetCaretRange when the focused
control exposes it, then falls back to the legacy GetSelection path
for controls that do not (older apps, Notepad on some Win10 builds,
controls behind UIA proxies). Each path returns the same
``{'preceding_chars': str, 'has_selection': bool}`` shape so callers
do not need to branch on which path produced the result.

IME composition handling (wh-pkhrp design constraint C):
``DocumentRange.GetText`` does not include in-flight composition
strings until the IME commits. Reading preceding context during an
active composition would feed TextPerfector text that did not
include the words the user is currently composing. The reader
detects composition-active state by checking whether the focused
control reports an empty selection AND has a non-empty
``CurrentLocalizedControlType`` whose probe is a known IME pattern
container. When composition is active the reader returns ``None`` so
the caller falls back -- the clipboard path waits for the IME
commit naturally because the keystroke flow does not advance until
then. The detection is conservative: a heuristic that mistakenly
treats a normal idle state as "composing" only loses the UIA fast
path for that read; a missed detection that proceeds during real
composition would feed wrong preceding text. We err toward the safer
miss.
"""
import logging
import _ctypes
import uiautomation as auto

logger = logging.getLogger(__name__)


def _has_text_pattern2(focused_control) -> bool:
    """True when the control exposes TextPattern2 (and ValuePattern2 hook).

    Wrapped in a helper so tests can stub the capability check
    independently from the rest of the read path. Returns False on any
    failure -- the caller falls back to GetSelection.

    The check is conservative: it requires both the
    ``auto.PatternId.TextPattern2`` constant to be a real integer (so
    a fully mocked uiautomation module does not silently activate the
    TextPattern2 branch in unit tests) AND the focused control to
    return a non-None pattern when queried.
    """
    try:
        pattern_id = getattr(auto.PatternId, "TextPattern2", None)
        if not isinstance(pattern_id, int):
            return False
        pattern = focused_control.GetPattern(pattern_id)
        return pattern is not None
    except (_ctypes.COMError, AttributeError, Exception):
        return False


# wh-pkhrp.3.8: AccessibleName / UIA Name property value the terminal
# dictation editor's QPlainTextEdit sets in
# services/wheelhouse/terminal_editor_window.py. The composition gate
# excludes this control from the fail-closed branch so Phase 3 drain
# words take the TextPattern2 fast path (~0.6 ms) instead of the
# clipboard fallback (~50-139 ms). Owning the constant string here
# (rather than importing terminal_editor_window) keeps the input
# process free of any Qt import.
_WHEELHOUSE_TERMINAL_EDITOR_ACCESSIBLE_NAME = "WheelHouseTerminalEditor"


def _is_composition_active(focused_control, text_pattern) -> bool:
    """Conservative fail-closed detection of an in-flight IME composition.

    wh-pkhrp.1.8: returns True for the ambiguous "empty selection
    inside an IME-capable control" case. Composition is a Windows
    IME concept; the active composition string is held by the IME
    process, not the focused control, so UIA cannot directly query
    "is the user composing right now". The safest signal reachable
    from TextPattern alone is "the user has placed an insertion
    caret but DocumentRange has not yet reflected any of the
    recently typed characters", which lines up with the
    composition-active interval.

    The check applies only to known IME-capable Qt and edit controls
    -- the controls where IME composition is a real risk and where
    the fast-path reader's preceding-chars output would otherwise be
    composed against stale state. For every other control (browsers,
    non-IME edit controls) the empty-selection case is the normal
    idle state; returning True there would lose UIA fast-path
    coverage on every dictation tick. Limiting the True branch to
    the IME-capable allowlist is the conservative trade-off the
    bead text recommends: erring toward the slow clipboard path
    during real IME composition is preferable to feeding
    TextPerfector text that does not include the user's in-flight
    composition.

    wh-pkhrp.3.8: the WheelHouse terminal dictation editor IS a
    QPlainTextEdit but its empty-selection state is the normal idle
    state for Phase 3 drain dictation -- the user is not composing
    IME text in it. The editor sets a stable AccessibleName / UIA
    Name property; the gate detects it and returns False so each
    drained word hits the TextPattern2 fast path. Any other
    QPlainTextEdit (Qt Creator, KDE editors, custom apps) keeps the
    fail-closed branch.
    """
    try:
        class_name = (focused_control.ClassName or "") if focused_control else ""
    except Exception:
        return False
    _IME_CAPABLE_CLASSES = {
        "edit",
        "richedit",
        "richedit20w",
        "richedit50w",
        "qplaintextedit",
        "qtextedit",
    }
    if class_name.lower() not in _IME_CAPABLE_CLASSES:
        return False
    # WheelHouse-owned terminal dictation editor: skip the
    # fail-closed branch even though the class matches. Any failure
    # reading the Name property falls through and applies the
    # default IME-capable handling (safer to misclassify a foreign
    # QPlainTextEdit as composing than to feed stale context).
    try:
        accessible_name = getattr(focused_control, "Name", "") or ""
    except Exception:
        accessible_name = ""
    if accessible_name == _WHEELHOUSE_TERMINAL_EDITOR_ACCESSIBLE_NAME:
        return False
    try:
        sel_ranges = text_pattern.GetSelection()
    except Exception:
        # No selection signal at all: cannot rule out composition,
        # but cannot confirm it either. Fail open (False) so the
        # reader proceeds; production callers fall back to the
        # clipboard path on a None result anyway.
        return False
    if not sel_ranges:
        return False
    try:
        sel_text = sel_ranges[0].GetText(-1)
    except Exception:
        return False
    if sel_text:
        # A non-empty selection means the user has selected text;
        # the caret position is the selection's range so a
        # composition cannot be in flight there.
        return False
    # Empty selection inside an IME-capable control: fail closed.
    # The reader returns None on True so the caller's clipboard
    # fallback waits for the IME commit naturally.
    return True


def _read_via_text_pattern2(focused_control, max_chars: int) -> dict | None:
    """TextPattern2 caret-range read path.

    Uses ``TextPattern2.GetCaretRange`` for the caret position and
    walks the document range backwards. Measured at ~0.6 ms p99 on
    QPlainTextEdit, well under the 5 ms budget.
    """
    try:
        pattern_id = auto.PatternId.TextPattern2
        pattern2 = focused_control.GetPattern(pattern_id)
        if pattern2 is None:
            return None
        # GetCaretRange returns (range, is_active). The active flag
        # signals whether the caret is at an active editing position;
        # treat False as a fallback signal but proceed if we got a
        # range, since the range itself is what we need.
        caret_result = pattern2.GetCaretRange()
        # Handle both tuple-return and bare-range bindings; older
        # uiautomation bindings return just the range, newer ones a
        # (bool, range) pair.
        if isinstance(caret_result, tuple):
            if len(caret_result) >= 2:
                caret_range = caret_result[1]
            else:
                caret_range = caret_result[0]
        else:
            caret_range = caret_result
        if caret_range is None:
            return None

        # Has-selection check via the legacy TextPattern path -- a
        # caret range itself does not say whether the user has a
        # selection.
        text_pattern = focused_control.GetPattern(auto.PatternId.TextPattern)
        has_selection = False
        if text_pattern is not None:
            try:
                sel_ranges = text_pattern.GetSelection()
                if sel_ranges:
                    sel_text = sel_ranges[0].GetText(-1)
                    has_selection = len(sel_text) > 0
            except _ctypes.COMError:
                has_selection = False

        doc_range = text_pattern.DocumentRange if text_pattern else None
        if doc_range is None:
            return None
        pre_range = doc_range.Clone()
        pre_range.MoveEndpointByRange(
            auto.TextPatternRangeEndpoint.End,
            caret_range,
            auto.TextPatternRangeEndpoint.Start,
        )
        pre_text = pre_range.GetText(-1)
        preceding_chars = pre_text[-max_chars:] if pre_text else ""

        return {
            "preceding_chars": preceding_chars,
            "has_selection": has_selection,
        }
    except _ctypes.COMError as e:
        logger.debug("UIA TextPattern2 context read failed (COM): %s", e)
        return None
    except Exception as e:
        logger.debug("UIA TextPattern2 context read failed: %s", e)
        return None


def read_context_via_text_pattern(focused_control=None, max_chars: int = 2) -> dict | None:
    """Read text context around caret using UIA TextPattern.

    Returns a dict matching the format of ClipboardOperations.gather_context():
        {'preceding_chars': str, 'has_selection': bool}

    Returns None if TextPattern is unavailable or the read fails.
    Callers should fall back to clipboard-based gathering on None.

    Prefers TextPattern2.GetCaretRange when available (wh-pkhrp design
    constraint A). Falls back to the legacy GetSelection path for
    controls that do not expose TextPattern2. Returns None when a
    composition is detected as active so the caller falls back to a
    path that waits for the IME commit (constraint C).

    Args:
        focused_control: Optional pre-acquired UIA control. If None,
            GetFocusedControl() is called.
        max_chars: Maximum preceding characters to return (default 2).
    """
    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            if focused_control is None:
                focused_control = auto.GetFocusedControl()
            if not focused_control:
                return None

            text_pattern = focused_control.GetPattern(auto.PatternId.TextPattern)
            if not text_pattern:
                return None

            # Composition gate. Skip the read while an IME is composing
            # so we do not feed TextPerfector stale preceding context.
            if _is_composition_active(focused_control, text_pattern):
                logger.debug(
                    "uia_text_reader: composition active, skipping read"
                )
                return None

            # Prefer TextPattern2 caret source on capable controls.
            if _has_text_pattern2(focused_control):
                tp2_result = _read_via_text_pattern2(focused_control, max_chars)
                if tp2_result is not None:
                    return tp2_result

            # Legacy GetSelection path (Notepad, Word, browsers, etc).
            try:
                sel_ranges = text_pattern.GetSelection()
            except _ctypes.COMError:
                return None

            if not sel_ranges:
                return None

            sel_range = sel_ranges[0]
            sel_text = sel_range.GetText(-1)
            has_selection = len(sel_text) > 0

            # Build range from doc start to caret (= start of selection)
            doc_range = text_pattern.DocumentRange
            pre_range = doc_range.Clone()
            pre_range.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                sel_range,
                auto.TextPatternRangeEndpoint.Start,
            )
            pre_text = pre_range.GetText(-1)
            preceding_chars = pre_text[-max_chars:] if pre_text else ""

            return {
                'preceding_chars': preceding_chars,
                'has_selection': has_selection,
            }

    except _ctypes.COMError as e:
        logger.debug("UIA TextPattern context read failed (COM): %s", e)
        return None
    except Exception as e:
        logger.debug("UIA TextPattern context read failed: %s", e)
        return None


def read_value_pattern_text(focused_control=None) -> str | None:
    """Read full text of focused control via UIA ValuePattern.

    Returns the control's text content, or None if ValuePattern is
    unavailable. Available for future ShadowBuffer validation (~250us).

    Args:
        focused_control: Optional pre-acquired UIA control.
    """
    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            if focused_control is None:
                focused_control = auto.GetFocusedControl()
            if not focused_control:
                return None

            value_pattern = focused_control.GetPattern(auto.PatternId.ValuePattern)
            if not value_pattern:
                return None

            return value_pattern.Value

    except _ctypes.COMError:
        return None
    except Exception:
        return None
