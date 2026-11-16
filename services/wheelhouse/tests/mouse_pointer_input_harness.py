"""Input-process side harness for the mouse pointer tests (U7, place 3 stage A).

It extends ``InputSide`` (tests/overlay_stack_input_harness.py) with fakes of the
public SendInput primitives that the four pointer actions end in:
``utils.win_input_sender.click_point``, ``move_pointer_to`` and ``drag_pointer``.
The pointer position read (``win32gui.GetCursorPos``) is already faked by
``InputSide``; this class replaces it so a test can make the read raise.

Each fake records its call as a tuple and returns the ``(succeeded, reason)``
that the test set, or raises the exception that the test set. No real input is
ever sent. The deeper ``user32.SendInput`` level is NOT faked on purpose: the
real drag runs an interpolation with real sleeps.

No fake here names a private member of a production class.
"""

from __future__ import annotations

import queue as queue_mod
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from tests.overlay_stack_input_harness import InputSide


class OnceFailingQueue:
    """A fake of the response queue (the other process side).

    The first ``put`` raises when ``fail_first_put`` is set. Every other put
    lands in ``replies``, which ``InputSide.send`` reads.
    """

    def __init__(self, *, fail_first_put: bool = False) -> None:
        self.replies: "queue_mod.Queue[Any]" = queue_mod.Queue()
        self.fail_next = fail_first_put
        self.put_attempts = 0

    def put(self, item: Any) -> None:
        self.put_attempts += 1
        if self.fail_next:
            self.fail_next = False
            raise OSError("the response queue is closed")
        self.replies.put(item)


class PointerSide(InputSide):
    """``InputSide`` plus fakes of the three pointer primitives."""

    def __init__(self, *, fail_first_put: bool = False, **kwargs: Any) -> None:
        self.queue_fake = OnceFailingQueue(fail_first_put=fail_first_put)
        super().__init__(reply_queue=self.queue_fake, **kwargs)
        self.replies = self.queue_fake.replies
        self.click_calls: list[tuple] = []
        self.move_calls: list[tuple] = []
        self.drag_calls: list[tuple] = []
        #: A (succeeded, reason) pair, a callable, or an exception to raise.
        self.click_result: Any = (True, None)
        self.move_result: Any = (True, None)
        self.drag_result: Any = (True, None)
        #: The pointer read: an (x, y) pair, or an exception to raise.
        self.pointer: Any = (60, 45)

    @staticmethod
    def _answer(result: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    def _install(self, stack: ExitStack) -> None:
        super()._install(stack)
        import utils.win_input_sender as sender
        import win32gui

        def click_point(x, y, button="left", click_count=1):
            self.click_calls.append((x, y, button, click_count))
            return self._answer(self.click_result)

        def move_pointer_to(x, y):
            self.move_calls.append((x, y))
            return self._answer(self.move_result)

        def drag_pointer(start_x, start_y, end_x, end_y, duration_ms=250):
            self.drag_calls.append(
                (start_x, start_y, end_x, end_y, duration_ms))
            return self._answer(self.drag_result)

        def cursor_pos():
            return self._answer(self.pointer)

        stack.enter_context(patch.object(sender, "click_point", click_point))
        stack.enter_context(
            patch.object(sender, "move_pointer_to", move_pointer_to))
        stack.enter_context(patch.object(sender, "drag_pointer", drag_pointer))
        stack.enter_context(patch.object(win32gui, "GetCursorPos", cursor_pos))

    @property
    def all_calls(self) -> list[tuple]:
        """Every primitive call, in no particular order across kinds."""
        return self.click_calls + self.move_calls + self.drag_calls
