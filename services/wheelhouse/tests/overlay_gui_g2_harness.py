"""A GuiManager rig for the area G2 boundary tests (wh-full-refactor.7.1).

Bead: wh-full-refactor.7.1 (stage A of place 4). The tests in
tests/test_overlay_gui_requirements_g2.py drive the overlay and grid handlers
of ``GuiManager`` (gui.py) only through the inputs that GuiManager receives in
production:

  * a Logic message on the state queue, read by the queue pump; and
  * the expiry of a timer that GuiManager armed.

They read only what leaves GuiManager: the commands on the queue to Logic, and
the calls on the fake operating system seam of
tests/overlay_paint_seam_harness.py (helper 1's harness, used unchanged).

What this file replaces:

  * ``gui.QTimer`` (a public module attribute). ``FakeClock.timer_class``
    gives a stand-in class. Each instance registers with the clock. ``start``
    sets a deadline on the fake clock; ``FakeClock.advance`` moves the clock
    and fires each timer whose deadline it passes, in deadline order, so a
    timer that its own slot re-arms fires again inside the same advance. No
    test waits in real time. A test never names the GuiManager attribute that
    holds a timer: it moves the clock, and each timer that is due fires. Like
    PySide6, ``start`` raises OverflowError for an interval above the signed
    32-bit limit and then leaves the timer as it was (measured with PySide6:
    a timer started at 5000 ms keeps 5000 ms after ``start(2**31)`` raises).
  * The tray icon: ``gui.pystray``, a wrapper of the Windows notification
    area (an operating system seam). The editor window is replaced by the
    conftest fixture ``mock_editor_window``.

The floating button and the working dialog are the real widgets. GuiManager
builds them but does not show them, because the rig never calls
``GuiManager.start``. The button starts a repeating 3000 ms timer that
raises it only while it is visible, so a long advance fires that timer many
times and changes nothing. At the end of each test the rig asserts that the
button and the dialog are not visible, so no test leaves a window on the
screen.

The queue pump runs on the public attribute ``queue_timer``; ``GuiRig.send``
puts the messages on the state queue and fires that fake timer once.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from queue import Empty, Queue
from typing import Callable, Optional
from unittest.mock import MagicMock, patch

import gui
from tests.overlay_paint_seam_harness import FakeMonitors, PaintSeam

# The largest interval that QTimer.start accepts (signed 32-bit int).
QT_INT_MAX = 2**31 - 1


class FakeSignal:
    """The ``timeout`` signal of a fake timer."""

    def __init__(self) -> None:
        self.slots: list[Callable] = []

    def connect(self, slot: Callable) -> None:
        self.slots.append(slot)

    def emit(self) -> None:
        for slot in list(self.slots):
            slot()


class FakeClock:
    """A clock in milliseconds that the fake timers run on."""

    def __init__(self) -> None:
        self.now = 0
        self.timers: list = []
        self.single_shots: list = []

    def timer_class(self):
        """A class that stands in for QTimer, bound to this clock."""
        clock = self

        class FakeTimer:
            def __init__(self, parent=None) -> None:
                self.parent = parent
                self.timeout = FakeSignal()
                self.single_shot = False
                self.interval_ms = 0
                self.deadline: Optional[int] = None
                self.fires = 0
                clock.timers.append(self)

            def setSingleShot(self, value: bool) -> None:
                self.single_shot = bool(value)

            def isSingleShot(self) -> bool:
                return self.single_shot

            def setInterval(self, ms: int) -> None:
                self.interval_ms = int(ms)

            def interval(self) -> int:
                return self.interval_ms

            def start(self, ms: Optional[int] = None) -> None:
                if ms is not None:
                    if int(ms) > QT_INT_MAX or int(ms) < -QT_INT_MAX - 1:
                        raise OverflowError(
                            f"Value {ms} exceeds limits of type int (4bytes)"
                        )
                    self.interval_ms = int(ms)
                self.deadline = clock.now + self.interval_ms

            def stop(self) -> None:
                self.deadline = None

            def isActive(self) -> bool:
                return self.deadline is not None

            def remainingTime(self) -> int:
                if self.deadline is None:
                    return -1
                return max(0, self.deadline - clock.now)

            def fire(self) -> None:
                """The timer runs out: emit timeout, as Qt does.

                A repeating timer that runs re-arms; a repeating timer that
                was never started (the queue timer, fired by hand) stays
                stopped.
                """
                if self.single_shot or self.deadline is None:
                    self.deadline = None
                else:
                    self.deadline = clock.now + max(1, self.interval_ms)
                self.fires += 1
                self.timeout.emit()

            @staticmethod
            def singleShot(ms, slot) -> None:
                # Recorded only; no G2 path uses it.
                clock.single_shots.append((ms, slot))

        return FakeTimer

    def advance(self, ms: int) -> None:
        """Move the clock forward by ``ms`` and fire each timer that is due.

        Timers fire in deadline order (creation order on a tie). A slot that
        re-arms its timer inside the window makes it fire again. A repeating
        timer fires once for each interval inside the window: the rig never
        starts the queue timer, and the 3000 ms timer of the floating button
        fires about 29000 times in one day, below the limit of 100000.
        """
        target = self.now + int(ms)
        for _ in range(100_000):
            due = [t for t in self.timers
                   if t.deadline is not None and t.deadline <= target]
            if not due:
                break
            timer = min(due, key=lambda t: (t.deadline, self.timers.index(t)))
            self.now = timer.deadline
            timer.fire()
        else:
            raise AssertionError("the fake timers fired 100000 times in one advance")
        self.now = target


class GuiRig:
    """A real GuiManager above the fake seam, with fake timers."""

    def __init__(self, gui_manager, seam: PaintSeam, clock: FakeClock,
                 state_queue: Queue, commands_queue: Queue) -> None:
        self.gui = gui_manager
        self.seam = seam
        self.clock = clock
        self.state_queue = state_queue
        self.commands_queue = commands_queue

    def send(self, *messages) -> None:
        """Put Logic messages on the state queue and run the pump once."""
        for message in messages:
            self.state_queue.put(message)
        self.gui.queue_timer.fire()

    def advance(self, ms: int) -> None:
        self.clock.advance(ms)

    def to_logic(self) -> list:
        """Take every command that GuiManager put on the queue to Logic."""
        out = []
        while True:
            try:
                out.append(self.commands_queue.get_nowait())
            except Empty:
                return out


@contextmanager
def gui_rig(monitors: Optional[FakeMonitors] = None,
            commands_queue: Optional[Queue] = None,
            config: Optional[dict] = None):
    """Build a GuiManager above a PaintSeam and a FakeClock; yield a GuiRig.

    The test must also use the fixtures ``qapp`` and ``mock_editor_window``.
    """
    seam = PaintSeam(monitors)
    clock = FakeClock()
    state_queue: Queue = Queue()
    commands = commands_queue if commands_queue is not None else Queue()
    with ExitStack() as stack:
        seam.install(stack)
        stack.enter_context(patch.object(gui, "QTimer", clock.timer_class()))
        stack.enter_context(patch.object(gui, "pystray", MagicMock()))
        manager = gui.GuiManager(threading.Event(), commands, state_queue,
                                 config=config)
        yield GuiRig(manager, seam, clock, state_queue, commands)
        assert not manager.button.isVisible(), "the floating button is visible"
        assert not manager.working_dialog.isVisible(), (
            "the working dialog is visible")
