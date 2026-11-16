"""Input-process side harness for the overlay stack requirement tests (U4).

Place 2, stage A of the refactor (bead ``wh-full-refactor.4.1``). It lets a test
drive ``UIActionHandler`` the way the Input process does and read only what
leaves the handler.

ENTRY. ``input_proc`` finds the handler method with ``getattr(ui_handler,
action)`` and calls it as ``method(request_id=request_id, **params)`` for the
actions that build their own reply. ``InputSide.send`` does the same call. For
``start_overlay_walk`` the command loop also adds ``command_dequeue_monotonic``
to the params, and ``send`` does that too.

WHAT IS REAL. The handler, ``ElementFinder`` (the snapshot store, the pins, the
walk), the settle detector, ``ClickExecutor`` and its verification block, and
the UI Automation walker code are the production objects.

WHAT IS FAKE. Only operating system seams:
  * the UI Automation root (``ui.uia_walker.create_automation``) and the element
    tree behind it, from :class:`FakeUiaElement`;
  * the foreground window, its process and the cursor (``win32gui``,
    ``win32process`` and ``psutil.Process``);
  * the UI Automation hit test members (``ElementFromPoint``,
    ``CompareElements``, ``ControlViewWalker``) on that same root, so the real
    ``uia_walker.point_hits_winner`` runs;
  * the monitor query (``win32api.MonitorFromPoint``) and the top level window
    enumeration (``win32gui.EnumWindows``).
A press goes through the fake element, and the harness records it in
``InputSide.invoked_names``.

No fake here names a private member of a production class.
"""

from __future__ import annotations

import queue as queue_mod
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from unittest.mock import patch

from comtypes import COMError

from tests.test_uia_walker import (
    UIA_INVOKE_PATTERN_ID,
    UIA_LEGACY_PATTERN_ID,
    FakeCachedElement,
    FakeElementArray,
    FakeRect,
)
from ui import uia_walker

UIA_BUTTON = uia_walker.UIA_BUTTON


@dataclass
class Window:
    """One top level window with the identity the Input process reads."""

    hwnd: int
    pid: int
    process_name: str
    creation_time_ms: int


NOTEPAD = Window(hwnd=1000, pid=4321, process_name="notepad.exe",
                 creation_time_ms=99)
OTHER = Window(hwnd=2000, pid=8765, process_name="calc.exe",
               creation_time_ms=77)


class _FakeInvokePattern:
    def __init__(self, element: "FakeUiaElement") -> None:
        self._element = element

    def Invoke(self) -> None:
        self._element.invoked_by.append(self._element.CachedName)
        if self._element.invoke_raises is not None:
            raise self._element.invoke_raises


class _FakeRawPattern:
    """The raw pattern pointer: only ``QueryInterface`` reaches Invoke."""

    def __init__(self, element: "FakeUiaElement") -> None:
        self._element = element

    def QueryInterface(self, _iface: Any) -> _FakeInvokePattern:
        return _FakeInvokePattern(self._element)


class _FakeLegacyPattern:
    """The MSAA LegacyIAccessible pattern: a default action and its press."""

    def __init__(self, element: "FakeUiaElement") -> None:
        self._element = element

    @property
    def CurrentDefaultAction(self) -> str:
        return self._element.default_action or ""

    def DoDefaultAction(self) -> None:
        self._element.default_actions_done.append(self._element.CachedName)


class _FakeRawLegacy:
    def __init__(self, element: "FakeUiaElement") -> None:
        self._element = element

    def QueryInterface(self, _iface: Any) -> _FakeLegacyPattern:
        return _FakeLegacyPattern(self._element)


class FakeUiaElement(FakeCachedElement):
    """A cached UI Automation element that a press can also reach.

    ``FakeCachedElement`` models the cached surface the walk reads. The click
    path also reads ``CurrentIsEnabled`` and ``CurrentBoundingRectangle`` and
    presses through the Invoke pattern, so this adds them.
    """

    def __init__(
        self,
        name: str,
        *,
        rect: Optional[FakeRect] = None,
        control_type: int = UIA_BUTTON,
        role: str = "button",
        current_rect: Optional[tuple[int, int, int, int]] = None,
        current_enabled: bool = True,
        invoke_raises: Optional[BaseException] = None,
        has_invoke: bool = True,
        default_action: Optional[str] = None,
    ) -> None:
        rect = rect or FakeRect(10, 20, 110, 70)
        super().__init__(
            name=name,
            control_type=control_type,
            localized_control_type=role,
            rect=rect,
            invoke_supported=has_invoke,
        )
        #: False: the control exposes no UI Automation Invoke pattern.
        self.has_invoke = has_invoke
        #: The MSAA default action text, or None for no Legacy pattern.
        self.default_action = default_action
        self.default_actions_done: list[str] = []
        self.CurrentIsEnabled = current_enabled
        self.CurrentBoundingRectangle = current_rect or (
            rect.left, rect.top, rect.right, rect.bottom,
        )
        self.invoke_raises = invoke_raises
        self.invoked_by: list[str] = []

    def GetCachedPattern(self, pattern_id):
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return _FakeRawPattern(self) if self.has_invoke else None
        return super().GetCachedPattern(pattern_id)

    def GetCurrentPattern(self, pattern_id) -> Any:
        """The live pattern read. The press helpers use it for Legacy."""
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return _FakeRawPattern(self) if self.has_invoke else None
        if pattern_id == UIA_LEGACY_PATTERN_ID:
            return (_FakeRawLegacy(self)
                    if self.default_action is not None else None)
        return None


def button_row(*names: str, top: int = 20) -> list[FakeUiaElement]:
    """Side-by-side buttons with distinct rectangles (same-rect ones collapse)."""
    return [
        FakeUiaElement(
            name, rect=FakeRect(10 + i * 120, top, 110 + i * 120, top + 50),
        )
        for i, name in enumerate(names)
    ]


class _TopLevel:
    def __init__(self, elements: Iterable[FakeUiaElement]) -> None:
        self._array = FakeElementArray(list(elements))

    def FindAllBuildCache(self, _scope, _cond, _cache):
        return self._array


class _CacheRequest:
    TreeScope = 0

    def AddProperty(self, _):
        pass

    def AddPattern(self, _):
        pass


class _FakeWalker:
    def GetParentElement(self, _element):
        return None


class FakeAutomationRoot:
    """The IUIAutomation root. ``trees`` maps a window handle to its elements."""

    def __init__(self, trees: dict[int, list[FakeUiaElement]]) -> None:
        self.trees = trees
        self.walks_started = 0
        self.after_walk: Optional[Callable[[int], None]] = None
        #: When set, every window read raises this error.
        self.read_error: Optional[BaseException] = None

    def CreateCacheRequest(self):
        return _CacheRequest()

    def CreateTrueCondition(self):
        return object()

    # The click-point hit test (``uia_walker.point_hits_winner``) runs for real
    # on these three members: the element under the point, the identity
    # comparison and the parent walk. Every fake element is a direct child of
    # the window, so no element has a parent.
    def ElementFromPoint(self, point):
        for elements in self.trees.values():
            for el in elements:
                left, top, right, bottom = el.CurrentBoundingRectangle
                if left <= point.x < right and top <= point.y < bottom:
                    return el
        return None

    def CompareElements(self, first, second):
        return first is second

    @property
    def ControlViewWalker(self):
        return _FakeWalker()

    def ElementFromHandle(self, hwnd):
        if self.read_error is not None:
            raise self.read_error
        if hwnd not in self.trees:
            # What the OS answers for a window that is gone.
            raise COMError(-2147220991, "The element is not available",
                           (None, None, None, 0, None))
        self.walks_started += 1
        top = _TopLevel(self.trees[hwnd])
        if self.after_walk is not None:
            self.after_walk(self.walks_started)
        return top

    # The settle listener registers handlers. A refusal makes it report that no
    # listener is available, which the production code handles.
    def AddStructureChangedEventHandler(self, *_args):
        raise RuntimeError("no event support in the fake root")

    def AddAutomationEventHandler(self, *_args):
        raise RuntimeError("no event support in the fake root")

    def RemoveAllEventHandlers(self):
        pass


class InputSide:
    """A real ``UIActionHandler`` on fake operating system seams."""

    def __init__(
        self,
        *,
        click_config: Optional[dict] = None,
        trees: Optional[dict[int, list[FakeUiaElement]]] = None,
        windows: Iterable[Window] = (NOTEPAD, OTHER),
        automation_fails: bool = False,
        reply_queue: Any = None,
    ) -> None:
        self.config: dict = {"ui_actions": {}}
        if click_config is not None:
            self.config["click"] = click_config
        self.windows = {w.hwnd: w for w in windows}
        self.foreground: int = NOTEPAD.hwnd
        self.trees: dict[int, list[FakeUiaElement]] = (
            trees if trees is not None else {NOTEPAD.hwnd: []}
        )
        self.root = FakeAutomationRoot(self.trees)
        self.automation_fails = automation_fails
        self.replies: "queue_mod.Queue[Any]" = queue_mod.Queue()
        self._reply_queue: Any = (reply_queue if reply_queue is not None else self.replies)
        self._stack: Optional[ExitStack] = None
        self.handler: Any = None
        self._counter = 0
        self.automation_builds = 0
        self.mouse_clicks: list[tuple] = []
        #: What the cursor position read (``win32gui.GetCursorPos``) answers.
        self.cursor_pos: Any = (60, 45)

    # -- seams ---------------------------------------------------------------

    def _install(self, stack: ExitStack) -> None:
        import psutil
        import win32api
        import win32gui
        import win32process

        real_process = psutil.Process

        class _Proc:
            def __init__(self, window: Window) -> None:
                self._window = window

            def name(self) -> str:
                return self._window.process_name

            def create_time(self) -> float:
                return self._window.creation_time_ms / 1000.0

        def process(pid=None):
            for w in self.windows.values():
                if w.pid == pid:
                    return _Proc(w)
            return real_process(pid) if pid is not None else real_process()

        def thread_and_pid(hwnd):
            w = self.windows.get(hwnd)
            if w is None:
                return (0, 0)
            return (1, w.pid)

        def create_automation():
            self.automation_builds += 1
            if self.automation_fails:
                raise OSError("UIAutomationCore is not available")
            return self.root

        stack.enter_context(patch.object(
            win32gui, "GetForegroundWindow", lambda: self.foreground))
        stack.enter_context(patch.object(
            win32process, "GetWindowThreadProcessId", thread_and_pid))
        stack.enter_context(patch.object(psutil, "Process", process))
        stack.enter_context(patch.object(
            win32gui, "GetCursorPos", lambda: self.cursor_pos))
        stack.enter_context(patch.object(
            win32gui, "EnumWindows", lambda _cb, _extra: None))
        stack.enter_context(patch.object(
            win32api, "MonitorFromPoint", lambda *_a, **_k: 1))
        stack.enter_context(patch.object(
            uia_walker, "create_automation", create_automation))

        # The mouse. A coordinate click never reaches SendInput: it is recorded
        # as (x, y, button, click_count).
        import utils.win_input_sender as sender

        def click_at(x, y, *, button="left", click_count=1):
            self.mouse_clicks.append((x, y, button, click_count))
            return (True, 2 * click_count, None)

        stack.enter_context(patch.object(sender, "click_at", click_at))
        stack.enter_context(patch.object(
            sender, "root_window_at_point", lambda _x, _y: self.foreground))

    def __enter__(self) -> "InputSide":
        from ui.ui_action_handler import UIActionHandler

        self._stack = ExitStack()
        self._install(self._stack)
        self.handler = UIActionHandler(
            response_queue=self._reply_queue, config=self.config,
        )
        return self

    def __exit__(self, *exc) -> None:
        assert self._stack is not None
        self._stack.close()

    # -- entry ---------------------------------------------------------------

    def send(self, action: str, **params: Any) -> list[dict]:
        """Send one command as ``input_proc`` does and return the replies."""
        self._counter += 1
        request_id = f"req-{self._counter}"
        method = getattr(self.handler, action)
        if action == "start_overlay_walk":
            params.setdefault("command_dequeue_monotonic", time.monotonic())
        method(request_id=request_id, **params)
        out: list[dict] = []
        while True:
            try:
                out.append(self.replies.get_nowait())
            except queue_mod.Empty:
                break
        for payload in out:
            assert payload.get("request_id") == request_id
            assert payload.get("action") == action
        return out

    def send_one(self, action: str, **params: Any) -> dict:
        """Send one command and require exactly one reply."""
        out = self.send(action, **params)
        assert len(out) == 1, out
        return out[0]

    # -- helpers for tests ---------------------------------------------------

    def set_window(self, window: Window, elements: list[FakeUiaElement]) -> None:
        self.windows[window.hwnd] = window
        self.trees[window.hwnd] = elements

    def walk(self, **params: Any) -> dict:
        """Run a walk and return its reply."""
        params.setdefault("overlay_session_id", 1)
        params.setdefault("paint_generation", 1)
        params.setdefault("trace_id", "t-walk")
        return self.send_one("start_overlay_walk", **params)

    @property
    def invoked_names(self) -> list[str]:
        names: list[str] = []
        for elements in self.trees.values():
            for el in elements:
                names.extend(el.invoked_by)
        return names


def item_id_for(reply: dict, name: str) -> str:
    """The item id of the named badge in a walk or paint reply."""
    for item in reply["snapshot_summary"]["items"]:
        if item["name"] == name:
            return item["item_id"]
    raise AssertionError(f"no badge named {name!r} in {reply!r}")
