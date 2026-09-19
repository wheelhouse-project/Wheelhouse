"""OS-level mocks for E2E speech pipeline tests.

Replaces Windows API calls with recording stubs:
- win_input_sender.press_keys -> records keystrokes
- clipboard operations -> in-memory clipboard
- uiautomation -> configurable mock controls
- capture_context -> returns configurable UIContext
"""
from dataclasses import dataclass, field
from typing import List, Tuple, Any
from unittest.mock import MagicMock


@dataclass
class Recording:
    """Records OS-level effects that would have happened."""
    keystrokes: List[Tuple[tuple, dict]] = field(default_factory=list)
    clipboard_pastes: List[str] = field(default_factory=list)
    clipboard_state: str = ""
    #: wh-review-pattern-fixes.46: the delivery record. One entry per text
    #: that production credited as delivered to the target window, in
    #: delivery order. ``clipboard_pastes`` above records every clipboard
    #: WRITE, which happens before the focus proof and before any Ctrl+V,
    #: so a run that delivered nothing still fills it. Read this list to
    #: assert that dictated text arrived; read ``clipboard_pastes`` only
    #: to diagnose what was put on the clipboard.
    #:
    #: ``AppAdapter`` appends here from
    #: ``ClipboardOperations.credit_paste_chars``, the one accounting hook
    #: production calls after a delivery succeeded (see the AppAdapter
    #: docstring for the two call sites).
    text_deliveries: List[str] = field(default_factory=list)
    #: wh-review-pattern-fixes.46: text that ``_raw_paste`` put back after
    #: a pre-send paste failure. A selection restore returns the user's
    #: own prior content, not new dictation, and production keeps it out
    #: of every retract-accounting field on purpose, so it is recorded
    #: apart from ``text_deliveries``.
    selection_restores: List[str] = field(default_factory=list)
    run_programs: List[str] = field(default_factory=list)
    typed_texts: List[str] = field(default_factory=list)
    unicode_sends: List[str] = field(default_factory=list)
    backspace_sends: List[int] = field(default_factory=list)
    # wh-review-pattern-fixes.42: the mouse and notification boundaries.
    # Each list holds the arguments of one call to the matching
    # utils.win_input_sender primitive, in call order.
    mouse_clicks: List[tuple] = field(default_factory=list)
    coordinate_clicks: List[tuple] = field(default_factory=list)
    pointer_moves: List[tuple] = field(default_factory=list)
    pointer_drags: List[tuple] = field(default_factory=list)
    hit_tests: List[tuple] = field(default_factory=list)
    notifications: List[dict] = field(default_factory=list)
    # wh-review-pattern-fixes.47: the voice-click boundary. Each list holds
    # one entry per call to a seam the ClickExecutor or the click handlers
    # would otherwise take to the host desktop, in call order.
    #: One entry per ``_capture_click_foreground`` call, holding the window
    #: handle the stand-in reported.
    click_foreground_captures: List[int] = field(default_factory=list)
    #: One entry per pre-click ``foreground_probe`` call, same value.
    click_foreground_probes: List[int] = field(default_factory=list)
    #: One (x, y) per on-screen check of a control's fresh centre point.
    click_on_screen_tests: List[tuple] = field(default_factory=list)
    #: One window handle per popup-visibility probe.
    click_popup_visible_tests: List[int] = field(default_factory=list)
    #: One window handle per popup-owner probe.
    click_popup_owner_tests: List[int] = field(default_factory=list)
    #: One window handle per shell-class re-read.
    click_shell_class_tests: List[int] = field(default_factory=list)
    #: One (x, y) per UIA point-hits-winner check. Separate from
    #: ``hit_tests``, which records the Win32 ``root_window_at_point``
    #: layer of the same obstruction gate.
    click_point_winner_tests: List[tuple] = field(default_factory=list)
    #: The accessible name of every control the no-op invoke provider was
    #: asked to press through the UIA Invoke pattern.
    element_invokes: List[str] = field(default_factory=list)
    #: The accessible name of every control the no-op MSAA provider was
    #: asked to press through DoDefaultAction.
    element_default_actions: List[str] = field(default_factory=list)
    #: One (name, role) per ``ElementFinder.find`` call the fake finder
    #: served.
    element_walks: List[tuple] = field(default_factory=list)
    #: One entry per ``create_automation`` call. AppAdapter injects the
    #: automation root, so a non-empty list means some path built its own
    #: root instead of using the injected one.
    automation_roots_created: List[str] = field(default_factory=list)

    #: The foreground identity every click stand-in reports. AppAdapter
    #: overwrites ``click_foreground_window`` with its own FOREGROUND_HWND
    #: so the click path names the one window the end-to-end tests target.
    #: The pre-click verification compares the walk-time capture against
    #: the click-time probe, so both stand-ins must read these fields.
    click_foreground_window: int = 1
    click_foreground_pid: int = 1234
    click_foreground_process: str = "notepad.exe"
    click_foreground_created_ms: int = 1
    #: The cursor position the walk-time capture reports.
    click_cursor: Tuple[int, int] = (0, 0)
    #: The class name the shell-class stand-in reports. The default is a
    #: taskbar class, so a shell-owned winner passes the shell probe. A
    #: test that needs a refused shell click sets another value.
    shell_class_name: str = "Shell_TrayWnd"

    #: The window handle ``root_window_at_point`` reports for every point.
    #: AppAdapter overwrites this with its own FOREGROUND_HWND so the
    #: hit-test answer names the one window the end-to-end tests target.
    #: A test that needs an obstructed point sets a different value.
    hit_test_hwnd: int = 1

    def press_keys(self, *keys):
        """Record a press_keys call."""
        self.keystrokes.append((keys, {}))

    def verified_press_keys(self, *keys, caller_notifies=False):
        """Record a verified chord and report a full delivery.

        ``caller_notifies`` mirrors the keyword
        utils.win_input_sender.verified_press_keys gained in 43c2b931
        (wh-keyboard-refusal-notice); the recorder accepts it so the
        callers that pass it still reach this stub. It changes nothing
        here because the stub never refuses.

        wh-review-pattern-fixes.36: ClipboardOperations sends every
        non-Flutter state-changing chord through
        ``win_input_sender.verified_press_keys``. Recording it in the
        same ``keystrokes`` list keeps every existing e2e assertion
        about the clipboard path describing the same keystrokes.

        Returns the (success, accepted, expected) triple that
        utils.win_input_sender.verified_press_keys produces.
        """
        self.keystrokes.append((keys, {}))
        return True, 1, 1

    def send_backspaces(self, count):
        """Record a batched backspace send and report full delivery.

        wh-review-pattern-fixes.40: ``UIActionHandler.retract`` binds
        ``utils.win_input_sender.send_backspaces`` directly, so an
        unpatched call sends real Backspace keys to whichever window has
        focus. The counts get their own list because no keystroke
        assertion in the existing e2e suite describes them.

        Returns True, the value the real function returns when SendInput
        accepted every event.
        """
        self.backspace_sends.append(count)
        return True

    def click_point(self, x, y, button="left", click_count=1):
        """Record a mouse-grid click and report that it landed.

        wh-review-pattern-fixes.42: ``UIActionHandler._mouse_click_seam``
        holds ``_win32_click_point``, which imports
        ``utils.win_input_sender.click_point`` inside the function body. An
        unpatched call moves and clicks the real mouse.

        Returns the (succeeded, reason) pair the real primitive returns
        after SendInput accepted the click.
        """
        self.mouse_clicks.append((x, y, button, click_count))
        return True, None

    def click_at(self, x, y, button="left", click_count=1):
        """Record a coordinate click and report that it landed.

        The ClickExecutor seam behind ``_win32_coordinate_click`` and
        ``_win32_gesture_click``. Both wrappers call the same primitive.

        Returns the (succeeded, events_sent, reason) triple the real
        primitive returns. ``events_sent`` counts the button-down and
        button-up pair of every click, which is what the executor's
        short-send check compares against.
        """
        self.coordinate_clicks.append((x, y, button, click_count))
        return True, 2 * int(click_count), None

    def move_pointer_to(self, x, y):
        """Record a pointer park and report that the cursor landed.

        Returns the (succeeded, reason) pair of the real primitive.
        """
        self.pointer_moves.append((x, y))
        return True, None

    def drag_pointer(self, start_x, start_y, end_x, end_y, duration_ms=250):
        """Record a drag and report that the whole gesture completed.

        Returns the (succeeded, reason) pair of the real primitive. The
        recorder never reports ``release_failed``, so no recorded drag can
        claim that a mouse button stayed held.
        """
        self.pointer_drags.append((start_x, start_y, end_x, end_y, duration_ms))
        return True, None

    def root_window_at_point(self, x, y):
        """Record a click-point hit test and answer with one fixed window.

        This reads the host desktop rather than sending input, but the
        ClickExecutor refuses a coordinate click whose point resolves to a
        window other than the target. Reading the developer's real desktop
        would make that refusal depend on which window sat under the point,
        so the recorder answers ``hit_test_hwnd`` for every point.
        """
        self.hit_tests.append((x, y))
        return self.hit_test_hwnd

    def capture_click_foreground(self):
        """Answer the walk-time foreground capture from the recording.

        wh-review-pattern-fixes.47: ``_capture_click_foreground`` reads the
        developer's real foreground window, its PID, its process name, its
        creation time and the real cursor position. The walk and the
        pre-click verification both compare against that reading, so a
        real read makes every click test depend on which window the
        developer had in front.

        Returns the ``ForegroundContext`` the real function returns.
        """
        from ui.element_finder import ForegroundContext

        self.click_foreground_captures.append(self.click_foreground_window)
        return ForegroundContext(
            foreground_window=self.click_foreground_window,
            foreground_pid=self.click_foreground_pid,
            foreground_process_name=self.click_foreground_process,
            foreground_window_creation_time=self.click_foreground_created_ms,
            cursor_at_walk=self.click_cursor,
            cursor_monitor_id=0,
        )

    def click_foreground_probe(self):
        """Answer the pre-click foreground probe from the recording.

        The click-time half of the pair above. It reports the same
        identity, so the executor's five-step verification block sees an
        unchanged foreground and proceeds to the press.

        Returns the ``ForegroundProbe`` the real seam returns.
        """
        from ui.click_executor import ForegroundProbe

        self.click_foreground_probes.append(self.click_foreground_window)
        return ForegroundProbe(
            window=self.click_foreground_window,
            pid=self.click_foreground_pid,
            process_name=self.click_foreground_process,
            window_creation_time=self.click_foreground_created_ms,
        )

    def click_on_screen(self, x, y):
        """Record an on-screen check and report that the point is visible.

        The real seam calls ``MonitorFromPoint`` on the developer's
        monitor layout, so a control the test placed at a fixed
        coordinate would be refused ``target_moved_offscreen`` on a
        machine whose monitors do not cover that point.
        """
        self.click_on_screen_tests.append((x, y))
        return True

    def popup_is_visible(self, hwnd):
        """Record a popup-visibility probe and report the window is up.

        The real seam is ``IsWindowVisible`` on the walked popup handle.
        """
        self.click_popup_visible_tests.append(hwnd)
        return True

    def popup_owner_of(self, hwnd):
        """Record a popup-owner probe and name the click-target window.

        The real seam is ``GetWindow(hwnd, GW_OWNER)``. The executor
        requires the owner to equal the focused window, so the recorder
        answers with the same handle the foreground stand-ins report.
        """
        self.click_popup_owner_tests.append(hwnd)
        return self.click_foreground_window

    def shell_class_of(self, hwnd):
        """Record a shell-class re-read and answer from the recording.

        The real seam is ``GetClassName``. The executor requires a
        taskbar class before it clicks a shell-owned winner.
        """
        self.click_shell_class_tests.append(hwnd)
        return self.shell_class_name

    def uia_point_hits_winner(self, automation, winner, x, y):
        """Record the UIA point-hits-winner check and report a hit.

        The real seam walks a live UIA tree from ``ElementFromPoint`` and
        compares ancestors against the winner's COM element.
        """
        self.click_point_winner_tests.append((int(x), int(y)))
        return True

    def create_automation(self):
        """Record a root build and hand back a fake IUIAutomation root.

        wh-review-pattern-fixes.47: the real ``create_automation``
        CoCreateInstances a live IUIAutomation object. AppAdapter injects
        the root on the handler, so nothing in the end-to-end path should
        reach this. The recorder exists so a path that DOES reach it is
        visible in ``automation_roots_created`` instead of building COM
        state.
        """
        self.automation_roots_created.append("create_automation")
        return FakeAutomationRoot()

    def invoke_element(self, element):
        """Record a UIA Invoke instead of pressing a real control.

        wh-review-pattern-fixes.47: ``ClickExecutor`` binds
        ``ui.uia_walker.invoke_via_invoke_pattern`` as a DEFAULT ARGUMENT
        of its constructor, so the binding is made when the class body is
        executed and no module-level patch can replace it. The adapter
        therefore replaces the built executor's provider attribute.

        The real provider returns None.
        """
        self.element_invokes.append(_element_name(element))

    def do_default_action_on_element(self, element):
        """Record an MSAA DoDefaultAction instead of pressing a control.

        The fallback provider ``ClickExecutor`` reaches when the Invoke
        pattern is structurally absent. Same default-argument binding
        situation as ``invoke_element``. The real provider returns None.
        """
        self.element_default_actions.append(_element_name(element))

    def notify(self, **kwargs):
        """Record a desktop notification instead of raising one.

        wh-review-pattern-fixes.42: ``UIActionHandler.show_notification``
        imports ``plyer.notification`` inside the function body, so an
        unpatched call puts a real toast on the developer's screen.
        ``plyer``'s notify returns None.
        """
        self.notifications.append(dict(kwargs))

    def type_string(self, text):
        """Record a type_string call."""
        self.typed_texts.append(text)

    def type_text_verified(self, text, chunk_delay=0.001, *, caller_notifies=False):
        """Record the type_text action's verified SendInput delivery.

        ui_action_handler.type_text switched from type_string to
        type_string_verified in 43c2b931 (wh-keyboard-refusal-notice).
        That call is still raw keystrokes, so it lands in typed_texts
        and not in clipboard_pastes. Returns the (success, chars_sent,
        error) triple utils.win_input_sender.type_string_verified
        produces.
        """
        self.typed_texts.append(text)
        return True, len(text), None

    def type_string_verified(self, text, chunk_delay=0.001, *, caller_notifies=False):
        """Record a Unicode SendInput delivery (wh-wxkp).

        ``chunk_delay`` and ``caller_notifies`` mirror the signature of
        utils.win_input_sender.type_string_verified (the keyword was
        added in 43c2b931, wh-keyboard-refusal-notice) so callers that
        pass them still reach this stub. Neither changes the recording.

        Mirrors the delivered text into clipboard_pastes so the large body
        of pre-Unicode e2e assertions ("N pastes with these exact strings")
        keeps describing the observable inserted text regardless of which
        transport delivered it. unicode_sends is the precise per-transport
        view for tests that care HOW the text was delivered.

        Returns the (success, chars_sent, error) triple
        utils.win_input_sender.type_string_verified produces.
        """
        self.unicode_sends.append(text)
        self.clipboard_pastes.append(text)
        return True, len(text), None

    def get_keystroke_keys(self) -> List[tuple]:
        """Get just the key tuples from recorded keystrokes."""
        return [ks[0] for ks in self.keystrokes]

    def clear(self):
        self.keystrokes.clear()
        self.clipboard_pastes.clear()
        self.text_deliveries.clear()
        self.selection_restores.clear()
        self.clipboard_state = ""
        self.run_programs.clear()
        self.typed_texts.clear()
        self.unicode_sends.clear()
        self.backspace_sends.clear()
        self.mouse_clicks.clear()
        self.coordinate_clicks.clear()
        self.pointer_moves.clear()
        self.pointer_drags.clear()
        self.hit_tests.clear()
        self.notifications.clear()
        self.click_foreground_captures.clear()
        self.click_foreground_probes.clear()
        self.click_on_screen_tests.clear()
        self.click_popup_visible_tests.clear()
        self.click_popup_owner_tests.clear()
        self.click_shell_class_tests.clear()
        self.click_point_winner_tests.clear()
        self.element_invokes.clear()
        self.element_default_actions.clear()
        self.element_walks.clear()
        self.automation_roots_created.clear()


# ============================================================================
# wh-review-pattern-fixes.47: the fake voice-click element tree
# ============================================================================

#: The window handle the fake popup-owned control was walked from. Any
#: non-zero value makes ``ClickExecutor`` run the popup-liveness probe, so
#: a click on that control exercises the popup seams.
FAKE_POPUP_HWND = 7701

#: The snapshot id the fake finder serves. ``click_snapshot_item`` looks a
#: badge up by (snapshot_id, item_id), so a test needs both ids.
FAKE_SNAPSHOT_ID = "fake-snapshot-1"

#: The two badges the fake snapshot holds. The first is a control in the
#: focused window (no popup probe); the second was walked from an owned
#: popup, so clicking it runs the popup-visibility and popup-owner seams.
FAKE_PRIMARY_ITEM_ID = "fake-item-primary"
FAKE_POPUP_ITEM_ID = "fake-item-popup"


def _element_name(element) -> str:
    """Best-effort accessible name of a control reference, for recording."""
    return str(getattr(element, "name", element))


class FakeControlRef:
    """Stand-in for the live COM element behind an ``ElementMatch``.

    wh-review-pattern-fixes.47: ``ClickExecutor._verify`` re-reads
    ``CurrentIsEnabled`` and ``CurrentBoundingRectangle`` on the winner's
    ``control_ref`` before it presses anything, and the press providers
    query UIA patterns on the same object. A real walk hands over a live
    COM proxy for a control on the developer's screen. This class answers
    both reads from plain Python values.

    ``rect`` is in UIA's native left/top/right/bottom order, which is what
    the executor's rect parser expects.
    """

    def __init__(self, name: str, rect=(100, 200, 140, 240)):
        self.name = name
        self.CurrentIsEnabled = True
        self.CurrentBoundingRectangle = rect

    def __repr__(self):  # pragma: no cover -- diagnostic only
        return f"FakeControlRef({self.name!r})"


class FakeAutomationRoot:
    """Stand-in for the IUIAutomation root the click feature walks.

    wh-review-pattern-fixes.47: ``UIActionHandler._get_click_element_finder``
    calls ``ui.uia_walker.create_automation()``, which CoCreateInstances a
    real IUIAutomation object. AppAdapter stores one of these on
    ``handler._click_automation_root`` instead, so the create call never
    runs and ``_point_hits_winner_via_automation_root`` finds a usable
    root.

    It carries no behaviour: the one consumer that reads it in the
    end-to-end path is the point-hits-winner seam, which the adapter
    replaces with a recorder.
    """


def make_fake_click_snapshot(recording: "Recording"):
    """Build the two-badge WalkSnapshot the fake finder serves.

    The snapshot's foreground identity is copied from ``recording`` so it
    matches what the foreground stand-ins report; ``get_snapshot`` in
    production drops a snapshot whose foreground identity no longer
    matches, and the executor refuses a click whose foreground moved.
    """
    from ui.element_types import ElementMatch, WalkSnapshot

    def _match(item_id, number, name, source_window_hwnd):
        return ElementMatch(
            item_id=item_id,
            display_number=number,
            name=name,
            role="Button",
            # (x, y, w, h) -- the same rectangle FakeControlRef reports as
            # left/top/right/bottom, so the bounds-tolerance check sees no
            # drift between the walk and the click.
            bounds=(100, 200, 40, 40),
            monitor_id=0,
            score=1.0,
            is_eligible=True,
            source="primary",
            invoke_supported=True,
            is_enabled=True,
            control_ref=FakeControlRef(name),
            control_type_id=50000,
            source_window_hwnd=source_window_hwnd,
            source_window_is_shell=False,
        )

    return WalkSnapshot(
        snapshot_id=FAKE_SNAPSHOT_ID,
        matches=[
            _match(FAKE_PRIMARY_ITEM_ID, 1, "Save", 0),
            _match(FAKE_POPUP_ITEM_ID, 2, "Open Recent", FAKE_POPUP_HWND),
        ],
        created_at_monotonic=0.0,
        foreground_window=recording.click_foreground_window,
        foreground_pid=recording.click_foreground_pid,
        foreground_process_name=recording.click_foreground_process,
        foreground_window_creation_time=recording.click_foreground_created_ms,
        cursor_at_walk=recording.click_cursor,
        cursor_monitor_id=0,
    )


class FakeFindResult:
    """What :class:`FakeClickFinder` returns from ``find``.

    The real ``ElementFinder.FindResult`` also carries a ``_walk_result``
    field whose only job is to pin the COM keepalive chain. There is no
    COM object to pin here, so this stand-in carries the three fields the
    ``click_element`` handler reads and nothing else.
    """

    def __init__(self, outcome, snapshot, summary):
        self.outcome = outcome
        self.snapshot = snapshot
        self.summary = summary


class FakeClickFinder:
    """Deterministic stand-in for ``ElementFinder`` (wh-review-pattern-fixes.47).

    A real ElementFinder builds an IUIAutomation tree, walks the
    developer's focused window, reads live UIA properties, and hands back
    control references into that window. This one serves a fixed
    two-badge snapshot and records the queries it was asked for, so a full
    ``click_element`` or ``click_snapshot_item`` dispatch runs the real
    handler, the real ClickExecutor and the real verification block
    against data the test owns.

    ``find`` matches on the accessible name, case-insensitively, and
    reports ``not_found`` for anything else.
    """

    def __init__(self, recording: "Recording"):
        self.recording = recording
        self.snapshot = make_fake_click_snapshot(recording)
        self.summary = self._build_summary(self.snapshot)

    @staticmethod
    def _build_summary(snapshot):
        from ui.element_types import (
            WalkSnapshotSummary, WalkSnapshotSummaryItem,
        )

        return WalkSnapshotSummary(
            snapshot_id=snapshot.snapshot_id,
            items=[
                WalkSnapshotSummaryItem(
                    item_id=m.item_id,
                    display_number=m.display_number,
                    name=m.name,
                    role=m.role,
                    bounds=m.bounds,
                    monitor_id=m.monitor_id,
                )
                for m in snapshot.matches
            ],
            created_at_monotonic=snapshot.created_at_monotonic,
        )

    def find(self, query, foreground, deadline=None):
        """Serve the badge whose name matches, or report not_found."""
        from ui.clear_winner_rule import Outcome

        self.recording.element_walks.append((query.name, query.role))
        wanted = (query.name or "").strip().lower()
        winner = next(
            (m for m in self.snapshot.matches if m.name.lower() == wanted),
            None,
        )
        if winner is None:
            outcome = Outcome(
                outcome="not_found", reason=None, winner=None, candidates=(),
            )
        else:
            outcome = Outcome(
                outcome="ok", reason=None, winner=winner, candidates=(),
            )
        return FakeFindResult(outcome, self.snapshot, self.summary)

    def get_snapshot(self, snapshot_id, **kwargs):
        """Serve the fixed snapshot by id; anything else is a miss."""
        if snapshot_id == self.snapshot.snapshot_id:
            return self.snapshot
        return None

    def describe_snapshot_miss(self, snapshot_id, **kwargs):
        """Name the cause of a snapshot miss. There is never one here."""
        return None


def make_mock_context(process_name="notepad.exe", is_flutter=False, is_terminal=False):
    """Create a mock UIContext for testing.

    Returns a UIContext-compatible object without importing uiautomation.
    """
    from services.wheelhouse.ui.context import UIContext
    mock_control = MagicMock()
    mock_control.IsKeyboardFocusable = True
    mock_control.ClassName = "Edit"
    mock_control.Exists.return_value = True
    mock_control.ProcessId = 1234
    return UIContext(
        focused_control=mock_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name="Edit",
    )
