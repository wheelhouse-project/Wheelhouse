"""Rate-limit and dedup maps for the rejection toast pipeline (wh-zib65).

Two independent maps in two different processes:

  * :class:`ToastSuppressionMap` -- GUI process. Keyed by
    ``(process_name, class_name, reason)``. Returns a
    :class:`ToastDecision` indicating whether to show the toast and,
    when shown, whether it is the first time for this key (8 second
    dwell) or a repeat (4 second dwell). Suppresses repeats inside a
    60-second cooldown. The map persists for the GUI process
    lifetime.

  * :class:`FirstRejectionLogMap` -- input process. Keyed by the same
    tuple. ``should_log`` returns ``True`` exactly once per key; later
    calls return ``False`` unless the optional re-escalation window
    (``rejection_reescalation_seconds``, default disabled) has elapsed
    since the last INFO log for that key
    (wh-rejection-log-reescalation). The input process uses this to
    log INFO on the first rejection per key alongside the existing
    per-call DEBUG log so a continuous dictation session does not
    drown the diagnostic stream. The map persists for the input
    process lifetime.

The two maps DO NOT coordinate. They serve different purposes and are
explicitly tested in isolation. See wh-x4mv.9 (review epic) and
wh-zib65 (the implementation bead) for the design rationale.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


_DEFAULT_COOLDOWN_SECONDS = 60.0
_DEFAULT_FIRST_DWELL_MS = 8000
_DEFAULT_REPEAT_DWELL_MS = 4000


RejectionKey = Tuple[str, str, str, str]
"""Suppression and dedup key for rejection events.

Shape: ``(process_name, class_name, control_type, reason)``.

``control_type`` was added per wh-9weum.4.2 (Gemini round 1 finding).
Frameworks that share a single ClassName across many control types --
the canonical case is Chromium's ``Chrome_RenderWidgetHostHWND`` which
hosts every interactive widget in the page -- would otherwise collapse
a button rejection, a checkbox rejection, and a list-item rejection
into a single suppression key. The first toast would show; the rest
would silently drop inside the 60-second cooldown even though they
are different controls. Including ``control_type`` distinguishes them.
"""


@dataclass(frozen=True)
class ToastDecision:
    """Decision returned by :class:`ToastSuppressionMap.decide`.

    ``show`` is True when the GUI should render the toast. When
    ``show`` is True, ``is_first`` says whether it is the first toast
    for this key (and therefore uses ``first_dwell_ms``) or a repeat
    after the cooldown (``repeat_dwell_ms``). ``lifetime_ms`` is the
    dwell value the widget should honour; it is zero when ``show`` is
    False so callers cannot accidentally show a suppressed toast.
    """

    show: bool
    is_first: bool
    lifetime_ms: int


class ToastSuppressionMap:
    """Cooldown + first/repeat dwell tracker for the GUI rejection toast."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        first_dwell_ms: int = _DEFAULT_FIRST_DWELL_MS,
        repeat_dwell_ms: int = _DEFAULT_REPEAT_DWELL_MS,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        self._cooldown = float(cooldown_seconds)
        self._first_ms = int(first_dwell_ms)
        self._repeat_ms = int(repeat_dwell_ms)
        self._time_source: Callable[[], float] = (
            time_source or time.monotonic
        )
        # key -> last_shown_timestamp. Absence of a key means
        # not-yet-shown (the first show uses first_dwell).
        self._last_shown: dict[RejectionKey, float] = {}
        self._lock = threading.Lock()

    def decide(self, key: RejectionKey) -> ToastDecision:
        now = self._time_source()
        with self._lock:
            last = self._last_shown.get(key)
            if last is None:
                self._last_shown[key] = now
                return ToastDecision(
                    show=True, is_first=True,
                    lifetime_ms=self._first_ms,
                )
            if now - last < self._cooldown:
                return ToastDecision(
                    show=False, is_first=False, lifetime_ms=0,
                )
            self._last_shown[key] = now
            return ToastDecision(
                show=True, is_first=False,
                lifetime_ms=self._repeat_ms,
            )


class FirstRejectionLogMap:
    """First-time-per-key gate for the input process's diagnostic INFO log.

    ``reescalation_seconds`` (wh-rejection-log-reescalation, from the
    deepseek wh-soft-allow-verdict-tier.2.3 finding) optionally lets the
    INFO line fire again once per elapsed window, so a persistent
    rejection ("the notice keeps appearing on my approved target") stays
    visible at INFO level instead of logging exactly once and then
    looking self-resolved. 0 (the default) keeps the original
    once-per-key behaviour. Suppressed calls do not move the window;
    it restarts only when a log actually fires.
    """

    def __init__(
        self,
        *,
        reescalation_seconds: float = 0.0,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        self._reescalation = float(reescalation_seconds)
        self._time_source: Callable[[], float] = (
            time_source or time.monotonic
        )
        # key -> timestamp of the last INFO log that fired. Absence
        # means never logged (the next call logs).
        self._last_logged: dict[RejectionKey, float] = {}
        self._lock = threading.Lock()

    def should_log(self, key: RejectionKey) -> bool:
        now = self._time_source()
        with self._lock:
            last = self._last_logged.get(key)
            if last is None:
                self._last_logged[key] = now
                return True
            if self._reescalation > 0 and now - last >= self._reescalation:
                self._last_logged[key] = now
                return True
            return False


def reescalation_seconds_from_config(config: dict) -> float:
    """Read [ui_actions.text_target].rejection_reescalation_seconds.

    Defensive like the rest of the config surface: any missing section,
    wrong type, or negative value degrades to 0.0 (disabled = the
    original once-per-key behaviour) and never raises.
    """
    section = config.get("ui_actions", {})
    if not isinstance(section, dict):
        return 0.0
    section = section.get("text_target", {})
    if not isinstance(section, dict):
        return 0.0
    raw = section.get("rejection_reescalation_seconds", 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    return value if value > 0 else 0.0
