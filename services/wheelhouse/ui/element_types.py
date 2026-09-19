"""Pure-data shapes for voice-driven UI element clicking (wh-kxgcg).

Phase 1 foundational data types for the element-clicking feature (epic
wh-l4h.1). These are the frozen dataclasses that later slices -- the UIA
walker, confidence scorer, clear-winner rule, and IPC schemas -- build on.
The authoritative field spec lives in the v5 design doc:
docs/plans/2026-05-21-voice-element-clicking-design-v5.md under "Key types".

Process-boundary split:
- WalkSnapshot stays Input-process-local: each ElementMatch.control_ref
  holds a live COM handle that cannot cross a process boundary.
- WalkSnapshotSummary / WalkSnapshotSummaryItem are the plain-data shapes
  carrying only display-safe primitives across Input -> Logic -> GUI.

These classes carry no logic beyond what @dataclass(frozen=True) generates.
"""

import enum
from dataclasses import dataclass
from typing import Any


class ClickGesture(str, enum.Enum):
    """Which mouse gesture a click request asks for (wh-click-gesture-param).

    ``INVOKE`` is the DEFAULT and means exactly what every click meant before
    this field existed: press the control through UI Automation
    (``InvokePattern``, with the executor's existing fallbacks). It carries no
    button and no click count, because ``Invoke()`` has neither.

    ``RIGHT_CLICK`` and ``DOUBLE_CLICK`` are PHYSICAL gestures. They cannot be
    expressed through Invoke at all, so ``ClickExecutor`` routes them to the
    existing guarded coordinate-click path -- the same five-step pre-click
    verification and the same two occlusion hit-test layers -- with a different
    button or click count. See the "Gesture parameter" subsection of
    docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md.

    It subclasses ``str`` so the member survives any path that stringifies or
    serializes it (the Logic -> Input request carries an ``ElementQuery``
    pickled through SharedMemory, and the numbered-badge request carries the
    bare value as a payload field), and so a log line prints something a reader
    recognizes. The values are the wire-visible strings; do not rename them
    without changing both sides of the IPC.
    """

    INVOKE = "invoke"
    RIGHT_CLICK = "right_click"
    DOUBLE_CLICK = "double_click"


# The gesture every pre-existing call site means: today's Invoke behaviour.
DEFAULT_GESTURE = ClickGesture.INVOKE


@dataclass(frozen=True)
class ElementQuery:
    """Parsed voice command.

    ``gesture`` (wh-click-gesture-param) defaults to
    :data:`DEFAULT_GESTURE` -- today's Invoke behaviour -- so every
    construction site that predates the field is unchanged in meaning as well
    as in shape.
    """

    name: str
    role: str | None
    ordinal: int | None
    spatial: str | None
    raw_utterance: str
    gesture: ClickGesture = DEFAULT_GESTURE


@dataclass(frozen=True)
class ElementMatch:
    """A found control. Input-process-local; control_ref is a live COM handle.

    ``control_type_id`` carries the numeric UIA control-type id (e.g.
    UIA_GroupControlTypeId 50026) the walker read from CachedControlType. It is
    the locale-INVARIANT role signal: unlike ``role`` (the localized
    control-type string, which differs by Windows display language), the id is
    identical across languages. The browser DOM-folding predicates compare it so
    folding works on non-English Windows. Defaulted to 0 (a sentinel no real UIA
    control uses) so existing positional/keyword construction sites that predate
    the field keep compiling; the walker always populates it from the cached
    control type. It is a plain int and display-safe, but is intentionally NOT
    added to the cross-process WalkSnapshotSummaryItem -- the folding it feeds is
    an in-process pass.

    ``source_window_hwnd`` carries the HWND of the top-level window the match
    was walked from (wh-n29v.45). It is 0 (a sentinel) for a match from the
    PRIMARY focused-window subtree and the owning window's HWND for a match
    from any ADDITIONAL walked subtree -- a classic Win32 ``#32768`` /
    UIA-Menu owned-popup subtree, or a taskbar shell-window subtree
    (wh-overlay-taskbar-numbers). The pre-click probe (``ClickExecutor``)
    reads it: a non-zero value means the match's owning window must be
    re-verified before invoking. It is a plain int and Input-process-local;
    like ``control_type_id`` it is intentionally NOT added to the
    cross-process ``WalkSnapshotSummaryItem`` (the probe runs only in the
    Input process at click time).

    ``source_window_is_shell`` (wh-overlay-taskbar-numbers) distinguishes the
    two non-zero ``source_window_hwnd`` cases: False (the default, and the
    value every pre-existing construction site keeps) means popup-owned, and
    the pre-click probe requires the window to be visible AND owned by the
    focused window; True means the match came from a taskbar shell window
    (``Shell_TrayWnd`` / ``Shell_SecondaryTrayWnd`` / a tray-overflow
    window), which is an UNOWNED always-on-top top-level, so the probe
    requires only that the shell window is still visible -- the popup probe's
    owner check would refuse every taskbar click.

    ``bounds_outside_menu`` (wh-vscode-menu-badge-misplaced): see the
    field of the same name on ``WalkSnapshotSummaryItem``, which carries it
    to the GUI.
    """

    item_id: str
    display_number: int
    name: str
    role: str
    bounds: tuple[int, int, int, int]
    monitor_id: int
    score: float
    is_eligible: bool
    source: str
    invoke_supported: bool
    is_enabled: bool
    control_ref: Any
    control_type_id: int = 0
    source_window_hwnd: int = 0
    source_window_is_shell: bool = False
    bounds_outside_menu: bool = False


@dataclass(frozen=True)
class WalkSnapshot:
    """All matches the walker found in the focused window for one query.

    Input-process-local. The full snapshot (with control_ref per match)
    never crosses a process boundary; a WalkSnapshotSummary is built
    alongside it for that purpose.
    """

    snapshot_id: str
    matches: list[ElementMatch]
    created_at_monotonic: float
    foreground_window: int
    foreground_pid: int
    foreground_process_name: str
    foreground_window_creation_time: int
    cursor_at_walk: tuple[int, int]
    cursor_monitor_id: int


@dataclass(frozen=True)
class WalkSnapshotSummary:
    """Plain-data summary suitable for crossing the Input -> Logic -> GUI boundary.

    Carries only display-safe primitives; the GUI process reads this to paint
    the numbered overlay, never the Input-local WalkSnapshot.
    """

    snapshot_id: str
    items: list["WalkSnapshotSummaryItem"]
    created_at_monotonic: float


@dataclass(frozen=True)
class WalkSnapshotSummaryItem:
    """One display-safe row in a WalkSnapshotSummary.

    ``bounds_outside_menu`` (wh-vscode-menu-badge-misplaced) is True when
    the control's rectangle is not fully inside the rectangle of the menu
    that comes before it in the same walk. The walker sets it only on a
    browser walk. Chromium can report the rows at the end of a long menu
    below their real position, outside the menu, so True means "this
    rectangle may be wrong". The overlay then keeps its normal badge
    placement for the control instead of the wide-row placement, which
    trusts the rectangle. The default, False, means "not suspect": every
    non-browser walk, every control with no menu before it, and every
    payload from a sender that does not send the field.
    """

    item_id: str
    display_number: int
    name: str
    role: str
    bounds: tuple[int, int, int, int]
    monitor_id: int
    bounds_outside_menu: bool = False
