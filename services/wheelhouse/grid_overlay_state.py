"""Logic-process state machine and cell arithmetic for the mouse grid.

Bead ``wh-grid-state-machine`` under the ``wh-mouse-grid`` molecule. The
authoritative spec is
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``,
specifically "### Logic process -- grid state machine and all geometry",
the "### Refinement" and "### Actions at the current cell center" rules,
and the "## Configuration" table.

The mouse grid is the accessibility-tree-free fallback for pointing at a
place on screen: nine numbered cells are painted over one monitor, a
spoken number redraws the grid inside that cell, and an action (click,
double click, right click, move here, mark, drag) fires at the current
cell's center. It exists because UI Automation cannot see inside some
Electron apps, games, canvas tools, and maps.

This module is deliberately shaped like ``click_overlay_state.py`` (the
numbered overlay's state machine), and for the same reason: the Logic
process owns every decision, and the decision layer is a small finite
state machine that

  * tracks open/closed, the monitor the grid lives on, the current
    rectangle, and the optional mark point,
  * refuses a refinement that would take the cell below the configured
    minimum size, and
  * RETURNS, as plain data, the side effects the integration layer must
    perform (paint the grid, paint the pin, clear, close the numbered
    overlay, perform a drag, fire a notice).

PURITY. The class performs NO input/output of any kind: no Win32, no
monitor enumeration, no IPC queues, no config reads, no timers. Every
monitor rectangle it needs arrives ON the event. ``apply(event)`` is a
deterministic function of the current state and the event; it returns
``(outcome, effects)`` and never raises on a well-typed call. Resolving
the focused window's monitor, enumerating the desktop, sending the paint
events to the GUI, and running the drag inside the Input process are all
the INTEGRATION layer's job (separate beads: grid speech routing, grid
paint mode, mouse primitives).

Coordinates
-----------
Every rectangle and point in this module is in **virtual-desktop physical
pixels** -- the same coordinate system ``EnumDisplayMonitors`` /
``GetMonitorInfo`` report (see ``shared/monitor_geometry.py``) and the
same one UIA bounding rectangles use. Negative coordinates are normal: a
monitor placed left of or above the primary has a negative origin. The
arithmetic below never assumes a non-negative origin.

Mutual exclusion with the numbered overlay
------------------------------------------
Exactly one overlay may be live, so that a bare spoken number is never
ambiguous. The two state machines stay decoupled -- neither imports the
other -- and the integration layer wires them together in both
directions:

  * **Grid opens -> numbers close.** Every accepted ``open_grid`` emits a
    ``CLOSE_NUMBERED_OVERLAY`` effect FIRST. The integration performs it
    by applying ``OverlayEvent(OverlayEventKind.HIDE_NUMBERS)`` to the
    ``ClickOverlayStateMachine`` (that is the numbered overlay's
    documented close hook; it dispatches the clear, unpins the snapshot,
    and enters ``closed``, and it is a NO_OP when the overlay is already
    closed, so the grid does not have to know the overlay's state).
  * **Numbers open -> grid closes.** When the integration handles "apply
    numbers" (or the ambiguous-click auto-open) it applies
    ``GridEvent(GridEventKind.NUMBERED_OVERLAY_OPENED)`` here BEFORE
    driving the overlay machine; the grid clears and forgets its mark.

Concurrency model
-----------------
Not thread-safe, by the same contract as ``click_overlay_state.py``: the
machine is owned by the Logic process and must be driven from the single
asyncio event loop that serialises the speech-pipeline handlers. A
consumer on another thread must marshal onto that loop.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Sequence


# The grid is 3x3, so nine cells numbered 1..9. Named constants rather
# than bare 3 / 9 literals so the arithmetic below reads as "divisions",
# not as an accidental magic number; the spec fixes both values.
GRID_DIVISIONS = 3
GRID_CELL_COUNT = GRID_DIVISIONS * GRID_DIVISIONS

# The notice reason for "drag" spoken with no mark set. The integration
# layer owns the wording; the machine only names the case.
DRAG_WITHOUT_MARK = "drag_without_mark"


@dataclass(frozen=True)
class GridPoint:
    """A point in virtual-desktop physical pixels."""

    x: int
    y: int


@dataclass(frozen=True)
class GridRect:
    """A rectangle in virtual-desktop physical pixels.

    Stored as origin plus size (``left`` / ``top`` / ``width`` /
    ``height``) rather than as four edges, so the arithmetic never has to
    decide whether the far edges are inclusive. ``right`` and ``bottom``
    are EXCLUSIVE: a rectangle covers the integer coordinates
    ``left <= x < right`` and ``top <= y < bottom``. That convention is
    what makes the nine cells tile exactly -- one cell's ``right`` is the
    next cell's ``left`` with no pixel counted twice and none skipped.

    ``left`` and ``top`` may be negative (a monitor left of or above the
    primary). ``width`` and ``height`` are expected to be non-negative;
    a degenerate zero-size rectangle is representable so that the
    subdivision of a tiny rectangle stays total, but ``is_valid`` is
    ``False`` for it and the state machine refuses to open on one.
    """

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """The exclusive right edge (``left + width``)."""

        return self.left + self.width

    @property
    def bottom(self) -> int:
        """The exclusive bottom edge (``top + height``)."""

        return self.top + self.height

    @property
    def is_valid(self) -> bool:
        """Whether the rectangle encloses at least one pixel."""

        return self.width > 0 and self.height > 0

    @property
    def center(self) -> GridPoint:
        """The rectangle's center point (see :func:`cell_center`)."""

        return cell_center(self)

    def as_tuple(self) -> tuple[int, int, int, int]:
        """``(left, top, width, height)`` -- the paint-event wire order."""

        return (self.left, self.top, self.width, self.height)


# ---------------------------------------------------------------------------
# Cell arithmetic -- pure functions, no window, no Windows API
# ---------------------------------------------------------------------------


def _edges(origin: int, size: int) -> tuple[int, int, int, int]:
    """The four boundary coordinates of a 3-way split of ``size``.

    ROUNDING RULE (the one decision a 3x3 split of an arbitrary rectangle
    has to make). Boundary ``i`` sits at ``origin + (size * i) // 3``:
    each edge is the exact proportional position floored to an integer.
    Two consequences, both wanted:

      * The nine cells tile the parent EXACTLY. Consecutive cells share a
        boundary coordinate by construction, and the last boundary is
        ``origin + size``, so there is no gap, no overlap, and no
        off-by-one at the far edge -- whatever the parent size.
      * When the size does not divide by three, the leftover pixels land
        in the TRAILING cells. A width of 10 splits 3 / 3 / 4; a width of
        11 splits 3 / 4 / 4. The largest cell is never more than one
        pixel wider than the smallest, which is invisible on screen and
        cannot accumulate: each refinement re-derives the edges from the
        current rectangle rather than from a running fractional offset.

    ``origin`` is added AFTER the floor division so a negative origin (a
    monitor left of or above the primary) cannot change the split -- the
    division operates on the non-negative ``size`` alone.
    """

    return (
        origin,
        origin + (size * 1) // GRID_DIVISIONS,
        origin + (size * 2) // GRID_DIVISIONS,
        origin + size,
    )


def cell_rects(rect: GridRect) -> tuple[GridRect, ...]:
    """Split ``rect`` into its nine cells, in spoken-number order.

    The returned tuple is indexed by ``number - 1``: cells run left to
    right within a row and rows run top to bottom, so 1 is the top-left
    cell, 5 the middle, and 9 the bottom-right -- telephone-keypad order,
    which is what every grid tool teaches and what the painted numbers
    show.

    The nine cells exactly tile ``rect`` (see :func:`_edges` for the
    rounding rule). A rectangle smaller than three pixels on a side
    yields some zero-width or zero-height cells rather than fewer than
    nine cells; the state machine's minimum-size floor keeps refinement
    away from that region in practice.
    """

    xs = _edges(rect.left, rect.width)
    ys = _edges(rect.top, rect.height)
    cells: list[GridRect] = []
    for row in range(GRID_DIVISIONS):
        for col in range(GRID_DIVISIONS):
            cells.append(
                GridRect(
                    left=xs[col],
                    top=ys[row],
                    width=xs[col + 1] - xs[col],
                    height=ys[row + 1] - ys[row],
                )
            )
    return tuple(cells)


def cell_rect(rect: GridRect, number: int) -> GridRect:
    """Return the sub-rectangle of ``rect`` for spoken ``number`` (1..9).

    Raises ``ValueError`` for a number outside 1..9. The state machine
    never calls this with an out-of-range number (it screens the number
    first and treats an out-of-range one as a no-op), so a raise here
    means a caller skipped that screen -- a programming error worth
    surfacing rather than silently clamping to a cell the user did not
    ask for.
    """

    if not isinstance(number, int) or isinstance(number, bool):
        raise ValueError(f"cell number must be an int, got {number!r}")
    if not 1 <= number <= GRID_CELL_COUNT:
        raise ValueError(
            f"cell number must be in 1..{GRID_CELL_COUNT}, got {number}"
        )
    return cell_rects(rect)[number - 1]


def cell_center(rect: GridRect) -> GridPoint:
    """The center point of ``rect``, floored to whole pixels.

    ``left + width // 2`` (not ``(left + right) // 2``) so the result is
    unaffected by a negative origin: the halving operates on the
    non-negative size. For an odd size the point sits one pixel left of
    or above the true center, which is inside the rectangle for any
    rectangle at least one pixel on a side -- important, because this
    point is where the pointer is placed.
    """

    return GridPoint(rect.left + rect.width // 2, rect.top + rect.height // 2)


def can_refine(rect: GridRect, min_cell_px: int) -> bool:
    """Whether a spoken number may still narrow ``rect``.

    The test is on the CURRENT rectangle, not on the cell a refinement
    would produce: the spec stops refinement once "a cell falls below
    ``grid_min_cell_px`` on a side", and a cell that small already
    locates the pointer to within a click's precision. The boundary is
    INCLUSIVE -- a rectangle of exactly the floor on both sides still
    refines once more; the next number is the one that is ignored.

    Both sides must clear the floor: a rectangle that is wide but only a
    few pixels tall is as unrefinable as a small square.

    The effective floor is ``max(min_cell_px, GRID_DIVISIONS)``. A side
    shorter than three pixels cannot split into three non-empty cells --
    ``_edges`` would hand back a zero-width or zero-height cell, and
    refining into one would leave the machine holding a rectangle with no
    pixels in it and nothing sensible to paint. Three is therefore a hard
    lower bound on the floor whatever the operator configures. It never
    binds at the default (24 is far above it); it exists so an
    aggressively small configured floor degrades to "stops one step
    earlier" instead of to a degenerate rectangle.
    """

    floor = max(min_cell_px, GRID_DIVISIONS)
    return rect.width >= floor and rect.height >= floor


def primary_monitor(monitors: Sequence[GridRect]) -> Optional[GridRect]:
    """The primary monitor, or ``None`` for an empty list.

    Windows always places the primary monitor's rectangle at the
    virtual-desktop origin, so the monitor whose ``(left, top)`` is
    ``(0, 0)`` is the primary by definition -- a geometric test that does
    not depend on enumeration order. If no monitor sits at the origin
    (a degenerate or synthetic topology), fall back to the FIRST monitor
    in the list, which is the primary under a normal Windows enumeration;
    ``shared/monitor_geometry.py:_resolve_target_monitor`` makes the same
    first-monitor fallback for the same reason.
    """

    for monitor in monitors:
        if monitor.left == 0 and monitor.top == 0:
            return monitor
    if monitors:
        return monitors[0]
    return None


def next_monitor(
    monitors: Sequence[GridRect], current: Optional[GridRect]
) -> Optional[GridRect]:
    """The monitor after ``current`` in ``monitors``, wrapping around.

    Monitors are matched by rectangle equality, which is unambiguous on a
    real desktop: two monitors cannot occupy the same virtual-desktop
    rectangle, whatever their resolutions or DPI. Mixed resolutions and
    negative origins need no special handling.

    Returns the FIRST monitor when ``current`` is ``None`` or is not in
    the list -- the topology changed under us (a monitor was
    disconnected), and restarting on a monitor that exists beats
    refusing. Returns ``None`` only for an empty list. With a single
    monitor the wrap returns that same monitor, which the state machine
    treats as a restart at its full rectangle rather than as a failure.
    """

    if not monitors:
        return None
    if current is not None:
        for index, monitor in enumerate(monitors):
            if monitor == current:
                return monitors[(index + 1) % len(monitors)]
    return monitors[0]


def resolve_open_monitor(
    monitors: Sequence[GridRect], focused: Optional[GridRect]
) -> Optional[GridRect]:
    """Where a freshly opened grid goes.

    The focused window's monitor when the integration could determine it
    (Logic already tracks that monitor for the numbered overlay's
    focus-following), otherwise the primary monitor -- the spec's
    "focused window's monitor undeterminable -> grid opens on the primary
    monitor". A degenerate ``focused`` rectangle (zero width or height)
    counts as undeterminable. Returns ``None`` only when there is nothing
    to open on at all, in which case the grid stays closed.
    """

    if focused is not None and focused.is_valid:
        return focused
    return primary_monitor(monitors)


# ---------------------------------------------------------------------------
# State machine: states, events, effects, outcomes
# ---------------------------------------------------------------------------


class GridState(enum.Enum):
    """Lifecycle states for the mouse grid.

    Two states only, unlike the numbered overlay's seven: the grid has no
    in-flight phase to model. Nothing has to be walked or built before it
    can be painted -- the geometry is known the instant the monitor is,
    so an ``open_grid`` moves straight to ``open`` and the paint is a
    fire-and-forget effect. There is no generation pair either, for the
    same reason: no response can arrive late enough to be confused with a
    newer one.
    """

    CLOSED = "closed"
    OPEN = "open"


class GridEventKind(enum.Enum):
    """Inbound event kinds for the grid state machine.

    ``OPEN_GRID`` and ``NEXT_MONITOR`` carry monitor geometry
    (``focused_monitor`` / ``monitors``); ``REFINE`` carries ``number``;
    the rest carry nothing.
    """

    # "show grid".
    OPEN_GRID = "open_grid"
    # A bare spoken number 1..9 (with or without the optional "number"
    # prefix) while the grid is open.
    REFINE = "refine"
    # "mark" -- anchor the drag start at the current cell center.
    MARK = "mark"
    # "drag" -- from the mark to the current cell center.
    DRAG_REQUESTED = "drag_requested"
    # "grid next screen".
    NEXT_MONITOR = "next_monitor"
    # "hide grid".
    DISMISS = "dismiss"
    # A click / double click / right click / move here was performed at
    # the current cell center; the grid closes behind it.
    ACTION_COMPLETED = "action_completed"
    # The numbered overlay is being opened ("show numbers", or the
    # ambiguous-click auto-open). Mutual exclusion: the grid closes.
    NUMBERED_OVERLAY_OPENED = "numbered_overlay_opened"


@dataclass(frozen=True)
class GridEvent:
    """A single inbound event for the grid state machine.

    Lightweight, machine-local event representation (NOT an IPC wire
    schema), mirroring ``click_overlay_state.OverlayEvent``. Carries only
    the primitives the machine needs to decide a transition; fields not
    meaningful for a kind stay at their defaults.

    Fields:
      kind: which event this is.
      number: the spoken cell number for ``REFINE``. A value outside
        1..9 is treated as a no-op rather than an error -- the routing
        layer parses spoken text, and a number it could not have meant
        for the grid must not disturb the grid's state.
      monitors: the current desktop topology, in enumeration order, for
        ``OPEN_GRID`` (primary fallback) and ``NEXT_MONITOR`` (the
        wrap-around). Passed on the event rather than cached on the
        machine so a monitor connected or disconnected mid-session is
        picked up on the next command with no invalidation logic.
      focused_monitor: the monitor containing the focused window for
        ``OPEN_GRID``, or ``None`` when the integration could not
        determine it (then the primary monitor is used).
    """

    kind: GridEventKind
    number: int = 0
    monitors: tuple[GridRect, ...] = ()
    focused_monitor: Optional[GridRect] = None


class GridEffectKind(enum.Enum):
    """The kinds of side effect the integration layer performs."""

    # Paint (or repaint) the nine cells over ``rect`` on ``monitor``.
    PAINT_GRID = "paint_grid"
    # Paint the drag-anchor pin at ``point``.
    PAINT_PIN = "paint_pin"
    # Remove the grid and any pin from the screen.
    CLEAR_GRID = "clear_grid"
    # Close the numbered overlay (mutual exclusion). The integration
    # performs this by applying OverlayEvent(HIDE_NUMBERS) to the
    # ClickOverlayStateMachine; see the module docstring.
    CLOSE_NUMBERED_OVERLAY = "close_numbered_overlay"
    # Run a drag in the Input process: button down at ``start_point``,
    # interpolated movement over ``duration_ms``, release at
    # ``end_point``.
    PERFORM_DRAG = "perform_drag"
    # Surface a notice named by ``notice_reason``; the integration owns
    # the wording and the rate limiting.
    FIRE_NOTICE = "fire_notice"


@dataclass(frozen=True)
class GridEffect:
    """One side effect the integration layer must perform, as data.

    Not every field is meaningful for every ``kind``; the unused ones
    stay at their defaults. The class never performs the effect -- it
    only describes it. Effects are returned in the order the integration
    must apply them.

    Fields:
      kind: which effect.
      monitor: the monitor the grid is painted on (PAINT_GRID). The GUI
        needs it to pick the right per-monitor overlay window.
      rect: the rectangle the nine cells subdivide (PAINT_GRID).
      point: where to paint the pin (PAINT_PIN).
      start_point / end_point: the drag endpoints (PERFORM_DRAG).
      duration_ms: how long the interpolated drag movement takes
        (PERFORM_DRAG), from the configured ``drag_duration_ms``.
      notice_reason: which notice to surface (FIRE_NOTICE).
    """

    kind: GridEffectKind
    monitor: Optional[GridRect] = None
    rect: Optional[GridRect] = None
    point: Optional[GridPoint] = None
    start_point: Optional[GridPoint] = None
    end_point: Optional[GridPoint] = None
    duration_ms: int = 0
    notice_reason: str = ""


class GridOutcome(enum.Enum):
    """Result of applying an event to the grid state machine.

    ACCEPTED: the event drove a transition and/or produced effects.
    NO_OP: a well-defined event that means nothing in this state (a
      grid command while the grid is closed, a ``REFINE`` whose number is
      not 1..9). No state change, no effects. Exception: ``DISMISS``
      while closed is ACCEPTED with a defensive ``CLEAR_GRID`` -- see
      ``_on_dismiss``.
    IGNORED_MIN_CELL: a ``REFINE`` refused because the current cell is
      already below ``grid_min_cell_px`` on a side. Deliberately distinct
      from NO_OP: the number WAS consumed by the grid (it must not fall
      through to dictation or to any other consumer), it simply changed
      nothing. The spec calls for silence here -- no notice.
    NO_MARK: a ``DRAG_REQUESTED`` with no mark set. The grid stays open
      and a notice explains the mark step; no input is sent.
    NO_MONITOR: there is no monitor to work with -- an ``OPEN_GRID`` with
      neither a focused monitor nor a topology, or a ``NEXT_MONITOR``
      with an empty topology. State is unchanged.
    """

    ACCEPTED = "accepted"
    NO_OP = "no_op"
    IGNORED_MIN_CELL = "ignored_min_cell"
    NO_MARK = "no_mark"
    NO_MONITOR = "no_monitor"


@dataclass(frozen=True)
class GridApplyResult:
    """The (outcome, ordered effects) pair returned by ``apply``."""

    outcome: GridOutcome
    effects: tuple[GridEffect, ...] = ()


@dataclass
class GridOverlayStateMachine:
    """Logic-side mouse-grid state machine.

    See the module docstring for the contract. State fields:

      state: ``GridState.CLOSED`` or ``GridState.OPEN``.
      monitor: the monitor the grid lives on while open, else ``None``.
        Refinement never crosses monitors, so this is fixed for the
        session until "grid next screen" moves it.
      rect: the current rectangle -- the whole monitor when the grid
        opens, then the chosen cell after each refinement. ``None``
        while closed.
      mark: the drag anchor, or ``None``. Set by "mark", consumed by
        "drag", and cleared by EVERY path that closes the grid, so a
        stale mark can never cause a surprise drag in a later session.

    The constructor takes the two configuration values as plain ints (the
    integration reads them from ``ClickConfig``; this class does NOT read
    config), matching how ``ClickOverlayStateMachine`` takes its
    deadlines.
    """

    grid_min_cell_px: int = 24
    drag_duration_ms: int = 250

    state: GridState = GridState.CLOSED
    monitor: Optional[GridRect] = None
    rect: Optional[GridRect] = None
    mark: Optional[GridPoint] = None

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """Whether the grid is on screen and claiming spoken numbers."""

        return self.state is GridState.OPEN

    @property
    def current_center(self) -> Optional[GridPoint]:
        """Where an action would act, or ``None`` while closed.

        Every grid action fires at this point, so the routing layer reads
        it here rather than recomputing the geometry.
        """

        if self.rect is None:
            return None
        return cell_center(self.rect)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def apply(self, event: GridEvent) -> GridApplyResult:
        """Apply ``event`` and return ``(outcome, ordered effects)``.

        Never raises on a well-typed call. Every command that arrives
        while the grid is closed -- except ``OPEN_GRID`` -- is a NO_OP
        rather than an error: these events originate in speech, and a
        misrecognition or a race with a just-closed grid must not put the
        machine into a state the user has to recover from.
        """

        handler = _DISPATCH.get(event.kind)
        if handler is None:  # pragma: no cover - all kinds are in _DISPATCH
            return GridApplyResult(GridOutcome.NO_OP)
        return handler(self, event)

    def reset_to_closed(self) -> tuple[GridEffect, ...]:
        """Force the machine to ``closed`` and return the clear effect.

        Mirrors ``ClickOverlayStateMachine.reset_to_closed``. The
        integration calls this on the recovery paths the spec lists --
        the GUI process restarting under a live grid, or the monitor the
        grid is on disappearing -- so the next "show grid" starts fresh.
        Returns a single ``CLEAR_GRID`` when the grid was open (something
        may still be on screen), and an empty tuple when it was already
        closed, so an already-clean machine emits no spurious clear.
        """

        if self.state is GridState.CLOSED:
            return ()
        self._enter_closed()
        return (GridEffect(kind=GridEffectKind.CLEAR_GRID),)

    # ------------------------------------------------------------------
    # Internal: state entry helpers
    # ------------------------------------------------------------------
    def _enter_closed(self) -> None:
        """Set fields for the ``closed`` state.

        Drops the monitor, the rectangle, AND the mark. The mark dying
        here is the spec's guarantee that it never persists past the
        overlay: every close path routes through this method, so there is
        no path that can leave one behind.
        """

        self.state = GridState.CLOSED
        self.monitor = None
        self.rect = None
        self.mark = None

    def _start_on(self, monitor: GridRect) -> None:
        """Open (or restart) the grid at ``monitor``'s full rectangle."""

        self.state = GridState.OPEN
        self.monitor = monitor
        self.rect = monitor

    # ------------------------------------------------------------------
    # Internal: effect builders
    # ------------------------------------------------------------------
    def _paint_grid(self) -> GridEffect:
        return GridEffect(
            kind=GridEffectKind.PAINT_GRID,
            monitor=self.monitor,
            rect=self.rect,
        )

    def _paint_pin(self) -> GridEffect:
        return GridEffect(kind=GridEffectKind.PAINT_PIN, point=self.mark)

    def _clear_grid(self) -> GridEffect:
        return GridEffect(kind=GridEffectKind.CLEAR_GRID)

    def _close_numbered_overlay(self) -> GridEffect:
        return GridEffect(kind=GridEffectKind.CLOSE_NUMBERED_OVERLAY)

    def _close_with_clear(self) -> GridApplyResult:
        """Every "the grid goes away now" path: clear, then close."""

        self._enter_closed()
        return GridApplyResult(GridOutcome.ACCEPTED, (self._clear_grid(),))

    # ------------------------------------------------------------------
    # Internal: per-event handlers
    # ------------------------------------------------------------------
    def _on_open_grid(self, event: GridEvent) -> GridApplyResult:
        """"show grid" -- from either state.

        Resolves the monitor (focused window's, else primary), restarts
        at that monitor's full rectangle, and paints. The
        ``CLOSE_NUMBERED_OVERLAY`` effect comes FIRST so the two overlays
        are never on screen together even for one frame.

        Re-saying it while the grid is already open restarts the
        rectangle at the full monitor but KEEPS an existing mark: the
        session never ended, the pin is still on screen, and a user who
        marked a point and then wants to start the destination over
        should not silently lose the anchor. Only a close path clears the
        mark. The pin is repainted so the GUI does not have to remember
        it across the restart.
        """

        monitor = resolve_open_monitor(event.monitors, event.focused_monitor)
        if monitor is None:
            # Nothing to open on. Leave the machine exactly as it was --
            # in particular, do NOT close a live grid on a failed reopen.
            return GridApplyResult(GridOutcome.NO_MONITOR)

        self._start_on(monitor)
        effects = [self._close_numbered_overlay(), self._paint_grid()]
        if self.mark is not None:
            effects.append(self._paint_pin())
        return GridApplyResult(GridOutcome.ACCEPTED, tuple(effects))

    def _on_refine(self, event: GridEvent) -> GridApplyResult:
        """A bare spoken number 1..9 while the grid is open."""

        if self.state is GridState.CLOSED or self.rect is None:
            return GridApplyResult(GridOutcome.NO_OP)
        if not 1 <= event.number <= GRID_CELL_COUNT:
            # Not a grid cell number. The routing layer screens this too;
            # refusing here as well keeps the machine total.
            return GridApplyResult(GridOutcome.NO_OP)
        if not can_refine(self.rect, self.grid_min_cell_px):
            # Silently ignored per the spec: a cell this small already
            # locates the pointer to within a click's precision, and a
            # notice on every further number would be noise.
            return GridApplyResult(GridOutcome.IGNORED_MIN_CELL)

        self.rect = cell_rect(self.rect, event.number)
        return GridApplyResult(GridOutcome.ACCEPTED, (self._paint_grid(),))

    def _on_mark(self, event: GridEvent) -> GridApplyResult:
        """"mark" -- anchor the drag start, then start the grid over.

        The rectangle resets to the full monitor because the user now has
        to navigate to a SECOND point; leaving them zoomed into the
        anchor's cell would make the destination unreachable without a
        "show grid". The grid stays open and the pin marks the anchor.
        """

        if self.state is GridState.CLOSED or self.rect is None:
            return GridApplyResult(GridOutcome.NO_OP)
        if self.monitor is None:  # pragma: no cover - open implies a monitor
            return GridApplyResult(GridOutcome.NO_OP)

        self.mark = cell_center(self.rect)
        self.rect = self.monitor
        return GridApplyResult(
            GridOutcome.ACCEPTED, (self._paint_pin(), self._paint_grid())
        )

    def _on_drag_requested(self, event: GridEvent) -> GridApplyResult:
        """"drag" -- from the mark to the current cell center.

        The grid closes at REQUEST time rather than on a later
        ``ACTION_COMPLETED``: both endpoints have to be read out of the
        state that exists now, and the overlay must not still be painted
        over the screen while the pointer drags across it for
        ``drag_duration_ms``. The Input process guarantees the button
        release even if the movement fails partway, so there is no state
        here that a failed drag would need to unwind.
        """

        if self.state is GridState.CLOSED or self.rect is None:
            return GridApplyResult(GridOutcome.NO_OP)
        if self.mark is None:
            # No input is sent; the notice explains the mark step. The
            # grid stays open so the user can say "mark" and continue.
            return GridApplyResult(
                GridOutcome.NO_MARK,
                (
                    GridEffect(
                        kind=GridEffectKind.FIRE_NOTICE,
                        notice_reason=DRAG_WITHOUT_MARK,
                    ),
                ),
            )

        drag = GridEffect(
            kind=GridEffectKind.PERFORM_DRAG,
            start_point=self.mark,
            end_point=cell_center(self.rect),
            duration_ms=self.drag_duration_ms,
        )
        self._enter_closed()
        return GridApplyResult(
            GridOutcome.ACCEPTED, (drag, self._clear_grid())
        )

    def _on_next_monitor(self, event: GridEvent) -> GridApplyResult:
        """"grid next screen" -- restart at the next monitor, full size.

        Refinement never crosses monitors, so moving the grid is a
        restart, not a translation. The mark SURVIVES the move: it is a
        virtual-desktop point, and a drag whose two endpoints are on
        different monitors is a legitimate thing to want. The pin is
        repainted so the GUI can re-assert it on the new monitor's
        overlay window.
        """

        if self.state is GridState.CLOSED:
            return GridApplyResult(GridOutcome.NO_OP)
        target = next_monitor(event.monitors, self.monitor)
        if target is None:
            return GridApplyResult(GridOutcome.NO_MONITOR)

        self._start_on(target)
        effects = [self._paint_grid()]
        if self.mark is not None:
            effects.append(self._paint_pin())
        return GridApplyResult(GridOutcome.ACCEPTED, tuple(effects))

    def _on_dismiss(self, event: GridEvent) -> GridApplyResult:
        """"hide grid" -- close, forget the pin, press nothing.

        While CLOSED this still emits a defensive ``CLEAR_GRID``
        (wh-mouse-grid.1.3): the machine has no paint acknowledgement, so
        a paint can outlive it -- e.g. the GUI painted after Logic timed
        out and reset. The spoken dismiss is the user's recovery path for
        that stranded overlay, and a redundant clear over an already-blank
        screen is harmless. Full paint-ack tracking is wh-grid-paint-ack.
        """

        if self.state is GridState.CLOSED:
            return GridApplyResult(GridOutcome.ACCEPTED, (self._clear_grid(),))
        return self._close_with_clear()

    def _on_action_completed(self, event: GridEvent) -> GridApplyResult:
        """A click / double click / right click / move here was performed.

        The integration applies this once the action has been dispatched
        to the Input process. The grid closes and the mark dies with the
        session. A late ``ACTION_COMPLETED`` after the grid already
        closed is a NO_OP, not an error -- it is the tail of work the
        integration itself started.
        """

        if self.state is GridState.CLOSED:
            return GridApplyResult(GridOutcome.NO_OP)
        return self._close_with_clear()

    def _on_numbered_overlay_opened(self, event: GridEvent) -> GridApplyResult:
        """The numbered overlay is opening; the grid must go (exclusion)."""

        if self.state is GridState.CLOSED:
            return GridApplyResult(GridOutcome.NO_OP)
        return self._close_with_clear()


# Dispatch table: event kind -> handler. Built after the class so the
# methods exist. ``apply`` looks the incoming kind up here. Keyed on the
# EVENT (not the state, as click_overlay_state.py is) because the grid
# has only two states and each handler's state check is one line, while
# the per-event behaviour is what actually differs.
_DISPATCH = {
    GridEventKind.OPEN_GRID: GridOverlayStateMachine._on_open_grid,
    GridEventKind.REFINE: GridOverlayStateMachine._on_refine,
    GridEventKind.MARK: GridOverlayStateMachine._on_mark,
    GridEventKind.DRAG_REQUESTED: GridOverlayStateMachine._on_drag_requested,
    GridEventKind.NEXT_MONITOR: GridOverlayStateMachine._on_next_monitor,
    GridEventKind.DISMISS: GridOverlayStateMachine._on_dismiss,
    GridEventKind.ACTION_COMPLETED: GridOverlayStateMachine._on_action_completed,
    GridEventKind.NUMBERED_OVERLAY_OPENED: (
        GridOverlayStateMachine._on_numbered_overlay_opened
    ),
}


__all__ = [
    "DRAG_WITHOUT_MARK",
    "GRID_CELL_COUNT",
    "GRID_DIVISIONS",
    "GridApplyResult",
    "GridEffect",
    "GridEffectKind",
    "GridEvent",
    "GridEventKind",
    "GridOutcome",
    "GridOverlayStateMachine",
    "GridPoint",
    "GridRect",
    "GridState",
    "can_refine",
    "cell_center",
    "cell_rect",
    "cell_rects",
    "next_monitor",
    "primary_monitor",
    "resolve_open_monitor",
]
