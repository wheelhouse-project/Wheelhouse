"""Tests for the focus-redirect policy component (wh-xiazj).

Coverage targets from the bead spec, the round-1 review amendments
(wh-7gt07.2.3 and wh-7gt07.2.4), and the wh-xiazj.1 codex review
findings (wh-xiazj.1.1 through wh-xiazj.1.5):

  * Terminal-at-prompt focus -> open editor; reason terminal_at_prompt.
  * Terminal-busy focus -> no redirect; reason terminal_busy.
  * Non-terminal focus -> no redirect; reason not_a_terminal; detector
    is not called.
  * Editor already open (any of OPEN_REQUESTED, OPEN_APPLIED,
    FOCUS_PENDING, FOCUS_CONFIRMED, SUBMITTING) -> no redirect; reason
    editor_already_open; detector is not called.
  * Editor lifecycle ERROR -> no redirect; reason
    editor_lifecycle_error; detector is not called (wh-xiazj.1.1).
  * Zero focused HWND -> no redirect; reason
    cannot_resolve_focused_process; detector is not called.
  * process_name_for_hwnd returns None -> no redirect; reason
    cannot_resolve_focused_process; detector is not called
    (wh-xiazj.1.5).
  * GetWindowThreadProcessId raises -> no redirect; reason
    cannot_resolve_focused_process; detector is not called
    (wh-xiazj.1.5).
  * GetWindowThreadProcessId returns zero PID -> no redirect; reason
    cannot_resolve_focused_process; detector is not called
    (wh-xiazj.1.5).
  * Slow detector (longer than the deadline) -> fail closed; reason
    prompt_detector_timeout.
  * Detector raises -> fail closed; reason prompt_detector_error.
  * Cache hit within an utterance -> detector invoked once across two
    calls with the same (focused_hwnd, pid).
  * on_utterance_end() invalidates the cache -> detector invoked twice
    when the same (focused_hwnd, pid) is queried before and after.
  * Cache entry older than _CACHE_MAX_AGE_S expires even without an
    on_utterance_end call (wh-xiazj.1.4).
  * Mirror flips to a busy state during the detector await -> fail
    closed with editor_already_open (wh-xiazj.1.2).
  * Mirror flips to ERROR during the detector await -> fail closed
    with editor_lifecycle_error (wh-xiazj.1.2).
  * Concurrent same-key calls -> first runs the detector, the second
    returns prompt_detector_in_flight (wh-xiazj.1.3).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import Mock

import pytest

from services.wheelhouse.shared.editor_lifecycle import (
    EditorState,
    LogicMirror,
)
from services.wheelhouse.speech.focus_redirect_policy import (
    _CACHE_MAX_AGE_S,
    FocusRedirectPolicy,
    RedirectDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FOCUSED_HWND = 0x1234
_FOCUSED_PID = 4242
_TERMINAL_PROCESS = "WindowsTerminal.exe"
_NON_TERMINAL_PROCESS = "notepad.exe"


def _patch_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_name: str = _TERMINAL_PROCESS,
    pid: int = _FOCUSED_PID,
) -> None:
    """Stub the HWND-to-process resolution boundary inside the policy module.

    The policy resolves the focused HWND via
    ``process_name_for_hwnd`` (imported at module level) and
    ``win32process.GetWindowThreadProcessId``. Both are patched on the
    policy module so the test does not depend on real Win32 state.
    """
    from services.wheelhouse.speech import focus_redirect_policy as mod

    monkeypatch.setattr(
        mod,
        "process_name_for_hwnd",
        lambda _hwnd: process_name,  # pyright: ignore[reportUnusedLambda]
    )

    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        return_value=(0, pid)
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)


def _make_policy(
    *,
    detector_call,
    detector_timeout_s: float = 0.1,
    mirror: LogicMirror | None = None,
) -> FocusRedirectPolicy:
    return FocusRedirectPolicy(
        mirror=mirror or LogicMirror(),
        prompt_detector_call=detector_call,
        detector_timeout_s=detector_timeout_s,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_when_terminal_at_prompt(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision == RedirectDecision(
        open_editor=True,
        target_terminal_hwnd=_FOCUSED_HWND,
        reason="terminal_at_prompt",
    )
    assert detector.call_count == 1
    detector.assert_called_with(_TERMINAL_PROCESS, _FOCUSED_PID)


@pytest.mark.asyncio
async def test_no_redirect_when_terminal_busy(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=False)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "terminal_busy"
    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_no_redirect_when_not_terminal(monkeypatch):
    _patch_resolution(monkeypatch, process_name=_NON_TERMINAL_PROCESS)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "not_a_terminal"
    assert detector.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "editor_state",
    [
        EditorState.OPEN_REQUESTED,
        EditorState.OPEN_APPLIED,
        EditorState.FOCUS_PENDING,
        EditorState.FOCUS_CONFIRMED,
        EditorState.SUBMITTING,
    ],
)
async def test_no_redirect_when_editor_open_states(
    monkeypatch, editor_state
):
    # _patch_resolution is still applied so that a non-short-circuit
    # path would otherwise resolve; if the short-circuit broke, the
    # detector would be called -- the test verifies it is not.
    _patch_resolution(monkeypatch)

    mirror = LogicMirror()
    mirror.state = editor_state

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector, mirror=mirror)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "editor_already_open"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_no_redirect_when_editor_error(monkeypatch):
    # wh-xiazj.1.1: ERROR is a recovery state and must block redirect
    # with a distinct reason so the failure surfaces in structured
    # telemetry instead of looking like an ordinary "editor already
    # opening" decline.
    _patch_resolution(monkeypatch)

    mirror = LogicMirror()
    mirror.state = EditorState.ERROR

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector, mirror=mirror)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "editor_lifecycle_error"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_no_redirect_when_zero_hwnd():
    # No resolution patches: the zero HWND must short-circuit before
    # any process resolution would have been attempted.
    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(0)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "cannot_resolve_focused_process"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_no_redirect_when_process_name_resolution_fails(monkeypatch):
    # wh-xiazj.1.5: process_name_for_hwnd returns None (the helper's
    # documented "cannot determine" return value).
    from services.wheelhouse.speech import focus_redirect_policy as mod

    monkeypatch.setattr(
        mod,
        "process_name_for_hwnd",
        lambda _hwnd: None,  # pyright: ignore[reportUnusedLambda]
    )
    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        return_value=(0, _FOCUSED_PID)
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "cannot_resolve_focused_process"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_no_redirect_when_get_window_thread_process_id_raises(
    monkeypatch,
):
    # wh-xiazj.1.5: GetWindowThreadProcessId raises for stale or fake
    # HWNDs; fail closed.
    from services.wheelhouse.speech import focus_redirect_policy as mod

    monkeypatch.setattr(
        mod,
        "process_name_for_hwnd",
        lambda _hwnd: _TERMINAL_PROCESS,  # pyright: ignore[reportUnusedLambda]
    )
    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        side_effect=OSError("bad hwnd")
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "cannot_resolve_focused_process"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_no_redirect_when_get_window_thread_process_id_returns_zero_pid(
    monkeypatch,
):
    # wh-xiazj.1.5: GetWindowThreadProcessId can return (thread_id, 0)
    # for a window with no owning process -- fail closed.
    _patch_resolution(monkeypatch, pid=0)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "cannot_resolve_focused_process"
    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_detector_timeout_fails_closed(monkeypatch):
    _patch_resolution(monkeypatch)

    # A blocking sleep that exceeds the deadline. The detector runs in
    # the executor, so this thread sleep does not block the event
    # loop; asyncio.wait_for raises TimeoutError.
    def slow_detector(*_args) -> bool:
        time.sleep(0.5)
        return True

    policy = _make_policy(
        detector_call=slow_detector,
        detector_timeout_s=0.05,
    )

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "prompt_detector_timeout"


@pytest.mark.asyncio
async def test_detector_exception_fails_closed(monkeypatch):
    _patch_resolution(monkeypatch)

    def raising_detector(*_args) -> bool:
        raise RuntimeError("synthetic detector failure")

    policy = _make_policy(detector_call=raising_detector)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "prompt_detector_error"


@pytest.mark.asyncio
async def test_cache_hit_skips_executor(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    first = await policy.should_redirect(_FOCUSED_HWND)
    second = await policy.should_redirect(_FOCUSED_HWND)

    assert first.open_editor is True
    assert second.open_editor is True
    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_on_utterance_end_invalidates_cache(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    await policy.should_redirect(_FOCUSED_HWND)
    policy.on_utterance_end()
    await policy.should_redirect(_FOCUSED_HWND)

    assert detector.call_count == 2


@pytest.mark.asyncio
async def test_cache_max_age_expires_stale_entry(monkeypatch):
    # wh-xiazj.1.4: even when on_utterance_end is missed, an entry
    # older than _CACHE_MAX_AGE_S must be discarded so a stale True
    # cannot redirect into a now-busy shell.
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    # Seed the cache with a stale entry. Reaching into the private
    # dict is acceptable here because the contract under test is the
    # fallback expiry behaviour, and the cache shape is an
    # implementation detail this test owns alongside the policy.
    stale_time = time.monotonic() - (_CACHE_MAX_AGE_S + 1.0)
    policy._cache[(_FOCUSED_HWND, _FOCUSED_PID)] = (True, stale_time)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is True
    # The stale entry was discarded; the detector was invoked.
    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_mirror_busy_after_detector_await_fails_closed(monkeypatch):
    # wh-xiazj.1.2: between the initial mirror read and the return,
    # the executor await yields. Another lifecycle event can move
    # the mirror to OPEN_REQUESTED. The post-await re-check must
    # catch this and fail closed.
    _patch_resolution(monkeypatch)

    mirror = LogicMirror()

    def mutating_detector(*_args) -> bool:
        # Simulate a parallel redirect path that bumped the mirror
        # while this detector was executing in the worker thread.
        mirror.state = EditorState.OPEN_REQUESTED
        return True

    policy = _make_policy(detector_call=mutating_detector, mirror=mirror)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "editor_already_open"


@pytest.mark.asyncio
async def test_mirror_error_after_detector_await_fails_closed(monkeypatch):
    # wh-xiazj.1.2: same race as above, but the mirror transitions
    # into ERROR (a lifecycle recovery state) instead of an in-flight
    # state. The reason string must reflect that.
    _patch_resolution(monkeypatch)

    mirror = LogicMirror()

    def mutating_detector(*_args) -> bool:
        mirror.state = EditorState.ERROR
        return True

    policy = _make_policy(detector_call=mutating_detector, mirror=mirror)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.target_terminal_hwnd == 0
    assert decision.reason == "editor_lifecycle_error"


@pytest.mark.asyncio
async def test_detector_result_not_cached_when_utterance_ended_during_await(
    monkeypatch,
):
    # wh-xiazj.2.1: if on_utterance_end fires while the detector is
    # still executing in the worker thread, the cache write at the
    # end of the detector path must be discarded. Otherwise a stale
    # True from the previous utterance would seed the new
    # utterance's cache and the policy could redirect to a
    # now-busy shell for up to _CACHE_MAX_AGE_S.
    _patch_resolution(monkeypatch)

    detector_started = threading.Event()

    def slow_detector(*_args) -> bool:
        detector_started.set()
        time.sleep(0.1)
        return True

    policy = _make_policy(
        detector_call=slow_detector,
        detector_timeout_s=1.0,
    )

    first_task = asyncio.create_task(policy.should_redirect(_FOCUSED_HWND))

    # Yield until the detector has actually entered the worker
    # function so on_utterance_end fires against a live detector.
    while not detector_started.is_set():
        await asyncio.sleep(0.005)

    policy.on_utterance_end()

    first_decision = await first_task

    # The first call's caller still gets its decision -- the post-await
    # path runs normally for the in-flight utterance.
    assert first_decision.open_editor is True
    # But the cache must NOT carry the result forward into the next
    # utterance. The post-detector write was discarded because the
    # generation changed during the await.
    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_late_detector_result_populates_cache_after_timeout(
    monkeypatch,
):
    # wh-redirect-late-cache-and-fg-poll: when the detector finishes
    # just after the deadline (~110-130 ms on the production prompt
    # detector vs. a 100 ms deadline), the late True result must land
    # in the per-utterance cache so the next word's should_redirect
    # short-circuits to a cache hit instead of running yet another
    # detector call that also times out. Without this, the first
    # three words of every terminal dictation utterance get rejected
    # with prompt_detector_timeout (observed in wheelhouse.log at
    # trace T-17786104422).
    _patch_resolution(monkeypatch)
    detector_done = threading.Event()
    call_count = 0

    def late_detector(*_args) -> bool:
        nonlocal call_count
        call_count += 1
        # First call simulates the slow cold path; sleep longer than
        # the 50 ms deadline used below so wait_for fires the timeout.
        time.sleep(0.12)
        detector_done.set()
        return True

    policy = _make_policy(
        detector_call=late_detector,
        detector_timeout_s=0.05,
    )

    first_decision = await policy.should_redirect(_FOCUSED_HWND)
    assert first_decision.open_editor is False
    assert first_decision.reason == "prompt_detector_timeout"

    # Wait for the worker thread to finish so the done-callback has a
    # chance to record the late result.
    assert detector_done.wait(timeout=1.0)
    # Poll the event loop until the done callback fires (the asyncio
    # future wrapping the executor's concurrent.futures.Future is
    # signalled on the loop, so we must yield to give it a chance to
    # run). When the done callback has fired, the in-flight marker is
    # cleared and the cache is populated.
    for _ in range(100):
        if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("Future done-callback never fired")

    second_decision = await policy.should_redirect(_FOCUSED_HWND)

    assert second_decision.open_editor is True
    assert second_decision.reason == "terminal_at_prompt"
    # Crucially, the detector was NOT called a second time; the late
    # cache write means the second call short-circuits.
    assert call_count == 1


@pytest.mark.asyncio
async def test_late_detector_result_not_cached_when_utterance_ended_during_timeout(
    monkeypatch,
):
    # wh-redirect-late-cache-and-fg-poll: the late-cache write must
    # honour the same generation-mismatch protection as the inline
    # write (wh-xiazj.2.1). If on_utterance_end fires after the
    # wait_for timeout but before the future completes, the late
    # result belongs to a finished utterance and must not seed the
    # next one.
    _patch_resolution(monkeypatch)
    detector_done = threading.Event()

    def late_detector(*_args) -> bool:
        time.sleep(0.12)
        detector_done.set()
        return True

    policy = _make_policy(
        detector_call=late_detector,
        detector_timeout_s=0.05,
    )

    first_decision = await policy.should_redirect(_FOCUSED_HWND)
    assert first_decision.reason == "prompt_detector_timeout"

    # Utterance ends BEFORE the detector finishes -- the generation
    # bumps while the future is still mid-flight.
    policy.on_utterance_end()

    assert detector_done.wait(timeout=1.0)
    # Poll until the done callback fires so the generation-mismatch
    # branch in _release runs and discards the late result.
    for _ in range(100):
        if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("Future done-callback never fired")

    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_late_false_detector_result_not_cached(monkeypatch):
    # wh-redirect-late-cache-and-fg-poll (adversarial-review finding 2):
    # a late False detector result must NOT be cached. Caching False
    # would silently strand the rest of the utterance on a stale
    # "terminal_busy" verdict for up to _CACHE_MAX_AGE_S even if the
    # shell dropped back to a prompt between words. Only positive late
    # results seed the cache; subsequent words re-run the detector on
    # a False answer.
    _patch_resolution(monkeypatch)
    detector_done = threading.Event()

    def late_false_detector(*_args) -> bool:
        time.sleep(0.12)
        detector_done.set()
        return False

    policy = _make_policy(
        detector_call=late_false_detector,
        detector_timeout_s=0.05,
    )

    first_decision = await policy.should_redirect(_FOCUSED_HWND)
    assert first_decision.reason == "prompt_detector_timeout"

    assert detector_done.wait(timeout=1.0)
    for _ in range(100):
        if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("Future done-callback never fired")

    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_concurrent_same_key_awaits_existing_future(monkeypatch):
    # wh-redirect-await-inflight: when a detector call for the same key
    # is still executing in the worker thread, an overlapping
    # should_redirect for the same key must NOT schedule a second
    # detector job. Instead, it awaits the existing future and uses
    # the same result. This closes the race where the future has
    # resolved but its done-callback has not yet cleared the
    # in-flight marker -- under the prior set-based design, the
    # second word in that 30 ms window got dropped with
    # prompt_detector_in_flight even though a True result was already
    # available.
    _patch_resolution(monkeypatch)

    detector_started = threading.Event()
    detector_call_count = 0

    def slow_started_detector(*_args) -> bool:
        nonlocal detector_call_count
        detector_call_count += 1
        detector_started.set()
        time.sleep(0.3)
        return True

    policy = _make_policy(
        detector_call=slow_started_detector,
        detector_timeout_s=1.0,
    )

    first_task = asyncio.create_task(policy.should_redirect(_FOCUSED_HWND))

    # Yield until the worker thread has actually entered the
    # detector function so the second call observes the in-flight
    # future and awaits it.
    while not detector_started.is_set():
        await asyncio.sleep(0.005)

    second_decision = await policy.should_redirect(_FOCUSED_HWND)
    first_decision = await first_task

    # Both calls now succeed via the same detector run.
    assert first_decision.open_editor is True
    assert first_decision.reason == "terminal_at_prompt"
    assert second_decision.open_editor is True
    assert second_decision.reason == "terminal_at_prompt"
    assert detector_call_count == 1


@pytest.mark.asyncio
async def test_resolved_inflight_future_is_awaited_before_callback(
    monkeypatch,
):
    # wh-redirect-await-inflight: simulate the race window the production
    # bug reproduced -- the future has resolved but its add_done_callback
    # has not yet fired, so _in_flight still maps the key to the future
    # and _cache is still empty. should_redirect must await the future
    # and use its result instead of declining with
    # prompt_detector_in_flight (the old behaviour) or running a fresh
    # detector call.
    _patch_resolution(monkeypatch)

    detector_calls = 0

    def detector(*_args) -> bool:
        nonlocal detector_calls
        detector_calls += 1
        return True

    policy = _make_policy(
        detector_call=detector,
        detector_timeout_s=0.5,
    )

    # Build a Future that has already resolved with True and seed it
    # into the in_flight map. This is exactly the race-window state:
    # future done, callback not fired, cache empty.
    loop = asyncio.get_running_loop()
    fake_future: asyncio.Future = loop.create_future()
    fake_future.set_result(True)
    policy._in_flight[(_FOCUSED_HWND, _FOCUSED_PID)] = fake_future

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is True
    assert decision.reason == "terminal_at_prompt"
    # The await consumed the seeded future's result; no fresh detector
    # call ran for the second waiter.
    assert detector_calls == 0


@pytest.mark.asyncio
async def test_inflight_await_terminal_busy_from_existing_future(
    monkeypatch,
):
    # wh-redirect-await-inflight: when the existing future resolves to
    # False (shell busy), the second waiter must surface the same
    # terminal_busy verdict instead of opening the editor or scheduling
    # a fresh detector call.
    _patch_resolution(monkeypatch)

    detector_calls = 0

    def detector(*_args) -> bool:
        nonlocal detector_calls
        detector_calls += 1
        return False

    policy = _make_policy(
        detector_call=detector,
        detector_timeout_s=0.5,
    )

    loop = asyncio.get_running_loop()
    fake_future: asyncio.Future = loop.create_future()
    fake_future.set_result(False)
    policy._in_flight[(_FOCUSED_HWND, _FOCUSED_PID)] = fake_future

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is False
    assert decision.reason == "terminal_busy"
    assert detector_calls == 0


# ---------------------------------------------------------------------------
# wh-redirect-detector-deadline: the module-level default detector deadline
# must be wide enough to cover the real prompt detector's worst case under
# AttachConsole / FreeConsole churn. The user-reported reproduction showed
# the detector returning True roughly 1 to 7 milliseconds after the original
# 100 ms deadline. Bumping the default to 500 ms gives roughly five times
# the observed worst case while staying well below the user-perceptible
# delay threshold for a first word. Both the policy module's constant and
# the integration factory's parameter default must agree, otherwise the
# integration call site in speech_handler keeps the old value in production.
# ---------------------------------------------------------------------------


def test_default_detector_timeout_is_at_least_500ms():
    from services.wheelhouse.speech.focus_redirect_policy import (
        _DEFAULT_DETECTOR_TIMEOUT_S,
    )
    assert _DEFAULT_DETECTOR_TIMEOUT_S >= 0.5, (
        "Policy default detector deadline must be at least 500 ms; "
        "the prompt detector's AttachConsole path can take "
        "100 ms-plus on Windows Terminal targets."
    )


# wh-g2-refactor.18: test_integration_factory_default_detector_timeout_matches_policy_default
# was retired with the focus-redirect integration's create_focus_redirect_path
# factory. The policy module's default still has the contract test above
# (test_default_detector_timeout_is_at_least_500ms).


# ---------------------------------------------------------------------------
# wh-prewarm-detector-vad-start: pre-warming the prompt detector at VAD_START.
#
# The prompt detector takes 100 ms-plus on Windows Terminal targets because the
# AttachConsole / FreeConsole round-trip serialises on a process-global lock.
# The 500 ms timeout in should_redirect is a backstop, not a fast path. By
# calling FocusRedirectPolicy.prewarm at Silero VAD speech_start, the policy
# starts the detector roughly 1.5 seconds before the first dictated word
# arrives. By that point the result is cached and should_redirect returns
# without scheduling another detector call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_caches_detector_result(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(_FOCUSED_HWND)

    # Wait for the executor work to complete and the done-callback to fire.
    for _ in range(200):
        if (_FOCUSED_HWND, _FOCUSED_PID) in policy._cache:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("prewarm never populated the cache")

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is True
    assert decision.reason == "terminal_at_prompt"
    # The detector was invoked once during prewarm; should_redirect hit the cache.
    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_prewarm_negative_result_not_cached(monkeypatch):
    # A False detector result during prewarm must NOT be cached. Caching
    # False would silently strand the next utterance on a stale "busy"
    # verdict for up to _CACHE_MAX_AGE_S even if the shell dropped back
    # to a prompt. Mirrors the late-False rule in the should_redirect
    # late-cache path.
    #
    # Codex finding wh-prewarm-detector-vad-start.1.2 fix: wait on
    # ``detector.call_count`` and the prewarm-owned dedup marker, NOT
    # on ``_in_flight``. The earlier wait-on-_in_flight was true
    # immediately because pre-warm did not touch _in_flight at the
    # time, so the cache assertion could have run before the
    # done-callback executed -- a regression that made False land
    # in the cache could have passed unnoticed.
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=False)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(_FOCUSED_HWND)

    for _ in range(200):
        if (
            detector.call_count >= 1
            and _FOCUSED_HWND not in policy._prewarm_hwnds_inflight
            and (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight
        ):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail(
            "prewarm never finished: detector ran "
            f"{detector.call_count} time(s); _prewarm_hwnds_inflight="
            f"{policy._prewarm_hwnds_inflight}; _in_flight keys="
            f"{list(policy._in_flight.keys())}"
        )

    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_prewarm_non_terminal_is_noop(monkeypatch):
    _patch_resolution(monkeypatch, process_name=_NON_TERMINAL_PROCESS)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(_FOCUSED_HWND)

    # Give the loop a chance to run any (incorrectly) scheduled work.
    await asyncio.sleep(0.05)

    assert detector.call_count == 0
    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache
    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight


@pytest.mark.asyncio
async def test_prewarm_zero_hwnd_is_noop(monkeypatch):
    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(0)

    await asyncio.sleep(0.02)

    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_prewarm_skips_when_cache_fresh(monkeypatch):
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    # Seed the cache with a fresh entry.
    policy._cache[(_FOCUSED_HWND, _FOCUSED_PID)] = (True, time.monotonic())

    policy.prewarm(_FOCUSED_HWND)

    await asyncio.sleep(0.02)

    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_prewarm_does_not_double_fire(monkeypatch):
    # When a prewarm for the same key is still running in the executor, a
    # second prewarm must be a no-op rather than scheduling a parallel
    # detector call. The existing in-flight future will populate the cache
    # on completion.
    _patch_resolution(monkeypatch)

    detector_started = threading.Event()
    detector_call_count = 0

    def slow_detector(*_args) -> bool:
        nonlocal detector_call_count
        detector_call_count += 1
        detector_started.set()
        time.sleep(0.1)
        return True

    policy = _make_policy(
        detector_call=slow_detector,
        detector_timeout_s=1.0,
    )

    policy.prewarm(_FOCUSED_HWND)

    # Wait for the worker thread to enter the detector.
    while not detector_started.is_set():
        await asyncio.sleep(0.005)

    # Second prewarm during in-flight: must NOT schedule another call.
    policy.prewarm(_FOCUSED_HWND)

    # Wait for the first call to complete.
    for _ in range(200):
        if (_FOCUSED_HWND, _FOCUSED_PID) in policy._cache:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("first prewarm never completed")

    assert detector_call_count == 1


@pytest.mark.asyncio
async def test_prewarm_then_foreground_change_falls_back(monkeypatch):
    # Pre-warm caches against (focused_hwnd, pid). If the user switches
    # windows between prewarm and the first dictated word, should_redirect
    # arrives with a different HWND, the cache lookup misses, and the
    # detector runs fresh under the deadline. This is the documented
    # cold-start backstop case.
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    HWND_A = _FOCUSED_HWND
    HWND_B = _FOCUSED_HWND + 1

    policy.prewarm(HWND_A)
    for _ in range(200):
        if (HWND_A, _FOCUSED_PID) in policy._cache:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("prewarm never populated the cache for HWND_A")

    decision = await policy.should_redirect(HWND_B)

    assert decision.open_editor is True
    assert decision.reason == "terminal_at_prompt"
    # The cache lookup for HWND_B missed; the detector ran a second time.
    assert detector.call_count == 2


@pytest.mark.asyncio
async def test_prewarm_handles_resolution_failure(monkeypatch):
    # process_name_for_hwnd returning None or GetWindowThreadProcessId
    # raising must not raise out of prewarm; the call is fire-and-forget
    # from the websocket vad_start handler and a raise would break the
    # GUI activity-state pulse.
    from services.wheelhouse.speech import focus_redirect_policy as mod

    monkeypatch.setattr(mod, "process_name_for_hwnd", lambda _h: None)
    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        return_value=(0, _FOCUSED_PID)
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(_FOCUSED_HWND)

    await asyncio.sleep(0.02)

    assert detector.call_count == 0
    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_prewarm_ignores_get_window_thread_process_id_raise(monkeypatch):
    from services.wheelhouse.speech import focus_redirect_policy as mod

    monkeypatch.setattr(
        mod, "process_name_for_hwnd", lambda _h: _TERMINAL_PROCESS,
    )
    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        side_effect=OSError("bad hwnd"),
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    # Must not raise.
    policy.prewarm(_FOCUSED_HWND)

    await asyncio.sleep(0.02)

    assert detector.call_count == 0


@pytest.mark.asyncio
async def test_prewarm_skips_when_existing_inflight_future(monkeypatch):
    # If a should_redirect call already scheduled the detector, prewarm
    # must NOT schedule another one. Mirrors the should_redirect await-the-
    # existing-future contract: one detector run per key per utterance.
    _patch_resolution(monkeypatch)

    detector_call_count = 0

    def detector(*_args) -> bool:
        nonlocal detector_call_count
        detector_call_count += 1
        return True

    policy = _make_policy(detector_call=detector)

    loop = asyncio.get_running_loop()
    fake_future: asyncio.Future = loop.create_future()
    policy._in_flight[(_FOCUSED_HWND, _FOCUSED_PID)] = fake_future

    policy.prewarm(_FOCUSED_HWND)

    await asyncio.sleep(0.02)

    assert detector_call_count == 0
    # The seeded future is still the in-flight marker; prewarm did not replace it.
    assert policy._in_flight.get((_FOCUSED_HWND, _FOCUSED_PID)) is fake_future
    fake_future.cancel()


@pytest.mark.asyncio
async def test_prewarm_after_close_is_noop(monkeypatch):
    # wh-prewarm-detector-vad-start (adversarial review #2): a vad_start
    # arriving after close() must not schedule fresh work. Without the
    # _closing guard the executor's wait=True shutdown could block on a
    # detector call started in the millisecond before the shutdown.
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.close()

    policy.prewarm(_FOCUSED_HWND)

    await asyncio.sleep(0.02)

    assert detector.call_count == 0
    assert _FOCUSED_HWND not in policy._prewarm_hwnds_inflight


@pytest.mark.asyncio
async def test_prewarm_pops_stale_cache_entry(monkeypatch):
    # Deepseek finding wh-prewarm-detector-vad-start.2.1: prewarm must
    # pop a stale cache entry before scheduling its own detector,
    # mirroring should_redirect's own stale-pop. Otherwise the new
    # detector's done-callback hits its "key in cache" guard and the
    # fresh result is discarded; the stale True entry then poisons
    # subsequent should_redirect calls for the same key.
    _patch_resolution(monkeypatch)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    stale_time = time.monotonic() - (_CACHE_MAX_AGE_S + 1.0)
    policy._cache[(_FOCUSED_HWND, _FOCUSED_PID)] = (True, stale_time)

    policy.prewarm(_FOCUSED_HWND)

    for _ in range(200):
        cached = policy._cache.get((_FOCUSED_HWND, _FOCUSED_PID))
        if cached is not None and cached[1] > stale_time:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail(
            "prewarm did not refresh the cache: stale entry was not popped, "
            "fresh detector result was discarded by the in-cache guard"
        )

    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_prewarm_generation_mismatch_during_resolve_skips_detector(
    monkeypatch,
):
    # Deepseek finding wh-prewarm-detector-vad-start.2.2: the
    # generation-mismatch guard in _prewarm_async (which prevents an
    # utterance-N pre-warm from scheduling a detector after
    # on_utterance_end has bumped the generation for utterance N+1)
    # was unverified.
    #
    # The test slows _resolve_hwnd_to_terminal so on_utterance_end can
    # fire WHILE the resolve is in flight. The post-resolve gen check
    # should then skip the detector schedule entirely. Without the
    # guard the detector would run unnecessarily; the assertion
    # ``detector.call_count == 0`` distinguishes the two paths.
    # ``_release``'s own gen check covers the cache-write side but
    # does NOT prevent the wasted detector call -- that is what this
    # test pins down.
    from services.wheelhouse.speech import focus_redirect_policy as mod

    resolve_started = threading.Event()
    resolve_can_finish = threading.Event()

    def slow_process_name_for_hwnd(_h: int) -> str:
        resolve_started.set()
        resolve_can_finish.wait(timeout=2.0)
        return _TERMINAL_PROCESS

    monkeypatch.setattr(
        mod, "process_name_for_hwnd", slow_process_name_for_hwnd,
    )
    fake_win32process = Mock()
    fake_win32process.GetWindowThreadProcessId = Mock(
        return_value=(0, _FOCUSED_PID),
    )
    monkeypatch.setattr(mod, "win32process", fake_win32process)

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector, detector_timeout_s=1.0)

    policy.prewarm(_FOCUSED_HWND)

    while not resolve_started.is_set():
        await asyncio.sleep(0.005)

    policy.on_utterance_end()

    resolve_can_finish.set()

    for _ in range(200):
        if _FOCUSED_HWND not in policy._prewarm_hwnds_inflight:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("prewarm task did not complete")

    # The wait above only confirms _prewarm_async returned. The
    # detector_future (if scheduled) runs on the executor in
    # microseconds for a Mock; this grace period gives it time to
    # complete so detector.call_count is observable. With the
    # generation guard in place, the detector is never scheduled and
    # call_count stays at 0; without the guard, the detector runs
    # within this window and call_count becomes 1.
    for _ in range(40):
        if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)

    assert detector.call_count == 0, (
        "_prewarm_async generation guard skipped: detector was scheduled "
        "even though on_utterance_end had already bumped the generation "
        "during resolve"
    )
    assert (_FOCUSED_HWND, _FOCUSED_PID) not in policy._cache


@pytest.mark.asyncio
async def test_prewarm_and_should_redirect_share_one_detector_call(
    monkeypatch,
):
    # Codex review-loop finding wh-prewarm-detector-vad-start.1.1: if a
    # first dictated word arrives while prewarm is still inside the
    # detector, should_redirect must AWAIT the same detector future
    # via _in_flight rather than queue a duplicate detector call
    # behind it on the single-worker executor. The original design
    # did not publish prewarm's future into _in_flight; the regression
    # would make the first-word wait become prewarm-detector +
    # duplicate-detector, easily exceeding the 500 ms deadline.
    _patch_resolution(monkeypatch)

    detector_started = threading.Event()
    detector_call_count = 0

    def slow_detector(*_args) -> bool:
        nonlocal detector_call_count
        detector_call_count += 1
        detector_started.set()
        time.sleep(0.15)
        return True

    policy = _make_policy(
        detector_call=slow_detector,
        detector_timeout_s=1.0,
    )

    policy.prewarm(_FOCUSED_HWND)

    while not detector_started.is_set():
        await asyncio.sleep(0.005)

    decision = await policy.should_redirect(_FOCUSED_HWND)

    assert decision.open_editor is True
    assert decision.reason == "terminal_at_prompt"
    assert detector_call_count == 1, (
        f"expected one detector call (shared between prewarm and "
        f"should_redirect via _in_flight); got {detector_call_count}"
    )


@pytest.mark.asyncio
async def test_prewarm_resolution_runs_on_executor_thread(monkeypatch):
    # wh-prewarm-detector-vad-start (adversarial review #1): the
    # resolution work (process_name_for_hwnd / GetWindowThreadProcessId)
    # must run on the executor thread, NOT the asyncio loop thread.
    # Otherwise a hung target process could stall OpenProcess on the
    # loop and freeze the GUI hearing pulse plus the speech pipeline.
    import threading as _th

    _patch_resolution(monkeypatch)

    loop_thread_id = _th.get_ident()
    process_name_thread_id: list[int] = []

    from services.wheelhouse.speech import focus_redirect_policy as mod

    def _capturing_process_name_for_hwnd(_h: int) -> str:
        process_name_thread_id.append(_th.get_ident())
        return _TERMINAL_PROCESS

    monkeypatch.setattr(
        mod, "process_name_for_hwnd", _capturing_process_name_for_hwnd,
    )

    detector = Mock(return_value=True)
    policy = _make_policy(detector_call=detector)

    policy.prewarm(_FOCUSED_HWND)

    for _ in range(200):
        if process_name_thread_id:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("resolution never ran")

    assert process_name_thread_id[0] != loop_thread_id, (
        "process_name_for_hwnd must run on the executor thread, not the "
        "asyncio loop thread (adversarial review #1)"
    )


@pytest.mark.asyncio
async def test_prewarm_detector_error_after_generation_bump_is_retrieved(
    monkeypatch,
):
    # wh-prewarm-exception-leak: a pre-warm probe raising AFTER
    # on_utterance_end bumped the cache generation must still have its
    # exception retrieved by the done-callback. The old callback
    # returned on the generation mismatch BEFORE calling
    # fut.exception(); asyncio then reported the never-retrieved
    # exception to the loop's global exception handler at garbage
    # collection, and main.py's handler shut the whole app down
    # (observed 2026-07-10 15:11:59 in wheelhouse.log).
    import gc

    _patch_resolution(monkeypatch)

    release_detector = threading.Event()

    def failing_detector(*_args) -> bool:
        release_detector.wait(timeout=5.0)
        raise RuntimeError("probe transport failed")

    policy = _make_policy(detector_call=failing_detector)

    loop = asyncio.get_running_loop()
    reports: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: reports.append(ctx))
    try:
        policy.prewarm(_FOCUSED_HWND)

        # Wait for the prewarm coroutine to publish the detector future.
        for _ in range(500):
            if (_FOCUSED_HWND, _FOCUSED_PID) in policy._in_flight:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("prewarm never published a detector future")

        # The utterance ends (generation bump) BEFORE the worker
        # thread raises -- the exact ordering of the 2026-07-10 crash.
        policy.on_utterance_end()
        release_detector.set()

        for _ in range(500):
            if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("detector done-callback never fired")

        # Let any pending loop callbacks run, then force collection of
        # the dropped future. An unretrieved exception is reported to
        # the loop exception handler from the future's finalizer.
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)

        never_retrieved = [
            ctx for ctx in reports
            if "never retrieved" in ctx.get("message", "")
        ]
        assert never_retrieved == [], (
            "the pre-warm done-callback must retrieve the future's "
            "exception even when the cache generation has moved on"
        )
    finally:
        loop.set_exception_handler(None)
        policy.close()


@pytest.mark.asyncio
async def test_prewarm_detector_error_is_logged_at_warning(
    monkeypatch, caplog,
):
    # wh-log-crash-fixes.1.1: now that the done-callback retrieves the
    # exception, asyncio's ERROR "never retrieved" report no longer
    # fires for this path -- so the callback itself must log the
    # discarded failure at WARNING. The production log level is INFO;
    # a DEBUG line leaves a recurring probe failure invisible.
    _patch_resolution(monkeypatch)

    release_detector = threading.Event()

    def failing_detector(*_args) -> bool:
        release_detector.wait(timeout=5.0)
        raise RuntimeError("probe transport failed")

    policy = _make_policy(detector_call=failing_detector)
    try:
        with caplog.at_level(logging.INFO):
            policy.prewarm(_FOCUSED_HWND)

            for _ in range(500):
                if (_FOCUSED_HWND, _FOCUSED_PID) in policy._in_flight:
                    break
                await asyncio.sleep(0.005)
            else:
                pytest.fail("prewarm never published a detector future")

            policy.on_utterance_end()
            release_detector.set()

            for _ in range(500):
                if (_FOCUSED_HWND, _FOCUSED_PID) not in policy._in_flight:
                    break
                await asyncio.sleep(0.005)
            else:
                pytest.fail("detector done-callback never fired")

        visible = [
            record for record in caplog.records
            if record.levelno >= logging.WARNING
            and "RuntimeError" in record.getMessage()
        ]
        assert visible, (
            "a discarded detector failure must be visible at the "
            "production log level (INFO), not DEBUG-only"
        )
    finally:
        policy.close()
