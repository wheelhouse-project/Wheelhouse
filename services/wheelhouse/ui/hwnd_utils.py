"""Win32 HWND comparison helpers for foreground/focus checks.

Module exists so insertion (verified_paste) and retraction (retract focus
verification) compare HWNDs through the same normalization function.
Without normalization, Chromium and Electron applications produce false
mismatches: the UIA-captured ``GetTopLevelControl().NativeWindowHandle``
can be a renderer child window (e.g. ``Chrome_RenderWidgetHostHWND``)
while ``win32gui.GetForegroundWindow()`` returns the actual top-level
OS window (``Chrome_WidgetWin_1``). Comparing those two without
``GetAncestor(GA_ROOT)`` always returns False, so a successful paste is
classified as a focus drift (wh-oe7u.3).
"""
import ctypes
import logging
import secrets
import threading
from ctypes import wintypes
from typing import Iterable, Optional

import psutil
import win32gui
import win32process

logger = logging.getLogger(__name__)

# win32con.GA_ROOT == 2 -- inlined here so this module has no transitive
# dependency on win32con just for one constant. GWL_EXSTYLE and
# WS_EX_TOOLWINDOW are inlined for the same reason.
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

# wh-ensure-focused-same-process-fallback.1.12: pywin32's win32gui does
# not expose SetProp/GetProp, so the provenance helpers below go through
# ctypes. Explicit argtypes/restype keep the calls 64-bit-safe: HANDLE
# and HWND are pointer-sized, and leaving them to ctypes' default int
# marshalling would truncate above 2**32.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
_user32.SetPropW.restype = wintypes.BOOL
_user32.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
_user32.GetPropW.restype = wintypes.HANDLE

# The single property name bounds the global-atom cost: SetProp interns
# one atom per distinct string, and destroyed windows that were never
# RemoveProp'd leak only references on that one atom, never new atoms.
# WheelHouse does not own the tagged windows, so it cannot RemoveProp
# before their destruction; the one permanently-interned atom is the
# accepted cost.
_PROVENANCE_PROP_NAME = "WheelHouse.RejectionTargetTag"

# Markers start at 1 so 0 always means "no provenance recorded".
#
# .1.24 (codex round 15): a lock-guarded integer, not
# itertools.count. The index is consumed only AFTER SetPropW succeeds
# -- with an unconditional next() a failed write burned an index, and
# writes fail in production (UIPI blocks SetProp against an elevated
# window, and every rejection at such a window tries to tag it), so a
# stream of failing writes could drain all 2**20 indexes without
# tagging anything, permanently refusing provenance for healthy
# targets until the process restarted. The lock makes
# read-check-allocate-write atomic across threads: next() was atomic
# on its own, but check-then-increment around a write is not.
_provenance_lock = threading.Lock()
_next_marker_index = 1

# wh-ensure-focused-same-process-fallback.1.21 (codex round 13): a
# window property survives the exit of the process that set it -- it
# lives on the window object and only RemoveProp or window destruction
# clears it. WheelHouse cannot RemoveProp windows it does not own, so
# after an Input-process restart the counter above restarts at 1 while
# live windows still carry the OLD run's markers. A bare counter would
# then hand a new window the same value a survivor already carries,
# and the aggregation gate's marker-equality proof
# (RejectedInsertionStrategy._emit_rejection_event) would merge text
# dictated at two DIFFERENT windows. The per-run random salt makes
# that collision improbable rather than impossible (.1.23): 43 random
# salt bits above a 20-bit counter field partition the value space
# into per-run bands that are disjoint unless two runs draw the SAME
# salt, so the cross-run collision chance -- including a survivor
# against a survivor from a different old run -- is about 2**-43 per
# pair of runs. That residual is accepted.
#
# .1.22 (codex round 14): the bands are disjoint only while the index
# stays inside its 20-bit field. The original .1.21 arithmetic added
# an UNBOUNDED counter, so a run that tagged more than 2**20 - 1
# windows spilled into the adjacent band and could exactly equal
# another run's marker (salt 1<<20 at index (1<<20)+1 == salt 2<<20
# at index 1) -- and the residual documented here at the time
# (2**-23) was wrong in both directions. tag_hwnd_provenance now
# refuses once the index leaves the field, returning 0 into the
# existing fail-closed no-provenance path, so a spilled value never
# exists and the 2**-43 equal-salt bound holds unconditionally.
# Survivor markers stay usable as identity: tag_hwnd_provenance
# returns them unchanged, and equality checks only ever need
# distinctness between different windows, which unique-within-run
# indexes plus salted disjoint bands provide. The value stays below
# 2**63, inside HANDLE range on 64-bit Windows.
_MARKER_INDEX_BITS = 20
_MARKER_INDEX_LIMIT = 1 << _MARKER_INDEX_BITS
_RUN_SALT = secrets.randbits(43) << _MARKER_INDEX_BITS


def tag_hwnd_provenance(hwnd: Optional[int]) -> int:
    """Mark ``hwnd``'s window OBJECT and return the marker (0 on failure).

    wh-ensure-focused-same-process-fallback.1.12: every other retry
    guard compares numeric handle values (GA_ROOT normalization, PID,
    the .1.7 root snapshot), so Windows reusing a numeric handle for a
    NEW same-PID top-level window that is its own GA_ROOT aliases them
    all. A window property is stored on the window object and dies
    with it, so ``read_hwnd_provenance`` on a recycled handle returns
    0 -- the one probe a SAME-RUN recycle cannot alias. (Cross-run, a
    recycled window may still carry a survivor marker from an earlier
    Input-process run; that equals a fresh marker only on matching
    43-bit salts, about 2**-43 per pair of runs -- the accepted
    residual at ``_RUN_SALT``. UIA RuntimeId cannot do this job at
    all: for HWND-backed elements it is ``[42, hwnd]``, derived from
    the very handle value that recycles.)

    If the window already carries a marker -- from an earlier
    rejection in this process run, or surviving from a previous run
    (window properties outlive the process that set them) -- that
    marker is returned unchanged rather than overwritten, so two live
    cache entries against the same window cannot invalidate each
    other; the existing property proves object identity just as well
    as a fresh one. Fresh markers carry the per-run ``_RUN_SALT``
    (.1.21), so a fresh tag equals a survivor from another run only
    if the two runs drew the same 43-bit salt (about 2**-43 per pair
    of runs, .1.23 -- an accepted residual) and marker equality keeps
    meaning "same window object" across Input-process restarts.

    Returns 0 when the handle is falsy, the property cannot be read,
    the run's 20-bit marker-index space is exhausted (.1.22 -- a
    value past the field would spill into another run's salt band),
    or SetProp fails -- SetProp fails against a destroyed handle and
    against a higher-integrity window (UIPI blocks the write).
    Callers treat 0 as "no provenance recorded" and the retry handler
    refuses such entries (fail closed, same contract as the .1.8
    root gate).
    """
    global _next_marker_index
    if not hwnd:
        return 0
    try:
        with _provenance_lock:
            existing = _user32.GetPropW(hwnd, _PROVENANCE_PROP_NAME)
            if existing:
                return int(existing)
            marker_index = _next_marker_index
            if marker_index >= _MARKER_INDEX_LIMIT:
                logger.debug(
                    "tag_hwnd_provenance: per-run marker space "
                    "exhausted (index=%d); refusing to tag hwnd=%s so "
                    "the value cannot spill into another run's salt "
                    "band", marker_index, hwnd,
                )
                return 0
            marker = _RUN_SALT + marker_index
            if not _user32.SetPropW(hwnd, _PROVENANCE_PROP_NAME, marker):
                logger.debug(
                    "tag_hwnd_provenance: SetPropW(%s) failed "
                    "(destroyed handle or UIPI-protected window); "
                    "index %d stays unconsumed", hwnd, marker_index,
                )
                return 0
            _next_marker_index = marker_index + 1
        return marker
    except Exception as e:
        logger.debug("tag_hwnd_provenance(%s) failed: %s", hwnd, e)
        return 0


def read_hwnd_provenance(hwnd: Optional[int]) -> int:
    """Return ``hwnd``'s provenance marker, or 0 when it carries none.

    0 covers every non-match case: falsy handle, GetProp failure, a
    window that was never tagged, and -- the case the mechanism
    exists for -- a RECYCLED numeric handle whose new window object
    never carried the property. Callers compare the result against
    the marker stored at rejection time and refuse on any mismatch.
    """
    if not hwnd:
        return 0
    try:
        value = _user32.GetPropW(hwnd, _PROVENANCE_PROP_NAME)
    except Exception as e:
        logger.debug("read_hwnd_provenance(%s) failed: %s", hwnd, e)
        return 0
    return int(value) if value else 0


# wh-fc1x.2: hardcoded fallback for the same-process foreground-check
# browser list. The CANONICAL list lives in
# services/wheelhouse/config.toml under
# [ui_actions.foreground_check].same_process_browser_names so users can
# add or remove browsers without editing Python. This frozenset only
# fires when the config key is missing entirely (e.g. an older
# config.toml predating wh-fc1x.2, or a programmatic test that passes a
# minimal config dict).
#
# Both VerifiedUnicodeStrategy
# (services/wheelhouse/ui/strategies/specific.py) and
# ClipboardOperations.verified_paste
# (services/wheelhouse/ui/clipboard_operations.py) consume the resolved
# list. Both code paths ask the same question: did the foreground HWND
# drift to a Chromium helper popup or sibling top-level inside the same
# browser process? When the answer is yes, the keystrokes still land in
# the focused renderer of the main HWND, so a strict GA_ROOT mismatch
# is a false-positive failure. The relaxation is opt-in to known
# browser exe names only; other apps (Word, Outlook, Visual Studio)
# keep the strict GA_ROOT contract because their multi-top-level shapes
# usually mean a paste was misdirected.
#
# Independent from text_target.DEFAULT_BROWSER_PROCESS_NAMES per review
# wh-sm5s.4 (do not reuse foreground-check browser list for text
# targeting): the two lists answer different questions and evolve
# separately.
_FALLBACK_SAME_PROCESS_BROWSER_NAMES: frozenset[str] = frozenset({
    "brave.exe",
    "brave_beta.exe",
    "chrome.exe",
    "chromium.exe",
    "msedge.exe",
    "edge.exe",
    "vivaldi.exe",
    "opera.exe",
    "operagx.exe",
    "arc.exe",
})


def coerce_browser_name_list(value, *, key_name: str) -> list[str]:
    """Validate a list-of-strings config value for the foreground-check.

    Mirrors the wh-ix1z.10 type-validation pattern in text_target's
    build_predicate_from_config: malformed config types log a warning
    and yield an empty list rather than iterating per character or
    silently misbehaving. Non-string entries inside a list are skipped
    with a warning. ``key_name`` is the config key name used in the
    warning message so the operator can tell which entry was rejected.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                logger.warning(
                    "foreground_check config: %s contains non-string "
                    "entry %r -- skipped",
                    key_name, entry,
                )
                continue
            out.append(entry)
        return out
    logger.warning(
        "foreground_check config: %s must be a list of strings, got %s "
        "-- ignoring",
        key_name, type(value).__name__,
    )
    return []


def resolve_same_process_browser_names(config: dict) -> frozenset[str]:
    """Resolve the foreground-check same-process browser list from config.

    Reads two config keys under ``[ui_actions.foreground_check]``:

    - ``same_process_browser_names`` -- the canonical full list. Edit
      this in services/wheelhouse/config.toml to add or remove browsers
      without changing code.
    - ``same_process_browser_names_extend`` -- backward-compat key from
      wh-3nwy. Entries ADD to the canonical list.

    When ``same_process_browser_names`` is missing entirely (older
    config files predating wh-fc1x.2, or test fixtures that pass a
    minimal config dict), falls back to
    ``_FALLBACK_SAME_PROCESS_BROWSER_NAMES`` and logs a debug message
    so the operator can tell the safety baseline kicked in.

    Returns a lower-cased frozenset.
    """
    section = (
        config.get("ui_actions", {})
        .get("foreground_check", {})
    )
    raw_canonical = section.get("same_process_browser_names")
    if raw_canonical is None:
        canonical: Iterable[str] = _FALLBACK_SAME_PROCESS_BROWSER_NAMES
        logger.debug(
            "foreground_check config: same_process_browser_names missing "
            "-- using hardcoded fallback (%d entries)",
            len(_FALLBACK_SAME_PROCESS_BROWSER_NAMES),
        )
    else:
        canonical = coerce_browser_name_list(
            raw_canonical, key_name="same_process_browser_names",
        )
    extension = coerce_browser_name_list(
        section.get("same_process_browser_names_extend", []),
        key_name="same_process_browser_names_extend",
    )
    return frozenset(
        n.lower() for n in (*canonical, *extension)
    )


def normalize_hwnd_for_foreground_compare(hwnd: Optional[int]) -> Optional[int]:
    """Return the root-normalized HWND for foreground/focus comparison.

    Returns the result of ``GetAncestor(hwnd, GA_ROOT)`` or ``None`` on
    any failure (zero/None input, GetAncestor exception, GetAncestor
    returns 0). Callers MUST treat ``None`` as "cannot compare" and
    fail closed -- crediting a paste or proceeding with a retract on
    None would silently bypass the focus-drift gate the helper exists
    to enforce (wh-oe7u.3).

    The helper is symmetric: callers normalize BOTH the captured/expected
    HWND (e.g. UIA ``NativeWindowHandle``) AND the observed HWND
    (e.g. ``GetForegroundWindow()``) before comparing. Comparing one
    normalized value to one raw value would re-create the Chromium
    child-vs-root mismatch the helper exists to remove.
    """
    if not hwnd:
        return None
    try:
        root = win32gui.GetAncestor(hwnd, GA_ROOT)
    except Exception as e:
        logger.warning("GetAncestor(%s, GA_ROOT) failed: %s", hwnd, e)
        return None
    if not root:
        # wh-captured-target-window-lost Step 1: this return was silent,
        # so a production log could not distinguish it from the other
        # None paths. GetAncestor returns 0 for a destroyed handle --
        # the suspected Brave invisible-helper case. DEBUG on purpose:
        # a diagnostic, not a failure; the caller reports the refusal
        # at its own level.
        logger.debug(
            "normalize_hwnd_for_foreground_compare: GetAncestor(%s, "
            "GA_ROOT) returned 0 (window likely destroyed)", hwnd,
        )
        return None
    return int(root)


def top_level_hwnd_from_control(focused_control) -> Optional[int]:
    """Return the RAW top-level HWND of a UIA control, or None.

    Returns ``None`` when there is no control, when the COM call raises,
    when there is no top-level control, or when the handle is zero.

    The result is NOT normalized. Every caller that compares the handle
    with a foreground handle must pass it through
    ``normalize_hwnd_for_foreground_compare`` first (wh-oe7u.3), and each
    caller does that in its own module so a test can patch the
    normalization at the call site it is exercising.

    wh-review-pattern-fixes.45: extracted so
    ``ui.strategies.specific._hwnd_from_control`` and
    ``ui.ui_action_handler._captured_hwnd_from_control`` read the handle
    out of a control through one piece of code. The selection paths and
    the strategy paths must agree on what "the target window" means, or
    the pre-send proof compares two different identities.
    """
    if not focused_control:
        # wh-captured-target-window-lost Step 1: name each None path so
        # the log can say which one produced the None behind a
        # "could not be resolved to an HWND" refusal. DEBUG on purpose;
        # only the None returns log, never the per-word success path.
        logger.debug("top_level_hwnd_from_control: no control")
        return None
    try:
        top = focused_control.GetTopLevelControl()
        hwnd = top.NativeWindowHandle if top else None
    except Exception as e:
        logger.debug("Could not resolve target HWND from control: %s", e)
        return None
    if top is None:
        logger.debug(
            "top_level_hwnd_from_control: GetTopLevelControl returned "
            "nothing (control likely stale)",
        )
        return None
    if not hwnd:
        logger.debug(
            "top_level_hwnd_from_control: NativeWindowHandle was 0 on "
            "the top-level control",
        )
        return None
    return int(hwnd)


def hwnd_is_invisible_or_toolwindow(hwnd: Optional[int]) -> bool:
    """True when ``hwnd`` is invisible or carries WS_EX_TOOLWINDOW.

    wh-ensure-focused-same-process-fallback.1.1: the same-process
    foreground fallback in ``WindowFocusManager._foreground_matches``
    may credit only a target shaped like the incident's invisible
    Chromium helper (Chrome_WidgetWin_0: invisible, WS_EX_TOOLWINDOW,
    its own GA_ROOT). Without this gate the fallback also credited the
    two-browser-windows sibling shape -- target A and foreground B are
    both visible main frames of the same browser process -- and the
    keystrokes landed in the wrong window.

    wh-ensure-focused-same-process-fallback.1.28: also the shape probe
    behind the pair guard inside ``same_process_fallback_matches``,
    which refuses a same-process credit when NEITHER handle has the
    helper shape.

    Fail closed: a falsy hwnd or any probe failure answers False, which
    callers treat as "visible main window" and stay strict.

    wh-ensure-focused-same-process-fallback.1.31 (codex round 20): a
    DESTROYED window also answers False from IsWindowVisible -- without
    raising -- and used to read as "invisible helper" here. That was
    safe while a PID probe always ran after this one, but since .1.28
    this probe is the LAST gate in ``same_process_fallback_matches``
    and the sole recheck at the end of
    ``WindowFocusManager._foreground_matches``: nothing after it
    refuses a dead handle. An invisible answer therefore confirms the
    handle still names a window via ``IsWindow`` before it counts as
    the helper shape. A handle recycled between the two probes can
    still slip through; that is the same single-probe residual the
    .1.5 ordering accepts, and ``same_process_fallback_matches``
    additionally revalidates both PIDs after an eligible answer.
    """
    if not hwnd:
        return False
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return bool(win32gui.IsWindow(hwnd))
        ex_style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        return bool(ex_style & WS_EX_TOOLWINDOW)
    except Exception as e:
        logger.debug(
            "hwnd_is_invisible_or_toolwindow(%s) probe failed: %s",
            hwnd, e,
        )
        return False


def hwnd_no_longer_exists(hwnd: Optional[int]) -> bool:
    """True only when ``hwnd`` is proven to name no window any more.

    wh-paste-target-window-vanished: the one question the paste path
    could not ask. ``normalize_hwnd_for_foreground_compare`` answers
    None for a destroyed handle, but it answers None for three other
    reasons as well -- a falsy handle, GetAncestor returning 0, and any
    other exception -- so None cannot decide whether the window is gone
    or the comparison merely failed. ``IsWindow`` answers that one
    question directly.

    Fail closed, in the direction that keeps today's behaviour: a falsy
    handle or any probe failure answers False, meaning "not proven
    gone". The caller then refuses its recovery and the word fails as
    it did before. Windows also recycles handle numbers, so ``IsWindow``
    can answer True for a different window that took the number; that
    too reads as "not proven gone" and refuses.
    """
    if not hwnd:
        return False
    try:
        return not win32gui.IsWindow(hwnd)
    except Exception as e:
        logger.debug(
            "hwnd_no_longer_exists(%s) probe failed: %s", hwnd, e,
        )
        return False


def hwnds_match_for_foreground_compare(
    expected_hwnd: Optional[int],
    observed_hwnd: Optional[int],
    *,
    allow_same_process: bool = False,
    expected_process_name: Optional[str] = None,
    expected_pid_snapshot: Optional[int] = None,
) -> bool:
    """Pairwise foreground-HWND comparison with optional same-process fallback.

    Tries `GA_ROOT` equality first via `normalize_hwnd_for_foreground_compare`.
    Same-root match returns True. If either side fails to normalize,
    returns False (fail closed).

    When ``allow_same_process=True``, a different-root pair can still
    match if both HWNDs belong to the same process (compared via
    `GetWindowThreadProcessId`). The same-process fallback handles the
    wh-3nwy Chromium case: Brave's autocomplete / autofill / spellcheck
    helper windows are top-level surfaces that own foreground briefly
    during a paste; the OS keyboard focus stays on the main Brave
    HWND, the synthesized keystrokes still route to the focused
    renderer, and the text lands. Without the same-process fallback,
    the strict GA_ROOT compare returns False even though the paste
    succeeded.

    The fallback is fail-closed in every uncertain case:
    - GetWindowThreadProcessId raises -> False.
    - Either PID returns 0 -> False.
    - PIDs differ -> False.
    - Neither handle has the transient-helper shape (invisible or
      WS_EX_TOOLWINDOW) -> False; two visible plain same-process
      windows are the wrong-window sibling pair
      (wh-ensure-focused-same-process-fallback.1.28, enforced inside
      ``same_process_fallback_matches``).
    - Either handle died or changed process identity between the
      identity probes and the credit -> False
      (wh-ensure-focused-same-process-fallback.1.31: a dead handle
      reads as not-helper, and both PIDs are re-probed after an
      eligible shape answer).

    Optional ``expected_process_name`` (case-insensitive) constrains
    the fallback to a specific exe name. When set, the matched PID's
    process name (psutil.Process(pid).name()) must equal the value.
    Use this to scope the relaxation to known browsers (e.g.
    "brave.exe") so unrelated apps cannot accidentally use the
    same-process path.

    Optional ``expected_pid_snapshot`` is the PID the caller resolved
    ``expected_process_name`` from (one ``process_identity_for_hwnd``
    sample). The fallback refuses when its own first sample of
    ``expected_hwnd`` differs -- see the .1.34 paragraph on
    ``same_process_fallback_matches``. Callers that pass a name they
    resolved themselves should always pass the pid it came from.

    Callers MUST set allow_same_process=True explicitly to opt in. The
    default (False) preserves the strict GA_ROOT-only contract that
    existing call sites (clipboard post-paste, retract focus check)
    still rely on.

    References: wh-3nwy (post-send foreground check false-positive),
    wh-fc1x, wh-ix1z.4 (codex-review-loop round 1 design pass:
    pairwise helper rather than overloading the single-arg
    normalizer).
    """
    expected_root = normalize_hwnd_for_foreground_compare(expected_hwnd)
    if expected_root is None:
        return False
    observed_root = normalize_hwnd_for_foreground_compare(observed_hwnd)
    if observed_root is None:
        return False
    if expected_root == observed_root:
        return True
    if not allow_same_process:
        return False
    return same_process_fallback_matches(
        expected_hwnd, observed_hwnd,
        expected_process_name=expected_process_name,
        expected_pid_snapshot=expected_pid_snapshot,
    )


def same_process_fallback_matches(
    expected_hwnd: Optional[int],
    observed_hwnd: Optional[int],
    *,
    expected_process_name: Optional[str] = None,
    expected_pid_snapshot: Optional[int] = None,
) -> bool:
    """Same-process fallback comparison on the RAW handles -- no roots.

    Compares the current PIDs of both raw HWNDs (so a transient helper
    window owned by the same process matches even when its root
    differs) and, when ``expected_process_name`` is set, requires the
    shared PID's exe name (case-insensitive) to equal it.

    wh-ensure-focused-same-process-fallback.1.4: this variant exists
    for callers that have ALREADY confirmed a root mismatch and now
    want only the same-process verdict. Handing the raw handles back
    to ``hwnds_match_for_foreground_compare`` re-normalized them, and
    Windows reuses numeric HWNDs: a target destroyed between the
    caller's strict compare and the fallback could re-normalize to the
    observed root and take the strict-equality return before any PID
    or exe probe ran -- crediting a foreground window the captured
    target never identified. This function never looks at roots, so a
    rebinding cannot convert into strict equality; the rebound handle
    must instead survive the PID and exe-name probes, which fail
    closed on any changed identity, failed probe, zero PID, mismatched
    PID, or exe mismatch.

    wh-ensure-focused-same-process-fallback.1.28 (codex round 18):
    the PID and exe probes alone cannot tell the wh-3nwy transient
    helper apart from a genuine second browser window -- target A and
    observed foreground B both visible plain main frames of the same
    browser process, the exact sibling shape the .1.1 gate refuses in
    ``WindowFocusManager._foreground_matches``. Every legitimate
    credit here involves a helper-shaped window on at least one side
    (the incident target was invisible + WS_EX_TOOLWINDOW; the
    wh-3nwy observed helpers are transient popups), so the pair is
    credited only when ``hwnd_is_invisible_or_toolwindow`` answers
    True for either handle. The shape probes run LAST, after the
    identity probes, for the .1.5 reason: a handle recycled after an
    early shape probe could smuggle a visible sibling through on the
    stale answer. A probe failure reads as "visible plain window" on
    both sides and refuses (fail closed). Trade-off: a same-process
    helper that is visible and not WS_EX_TOOLWINDOW would now be
    refused -- a recoverable wrong-way refusal (the strategies fall
    back or refuse to send) accepted over an unrecoverable paste into
    the wrong window.

    wh-ensure-focused-same-process-fallback.1.31 (codex round 20):
    running the shape probes last opened a dead-handle hole -- a
    handle destroyed after the PID probes answered False from
    IsWindowVisible without raising, read as "invisible helper", and
    nothing after the gate refused, so the invalid handle itself
    became the affirmative credit. Two layers close it: the shape
    probe now confirms liveness via IsWindow (an invisible answer for
    a dead handle reads as not-helper), and after an eligible shape
    answer both PIDs are re-probed and must equal the sampled values,
    so a handle that died or was recycled to another process inside
    the probe interval refuses. The remaining residual is a recycle
    between the final re-probe and the caller's send -- the same
    single-probe interval the .1.5 ordering documents.

    wh-ensure-focused-same-process-fallback.1.34 (deepseek round 24):
    the probes above cannot refuse a SAME-exe cross-process rebind.
    The caller resolves ``expected_process_name`` from a PID it
    sampled itself; when the helper dies and Windows reuses the
    handle for a helper of another process running the same exe (a
    second Brave profile -- Chromium runs one process tree per
    profile, all named brave.exe), this function's own two samples
    agree on the NEW pid and the exe gate compares name strings, so
    every probe passes and the send is credited into the wrong
    process. The .1.4 fail-closed claim above holds only for rebinds
    that change the exe name. ``expected_pid_snapshot`` closes the
    interval with the sample the caller already took: when set, the
    first PID sample of ``expected_hwnd`` must equal it, before any
    other probe runs. The remaining residual is a rebind before the
    caller's single (pid, name) sample -- the same single-sample
    shape as the other accepted residuals in this epic.
    """
    expected_pid = _process_id_for_hwnd(expected_hwnd)
    observed_pid = _process_id_for_hwnd(observed_hwnd)
    if not expected_pid or not observed_pid:
        return False
    if (
        expected_pid_snapshot is not None
        and expected_pid != expected_pid_snapshot
    ):
        logger.debug(
            "same_process_fallback_matches: refusing pair "
            "(expected=%s observed=%s): the expected handle's pid %s "
            "differs from the caller's identity-sample pid %s -- the "
            "handle was reused between the caller's exe-name "
            "resolution and this probe",
            expected_hwnd, observed_hwnd, expected_pid,
            expected_pid_snapshot,
        )
        return False
    if expected_pid != observed_pid:
        return False

    if expected_process_name is not None:
        try:
            proc_name = psutil.Process(expected_pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError) as e:
            # ValueError covers psutil rejecting negative / non-positive PIDs
            # from stale or fake HWNDs (psutil 7.1.3 Process._init guards
            # pid < 0 before construction). All paths fail closed -- the
            # guard's contract is "uncertain -> not a match".
            logger.debug(
                "expected_process_name guard: psutil failure for pid=%s: %s",
                expected_pid, e,
            )
            return False
        if proc_name.lower() != expected_process_name.lower():
            return False

    # wh-ensure-focused-same-process-fallback.1.28 pair-shape guard --
    # see the docstring paragraph above. Last, so the credit reflects
    # the CURRENT handles.
    if not (
        hwnd_is_invisible_or_toolwindow(expected_hwnd)
        or hwnd_is_invisible_or_toolwindow(observed_hwnd)
    ):
        logger.debug(
            "same_process_fallback_matches: refusing visible sibling "
            "pair (expected=%s observed=%s): neither side has the "
            "transient-helper shape",
            expected_hwnd, observed_hwnd,
        )
        return False

    # wh-ensure-focused-same-process-fallback.1.31 revalidation -- see
    # the docstring paragraph above. A handle that died (or was
    # recycled to another process) between the PID sampling and the
    # shape gate must not be credited: re-probe both PIDs and require
    # them unchanged. Fail closed on any probe failure.
    if (
        _process_id_for_hwnd(expected_hwnd) != expected_pid
        or _process_id_for_hwnd(observed_hwnd) != observed_pid
    ):
        logger.debug(
            "same_process_fallback_matches: refusing pair "
            "(expected=%s observed=%s): a handle died or changed "
            "process identity after the identity probes",
            expected_hwnd, observed_hwnd,
        )
        return False
    return True


def process_identity_for_hwnd(
    hwnd: Optional[int],
) -> Optional[tuple[int, str]]:
    """Return ``(pid, lowercase exe name)`` for ``hwnd``, or None on failure.

    One PID sample feeds both values, so the pid names the exact
    process instance the exe name was resolved from. Callers that
    gate a same-process fallback on the exe name must pass this pid
    to ``same_process_fallback_matches`` as ``expected_pid_snapshot``
    (wh-ensure-focused-same-process-fallback.1.34): resolving the
    name and letting the fallback sample the PID again leaves an
    interval where the handle can be reused by another process
    running the same exe.

    None means "cannot determine" -- callers should treat that as
    "not eligible for the scoped relaxation" so the strict GA_ROOT
    behavior holds.
    """
    pid = _process_id_for_hwnd(hwnd)
    if pid is None:
        return None
    try:
        return pid, psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError) as e:
        logger.debug(
            "process_identity_for_hwnd: psutil failure for pid=%s: %s",
            pid, e,
        )
        return None


def process_name_for_hwnd(hwnd: Optional[int]) -> Optional[str]:
    """Return the lowercase exe name owning ``hwnd``, or None on any failure.

    Name-only convenience over ``process_identity_for_hwnd`` for
    callers that log or classify by exe name and take no fallback
    decision from it. A caller that feeds the name into a
    same-process fallback must use ``process_identity_for_hwnd``
    instead and pass the pid along (.1.34, see that docstring).
    """
    identity = process_identity_for_hwnd(hwnd)
    return None if identity is None else identity[1]


def _process_id_for_hwnd(hwnd: Optional[int]) -> Optional[int]:
    """Return the process ID owning ``hwnd``, or None on any failure.

    Defensive against zero / None HWND, GetWindowThreadProcessId
    exceptions, and pythoncom returning a falsy PID. None means
    "cannot determine" -- callers must fail closed on that.
    """
    if not hwnd:
        return None
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception as e:
        logger.debug(
            "GetWindowThreadProcessId(%s) failed: %s", hwnd, e,
        )
        return None
    if not pid:
        return None
    return int(pid)
