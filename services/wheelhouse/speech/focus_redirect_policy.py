"""Focus-redirect policy component for the terminal dictation editor (wh-xiazj).

The policy decides "should this dictation word route into the
persistent hidden dictation editor instead of typing into whatever has
foreground?". A True decision in production means the focused window
is a terminal sitting at a shell prompt; the SpeechProcessor then
calls ``LogicController.insert_editor_word`` instead of the standard
``intelligent_insert_text`` IPC.

Wiring (post wh-g2-refactor.18 slice 18.32.1): SpeechHandler constructs
the policy with a LogicMirror stub, a lazy-imported PromptDetector
callable, and the Win32 foreground-HWND provider. SpeechProcessor
consults ``should_redirect`` for every DICTATE word, calls
``on_utterance_end`` on each utterance-end marker, and the
WebSocketManager fires ``prewarm`` on every Silero vad_start so the
detector cache is populated before the first dictated word arrives.

The heavier FocusRedirectPath / FocusChangeWordBuffer / EditorLifecycle
machinery that used to mediate between this policy and the editor was
retired with the on-demand editor in wh-g2-refactor.18. The persistent
editor exists at GUI startup and the policy now feeds the per-word
editor IPC directly.

Required behaviour (from the bead spec, including the round-1 review
amendments and the wh-xiazj.1 codex review findings):

  1. Editor-already-open short-circuit. If the injected
     :class:`LogicMirror` reports the editor in ``ERROR``, the policy
     returns ``open_editor=False`` with reason
     ``editor_lifecycle_error`` (wh-xiazj.1.1 -- ERROR is a recovery
     state and a redirect would mask the prior failure). If the
     mirror reports ``OPEN_REQUESTED``, ``OPEN_APPLIED``,
     ``FOCUS_PENDING``, ``FOCUS_CONFIRMED``, or ``SUBMITTING``, the
     policy returns ``open_editor=False`` with reason
     ``editor_already_open`` so a second redirect cannot race the first.
  2. Resolve the focused window's process via
     ``ui.hwnd_utils.process_name_for_hwnd`` and
     ``win32process.GetWindowThreadProcessId``. Any failure -- zero
     HWND, exception, missing PID -- returns ``open_editor=False``
     with reason ``cannot_resolve_focused_process``.
  3. Terminal allowlist check. Compare the resolved exe name
     case-insensitively against :data:`_TERMINAL_PROCESS_NAMES`.
     Non-terminal focus returns ``open_editor=False`` with reason
     ``not_a_terminal``.
  4. Prompt-detector wrapping. Call the injected
     ``prompt_detector_call`` OFF the event loop on a dedicated
     single-worker thread pool (wh-xiazj.1.3 -- the production
     prompt_detector serialises on a process-global lock, so one
     worker is enough and shields the default executor from any
     starvation if the detector hangs). Bound the wait with
     ``asyncio.wait_for`` against a per-call deadline (default
     100 ms). On timeout return reason ``prompt_detector_timeout``;
     on any other exception return reason ``prompt_detector_error``.
     A per-key in-flight marker (wh-xiazj.1.3) suppresses repeated
     concurrent calls for the same ``(focused_hwnd, pid)`` while a
     prior detector run is still executing in the worker thread;
     those overlapping calls return reason
     ``prompt_detector_in_flight``. Results are cached per
     ``(focused_hwnd, pid)`` for the duration of one utterance and
     also expire after :data:`_CACHE_MAX_AGE_S` as a fallback in
     case the speech pipeline misses an :meth:`on_utterance_end`
     call (wh-xiazj.1.4). A False detector result -- shell is busy
     -- returns reason ``terminal_busy``. A True detector result --
     shell at prompt -- continues to step 5.
  5. Post-await mirror re-check (wh-xiazj.1.2). After the detector
     await yields to the event loop, the mirror may have moved into
     a busy or error state. Re-read the mirror before returning
     ``open_editor=True``. If the mirror is now busy, fail closed
     with ``editor_already_open``; if now in ``ERROR``, fail closed
     with ``editor_lifecycle_error``. Otherwise return
     ``open_editor=True`` with reason ``terminal_at_prompt`` and the
     focused HWND as the target.
  6. Fail-closed posture. Every reject path returns
     ``open_editor=False``. The policy never returns
     ``open_editor=True`` without a positive detector result inside
     the deadline AND a confirmed-idle mirror at decision time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import win32process

from services.wheelhouse.shared.editor_lifecycle import EditorState, LogicMirror
from services.wheelhouse.ui.hwnd_utils import process_name_for_hwnd

logger = logging.getLogger(__name__)


# States that mean the editor is in flight; a second redirect must not
# race the first. ``OPEN_REQUESTED`` is included because the editor is
# already being requested -- redirecting again would emit a second
# ``open_requested`` event before the first is applied.
_EDITOR_OPEN_STATES: frozenset[EditorState] = frozenset({
    EditorState.OPEN_REQUESTED,
    EditorState.OPEN_APPLIED,
    EditorState.FOCUS_PENDING,
    EditorState.FOCUS_CONFIRMED,
    EditorState.SUBMITTING,
})


# Terminal exe allowlist (case-insensitive comparison via .lower()).
# A future slice can move this to config; for now it is a module-level
# frozenset. Code.exe (VS Code integrated terminal) is deliberately
# excluded -- VS Code routes integrated terminals through the editor
# host, and conservatively treating Code.exe as not-a-terminal avoids
# redirecting on the editor itself.
_TERMINAL_PROCESS_NAMES: frozenset[str] = frozenset({
    "windowsterminal.exe",
    "wt.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "openconsole.exe",
})


# Default deadline for the prompt-detector call, in seconds. The
# round-1 review amendment (wh-7gt07.2.4) proposed 100 ms; the live
# measurement (wh-redirect-detector-deadline) showed the real detector
# regularly takes 100 ms-plus on Windows Terminal targets because the
# AttachConsole / FreeConsole round-trip inside _has_interactive_child
# runs under a global lock and takes about that long. Bumping the
# default to 500 ms gives roughly five times the observed worst case
# while staying below the user-perceptible delay for a first dictated
# word. The fail-closed behaviour on a genuinely hung detector still
# applies; the user just waits half a second for the terminal
# passthrough fallback instead of one tenth of a second.
_DEFAULT_DETECTOR_TIMEOUT_S: float = 0.5


# Fallback maximum age for a cache entry, in seconds. The cache is
# normally invalidated by ``on_utterance_end``; this fallback bounds
# the staleness when that callback is missed (wh-xiazj.1.4). A typical
# utterance is under 3 seconds; 5 seconds is a generous bound.
_CACHE_MAX_AGE_S: float = 5.0


@dataclass(frozen=True)
class RedirectDecision:
    """Outcome of a single ``should_redirect`` call.

    Fields:
      open_editor: True when the editor should be opened and given
        focus before the dictation word reaches the pipeline.
      target_terminal_hwnd: the terminal HWND the editor is being
        opened for. Zero when not redirecting (the field is meaningful
        only when ``open_editor`` is True).
      reason: short human-readable explanation. Values:
        ``terminal_at_prompt``, ``not_a_terminal``, ``terminal_busy``,
        ``editor_already_open``, ``editor_lifecycle_error``,
        ``prompt_detector_timeout``, ``prompt_detector_error``,
        ``prompt_detector_in_flight``,
        ``cannot_resolve_focused_process``.
    """

    open_editor: bool
    target_terminal_hwnd: int
    reason: str


class FocusRedirectPolicy:
    """Decide whether to open the editor and redirect focus.

    The policy is a constructor-injection seam. The owning Logic
    process supplies:

      * ``mirror`` -- the :class:`LogicMirror` whose state answers
        "is the editor already open?".
      * ``prompt_detector_call`` -- a callable that takes
        ``(process_name, pid)`` and returns ``True`` when the shell
        is at a prompt. Injecting the callable keeps the policy
        independent of the production ``prompt_detector.py`` module
        and lets tests supply a fake.
      * ``detector_timeout_s`` -- per-call deadline for the detector.
        100 ms by default.
      * ``loop`` -- optional event loop for ``run_in_executor``. When
        ``None``, ``should_redirect`` uses
        ``asyncio.get_running_loop()`` (the loop that is calling it).

    The cache is normally invalidated by :meth:`on_utterance_end`.
    As a defensive fallback, individual cache entries also expire
    after :data:`_CACHE_MAX_AGE_S` so a missed end-of-utterance
    callback cannot strand a stale ``True`` past the utterance it
    was measured for. The cache is keyed on
    ``(focused_hwnd, pid)`` so the busy/idle state is shell-aware
    -- a new shell PID inside the same terminal HWND re-runs the
    detector.

    The policy owns a dedicated single-worker ``ThreadPoolExecutor``
    for the detector calls (wh-xiazj.1.3). One worker is enough
    because the production ``prompt_detector`` serialises on a
    process-global console lock; running detection on the default
    executor would risk starvation if the lock blocks one call for
    longer than expected. Call :meth:`close` to shut the executor
    down at policy teardown.
    """

    def __init__(
        self,
        *,
        mirror: LogicMirror,
        prompt_detector_call: Callable[[str, int], bool],
        detector_timeout_s: float = _DEFAULT_DETECTOR_TIMEOUT_S,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._mirror = mirror
        self._prompt_detector_call = prompt_detector_call
        self._detector_timeout_s = detector_timeout_s
        self._loop = loop
        # Per-utterance cache. Keyed on (focused_hwnd, pid); value is
        # ``(at_prompt, monotonic_set_time)``. Invalidated by
        # ``on_utterance_end()`` and bounded by ``_CACHE_MAX_AGE_S``.
        self._cache: dict[tuple[int, int], tuple[bool, float]] = {}
        # Generation counter incremented by ``on_utterance_end`` (wh-xiazj.2.1).
        # ``should_redirect`` captures the generation at start; the cache
        # write at the end of the detector path runs only if the
        # generation has not changed during the await. A detector that
        # finishes after ``on_utterance_end`` fired writes against an
        # already-cleared cache for the previous utterance, so the
        # generation-mismatch path discards the write rather than
        # contaminating the next utterance with a stale entry.
        self._cache_generation: int = 0
        # Keys with an executor call still in flight, mapped to the
        # asyncio Future wrapping the executor work. A second
        # ``should_redirect`` for the same key awaits the existing
        # future instead of scheduling another detector call
        # (wh-xiazj.1.3 + wh-redirect-await-inflight). Awaiting the
        # same future closes the race where the future has resolved
        # but its ``add_done_callback`` has not yet cleared the map
        # nor populated the cache -- under the prior set-based design,
        # any second word that arrived in that 30 ms window got
        # dropped with ``prompt_detector_in_flight`` even though a
        # True result was already available. The map entry is
        # cleared by ``_release`` after the future completes; a
        # ``wait_for`` timeout in either the original waiter or a
        # second waiter does NOT clear it because the worker thread
        # is still running.
        self._in_flight: dict[tuple[int, int], asyncio.Future] = {}
        # Dedicated single-worker executor for detector calls. The
        # name prefix makes the thread visible in debuggers.
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="focus-redirect-detector",
        )
        # wh-prewarm-detector-vad-start: dedup set for in-flight pre-warm
        # jobs, keyed on focused_hwnd ALONE because the pre-warm worker
        # resolves pid inside the executor thread. A second prewarm() for
        # the same hwnd while one is still running is a no-op so we do not
        # queue parallel resolution + detector work for the same window.
        # Different from ``_in_flight`` (which is the per-(hwnd, pid)
        # marker that ``should_redirect`` uses to await an existing
        # detector future); pre-warm cannot share that map because it
        # does not know the pid until after the executor work runs.
        self._prewarm_hwnds_inflight: set[int] = set()
        # wh-prewarm-detector-vad-start (adversarial review #2): set by
        # ``close()`` so any vad_start that arrives during teardown is a
        # no-op. Without this the executor's wait=True shutdown would
        # block on a freshly-scheduled detector call started by a
        # vad_start that fired in the millisecond before close.
        self._closing: bool = False

    def on_utterance_end(self) -> None:
        """Invalidate the per-utterance prompt-detector cache.

        Called by the speech pipeline at the end of each utterance so
        the next utterance starts with fresh detector state. The cache
        is intentionally short-lived: shell busy/idle can flip between
        utterances and a stale True would redirect into a now-busy
        shell.

        The generation counter is bumped so any detector call still
        running in the worker thread cannot poison the cleared cache
        when it completes (wh-xiazj.2.1).
        """
        self._cache.clear()
        self._cache_generation += 1

    def prewarm(self, focused_hwnd: int) -> None:
        """Schedule the prompt detector for ``focused_hwnd`` in the background.

        wh-prewarm-detector-vad-start: the production prompt detector
        takes 100 ms-plus on Windows Terminal targets because the
        AttachConsole / FreeConsole round-trip serialises on a
        process-global lock. Calling this method at Silero VAD
        ``speech_start`` -- roughly 1.5 seconds before the first
        dictated word reaches the policy -- starts the detector early
        so the result is cached by the time ``should_redirect`` runs.

        Returns immediately. ALL blocking work -- HWND-to-process
        resolution (psutil + Win32 OpenProcess), terminal allowlist
        check, and the prompt detector itself -- runs on the policy's
        dedicated single-worker executor. The loop thread does only
        the dedup check and an :func:`asyncio.create_task` schedule
        for the inner :meth:`_prewarm_async` coroutine.

        Once resolution returns a real ``(focused_hwnd, pid)``, the
        detector future is registered in ``_in_flight`` keyed by that
        cache key. A concurrent :meth:`should_redirect` for the same
        key finds the existing future and awaits it via
        ``asyncio.shield`` instead of scheduling a duplicate detector
        call. This closes the codex review-loop finding
        wh-prewarm-detector-vad-start.1.1 (Same-key first word can
        queue duplicate detector behind prewarm). Without that fix,
        a fast first-word arrival would incur prewarm-detector +
        duplicate-detector wait time, defeating the purpose of the
        pre-warm.

        Fail-quietly contract: any failure in HWND/PID resolution is
        a no-op (logged at debug). The websocket vad_start handler
        depends on this -- a raise would skip the activity-state
        write that drives the GUI hearing pulse.

        No-op cases (return without scheduling):
          * the policy has been ``close()``d;
          * zero ``focused_hwnd``;
          * a pre-warm for the same HWND is already in flight;
          * no running event loop (no-op with a debug log).
        Off-loop no-op cases (handled by the inner coroutine, also
        a no-op):
          * ``process_name_for_hwnd`` returns None;
          * the resolved process is not in ``_TERMINAL_PROCESS_NAMES``;
          * ``GetWindowThreadProcessId`` raises or returns a zero PID;
          * the cache already holds a fresh entry for the resolved key;
          * an in-flight future already exists in ``_in_flight`` for
            the resolved key (a should_redirect that arrived ahead of
            this prewarm has already scheduled the detector).
        """
        if self._closing:
            return
        if not focused_hwnd:
            return
        if focused_hwnd in self._prewarm_hwnds_inflight:
            return

        try:
            loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "focus_redirect_policy.prewarm: no running event loop; "
                "skipping pre-warm for hwnd=%s",
                focused_hwnd,
            )
            return

        self._prewarm_hwnds_inflight.add(focused_hwnd)
        gen_at_start = self._cache_generation
        asyncio.create_task(
            self._prewarm_async(focused_hwnd, loop, gen_at_start)
        )

    async def _prewarm_async(
        self,
        focused_hwnd: int,
        loop: asyncio.AbstractEventLoop,
        gen_at_start: int,
    ) -> None:
        """Drive the pre-warm two-step: resolve off-loop, then schedule detector.

        The HWND-only dedup marker (``_prewarm_hwnds_inflight``) is
        cleared in the ``finally`` so a second vad_start for the same
        HWND that arrives AFTER the detector future is already
        published in ``_in_flight`` is no-op'd by the post-resolution
        ``cache_key in self._in_flight`` check inside this coroutine.
        """
        try:
            try:
                resolution = await loop.run_in_executor(
                    self._executor,
                    self._resolve_hwnd_to_terminal,
                    focused_hwnd,
                )
            except Exception:
                logger.exception(
                    "focus_redirect_policy._prewarm_async: resolve raised "
                    "for hwnd=%s",
                    focused_hwnd,
                )
                return
            if resolution is None:
                return
            process_name, pid = resolution
            cache_key = (focused_hwnd, int(pid))

            if self._closing:
                return
            if self._cache_generation != gen_at_start:
                return

            cached = self._cache.get(cache_key)
            if cached is not None:
                if (time.monotonic() - cached[1]) <= _CACHE_MAX_AGE_S:
                    return
                # Deepseek finding wh-prewarm-detector-vad-start.2.1:
                # pop the stale entry, mirroring should_redirect's own
                # stale-pop. Otherwise the in-flight detector future's
                # _release callback hits its ``if key in self._cache:
                # return`` guard, the fresh result is discarded, and
                # the stale True entry can poison the next
                # should_redirect call's _release for the same key.
                self._cache.pop(cache_key, None)

            if cache_key in self._in_flight:
                return

            detector_future = loop.run_in_executor(
                self._executor,
                self._prompt_detector_call,
                process_name,
                int(pid),
            )
            self._in_flight[cache_key] = detector_future
            detector_future.add_done_callback(
                self._make_release_callback(cache_key, gen_at_start)
            )
        finally:
            self._prewarm_hwnds_inflight.discard(focused_hwnd)

    def _make_release_callback(
        self,
        cache_key: tuple[int, int],
        gen_at_start: int,
    ):
        """Build the detector-future done-callback shared by the pre-warm
        and ``should_redirect`` paths.

        ORDER MATTERS (wh-prewarm-exception-leak): the future's
        exception must be retrieved BEFORE any early return. This
        callback is the only consumer of a pre-warm future (nothing
        awaits it), and an executor future whose raised exception is
        never retrieved is reported to the loop's global exception
        handler when the future is garbage-collected -- which main.py
        used to treat as fatal. On 2026-07-10 a console-probe timeout
        raised ~4 s late, after an utterance-end generation bump; the
        old generation-check-first ordering skipped ``fut.exception()``
        and the resulting GC-time report shut down the whole app.

        The guards after retrieval are unchanged: generation match
        (wh-xiazj.2.1), inline-write-wins, and True-only late cache
        (wh-redirect-late-cache-and-fg-poll).
        """

        def _release(fut: asyncio.Future) -> None:
            self._in_flight.pop(cache_key, None)
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is not None:
                # WARNING, not DEBUG: retrieving the exception here
                # removes asyncio's ERROR "never retrieved" report, so
                # this line is the production log's only trace of a
                # failing probe (wh-log-crash-fixes.1.1). A fast raise
                # on the awaited path logs twice (awaiter + callback);
                # accepted for a rare failure.
                logger.warning(
                    "focus_redirect_policy: detector future for key=%s "
                    "completed with %s (discarded)",
                    cache_key, type(exc).__name__,
                )
                return
            if self._cache_generation != gen_at_start:
                return
            if cache_key in self._cache:
                return
            result = bool(fut.result())
            if not result:
                return
            self._cache[cache_key] = (result, time.monotonic())

        return _release

    def _resolve_hwnd_to_terminal(
        self, focused_hwnd: int,
    ) -> Optional[tuple[str, int]]:
        """Off-loop HWND-to-(process_name, pid) resolution for pre-warm.

        Runs on the dedicated single-worker executor thread because
        ``process_name_for_hwnd`` calls into psutil which on Windows
        opens a process handle (``OpenProcess``); a hung target
        process would otherwise stall the websocket handler's loop
        thread (adversarial review #1 fix). Returns ``None`` for any
        no-op condition (process name unresolvable, non-terminal
        process, GetWindowThreadProcessId raise, zero pid).
        """
        process_name = process_name_for_hwnd(focused_hwnd)
        if process_name is None:
            return None
        if process_name.lower() not in _TERMINAL_PROCESS_NAMES:
            return None
        try:
            _, pid = win32process.GetWindowThreadProcessId(focused_hwnd)
        except Exception as exc:
            logger.debug(
                "focus_redirect_policy._resolve_hwnd_to_terminal: "
                "GetWindowThreadProcessId(%s) failed: %s",
                focused_hwnd, exc,
            )
            return None
        if not pid:
            return None
        return (process_name, int(pid))

    def close(self) -> None:
        """Shut the dedicated executor down.

        Waits for any in-flight detector call to complete so the
        production ``prompt_detector`` console attach/detach
        ``finally`` block can restore the calling process's standard
        file descriptors and console attachment before the daemon
        worker thread is killed (wh-xiazj.2.3). The detector deadline
        normally bounds the worst-case wait to one
        ``detector_timeout_s`` interval; a genuinely hung detector
        would block ``close()`` until interpreter shutdown.
        ``cancel_futures=True`` drops any queued work -- in practice
        there is none because the in-flight marker suppresses
        overlapping submissions, but the flag costs nothing.

        wh-prewarm-detector-vad-start (adversarial review #2): sets
        ``_closing`` first so any ``prewarm`` call still arriving from
        the websocket vad_start handler returns immediately instead of
        racing the shutdown to schedule a fresh detector job.
        """
        self._closing = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def should_redirect(self, focused_hwnd: int) -> RedirectDecision:
        """Decide whether to open the editor and redirect focus.

        Returns a :class:`RedirectDecision` carrying the open/no-open
        verdict, the target terminal HWND (when redirecting), and a
        short reason string for logging and structured telemetry.
        """
        # 1. Editor lifecycle short-circuits. ERROR is treated
        # separately from the in-flight set so the structured reason
        # surfaces the underlying lifecycle problem instead of looking
        # like an ordinary "already opening" decline.
        if self._mirror.state is EditorState.ERROR:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="editor_lifecycle_error",
            )
        if self._mirror.state in _EDITOR_OPEN_STATES:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="editor_already_open",
            )

        # 2. Resolve the focused window's process. ``process_name_for_hwnd``
        # returns None on any failure; ``GetWindowThreadProcessId`` raises
        # on a bad HWND, which we treat the same way.
        if not focused_hwnd:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="cannot_resolve_focused_process",
            )

        process_name = process_name_for_hwnd(focused_hwnd)
        if process_name is None:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="cannot_resolve_focused_process",
            )

        try:
            _, pid = win32process.GetWindowThreadProcessId(focused_hwnd)
        except Exception as exc:
            logger.debug(
                "focus_redirect_policy: GetWindowThreadProcessId(%s) "
                "failed: %s",
                focused_hwnd, exc,
            )
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="cannot_resolve_focused_process",
            )
        if not pid:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="cannot_resolve_focused_process",
            )

        # 3. Terminal allowlist check. Compare case-insensitively.
        if process_name.lower() not in _TERMINAL_PROCESS_NAMES:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="not_a_terminal",
            )

        cache_key = (focused_hwnd, int(pid))
        # Capture the cache generation BEFORE the await yields (wh-xiazj.2.1).
        # The post-detector cache write checks this against the
        # current generation so a detector that finishes after
        # ``on_utterance_end`` cleared the cache cannot contaminate
        # the new utterance.
        gen_at_start = self._cache_generation

        # 4. Cache check. A fresh entry short-circuits the detector
        # call. Stale entries (older than ``_CACHE_MAX_AGE_S``) are
        # dropped and the detector runs again.
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and (now - cached[1]) <= _CACHE_MAX_AGE_S:
            at_prompt = cached[0]
        else:
            if cached is not None:
                self._cache.pop(cache_key, None)
            # Per-key in-flight handling (wh-redirect-await-inflight):
            # if another call for the same key is still running in the
            # executor, AWAIT THE SAME FUTURE instead of scheduling a
            # second detector job. The same Future is awaitable from
            # multiple coroutines; if it has already resolved, the
            # await returns immediately with the result. This closes
            # the race window where the future is done but its
            # done-callback has not yet fired -- under the prior
            # decline-on-collision design that window dropped the
            # second word with prompt_detector_in_flight.
            existing_future = self._in_flight.get(cache_key)
            if existing_future is not None:
                try:
                    at_prompt = await asyncio.wait_for(
                        asyncio.shield(existing_future),
                        timeout=self._detector_timeout_s,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "focus_redirect_policy: existing in-flight "
                        "detector deadline exceeded (%.3fs) for "
                        "process=%s pid=%s -- failing closed",
                        self._detector_timeout_s, process_name, pid,
                    )
                    return RedirectDecision(
                        open_editor=False,
                        target_terminal_hwnd=0,
                        reason="prompt_detector_timeout",
                    )
                except Exception as exc:
                    logger.warning(
                        "focus_redirect_policy: existing in-flight "
                        "detector raised %s for process=%s pid=%s -- "
                        "failing closed",
                        type(exc).__name__, process_name, pid,
                    )
                    return RedirectDecision(
                        open_editor=False,
                        target_terminal_hwnd=0,
                        reason="prompt_detector_error",
                    )
                # Fall through to the post-await mirror re-check and
                # terminal_busy / terminal_at_prompt return below. The
                # original future's _release callback handles cache
                # writing; the second waiter does not need to.
                future = None
            else:
                loop = self._loop or asyncio.get_running_loop()
                future = loop.run_in_executor(
                    self._executor,
                    self._prompt_detector_call,
                    process_name,
                    int(pid),
                )
                self._in_flight[cache_key] = future

            # The in-flight marker is cleared when the underlying
            # executor work actually completes. Using
            # ``asyncio.shield`` on the awaiter means that a
            # ``wait_for`` timeout cancels the waiter, not the
            # underlying future -- the worker thread keeps running
            # until done and the callback fires at that point.
            #
            # wh-redirect-late-cache-and-fg-poll: when the detector
            # finishes after the ``wait_for`` deadline (typical for
            # the cold prompt-detector call which takes 110-130 ms vs.
            # the 100 ms deadline), record the late result in the
            # per-utterance cache. The next word's ``should_redirect``
            # then short-circuits via cache hit instead of running yet
            # another detector call that also times out.
            #
            # Three guards apply:
            #   * Generation match: if ``on_utterance_end`` fired
            #     between the timeout and the callback, the result
            #     belongs to the prior utterance and is discarded
            #     (wh-xiazj.2.1 parity).
            #   * Inline-write wins: if a later same-key call ran a
            #     fresh detector that wrote inline while this future
            #     was still in flight, do not overwrite. The newer
            #     value is more relevant to the current utterance.
            #   * True-only late cache: a late False would silently
            #     strand the rest of the utterance on a stale "busy"
            #     verdict for up to ``_CACHE_MAX_AGE_S`` even if the
            #     shell dropped back to a prompt mid-utterance. Only
            #     a positive late result is cached; a negative one
            #     lets the next word re-check the detector.
            if future is not None:
                # Shared with the pre-warm path; retrieves the future's
                # exception before every guard (wh-prewarm-exception-leak).
                future.add_done_callback(
                    self._make_release_callback(cache_key, gen_at_start)
                )

                try:
                    at_prompt = await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=self._detector_timeout_s,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "focus_redirect_policy: prompt_detector deadline "
                        "exceeded (%.3fs) for process=%s pid=%s -- failing "
                        "closed",
                        self._detector_timeout_s, process_name, pid,
                    )
                    return RedirectDecision(
                        open_editor=False,
                        target_terminal_hwnd=0,
                        reason="prompt_detector_timeout",
                    )
                except Exception as exc:
                    logger.warning(
                        "focus_redirect_policy: prompt_detector raised %s "
                        "for process=%s pid=%s -- failing closed",
                        type(exc).__name__, process_name, pid,
                    )
                    return RedirectDecision(
                        open_editor=False,
                        target_terminal_hwnd=0,
                        reason="prompt_detector_error",
                    )

            # Only write to the cache if the generation has NOT
            # changed during the await (wh-xiazj.2.1). If
            # ``on_utterance_end`` fired while the detector was
            # running, this result is for the previous utterance and
            # must not contaminate the new one's cache.
            if self._cache_generation == gen_at_start:
                self._cache[cache_key] = (at_prompt, time.monotonic())

        if not at_prompt:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="terminal_busy",
            )

        # 5. Post-await mirror re-check. The executor await yields to
        # the Logic event loop; on a cache hit there is no yield, but
        # the recheck is still cheap and keeps the two paths
        # symmetric. If the mirror has moved into a busy or error
        # state, fail closed.
        if self._mirror.state is EditorState.ERROR:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="editor_lifecycle_error",
            )
        if self._mirror.state in _EDITOR_OPEN_STATES:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="editor_already_open",
            )

        return RedirectDecision(
            open_editor=True,
            target_terminal_hwnd=focused_hwnd,
            reason="terminal_at_prompt",
        )
