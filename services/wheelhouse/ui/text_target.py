"""Predicate for whether a focused UIA control accepts text input.

Centralised so the router and the retract path share one definition of
"this control is a text-input target." Called before any keystroke
synthesis or clipboard write so dictation does not deliver keys to
non-text controls (wh-zndq) and retraction does not send Backspace to
non-text controls (wh-32d).

Decision rules (in order):

  1.  No focused control -> reject (no_focused_control).
  2.  ControlType / ClassName / IsKeyboardFocusable read raises -> reject
      (stale_com); the control snapshot is unusable.
  3.  Not keyboard-focusable -> reject (not_focusable).
  4.  ClassName in deny_class_names -> reject (denylist_class_name).
      Used for ControlTypes UIA does not separately enumerate, e.g.
      WinUI 3 MenuFlyoutSubItem.
  5.  ControlType in deny_control_types -> reject (denylist_control_type).
  6.  Browser-process empty-ClassName trap -> reject (default_reject).
      Hoisted ahead of the TextPattern probe by wh-fc1x.1 because Brave's
      page document body exposes TextPattern (for screen-reader access)
      yet typing into it fires browser keyboard accelerators -- the
      spacebar between dictated words triggers scroll-down, and the
      result is a page-down per word. Most real text inputs in
      browsers (the address bar, in-page <input>/<textarea>,
      contenteditable divs) carry a non-empty ClassName:
      BraveOmniboxViewViews for Brave's address bar, gLFyf for the
      Google search box, Chrome_RenderWidgetHostHWND for
      contenteditable. The page body shape with an empty ClassName
      is the reliable HARD reject the trap targets. ControlType=
      EditControl is exempted from the trap (it falls through to the
      TextPattern accept or the EditControl accept below): when UIA
      reports EditControl from a browser process, the control is a
      real text input -- a chrome:// or brave:// settings field, an
      internal browser dialog, or a web <input> that Chromium's
      accessibility tree surfaces as EditControl rather than as the
      document body. The 2026-05-19 production trace falsified the
      assumption that EVERY browser text input carries a non-empty
      ClassName: Brave exposed a real EditControl with empty
      ClassName, and dictation of "testing beta" was silently dropped
      by the unconditioned trap until this exemption was added. The
      browser process list is config-extendable via
      [ui_actions.text_target].browser_process_names_extend.
      [wh-fc1x.1, wh-9weum Phase 1, wh-jldm0]
  7.  TextPattern available -> accept (text_pattern_available).
      TextPattern is the canonical "this control accepts text" signal
      across browsers, native Windows edit controls, and Tkinter Entry.
      TextPattern2 is not required.
  8.  ControlType == EditControl AND IsEnabled -> accept (edit_control).
      Native Windows edit controls report ControlType=EditControl even
      when UIA's TextPattern check momentarily fails (some custom UI
      toolkits expose EditControl without surfacing TextPattern). The
      enabled gate filters disabled edit fields. DocumentControl is
      intentionally NOT accepted here -- the browser-empty-ClassName
      page-body case is now caught by step 6, and DocumentControl in
      non-browser apps still requires TextPattern.
      [wh-9weum Phase 1, wh-ko176]
  9.  ClassName in allow_class_names -> accept (class_name_allowlist).
      Empty by default; populated only with real-world evidence.
  10. (process, class, control_type) in soft-allow set -> accept
      (accept_soft_allow_tuple). The user has previously approved this
      target via the three-strikes grant prompt and the tuple is now
      on the persistent soft-allow list. The router maps this reason
      to ClipboardOnlyStrategy so the approved target keeps getting a
      silent Ctrl+V paste, as it did during the override flow.
      [wh-9weum Phase 3, wh-soft-allow-verdict-tier]
  11. ClassName non-empty and not in allowlist or soft-allow set ->
      reject (default_reject_paste_capable_class). The router maps
      this reason to RejectedInsertionStrategy so the rejection toast
      fires with the Try-it-anyway button. Editors that render their
      own UI (Zed, Sublime, GPU-rendered editors) match this shape
      until the user clicks Try-it-anyway enough times to grant a
      soft-allow tuple. The wh-prio bug was that the router used to
      route this reason directly to ClipboardOnlyStrategy, which
      silently pasted and never surfaced the toast in production.
      [wh-9weum Phase 1, wh-wmrbl, wh-3ypov,
      wh-soft-allow-verdict-tier]
  12. Default -> reject (default_reject). Includes the empty-ClassName
      non-browser case (no process-list entry, nothing to soft-paste
      into).

ValuePattern alone is NOT an accept signal: read-only checkboxes,
sliders, and other non-text controls expose ValuePattern but cannot
receive dictation. ValuePattern presence is recorded in the verdict's
supported_patterns for telemetry only.

Returns a TextTargetVerdict so callers log telemetry consistently and
correlate rejections with the real-world apps that triggered them.

References: wh-zndq (no-text-input dictation routing), wh-32d (backspace
sent to non-text elements), wh-fc1x (text input target handling epic),
wh-ix1z (codex-review-loop round 1 design pass), wh-sm5s + wh-jbo9
(converged review epics), wh-9weum (Phase 1 implementation epic).
"""
from __future__ import annotations

import atexit
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import _ctypes
import uiautomation as auto

from shared.soft_allow_schema import parse_soft_allow_file
from utils.system import get_bundled_data_dir, get_user_data_dir

logger = logging.getLogger(__name__)


# wh-9weum Phase 3 (wh-ymhdq, wh-e22yg): location of the soft-allow tuple
# list. Resolved through utils.system.get_user_data_dir (wh-k8ef) --
# services/wheelhouse/data in a source checkout, the persistent app-data
# root under a frozen build. This must match the logic-process writer
# path in main.LogicController._resolve_soft_allow_path, which derives
# from the same helper, otherwise the input process reads from a
# different location than the logic process writes to and soft-allow
# entries do not survive restart (wh-9weum.4.4).
_DEFAULT_SOFT_ALLOW_PATH: Path = (
    get_user_data_dir() / "soft_allow_tuples.toml"
)

# wh-k535r: location of the starter approved-control list shipped with
# the codebase. Same schema as soft_allow_tuples.toml; the predicate
# merges the two files in memory. Read-only -- the writer at
# utils/soft_allow_writer.py only rewrites the user file, so starter
# entries cannot be clobbered by a user grant. A fresh install gets
# the starter list without copying anything; a user can still grant
# new tuples via the three-clicks-plus-Yes flow and those go to the
# user file. Shipped data resolves inside the frozen bundle
# (sys._MEIPASS) under PyInstaller, never the user data dir (wh-k8ef).
_DEFAULT_SOFT_ALLOW_STARTER_PATH: Path = (
    get_bundled_data_dir() / "soft_allow_starter_tuples.toml"
)


def _load_soft_allow_tuples(
    path: Path,
    *,
    caller: str = "soft_allow loader",
) -> frozenset[tuple[str, str, str]]:
    """Read a soft-allow file into a (process, class, control_type) set.

    Delegates schema validation to shared.soft_allow_schema.parse_soft_allow_file
    and projects each entry to the (process, class, control_type)
    identity triple the predicate uses for lookup. The ``added_at``
    field is part of the on-disk schema (the writer's read-modify-write
    cycle keeps it as the user's original approval timestamp) but is
    not part of the predicate's lookup key.

    log_skipped_entries=True surfaces manual-edit mistakes in the log
    so a user editing the file can see why an entry was dropped. The
    ``caller`` argument is woven into the parser's log messages so the
    user file's loader and the starter file's loader produce
    distinguishable warnings.
    """
    entries = parse_soft_allow_file(
        path,
        log_skipped_entries=True,
        caller=caller,
    )
    return frozenset(
        (e.process_name, e.class_name, e.control_type) for e in entries
    )


# Default ControlType denylist. Integer constants from uiautomation.
# Order of inclusion follows wh-zndq comment 4. Tuned for the
# eight recorded incidents (Brave document body, Notepad WinUI 3 menu,
# Explorer file icon, Tkinter non-Entry widget).
DEFAULT_DENYLIST_CONTROL_TYPES: frozenset[int] = frozenset({
    auto.ControlType.MenuItemControl,
    auto.ControlType.MenuControl,
    auto.ControlType.MenuBarControl,
    auto.ControlType.ListItemControl,
    auto.ControlType.TreeItemControl,
    auto.ControlType.ImageControl,
    auto.ControlType.HyperlinkControl,
    auto.ControlType.ButtonControl,
    auto.ControlType.CheckBoxControl,
    auto.ControlType.RadioButtonControl,
    auto.ControlType.TabItemControl,
    auto.ControlType.ToolBarControl,
    auto.ControlType.SplitButtonControl,
})

# Default ClassName denylist. WinUI 3 menu sub-items expose a distinct
# ClassName ("MenuFlyoutSubItem") in some Windows builds where the UIA
# ControlType collapses to a generic value, so the ClassName check fires
# before the ControlType check.
DEFAULT_DENYLIST_CLASS_NAMES: frozenset[str] = frozenset({
    "MenuFlyoutSubItem",
    "MenuFlyoutItem",
})

# Default ClassName allowlist. Empty by design. The Tkinter "TkChild"
# heterogeneity (wh-zndq comment 5) shows blanket class allowlists are
# unsafe -- the same class label covers Entry, Label, Frame, and Canvas
# in one process. Populate only with real-world evidence.
DEFAULT_ALLOWLIST_CLASS_NAMES: frozenset[str] = frozenset()


# Default browser-process list for the wh-zndq empty-ClassName trap.
# Lower-cased, normalized exe names. The trap fires when one of these
# processes is foreground AND focused_control.ClassName is empty AND
# TextPattern is missing -- the exact shape that surfaced wh-zndq.
# Independent from the same-process foreground-check list at
# strategies/specific.py:VerifiedUnicodeStrategy.DEFAULT_SAME_PROCESS_BROWSER_NAMES
# (wh-9weum Phase 1, wh-jldm0; review note wh-sm5s.4): the two lists
# answer different questions ("did the foreground HWND drift to a
# Chromium helper popup?" vs. "is this a browser body where Ctrl+V
# would deliver nothing useful?"), so they evolve separately.
DEFAULT_BROWSER_PROCESS_NAMES: frozenset[str] = frozenset({
    "brave.exe",
    "brave_beta.exe",
    "chrome.exe",
    "chromium.exe",
    "msedge.exe",
    "edge.exe",
    "firefox.exe",
})


@dataclass(frozen=True)
class TextTargetVerdict:
    """Outcome of the text-target predicate.

    verdict: True if the focused control accepts text input.
    reason: stable token for telemetry correlation. One of:

        Accept tokens:
          - text_pattern_available
          - edit_control                  (wh-9weum Phase 1, wh-ko176)
          - class_name_allowlist
          - accept_soft_allow_tuple
                The (process, class, control_type) triple is in the
                soft-allow set the user has approved via the
                three-strikes grant prompt. The router maps this
                reason to ClipboardOnlyStrategy for a silent paste.
                (wh-9weum Phase 3, wh-soft-allow-verdict-tier)

        Reject tokens (verdict=False):
          - no_focused_control
          - stale_com
          - not_focusable
          - denylist_class_name
          - denylist_control_type
          - default_reject_paste_capable_class
                Soft reject. The control has a coherent ClassName but
                no positive accept signal AND no soft-allow entry. The
                router maps this reason to RejectedInsertionStrategy
                so the rejection toast fires with the Try-it-anyway
                button. A user click on Try-it-anyway followed by
                three verified retries promotes the tuple to
                accept_soft_allow_tuple on next focus.
                (wh-9weum Phase 1, wh-3ypov, wh-wmrbl,
                wh-soft-allow-verdict-tier)
          - default_reject
                Hard reject. The control either has no useful identity
                (empty ClassName outside the browser process list) or
                matches the wh-zndq browser-empty-ClassName trap. No
                soft fallback applies.

    supported_patterns: tuple of pattern names actually present on the
        control at decision time. Empty if probing failed or the
        decision did not need to probe.
    control_type: ControlTypeName of the focused control, or "" if
        unreadable.
    class_name: ClassName of the focused control, or "" if unreadable.
    process_name: process_name from the captured UIContext (logged for
        telemetry correlation).
    """

    verdict: bool
    reason: str
    supported_patterns: tuple[str, ...] = field(default_factory=tuple)
    control_type: str = ""
    class_name: str = ""
    process_name: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.verdict


# wh-hl3s: in-memory latency histogram for the text-target check.
#
# Each call to TextTargetPredicate.evaluate records its wall-clock cost
# keyed on the returned reason token. The histogram is process-wide.
# Atexit emits one INFO log line per reason at shutdown so production
# log files carry real-app per-call timings -- the wh-mm39e (performance
# check on the accept path) work needs that for actual app-driven
# comparison instead of mock-only microbenchmarks.
#
# Memory is bounded by _RESERVOIR_SIZE per reason: count, sum, min, and
# max grow with call count but use constant memory; raw samples for
# percentile estimation use Vitter's algorithm R reservoir sampling, so
# a session that fires evaluate ten million times still holds only
# 1024 samples per reason. At seven reasons that is ~7000 floats, well
# under a megabyte.
#
# Thread safety: the input process's IPC handler thread mutates
# TextTargetPredicate via add_soft_allow but does not currently call
# evaluate. The lock guards the bucket against a future caller landing
# on evaluate from another thread.
#
# Per-call overhead: the timing wrapper adds two perf_counter calls,
# one uncontended lock acquire, a dict lookup, a few small arithmetic
# ops, and (once the reservoir is full) one random.randint call. On
# CPython 3.12 each of those is on the order of 100-300 nanoseconds
# in isolation; the total is typically a few microseconds. The
# in-production cost of evaluate itself is dominated by the Windows
# UI Automation round-trips (milliseconds), so the wrapper's cost is
# noise against that.

_RESERVOIR_SIZE = 1024


class _LatencyHistogram:
    """Per-reason latency tracker with bounded memory (wh-hl3s).

    Buckets carry running totals (count, sum, min, max) plus a
    fixed-size reservoir of raw samples for percentile estimation.
    Reservoir sampling is unbiased across the call stream regardless of
    total call count.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, dict] = {}
        self._lock = threading.Lock()

    def record(self, reason: str, elapsed_us: float) -> None:
        """Add one sample to the bucket named by ``reason``."""
        with self._lock:
            bucket = self._buckets.get(reason)
            if bucket is None:
                bucket = {
                    "count": 0,
                    "sum_us": 0.0,
                    "min_us": float("inf"),
                    "max_us": 0.0,
                    "reservoir": [],
                }
                self._buckets[reason] = bucket
            bucket["count"] += 1
            bucket["sum_us"] += elapsed_us
            if elapsed_us < bucket["min_us"]:
                bucket["min_us"] = elapsed_us
            if elapsed_us > bucket["max_us"]:
                bucket["max_us"] = elapsed_us
            res = bucket["reservoir"]
            if len(res) < _RESERVOIR_SIZE:
                res.append(elapsed_us)
            else:
                # Vitter's algorithm R: replace a uniformly random slot
                # with probability _RESERVOIR_SIZE / count, so every
                # sample in the call stream has equal probability of
                # surviving in the reservoir.
                j = random.randint(0, bucket["count"] - 1)
                if j < _RESERVOIR_SIZE:
                    res[j] = elapsed_us

    def snapshot(self) -> dict[str, dict]:
        """Return a dictionary of per-reason summaries with percentiles."""
        with self._lock:
            out: dict[str, dict] = {}
            for reason, bucket in self._buckets.items():
                count = bucket["count"]
                summary: dict = {
                    "count": count,
                    "mean_us": (
                        bucket["sum_us"] / count if count else 0.0
                    ),
                    "min_us": (
                        bucket["min_us"] if count else 0.0
                    ),
                    "max_us": bucket["max_us"],
                }
                samples = sorted(bucket["reservoir"])
                if samples:
                    summary["p50_us"] = _percentile(samples, 50)
                    summary["p95_us"] = _percentile(samples, 95)
                    summary["p99_us"] = _percentile(samples, 99)
                else:
                    summary["p50_us"] = 0.0
                    summary["p95_us"] = 0.0
                    summary["p99_us"] = 0.0
                out[reason] = summary
            return out

    def reset(self) -> None:
        """Clear every bucket. Tests use this to isolate state."""
        with self._lock:
            self._buckets.clear()


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Linear-interpolated percentile from a sorted sample list.

    Returns 0.0 for an empty list. q is the percentile expressed as a
    number between 0 and 100 -- p50_us uses q=50, p95_us uses q=95.

    Raises ValueError when ``q`` is outside the documented [0, 100]
    range. The three current call sites in ``_LatencyHistogram.snapshot``
    pass hardcoded constants, so the guard is latent today, but a future
    caller that accepts a configurable ``q`` would silently return wrong
    values for ``q < 0`` (Python's negative list indexing wraps to the
    end of the list rather than raising) or raise a cryptic IndexError
    for ``q > 100``. Validate explicitly to keep the docstring contract
    enforceable. (deepseek review wh-hl3s.2.1)
    """
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q must be between 0 and 100, got {q}")
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    idx = (q / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


_check_latency_histogram = _LatencyHistogram()


def _log_check_latency_summary() -> None:
    """Emit one INFO log line per reason at shutdown (wh-hl3s).

    Called via atexit. Emits a "summary at <iso8601>" header line so a
    log reader can attribute each summary block to a specific process
    run, followed by one line per reason naming the reason token, the
    call count, the mean, the 50/95/99 percentile estimates from the
    reservoir, and the running min and max. Skips reasons with zero
    calls so a short-lived process (a unit test, a CLI tool) does not
    flood the log with empty rows.

    The whole body is wrapped in a broad try/except so a teardown-time
    failure (the queue-based log listener already stopped, the log
    file handle already closed, a malformed format string from a
    future refactor) cannot raise out of an atexit handler. Python
    3.10+ prints atexit-handler tracebacks to stderr, which would
    show up in the launcher's child-process exit logs and look like
    a crash to anyone reading them.
    """
    try:
        snapshot = _check_latency_histogram.snapshot()
        if not snapshot:
            return
        # ISO 8601 with seconds resolution; no microseconds or timezone
        # suffix needed since the line is informational only and a
        # second-precision timestamp matches the rest of the project's
        # log format conventions.
        from datetime import datetime
        logger.info(
            "text-target check latency: summary at %s",
            datetime.now().isoformat(timespec="seconds"),
        )
        for reason, stats in sorted(snapshot.items()):
            if stats["count"] == 0:
                continue
            logger.info(
                "text-target check latency: reason=%s count=%d "
                "mean=%.1fus p50=%.1fus p95=%.1fus p99=%.1fus "
                "min=%.1fus max=%.1fus",
                reason,
                stats["count"],
                stats["mean_us"],
                stats["p50_us"],
                stats["p95_us"],
                stats["p99_us"],
                stats["min_us"],
                stats["max_us"],
            )
    except Exception:  # noqa: BLE001 -- atexit must not raise
        # Swallow every error. The summary is best-effort; a teardown
        # race cannot be allowed to surface as a fake crash.
        pass


atexit.register(_log_check_latency_summary)


class TextTargetPredicate:
    """Configurable text-target predicate.

    The router and any retract-path caller share an instance so the
    answer to "is this control a text input?" is consistent everywhere.

    The deny / allow lists are constructor-injected so the WheelHouse
    config can extend them without a code change. The
    build_predicate_from_config helper merges defaults with config
    extensions; tests construct the predicate directly with explicit
    lists.
    """

    def __init__(
        self,
        *,
        denylist_control_types: Iterable[int] = DEFAULT_DENYLIST_CONTROL_TYPES,
        denylist_class_names: Iterable[str] = DEFAULT_DENYLIST_CLASS_NAMES,
        allowlist_class_names: Iterable[str] = DEFAULT_ALLOWLIST_CLASS_NAMES,
        browser_process_names: Iterable[str] = DEFAULT_BROWSER_PROCESS_NAMES,
        soft_allow_path: Path | None = None,
        soft_allow_tuples: Iterable[tuple[str, str, str]] | None = None,
        soft_allow_starter_path: Path | None = None,
        soft_allow_starter_tuples: (
            Iterable[tuple[str, str, str]] | None
        ) = None,
    ) -> None:
        self._deny_types: frozenset[int] = frozenset(denylist_control_types)
        self._deny_classes: frozenset[str] = frozenset(denylist_class_names)
        self._allow_classes: frozenset[str] = frozenset(allowlist_class_names)
        # Browser process list is matched case-insensitively against the
        # captured UIContext process_name. Lower-case once at construction
        # so the per-call check is a plain frozenset lookup.
        self._browser_processes: frozenset[str] = frozenset(
            n.lower() for n in browser_process_names
        )

        # wh-9weum Phase 3 (wh-e22yg, wh-wjagd): soft-allow tuple set.
        # Tests pass tuples explicitly via ``soft_allow_tuples``; the
        # production path passes a path and lets the loader read it.
        # Explicit tuples take precedence so a test never accidentally
        # loads the on-disk file. The runtime mutator add_soft_allow
        # rebinds _soft_allow under _soft_allow_lock so the input
        # process's IPC handler thread can update the set safely.
        #
        # wh-k535r: the starter list is a second source of soft-allow
        # entries shipped with the codebase. Loaded the same way --
        # explicit tuples take precedence over the path -- and unioned
        # with the user set at init. The starter set is read-only; a
        # user grant via add_soft_allow extends the merged set, and the
        # writer only touches the user file, so starter entries
        # cannot be clobbered by a grant against the same triple.
        #
        # Initialise _soft_allow to the empty set immediately after the
        # lock so any caller that grabs the lock between construction
        # and the union below sees a valid attribute instead of
        # AttributeError. The current call sites do not race the
        # constructor (predicate registration happens after __init__
        # returns), but the bare-minimum invariant -- the lock and the
        # set are always paired -- protects future refactors that
        # publish the predicate mid-construction.
        self._soft_allow_lock = threading.Lock()
        self._soft_allow: frozenset[tuple[str, str, str]] = frozenset()
        if soft_allow_tuples is not None:
            user_set: frozenset[tuple[str, str, str]] = frozenset(
                soft_allow_tuples,
            )
        elif soft_allow_path is not None:
            user_set = _load_soft_allow_tuples(
                soft_allow_path, caller="soft_allow loader",
            )
        else:
            user_set = frozenset()

        if soft_allow_starter_tuples is not None:
            starter_set: frozenset[tuple[str, str, str]] = frozenset(
                soft_allow_starter_tuples,
            )
        elif soft_allow_starter_path is not None:
            starter_set = _load_soft_allow_tuples(
                soft_allow_starter_path,
                caller="soft_allow starter loader",
            )
        else:
            starter_set = frozenset()

        self._soft_allow = user_set | starter_set

    @property
    def soft_allow_tuples(self) -> frozenset[tuple[str, str, str]]:
        """Return the current soft-allow set (snapshot reference).

        The set is rebound (not mutated) when add_soft_allow is called,
        so callers may hold the returned frozenset across the rebind
        without seeing partial state.
        """
        return self._soft_allow

    def add_soft_allow(self, tuple_: tuple[str, str, str]) -> None:
        """Add a (process, class, control_type) triple to the in-memory set.

        Replaces the internal frozenset with a new one that includes
        ``tuple_``. The rebind under ``_soft_allow_lock`` is the unit of
        atomicity -- readers always observe either the old or the new
        set, never an in-progress mutation. Callers that need the change
        to survive restart must also persist the new tuple to disk via
        utils.soft_allow_writer.append_soft_allow_tuple; this method
        deliberately does not write to disk so the disk path stays
        owned by the Logic process.
        """
        with self._soft_allow_lock:
            self._soft_allow = self._soft_allow | {tuple_}

    def evaluate(
        self,
        focused_control,
        *,
        class_name: str = "",
        process_name: str = "",
    ) -> TextTargetVerdict:
        """Time the check and record the per-call cost (wh-hl3s).

        Thin timing wrapper around ``_evaluate_impl``. The real
        decision logic lives in ``_evaluate_impl``; this method exists
        so every call site (router, retract path, default_predicate
        convenience, tests) contributes to the process-wide latency
        histogram without each caller having to instrument itself.

        The histogram entry is keyed on the returned reason token so
        per-branch costs stay separable -- the text_pattern_available
        accept path is the expected cost driver in production, and a
        regression there should be visible against the soft-reject and
        denylist paths in the shutdown log line.
        """
        t0 = time.perf_counter()
        verdict: TextTargetVerdict | None = None
        try:
            verdict = self._evaluate_impl(
                focused_control,
                class_name=class_name,
                process_name=process_name,
            )
            return verdict
        finally:
            # Record the timing even if _evaluate_impl raised. The
            # pathological cases (a UIA read raising past the inner
            # except blocks, a future refactor that lets an
            # exception escape) are exactly what we want telemetry
            # on. The local reviewer flagged that without try/finally
            # the histogram would silently under-count those cases.
            elapsed_us = (time.perf_counter() - t0) * 1_000_000.0
            reason = verdict.reason if verdict is not None else "exception"
            try:
                _check_latency_histogram.record(reason, elapsed_us)
            except Exception:  # noqa: BLE001 -- telemetry must not affect evaluate
                # codex review wh-hl3s.1.1: telemetry is best-effort. If
                # record() raises (a future refactor introduces a bug,
                # a logger handler raises mid-call), the raise here in
                # finally would replace a successful verdict return or
                # mask the _evaluate_impl exception in flight. Swallow
                # any record() failure so the on-the-wire dictation path
                # always sees the real decision and the real exception.
                pass

    def _evaluate_impl(
        self,
        focused_control,
        *,
        class_name: str = "",
        process_name: str = "",
    ) -> TextTargetVerdict:
        """Decide whether ``focused_control`` accepts text input.

        Args:
            focused_control: The UIA control to evaluate. None means no
                control had focus at the call site.
            class_name: ClassName telemetry hint from the captured
                UIContext (logged into the verdict). Recorded in the
                verdict only when ``focused_control.ClassName`` is
                empty so the telemetry stays useful for processes whose
                focused control reports an empty class. Does NOT
                participate in the deny / allow check itself -- that
                check uses ``focused_control.ClassName`` exclusively to
                avoid letting a stale captured class name (the slow
                path may evaluate a freshly recaptured control whose
                ClassName legitimately differs from the captured
                context's) inherit an allowlist or denylist match
                meant for a different control (wh-ix1z.11).
            process_name: process_name from the captured UIContext.
                Recorded in the verdict for telemetry only.

        Returns:
            TextTargetVerdict. Caller decides whether to silently drop
            (router) or refuse the action (retract path).
        """
        if focused_control is None:
            return TextTargetVerdict(
                verdict=False, reason="no_focused_control",
                process_name=process_name,
            )

        # Read identity once. Each access can raise on a stale COM
        # element so we wrap the whole read.
        try:
            ctrl_type_int = int(focused_control.ControlType)
            ctrl_type_name = focused_control.ControlTypeName or ""
            # Use focused_control.ClassName exclusively for the deny /
            # allow check. The class_name parameter is a telemetry hint
            # ONLY -- the slow-path preflight passes a class_name from
            # the original capture, but the freshly recaptured control
            # may have a different (or empty) ClassName, and inheriting
            # the captured value would let the new control match an
            # allowlist or denylist meant for a different control.
            ctrl_class = focused_control.ClassName or ""
            telemetry_class = ctrl_class or class_name or ""
            is_focusable = bool(focused_control.IsKeyboardFocusable)
            # IsEnabled gates the wh-ko176 EditControl accept signal
            # below. Read it eagerly inside the same try so a stale-COM
            # failure here returns stale_com instead of risking a raise
            # mid-evaluate. Defaults to True on a control that does not
            # expose the property; UIA's IsEnabled is True by default,
            # so a missing attribute should not block the accept path.
            is_enabled = bool(getattr(focused_control, "IsEnabled", True))
        except (_ctypes.COMError, AttributeError, OSError) as e:
            logger.debug("text_target: stale control read failed: %s", e)
            return TextTargetVerdict(
                verdict=False, reason="stale_com",
                process_name=process_name,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("text_target: unexpected read failure: %s", e)
            return TextTargetVerdict(
                verdict=False, reason="stale_com",
                process_name=process_name,
            )

        if not is_focusable:
            return TextTargetVerdict(
                verdict=False, reason="not_focusable",
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )

        # ClassName denylist runs before ControlType denylist so
        # WinUI 3 controls that report a generic ControlType but a
        # specific class are caught. The denylist check uses ctrl_class
        # (focused_control.ClassName only) so a freshly recaptured
        # control with empty ClassName cannot inherit a captured class
        # name that the slow-path preflight passed in.
        if ctrl_class and ctrl_class in self._deny_classes:
            return TextTargetVerdict(
                verdict=False, reason="denylist_class_name",
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )

        if ctrl_type_int in self._deny_types:
            return TextTargetVerdict(
                verdict=False, reason="denylist_control_type",
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )

        # wh-fc1x.1: browser-process empty-ClassName HARD reject. Runs
        # BEFORE the TextPattern probe because Brave's page document
        # body exposes TextPattern (for screen-reader access) but typing
        # into it lands as page-level keystrokes -- the spacebar between
        # words triggers the browser's scroll-down accelerator instead
        # of inserting text. The trap was designed for the page document
        # body, which UIA reports as DocumentControl (or PaneControl on
        # some Chromium builds). EditControl is exempted: when UIA
        # reports ControlType=EditControl from a browser process, the
        # control is a real text input (a chrome:// or brave:// settings
        # field, an internal browser dialog, or a web <input> that
        # Chromium's accessibility tree surfaces as EditControl). The
        # observed shape in production was ControlType=EditControl +
        # empty ClassName + brave.exe, dictating "testing beta" into
        # what the user expected to be a text field; the trap was
        # hard-rejecting it as a page body. The EditControl exemption
        # falls through to the TextPattern accept branch (rule 7) or the
        # EditControl accept branch (rule 8), both of which produce a
        # correct accept verdict. Real text inputs in browsers with
        # non-empty ClassName (BraveOmniboxViewViews, gLFyf, Chrome_-
        # RenderWidgetHostHWND) also bypass the trap by failing the
        # ``not ctrl_class`` check.
        process_lower = (process_name or "").lower()
        if (
            process_lower
            and process_lower in self._browser_processes
            and not ctrl_class
            and ctrl_type_int != int(auto.ControlType.EditControl)
        ):
            # wh-9weum.5.1: pass ctrl_class (empty in the trap) on the
            # verdict, NOT telemetry_class. The trap's defining signal
            # is empty ClassName; if the slow-path preflight passed a
            # non-empty captured-context class_name, telemetry_class
            # would be non-empty and the GUI wording helper at
            # rejection_toast_wording.compose_rejection_wording would
            # see "default_reject + browser + non-empty class_name"
            # and fall through to the generic OTHER bucket instead of
            # the browser-trap wording. Use the actual ClassName here
            # so the GUI side picks the right user-facing message.
            verdict = TextTargetVerdict(
                verdict=False, reason="default_reject",
                supported_patterns=(),
                control_type=ctrl_type_name, class_name=ctrl_class,
                process_name=process_name,
            )
            return verdict

        # wh-ix1z.8: probe TextPattern first; on accept, return without
        # probing ValuePattern. The accept verdict is already settled and
        # ValuePattern is recorded for telemetry only (a future
        # diagnostic mode could re-enable the probe). Skipping the
        # second GetPattern call cuts roughly half the predicate's
        # UIA-call cost on the common accept path.
        if self._has_pattern(focused_control, auto.PatternId.TextPattern):
            verdict = TextTargetVerdict(
                verdict=True, reason="text_pattern_available",
                supported_patterns=("TextPattern",),
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )
            self._log_accept(verdict, process_name)
            return verdict

        # wh-9weum Phase 1 (wh-ko176): EditControl ControlType is a
        # reliable native-edit-control signal. Some custom toolkits
        # report ControlType=EditControl without surfacing TextPattern
        # at the moment we probe; without this branch those targets
        # would route to the soft fallback, which costs an extra
        # clipboard write per word. The IsEnabled gate filters disabled
        # text fields. DocumentControl is intentionally NOT accepted
        # here -- per review wh-sm5s.5, the wh-zndq browser body case
        # ships as DocumentControl + empty ClassName + no TextPattern,
        # and accepting DocumentControl alone would re-introduce that
        # bug. DocumentControl stays gated behind TextPattern.
        if (
            ctrl_type_int == int(auto.ControlType.EditControl)
            and is_enabled
        ):
            verdict = TextTargetVerdict(
                verdict=True, reason="edit_control",
                supported_patterns=(),
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )
            self._log_accept(verdict, process_name)
            return verdict

        # Allowlist match uses ctrl_class (focused_control.ClassName
        # only) for the same reason as denylist: prevent stale captured
        # class names from inheriting an allowlist match.
        if ctrl_class and ctrl_class in self._allow_classes:
            verdict = TextTargetVerdict(
                verdict=True, reason="class_name_allowlist",
                supported_patterns=(),
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )
            self._log_accept(verdict, process_name)
            return verdict

        # Reject path: probe ValuePattern for telemetry detail. The
        # rejection telemetry is the only place the supported_patterns
        # field is informative (it tells us whether ValuePattern was
        # present on a rejected control, which guides the
        # evidence-driven allowlist decisions described in wh-zndq
        # comment 4 step 5).
        supported: tuple[str, ...] = ()
        if self._has_pattern(focused_control, auto.PatternId.ValuePattern):
            supported = ("ValuePattern",)

        # wh-fc1x.1 hoisted the browser-empty-ClassName trap to fire
        # BEFORE the TextPattern probe (see the block above), so by the
        # time control reaches here the empty-ClassName case has already
        # rejected. The soft-reject below applies only to non-browser
        # empty-ClassName focus and to non-empty-ClassName cases in any
        # process.

        # wh-9weum Phase 3 + wh-soft-allow-verdict-tier: soft-allow
        # accept tier. A (process, class, control_type) triple already
        # approved by the user (via the three-strikes grant prompt that
        # writes soft_allow_tuples.toml) accepts here with the dedicated
        # reason ``accept_soft_allow_tuple``. The router branches on the
        # reason to route to ClipboardOnlyStrategy, which silently
        # pastes via Ctrl+V -- the user already opted in to that
        # behaviour for this target, so the rejection toast must not
        # fire again. Unknown tuples fall through to the soft-reject
        # branch below.
        soft_allow = self._soft_allow
        if ctrl_class and (
            (process_name, ctrl_class, ctrl_type_name) in soft_allow
        ):
            verdict = TextTargetVerdict(
                verdict=True,
                reason="accept_soft_allow_tuple",
                supported_patterns=supported,
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )
            self._log_accept(verdict, process_name)
            return verdict

        # wh-9weum Phase 1 (wh-wmrbl, wh-3ypov) + wh-soft-allow-verdict-
        # tier: soft reject for unknown paste-capable classes. Non-empty
        # ClassName means the control has a coherent identity, but
        # without TextPattern, EditControl, or an explicit allowlist or
        # soft-allow entry the predicate cannot vouch for it. The router
        # maps this reason to RejectedInsertionStrategy so the rejection
        # toast fires with the Try-it-anyway button. The override flow
        # (three verified retries plus a Yes click) promotes the tuple
        # to accept_soft_allow_tuple, after which it takes the silent
        # paste path above. Empty-ClassName cases (other than the
        # browser trap) fall through to the default_reject hard reject
        # because there is no stable identity to soft-paste into.
        if ctrl_class:
            return TextTargetVerdict(
                verdict=False,
                reason="default_reject_paste_capable_class",
                supported_patterns=supported,
                control_type=ctrl_type_name, class_name=telemetry_class,
                process_name=process_name,
            )

        return TextTargetVerdict(
            verdict=False, reason="default_reject",
            supported_patterns=supported,
            control_type=ctrl_type_name, class_name=telemetry_class,
            process_name=process_name,
        )

    def _log_accept(
        self, verdict: TextTargetVerdict, process_name: str,
    ) -> None:
        """Telemetry log for accept verdicts (wh-fc1x.1).

        Routine dictation accepts are noisy at INFO, so the default
        level is DEBUG. Browser-process accepts are escalated to INFO
        because the open question for wh-fc1x.1 is which accept reason
        fires for Brave / Chrome / Edge focus on non-input elements
        (the existing predicate has no accept-side log, so investigators
        cannot tell from current production logs whether a browser
        dictation hit text_pattern_available, edit_control, or
        class_name_allowlist).
        """
        process_lower = (process_name or "").lower()
        is_browser = (
            process_lower != ""
            and process_lower in self._browser_processes
        )
        log_level = logging.INFO if is_browser else logging.DEBUG
        logger.log(
            log_level,
            "text_target: accepted -- reason=%s control_type=%s "
            "class=%s process=%s patterns=%s",
            verdict.reason,
            verdict.control_type or "?",
            verdict.class_name or "?",
            verdict.process_name or "?",
            ",".join(verdict.supported_patterns) if verdict.supported_patterns else "-",
        )

    @staticmethod
    def _has_pattern(focused_control, pattern_id: int) -> bool:
        """Single UIA pattern probe; returns True if the pattern is present.

        Defensive against COM/OS exceptions: any failure returns False
        (treat as "pattern not available") so the caller falls through
        to the reject path rather than raising.
        """
        try:
            return focused_control.GetPattern(pattern_id) is not None
        except (_ctypes.COMError, AttributeError, OSError):
            return False
        except Exception:  # pragma: no cover - defensive
            return False


# Module-level default instance for callers that do not need a
# customised predicate. Tests and the router build their own instances
# explicitly so the config-driven extensions stay testable.
#
# wh-9weum.5.2: pass soft_allow_path so the default predicate's
# soft-allow set is loaded from the production data file rather than
# starting empty. is_text_target() and any future caller that picks up
# default_predicate now sees the same approved tuples the router does.
# Loading happens lazily on first evaluate via the loader's missing-file
# tolerance; no eager I/O at import time.
def _make_default_predicate() -> "TextTargetPredicate":
    """Construct the module-level default predicate from the production paths.

    Factored out (wh-k535r.1.1) so tests can monkeypatch
    _DEFAULT_SOFT_ALLOW_PATH and _DEFAULT_SOFT_ALLOW_STARTER_PATH then
    re-invoke this helper, proving the default-construction path
    actually reads starter entries -- not just that it does not raise
    when the production starter file is empty.
    """
    return TextTargetPredicate(
        soft_allow_path=_DEFAULT_SOFT_ALLOW_PATH,
        soft_allow_starter_path=_DEFAULT_SOFT_ALLOW_STARTER_PATH,
    )


default_predicate = _make_default_predicate()


def is_text_target(
    focused_control,
    *,
    class_name: str = "",
    process_name: str = "",
) -> TextTargetVerdict:
    """Convenience wrapper over the module-level default predicate."""
    return default_predicate.evaluate(
        focused_control, class_name=class_name, process_name=process_name,
    )


def build_predicate_from_config(config: dict) -> TextTargetPredicate:
    """Build a TextTargetPredicate honoring config.toml extensions.

    Reads ``[ui_actions.text_target]`` from the loaded config dict (the
    older ``[ui.text_target]`` path is also accepted for forward
    compatibility with any test fixtures that used it). Each list
    EXTENDS the hardcoded default; entries cannot be removed via config.
    Removing a default entry requires a code change so the safety
    baseline cannot be loosened by accident.

    Config schema (all optional):

        [ui_actions.text_target]
        deny_control_types_extend = ["MenuItemControl", ...]
        deny_class_names_extend = ["MyCustomMenu", ...]
        allow_class_names_extend = ["MyCustomEdit", ...]
        browser_process_names_extend = ["arc.exe", ...]

    Unknown ControlType names log a warning and are skipped.

    The browser_process_names_extend list adds entries to the wh-zndq
    empty-ClassName trap (wh-9weum Phase 1, wh-jldm0). It is INDEPENDENT
    from the same-process foreground-check list at
    [ui_actions.foreground_check].same_process_browser_names_extend --
    extending one does NOT extend the other (review wh-sm5s.4).
    """
    config = config or {}
    # Prefer [ui_actions.text_target]; fall back to [ui.text_target] so
    # earlier test fixtures keep working without churn.
    section = (
        config.get("ui_actions", {}).get("text_target")
        or config.get("ui", {}).get("text_target")
        or {}
    )

    # wh-ix1z.10: validate each extend list's type. A bare string (a
    # common TOML typo) would otherwise iterate per character and add
    # one-character entries to the lists. Bad values log a warning and
    # are ignored; the default baseline is unchanged.
    deny_types_names = _coerce_string_list(
        section.get("deny_control_types_extend"),
        field_name="deny_control_types_extend",
    )
    deny_classes_extend = _coerce_string_list(
        section.get("deny_class_names_extend"),
        field_name="deny_class_names_extend",
    )
    allow_classes_extend = _coerce_string_list(
        section.get("allow_class_names_extend"),
        field_name="allow_class_names_extend",
    )
    browser_processes_extend = _coerce_string_list(
        section.get("browser_process_names_extend"),
        field_name="browser_process_names_extend",
    )

    deny_types: list[int] = list(DEFAULT_DENYLIST_CONTROL_TYPES)
    for name in deny_types_names:
        const = getattr(auto.ControlType, name, None)
        if const is None:
            logger.warning(
                "text_target config: unknown ControlType '%s' -- skipped",
                name,
            )
            continue
        deny_types.append(int(const))

    deny_classes: list[str] = list(DEFAULT_DENYLIST_CLASS_NAMES)
    deny_classes.extend(deny_classes_extend)

    allow_classes: list[str] = list(DEFAULT_ALLOWLIST_CLASS_NAMES)
    allow_classes.extend(allow_classes_extend)

    browser_processes: list[str] = list(DEFAULT_BROWSER_PROCESS_NAMES)
    browser_processes.extend(browser_processes_extend)

    return TextTargetPredicate(
        denylist_control_types=deny_types,
        denylist_class_names=deny_classes,
        allowlist_class_names=allow_classes,
        browser_process_names=browser_processes,
        soft_allow_path=_DEFAULT_SOFT_ALLOW_PATH,
        soft_allow_starter_path=_DEFAULT_SOFT_ALLOW_STARTER_PATH,
    )


def _coerce_string_list(value, *, field_name: str) -> list[str]:
    """Coerce a config value into a list of strings, warning on type errors.

    Accepts None (returns []), list, or tuple. Anything else logs a
    warning and returns []. Non-string entries inside a list log a
    warning and are skipped. Used by build_predicate_from_config to
    fail safe on malformed TOML rather than silently iterating per
    character on a bare string.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                logger.warning(
                    "text_target config: %s contains non-string entry %r "
                    "-- skipped",
                    field_name, entry,
                )
                continue
            out.append(entry)
        return out
    logger.warning(
        "text_target config: %s must be a list of strings, got %s -- ignoring",
        field_name, type(value).__name__,
    )
    return []
