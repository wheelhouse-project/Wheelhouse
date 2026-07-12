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

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ElementQuery:
    """Parsed voice command."""

    name: str
    role: str | None
    ordinal: int | None
    spatial: str | None
    raw_utterance: str


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
    PRIMARY focused-window subtree and the owning popup's HWND for a match from
    a classic Win32 ``#32768`` / UIA-Menu owned-popup subtree. The pre-click
    popup-closed probe (``ClickExecutor``) reads it: a non-zero value means the
    match is popup-owned and the probe verifies that popup HWND is still visible
    AND still owned by the focused window before invoking. It is a plain int
    and Input-process-local; like ``control_type_id`` it is intentionally NOT
    added to the cross-process ``WalkSnapshotSummaryItem`` (the probe runs only
    in the Input process at click time).
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
    """One display-safe row in a WalkSnapshotSummary."""

    item_id: str
    display_number: int
    name: str
    role: str
    bounds: tuple[int, int, int, int]
    monitor_id: int
