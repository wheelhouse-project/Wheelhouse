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
import logging
from typing import Iterable, Optional

import psutil
import win32gui
import win32process

logger = logging.getLogger(__name__)

# win32con.GA_ROOT == 2 -- inlined here so this module has no transitive
# dependency on win32con just for one constant.
GA_ROOT = 2


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
        return None
    return int(root)


def hwnds_match_for_foreground_compare(
    expected_hwnd: Optional[int],
    observed_hwnd: Optional[int],
    *,
    allow_same_process: bool = False,
    expected_process_name: Optional[str] = None,
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

    Optional ``expected_process_name`` (case-insensitive) constrains
    the fallback to a specific exe name. When set, the matched PID's
    process name (psutil.Process(pid).name()) must equal the value.
    Use this to scope the relaxation to known browsers (e.g.
    "brave.exe") so unrelated apps cannot accidentally use the
    same-process path.

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

    # Same-process fallback. Compare PIDs from BOTH the raw HWNDs (so a
    # transient helper window owned by the same process matches even
    # when its root differs).
    expected_pid = _process_id_for_hwnd(expected_hwnd)
    observed_pid = _process_id_for_hwnd(observed_hwnd)
    if not expected_pid or not observed_pid:
        return False
    if expected_pid != observed_pid:
        return False

    if expected_process_name is None:
        return True

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
    return proc_name.lower() == expected_process_name.lower()


def process_name_for_hwnd(hwnd: Optional[int]) -> Optional[str]:
    """Return the lowercase exe name owning ``hwnd``, or None on any failure.

    Used by callers that want to scope a same-process fallback to
    specific exe names (e.g. VerifiedUnicodeStrategy's wh-3nwy
    fallback, which is opt-in only for known Chromium-derived
    browsers per wh-ix1z.19).

    None means "cannot determine" -- callers should treat that as
    "not eligible for the scoped relaxation" so the strict GA_ROOT
    behavior holds.
    """
    pid = _process_id_for_hwnd(hwnd)
    if pid is None:
        return None
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError) as e:
        logger.debug(
            "process_name_for_hwnd: psutil failure for pid=%s: %s",
            pid, e,
        )
        return None


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
