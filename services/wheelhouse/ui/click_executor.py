"""ClickExecutor for voice-driven UI element clicking (wh-mzpvx).

The final, security-sensitive step of the voice element-clicking feature
(epic wh-l4h.1). After ``ElementFinder.find`` produces an
``Outcome(outcome="ok", winner=...)`` carrying a live COM ``control_ref``,
this module performs the v5 "Pre-click verification" block, executes the
click via ``InvokePattern.Invoke()``, and -- only under a separately gated,
fail-closed fallback -- a coordinate click on the freshly re-read bounding
rectangle. It returns a small Input-process-local :class:`ClickResult`.

Authoritative spec: docs/plans/2026-05-21-voice-element-clicking-design-v5.md,
sections "Pre-click verification (v4 expanded)" (the five-step block),
"Click execution paths (v4 -- fail closed on Invoke errors)" (the three
Invoke branches + the stronger coordinate-click eligibility check), and the
"Outcome reporting" reason table.

Fail-closed contract (the reason this slice exists):
=====================================================
A wrong fallback double-fires a hands-free click. So:

* A COM error from ``Invoke()`` is NOT proof the Invoke had no side effect.
  The provider may have activated the control, dismissed a dialog, moved
  focus, or torn down the window before the error propagated. The ONLY
  authority on whether a failed Invoke is side-effect-free is
  ``ui.invoke_error_codes.is_no_side_effect_hresult`` -- and even an
  allowlisted code permits a coordinate click only after the FULL pre-click
  verification block is re-run against the fresh foreground + COM state.
* A non-allowlisted COM error never coordinate-clicks unless the operator
  has explicitly set ``enable_coordinate_click_on_com_error=True`` AND the
  match passes the stronger eligibility check.
* The stronger coordinate-click eligibility check rejects a bare
  substring+role match (which the general find() predicate would accept):
  a coincidental substring label could click an unrelated region.

Injection idiom (mirrors ``ElementFinder``):
=============================================
Every Win32 / COM / SendInput / display seam is a constructor callable so the
test suite runs headless -- no real COM, no real foreground, no real
SendInput, no real monitors. The COM property reads (IsEnabled,
BoundingRectangle) happen on ``match.control_ref`` directly, so a fake
control_ref object supplies them in tests. The press itself goes through
the injected ``invoke_fn`` (default ``invoke_via_invoke_pattern``), which
fetches the control's UIA Invoke pattern and calls Invoke on it -- a real
``IUIAutomationElement`` has no direct ``Invoke`` method
(wh-click-invoke-on-element-not-pattern). The coordinate-click,
foreground probe, and on-screen check are injected callables; the real
SendInput-backed / Win32-backed implementations are supplied by the
production-wiring slice (this slice ships no real default for them).

This slice does NOT build the shared ClickElementResponse IPC schema, the
click-notice toast, ClickConfig, input_proc wiring, or the Logic awaiter --
those are separate wh-l4h.1 leaves. The executor returns its own local
result type only.

Reason-tag contract (must match exactly -- the notice-wording slice keys off
these literal strings): ``disabled``, ``bounds_invalid``,
``foreground_changed``, ``foreground_verification_failed``,
``invoke_com_error``, ``invoke_then_sendinput_failed``, ``sendinput_short``,
``target_moved_offscreen``, ``popup_closed``, ``bounds_stale``,
``click_point_obstructed``.

``click_point_obstructed`` (wh-explorer-navpane-click.1.1 / .1.4) is the
coordinate-fallback pre-send hit-test reason, produced by either of two
layered checks: (1) the root window under the click point is not the
winner's own top-level window (an always-on-top occluder that never takes
foreground); (2) the UI Automation element at the point does not resolve to
the winner, a descendant of it, or one of its containers (a SAME-ROOT
occluder -- an in-window overlay sharing the top-level window). Either check
raising -- including an un-injected placeholder seam -- maps to the same
reason. Emitted by ``_coordinate_fallback`` for ALL coordinate callers; no
input is sent.

``bounds_stale`` (Phase 1.5, design r1c.6) is a pre-click verification reason:
the control's freshly-read bounding rectangle moved MORE than
``overlay_bounds_tolerance_physical_px`` physical pixels (per dimension) from
its cached walk-time bounds, so the numbered badge no longer points where the
user saw it. It reuses the BoundingRectangle already read in step 5 (zero extra
Win32 round-trips). It is a PARTIAL defence against ONE case in the stale-badge
family (a control that moved beyond the tolerance); a within-tolerance move and
an unchanged-bounds obscuration both still click. It is an OPEN reason tag, so
no IPC schema change is needed.

``popup_closed`` (wh-n29v.45) is a pre-click verification reason emitted ONLY
for a popup-owned winner (``ElementMatch.source_window_hwnd != 0``): the
classic Win32 #32768 / UIA-Menu owned popup the control was walked from is no
longer visible or no longer owned by the focused window, so the menu closed
between the walk and the click. It is an OPEN reason tag (the
``ClickElementResponse.reason`` set is open), so no IPC schema change is
needed; the notice names the matched control ("the menu closed before
WheelHouse could click 'X'").

DoDefaultAction (MSAA LegacyIAccessible) press-fallback reason tags
(wh-l4h.1.17), used only inside the InvokePatternUnavailable branch:
``dda_unavailable`` (no MSAA press path), ``dda_no_default_action`` (pattern
present, nothing to fire), ``dda_no_default_action_failed`` (the press call
failed -- fail-closed: a non-success accDoDefaultAction may have fired the
action, so the executor never assumes success and never double-fires), and
``dda_no_side_effect_then_sendinput_failed`` (the press failed with a proven
side-effect-free HRESULT, the gated coordinate retry fired, but the coordinate
click itself failed to land -- the delivery-failure analogue of
``invoke_then_sendinput_failed``, kept distinct from the may-have-fired
``dda_no_default_action_failed``), and the structural-absence chain reasons
``dda_unavailable_then_sendinput_failed`` /
``dda_no_default_action_then_sendinput_failed``
(wh-explorer-navpane-click: both press patterns structurally absent, the
knob-free coordinate fallback fired, but the click did not land -- note the
PLAIN ``dda_unavailable`` / ``dda_no_default_action`` reasons now mean the
match ALSO failed the coordinate eligibility gate). The
success path emits no reason (``ok``); the ``dda_ok``,
``dda_no_side_effect_then_coord``, ``dda_unavailable_then_coord``, and
``dda_no_default_action_then_coord`` tags are telemetry markers on the log,
not ClickResult.reason values (``ok`` carries ``reason=None``).

The ``invoke_pattern_unavailable`` tag was retired in wh-l4h.1.17: the
``InvokePatternUnavailable`` branch now enters the DoDefaultAction fallback
above instead of failing under that tag, so no path emits it. It is omitted
from the contract list above for that reason (readers chasing old logs will
still find it in pre-wh-l4h.1.17 history).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from ui.element_types import ElementMatch, ElementQuery
from ui.invoke_error_codes import is_no_side_effect_hresult
from ui.uia_walker import (
    NAME_TO_CONTROL_TYPE_ID,
    DoDefaultActionUnavailable,
    InvokePatternUnavailable,
    NoDefaultAction,
    do_default_action_via_legacy_pattern,
    invoke_via_invoke_pattern,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForegroundProbe:
    """Current foreground identity sampled at pre-click verification time.

    The injected ``foreground_probe`` seam returns one of these. Each field
    mirrors a WalkSnapshot identity field so the verification block can compare
    them. Fields that the probe could not read are set to ``None`` (an access
    restriction such as an admin-elevated foreground), which drives the v5
    graceful-degrade rule. ``window`` is the only field a probe can always
    obtain (``GetForegroundWindow`` needs no process handle); ``pid`` is read
    via ``GetWindowThreadProcessId`` (may be 0 -> None); ``process_name`` and
    ``window_creation_time`` need ``OpenProcess`` and may be denied -> None.
    """

    window: int
    pid: Optional[int]
    process_name: Optional[str]
    window_creation_time: Optional[int]


@dataclass(frozen=True)
class SnapshotForeground:
    """The walk-time foreground identity the click is verified against.

    A plain-data projection of the WalkSnapshot's four foreground-identity
    fields, passed to ``click()`` so the executor does not need the whole
    Input-local WalkSnapshot. The production wiring slice builds this from the
    stored snapshot; tests construct it directly.
    """

    window: int
    pid: int
    process_name: str
    window_creation_time: int


@dataclass(frozen=True)
class ClickResult:
    """Input-process-local result of a ``ClickExecutor.click()`` call.

    Not the shared IPC ``ClickElementResponse`` (a later slice) -- this is the
    executor's own local return. The coordinator projects display-safe data
    (``matched_name``) out of it before any IPC send.

    Fields:
        outcome: ``"ok"`` on a successful click, else ``"execution_failed"``.
        reason: one of the v5 click-time reason tags when
            ``outcome == "execution_failed"``; ``None`` on ``ok``.
        matched_name: the winning match's name, carried for notice wording
            (set on both ok and execution_failed so the notice path can name
            the target). ``None`` only if the winner had no name.
        clicked_via: which execution path performed the click on ``ok``
            (``"invoke"`` or ``"coordinate"``); ``None`` on failure. Telemetry
            only.
    """

    outcome: Literal["ok", "execution_failed"]
    reason: Optional[str]
    matched_name: Optional[str]
    clicked_via: Optional[Literal["invoke", "coordinate"]] = None


# v5 design default, mirrored as a constructor default so this module reads no
# config; the real value arrives from ClickConfig (a separate slice).
_DEFAULT_ENABLE_COORDINATE_CLICK_ON_COM_ERROR = False

# Phase 1.5 pre-click bounds-tolerance default (design r1c.6), mirrored as a
# constructor default so this module reads no config. The real value is threaded
# in from the already-validated ``ClickConfig.overlay_bounds_tolerance_physical_px``
# (default 8, validated to [0, 200]) at the construction site in
# ui_action_handler.py. Matches the ClickConfig default so an un-wired executor
# behaves identically to the production wiring.
#
# UNIT NOTE (wh-n29v.88.1, renamed by wh-bounds-tol-rename-physical): the value
# is compared in PHYSICAL pixels. Both sides of the comparison come from UIA
# CurrentBoundingRectangle, which WheelHouse treats as physical pixels (see
# overlay_dpi_resolver.py), so the check is apples-to-apples. The name used to
# carry a ``_logical_px`` suffix inherited from the pre-rename config contract;
# the config key ``overlay_bounds_tolerance_logical_px`` is still accepted as a
# deprecated alias (ui/click_config.py:_DEPRECATED_OVERLAY_ALIASES). Note the
# effective on-screen slack scales down as display scale rises (8 physical px
# ~= 4 logical px at 200%). That is the safe direction: a high-DPI display
# over-refuses (the user re-says "show numbers"); it never clicks a control it
# should have refused.
_DEFAULT_OVERLAY_BOUNDS_TOLERANCE_PHYSICAL_PX = 8


def _placeholder_coordinate_click(_x: int, _y: int) -> tuple[bool, int]:
    """No-op-or-raise default for the coordinate-click seam.

    This slice ships NO real SendInput-backed coordinate click
    (``win_input_sender.py`` is out of scope and has no mouse primitive). The
    production-wiring slice injects the real callable. If a code path ever
    reaches the coordinate-click fallback without an injected function, fail
    closed by raising rather than silently "succeeding".
    """
    raise RuntimeError(
        "coordinate_click_fn was not injected; the production-wiring slice "
        "must supply a real SendInput-backed coordinate click"
    )


def _placeholder_window_at_point(_x: int, _y: int) -> int:
    """Raise-placeholder default for the click-point hit-test seam.

    Same contract as ``_placeholder_coordinate_click``: the production wiring
    (``UIActionHandler._get_click_executor``) injects the real
    ``WindowFromPoint`` -> ``GetAncestor(GA_ROOT)`` query
    (``utils/win_input_sender.py::root_window_at_point``). If a coordinate
    fallback ever runs without the injection, the raise is caught in
    ``_coordinate_fallback`` and mapped to ``click_point_obstructed`` -- fail
    closed, no input -- so production can never silently skip the hit-test
    (wh-explorer-navpane-click.1.1).
    """
    raise RuntimeError(
        "window_at_point_fn was not injected; the production-wiring slice "
        "must supply a real WindowFromPoint-backed hit-test"
    )


def _placeholder_point_hits_winner(
    _winner: "ElementMatch", _x: int, _y: int
) -> bool:
    """Raise-placeholder default for the UIA point-hits-winner seam.

    Same contract as the other two coordinate-path placeholders: the
    production wiring injects the real UI Automation query
    (``ui/uia_walker.py::point_hits_winner`` -- ``ElementFromPoint`` plus a
    bounded ancestor comparison against the winner). If a coordinate fallback
    ever runs without the injection, the raise is caught in
    ``_coordinate_fallback`` and mapped to ``click_point_obstructed`` -- fail
    closed, no input (wh-explorer-navpane-click.1.4).
    """
    raise RuntimeError(
        "point_hits_winner_fn was not injected; the production-wiring slice "
        "must supply a real ElementFromPoint-backed winner check"
    )


def _hresult_of(exc: BaseException) -> object:
    """Extract an HRESULT from a caught COM exception, fail-closed.

    A real ``comtypes.COMError`` exposes ``.hresult`` (signed 32-bit int), and
    that is the ONLY attribute trusted here. Returns whatever ``.hresult``
    holds (passed straight to ``is_no_side_effect_hresult``, which fail-closes
    on any non-int). Returns ``None`` when the exception has no ``.hresult``, so
    a non-COM exception fails closed as a non-allowlisted error.

    An earlier version fell back to ``args[0]`` when ``.hresult`` was absent
    (reviewer_0 finding wh-9f3t.27.3). That was a FAIL-OPEN hazard: a non-COM
    exception whose first argument happened to equal an allowlisted code (e.g.
    ``ValueError(0x80040201)``) would be misclassified as side-effect-free and
    open the gated coordinate-click -- the exact double-fire this module exists
    to prevent. The executor only ever calls ``control_ref.Invoke()``, a
    comtypes UIA method that raises ``comtypes.COMError`` with ``.hresult``
    populated, so the ``args[0]`` fallback served no real comtypes shape and is
    removed. Any exception lacking ``.hresult`` is treated as non-allowlisted.

    NOTE: this reads ``.hresult`` off whatever exception it is given; the COM-ness
    gate lives in ``ClickExecutor._handle_invoke_error`` via the injected
    ``com_error_predicate`` (reviewer_1 finding wh-9f3t.28.1). This function is
    only called AFTER that predicate has confirmed the exception is a real COM
    error, so a non-COM exception that merely carries a ``.hresult`` attribute
    never reaches the allowlist.
    """
    return getattr(exc, "hresult", None)


def _default_is_com_error(exc: BaseException) -> bool:
    """Default COM-error predicate: True only for a real ``comtypes.COMError``.

    Gates HRESULT allowlisting (reviewer_1 finding wh-9f3t.28.1). Without this
    gate, ``_hresult_of`` would trust the ``.hresult`` attribute of ANY
    exception, so a non-COM exception that merely carries
    ``hresult=UIA_E_NOTSUPPORTED`` (a custom class, a mocked failure, a wrapper
    that copies the field) would be classified as side-effect-free and open the
    gated coordinate-click -- the fail-OPEN direction this module exists to
    prevent. ``comtypes`` is imported lazily so this module stays importable on a
    host without it; if the import fails, no exception can be a COM error here
    and the executor fails closed (every Invoke error becomes
    ``invoke_com_error``). Production passes the real
    ``control_ref.Invoke()`` exceptions through this; tests inject a predicate
    that recognises their fake COM-error class.
    """
    try:
        from comtypes import COMError  # type: ignore
    except Exception:  # noqa: BLE001 -- import guard; absence means fail closed
        return False
    return isinstance(exc, COMError)


class ClickExecutor:
    """Executes a verified click on an ElementFinder winner, fail-closed.

    All Win32 / COM / SendInput / display seams are injected constructor
    callables (keyword-only), exactly like ``ElementFinder``. The v5 config
    value is mirrored as a constructor default so this module reads no config.

    Constructor seams:
        coordinate_click_fn: ``(x, y) -> (succeeded, events_sent)``. The
            production SendInput-backed click; ``events_sent`` lets the caller
            detect a short send (``sendinput_short``). Defaults to a
            raise-placeholder so an un-wired fallback fails closed.
        foreground_probe: ``() -> ForegroundProbe`` -- samples the CURRENT
            foreground identity at verification time.
        on_screen_fn: ``(x, y) -> bool`` -- True when the physical point is on
            a visible monitor. Used for the target-moved-offscreen check.
        com_error_predicate: ``(exc) -> bool`` -- True only when a caught
            ``Invoke()`` / ``DoDefaultAction()`` exception is a real COM error
            whose ``.hresult`` may be consulted against the no-side-effect
            allowlist. Defaults to a comtypes.COMError isinstance check. A
            non-COM exception is treated as a non-allowlisted failure (fail
            closed), so a stray ``.hresult`` attribute cannot open the
            coordinate-click fallback (reviewer_1 finding wh-9f3t.28.1).
        do_default_action_fn: ``(control_ref) -> None`` -- the MSAA
            LegacyIAccessible ``DoDefaultAction()`` press seam (wh-l4h.1.17),
            attempted ONLY when ``invoke_fn`` raised
            ``InvokePatternUnavailable`` (InvokePattern structurally
            unavailable). Returns normally on a successful press; raises
            ``DoDefaultActionUnavailable`` (no Legacy/DoDefaultAction path),
            ``NoDefaultAction`` (pattern present, no default action), or a COM
            error carrying ``.hresult`` (the call ran and failed). Defaults to
            the real MSAA press ``do_default_action_via_legacy_pattern``
            (wh-click-dda-wiring), exactly as ``invoke_fn`` defaults to the real
            ``invoke_via_invoke_pattern``. Tests inject a fake to drive the
            DoDefaultAction branches headlessly.
        enable_coordinate_click_on_com_error: v5 knob (default False). When
            False, a non-allowlisted Invoke COM error NEVER coordinate-clicks.
        overlay_bounds_tolerance_physical_px: Phase 1.5 knob (design r1c.6,
            default 8). Maximum per-dimension PHYSICAL-pixel drift (UIA units;
            see the unit note at ``_DEFAULT_OVERLAY_BOUNDS_TOLERANCE_PHYSICAL_PX``)
            between the cached walk-time ``ElementMatch.bounds`` and the
            freshly-read BoundingRectangle that step 5 will still accept; any
            dimension moving MORE than this returns ``bounds_stale``. Threaded in
            from the validated ``ClickConfig.overlay_bounds_tolerance_physical_px``.
        popup_visible_fn: ``(popup_hwnd) -> bool`` -- True when the owning popup
            window is still visible. Consulted by the popup-closed probe ONLY
            for a popup-owned winner (wh-n29v.45). Default None (Phase 1
            construction): a popup-owned match then fails closed to
            ``popup_closed``; the production-wiring slice injects a real
            ``IsWindowVisible`` seam.
        popup_owner_fn: ``(popup_hwnd) -> int`` -- the owner HWND of the popup
            window. The probe requires it to equal the focused window. Default
            None, same fail-closed contract as ``popup_visible_fn``; the
            production seam is ``GetWindow(hwnd, GW_OWNER)``.
    """

    def __init__(
        self,
        *,
        coordinate_click_fn: Callable[[int, int], tuple[bool, int]] = (
            _placeholder_coordinate_click
        ),
        foreground_probe: Callable[[], ForegroundProbe],
        on_screen_fn: Callable[[int, int], bool],
        com_error_predicate: Callable[[BaseException], bool] = _default_is_com_error,
        invoke_fn: Callable[[Any], None] = invoke_via_invoke_pattern,
        do_default_action_fn: Callable[
            [Any], None
        ] = do_default_action_via_legacy_pattern,
        enable_coordinate_click_on_com_error: bool = (
            _DEFAULT_ENABLE_COORDINATE_CLICK_ON_COM_ERROR
        ),
        overlay_bounds_tolerance_physical_px: int = (
            _DEFAULT_OVERLAY_BOUNDS_TOLERANCE_PHYSICAL_PX
        ),
        popup_visible_fn: Optional[Callable[[int], bool]] = None,
        popup_owner_fn: Optional[Callable[[int], int]] = None,
        window_at_point_fn: Callable[[int, int], int] = (
            _placeholder_window_at_point
        ),
        point_hits_winner_fn: Callable[["ElementMatch", int, int], bool] = (
            _placeholder_point_hits_winner
        ),
    ) -> None:
        self._coordinate_click_fn = coordinate_click_fn
        self._foreground_probe = foreground_probe
        self._on_screen_fn = on_screen_fn
        self._com_error_predicate = com_error_predicate
        # Click-point hit-test seam (wh-explorer-navpane-click.1.1 -- see
        # _coordinate_fallback). ``(x, y) -> root HWND at that physical screen
        # point`` (0 when none). Production injects
        # win_input_sender.root_window_at_point; the raising placeholder
        # default maps to a click_point_obstructed refusal so an un-wired
        # fallback can never click blind.
        self._window_at_point_fn = window_at_point_fn
        # UIA point-hits-winner seam (wh-explorer-navpane-click.1.4 -- see
        # _coordinate_fallback). ``(winner, x, y) -> bool``: does the UI
        # Automation element at the point resolve to the winner, a descendant
        # of it, or one of its containers? Catches SAME-ROOT occluders the
        # root-window comparison above cannot (an in-window overlay shares the
        # top-level window). Production injects a uia_walker.point_hits_winner
        # closure; same raising-placeholder discipline.
        self._point_hits_winner_fn = point_hits_winner_fn
        # The press itself. Default fetches the control's UIA Invoke pattern
        # and calls Invoke on it; a real IUIAutomationElement has no direct
        # Invoke method (wh-click-invoke-on-element-not-pattern). Injected in
        # tests so a fake control_ref can drive the Invoke branches.
        self._invoke_fn = invoke_fn
        # The MSAA LegacyIAccessible DoDefaultAction press seam (wh-l4h.1.17).
        # Consulted ONLY inside the InvokePatternUnavailable branch of
        # _handle_invoke_error -- never when InvokePattern is available but
        # raised a COM error. Defaults to the real press
        # do_default_action_via_legacy_pattern (wh-click-dda-wiring); injected
        # in tests so a fake control_ref can drive the DoDefaultAction branches.
        self._do_default_action_fn = do_default_action_fn
        self._enable_coordinate_click_on_com_error = (
            enable_coordinate_click_on_com_error
        )
        # Phase 1.5 pre-click bounds-tolerance (design r1c.6). The maximum
        # per-dimension PHYSICAL-pixel drift between the cached walk-time bounds
        # and the freshly-read BoundingRectangle that _verify will still accept;
        # any dimension moving MORE than this returns ``bounds_stale``. Threaded
        # in from the validated ClickConfig at the construction site; defaults to
        # the same value as the ClickConfig default.
        self._bounds_tolerance = overlay_bounds_tolerance_physical_px
        # Popup-closed probe seams (wh-n29v.45). Consulted by ``_verify`` ONLY
        # for a popup-owned winner (``winner.source_window_hwnd != 0``): before
        # invoking a control that was walked from a classic Win32 #32768 /
        # UIA-Menu owned popup, the executor verifies the owning popup HWND is
        # still visible AND still owned by the focused window (design line 396).
        # A primary-window match (source_window_hwnd == 0) never consults these,
        # so the probe costs zero extra Win32 round-trips on the Phase 1 path.
        # Left None on the Phase 1 construction: a None seam is treated as
        # "no popup-state information available", and the probe fails closed to
        # popup_closed only when a popup-owned match is actually presented (a
        # popup-owned winner with no way to confirm its popup is alive must not
        # be clicked blind). The production-wiring slice injects real Win32
        # IsWindowVisible / GetWindow(GW_OWNER) seams; tests inject fakes.
        self._popup_visible_fn = popup_visible_fn
        self._popup_owner_fn = popup_owner_fn
        # Number of input events a single coordinate click is expected to
        # synthesise (mouse-down + mouse-up). A seam reporting fewer than this
        # is a short send (``sendinput_short``). The production seam returns the
        # real SendInput count; tests assert against this value.
        self._expected_click_events = 2
        # Fresh rect centre stashed by _verify on success; read only by the
        # caller immediately after a None return. Instance state, not shared.
        self._last_rect: tuple[int, int] = (0, 0)

    # -- public click surface -------------------------------------------------

    def click(
        self,
        winner: ElementMatch,
        snapshot_foreground: SnapshotForeground,
        query: ElementQuery,
    ) -> ClickResult:
        """Verify, then click ``winner``; return a fail-closed ClickResult.

        ``winner`` is the ``Outcome.winner`` from a find() ``outcome == "ok"``;
        its ``control_ref`` is a live COM handle. ``snapshot_foreground`` is the
        walk-time foreground identity the click must still match.  ``query`` is
        the original ElementQuery, used only by the stronger coordinate-click
        eligibility check.

        CALLER KEEPALIVE OBLIGATION (reviewer_0 finding wh-9f3t.27.2): the
        ``winner.control_ref`` COM proxy is pinned ONLY by the ElementFinder's
        stored ``_walk_result`` keepalive (the IUIAutomation root, cache
        request, element array, and top-level element). The executor does NOT
        hold an independent keepalive for it -- it reads ``control_ref``
        through the ``winner`` argument and relies on that array keepalive
        staying reachable. The caller MUST therefore keep the originating
        ``FindResult`` (whose ``_walk_result`` field pins the chain), or an
        un-invalidated ElementFinder store for the same snapshot, reachable for
        the full duration of this call. A caller that drops the FindResult and
        lets the ElementFinder store be replaced (a new ``find()``), expire
        (TTL), or be ``invalidate()``-d before or during this call dangles
        ``control_ref`` and the first COM re-read here can hit a released
        proxy. See ``ui/element_finder.py`` FindResult / ``get_snapshot``
        COM-lifetime caveats.
        """
        # --- Pre-click verification (v5 order). ------------------------------
        verdict = self._verify(winner, snapshot_foreground)
        if verdict is not None:
            return self._fail(winner, verdict)
        # _verify stashed the fresh rect centre in self._last_rect; the
        # coordinate fallback (if reached) reads it there. The happy Invoke path
        # below needs no rect.

        # --- InvokePattern execution path. -----------------------------------
        try:
            self._invoke_fn(winner.control_ref)
        except Exception as exc:  # noqa: BLE001 -- COM raises broad exceptions
            return self._handle_invoke_error(
                exc, winner, snapshot_foreground, query
            )
        return ClickResult(
            outcome="ok",
            reason=None,
            matched_name=winner.name,
            clicked_via="invoke",
        )

    # -- pre-click verification ----------------------------------------------

    def _verify(
        self,
        winner: ElementMatch,
        snap: SnapshotForeground,
    ) -> Optional[str]:
        """Run the five-step v5 verification block.

        Returns ``None`` when verification passes (and stashes the fresh
        bounding-rect centre in ``self._last_rect`` for the coordinate
        fallback), or a reason tag string on the first failing step.

        Step 5 also applies the Phase 1.5 bounds-tolerance check (design r1c.6):
        the freshly-read BoundingRectangle (already in hand -- no extra Win32
        round-trip) is compared per dimension against the cached
        ``winner.bounds``; a drift exceeding ``self._bounds_tolerance`` (physical
        UIA pixels) in any of x/y/w/h returns ``bounds_stale``.
        """
        probe = self._foreground_probe()

        # Step 1: foreground HWND vs the snapshot.
        if probe.window != snap.window:
            return "foreground_changed"

        # Step 2: foreground PID, then process name on the current foreground.
        # A read that succeeded but DIFFERS is a real change -> foreground_changed.
        # A read that could not complete (None) is "lesser check could not
        # complete" and feeds the step-3 graceful-degrade decision.
        # A disagreeing PID/name returns "foreground_changed" immediately below,
        # so by the time step 3 runs the only open question is whether a lesser
        # check could not COMPLETE (probe field was None / access-denied).
        lesser_incomplete = False

        if probe.pid is not None:
            if probe.pid != snap.pid:
                return "foreground_changed"
        else:
            lesser_incomplete = True

        if probe.process_name is not None:
            if probe.process_name != snap.process_name:
                return "foreground_changed"
        else:
            lesser_incomplete = True

        # Step 3: window-creation-time check with the v5 graceful-degrade rule.
        if probe.window_creation_time is not None:
            if probe.window_creation_time != snap.window_creation_time:
                # Read succeeded and differs -> PID reuse after exit.
                return "foreground_changed"
            # Read succeeded and matches -> continue.
        else:
            # Creation-time read FAILED (access restriction). Accept the
            # foreground identity only if every lesser check (HWND already
            # matched; PID-when-available; process-name-when-available) also
            # matched. If any lesser check could not complete (the disagree
            # half already returned "foreground_changed" above), we cannot tell
            # whether the foreground changed -> verification failed (distinct
            # reason from foreground_changed).
            if lesser_incomplete:
                return "foreground_verification_failed"
            # HWND + PID + name all matched, only creation-time was denied ->
            # accept (this is the admin-elevated foreground case) and continue.

        # Popup-closed probe (wh-n29v.45, design line 396). Only for a
        # popup-owned winner: a control walked from a classic Win32 #32768 /
        # UIA-Menu owned popup carries that popup's HWND in
        # ``source_window_hwnd``. Menus are transient -- the popup can vanish
        # (or be re-parented) between the walk and the click -- so before
        # invoking we verify the popup window is STILL visible AND STILL owned
        # by the focused window (``snap.window``). Failure -> ``popup_closed``,
        # surfaced with the matched name ("the menu closed before WheelHouse
        # could click 'X'"). A primary-window match (source_window_hwnd == 0)
        # skips this entirely, so the probe adds no Win32 round-trip to the
        # Phase 1 path.
        if winner.source_window_hwnd:
            if not self._popup_still_open(winner.source_window_hwnd, snap.window):
                return "popup_closed"

        # Step 4: re-read IsEnabled on control_ref.
        try:
            enabled = winner.control_ref.CurrentIsEnabled
        except Exception:  # noqa: BLE001 -- COM property read can raise
            return "bounds_invalid"
        if not enabled:
            return "disabled"

        # Step 5: re-read BoundingRectangle on control_ref.
        try:
            rect = winner.control_ref.CurrentBoundingRectangle
        except Exception:  # noqa: BLE001 -- COM property read can raise
            return "bounds_invalid"
        parsed = self._parse_rect(rect)
        if parsed is None:
            return "bounds_invalid"
        x, y, w, h = parsed
        if w <= 0 or h <= 0:
            return "bounds_invalid"
        centre_x = x + w // 2
        centre_y = y + h // 2
        if not self._on_screen_fn(centre_x, centre_y):
            return "target_moved_offscreen"

        # Phase 1.5 bounds-tolerance check (design r1c.6). The fresh rect read
        # above is reused -- NO second BoundingRectangle round-trip. Both the
        # cached ``winner.bounds`` (from the walker's ``_rect_to_bounds``) and the
        # parsed fresh rect are in (x, y, w, h) screen-pixel form, so the
        # comparison is component-by-component. If ANY dimension moved MORE than
        # the configured per-dimension tolerance, the control is no longer where
        # the numbered badge indicated -> refuse with ``bounds_stale`` rather than
        # click a control the user can no longer see. A within-tolerance move and
        # an unchanged-bounds obscuration deliberately pass (partial defence; see
        # the module docstring).
        cached = self._parse_cached_bounds(winner.bounds)
        if cached is None:
            # Cached walk-time bounds are malformed (None, wrong length, or
            # non-int). ElementMatch.bounds is typed (x, y, w, h) ints and the
            # walker always builds that shape, so this never trips in
            # production; the guard fails CLOSED to ``bounds_invalid`` -- like
            # every other read in this method -- so a contract-violating winner
            # cannot raise an unpack error that escapes to the handler's generic
            # execution-failed path with no matched-name context (wh-n29v.89.1).
            return "bounds_invalid"
        cached_x, cached_y, cached_w, cached_h = cached
        tol = self._bounds_tolerance
        if (
            abs(x - cached_x) > tol
            or abs(y - cached_y) > tol
            or abs(w - cached_w) > tol
            or abs(h - cached_h) > tol
        ):
            return "bounds_stale"

        # The fresh rectangle replaces the cached one for the coordinate-click
        # fallback (v5 step 5).
        self._last_rect = (centre_x, centre_y)
        return None

    def _popup_still_open(self, popup_hwnd: int, focused_hwnd: int) -> bool:
        """True when the owning popup is still visible AND owned by the focus.

        The popup-closed probe for a popup-owned winner (wh-n29v.45). Both
        conditions must hold:

        * ``popup_visible_fn(popup_hwnd)`` is truthy -- the popup window is
          still on screen.
        * ``popup_owner_fn(popup_hwnd) == focused_hwnd`` -- the popup is still
          owned by the focused window (catches a re-parented / orphaned popup).

        Fails CLOSED in three cases, all returning False so the caller surfaces
        ``popup_closed`` and never invokes blind:

        * either seam is not wired (Phase 1 construction) -- a popup-owned match
          presented with no way to confirm its popup is alive must not be
          clicked;
        * either seam raises (the popup window raced closed mid-probe);
        * the visibility or ownership check fails.
        """
        if self._popup_visible_fn is None or self._popup_owner_fn is None:
            return False
        try:
            if not self._popup_visible_fn(popup_hwnd):
                return False
            return self._popup_owner_fn(popup_hwnd) == focused_hwnd
        except Exception:  # noqa: BLE001 -- popup raced closed -> fail closed
            return False

    @staticmethod
    def _parse_rect(rect: Any) -> Optional[tuple[int, int, int, int]]:
        """Normalise a re-read BoundingRectangle into (x, y, w, h) ints.

        UIA exposes BoundingRectangle as left/top/right/bottom (a tagRECT-like
        object with .left/.top/.right/.bottom) OR as a 4-tuple. Accept either.
        Returns ``None`` for an unparseable / degenerate shape so the caller
        fails closed to ``bounds_invalid``.
        """
        # tagRECT-like object: left/top/right/bottom.
        for attrs in (("left", "top", "right", "bottom"),):
            if all(hasattr(rect, a) for a in attrs):
                left = getattr(rect, "left")
                top = getattr(rect, "top")
                right = getattr(rect, "right")
                bottom = getattr(rect, "bottom")
                try:
                    return (
                        int(left),
                        int(top),
                        int(right) - int(left),
                        int(bottom) - int(top),
                    )
                except (TypeError, ValueError):
                    return None
        # 4-sequence: treat as left/top/right/bottom (UIA's native order).
        try:
            left, top, right, bottom = rect  # type: ignore[misc]
        except (TypeError, ValueError):
            return None
        try:
            return (
                int(left),
                int(top),
                int(right) - int(left),
                int(bottom) - int(top),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_cached_bounds(
        bounds: Any,
    ) -> Optional[tuple[int, int, int, int]]:
        """Validate the cached walk-time bounds into (x, y, w, h) ints.

        ``ElementMatch.bounds`` is typed ``tuple[int, int, int, int]`` and the
        walker's ``_rect_to_bounds`` always produces that shape (a zero rect on
        any failure), so in production this never rejects. The guard exists so a
        contract-violating winner -- a future walker change, or a direct/test
        caller that passes ``None``, a wrong-length tuple, or non-int values --
        fails CLOSED to ``bounds_invalid`` at the call site, consistent with
        every other read in ``_verify``. Without it the cached-bounds unpack
        would raise inside ``_verify`` and escape ``click`` (which does not wrap
        ``_verify``) to the handler's generic execution-failed path with no
        matched-name context (wh-n29v.89.1). Returns ``None`` on any malformed
        shape.
        """
        if not isinstance(bounds, tuple) or len(bounds) != 4:
            return None
        if not all(isinstance(v, int) for v in bounds):
            return None
        return (bounds[0], bounds[1], bounds[2], bounds[3])

    # -- Invoke error handling -----------------------------------------------

    def _handle_invoke_error(
        self,
        exc: BaseException,
        winner: ElementMatch,
        snap: SnapshotForeground,
        query: ElementQuery,
    ) -> ClickResult:
        """Map an Invoke() exception to a ClickResult, fail-closed.

        Gate 0: the exception must be a real COM error (per the injected
        ``com_error_predicate``) before its ``.hresult`` is consulted against
        the allowlist. A non-COM exception -- even one carrying an allowlisted
        ``.hresult`` attribute -- is treated as a non-allowlisted Invoke failure
        and NEVER coordinate-clicks (reviewer_1 finding wh-9f3t.28.1). This
        closes the fail-open hole that survived the reviewer_0 args[0] fix.

        Branch 2: allowlisted no-side-effect HRESULT -> gated coordinate-click
        fallback. Branch 3: any other COM error -> invoke_com_error by default,
        coordinate-click only with the knob True. Both branches require the
        stronger coordinate-click eligibility check.
        """
        # Diagnostic capture (wh-click-invoke-fails): the executor previously
        # logged nothing on an Invoke failure, so the real exception type and
        # HRESULT never reached the log -- only the "invoke_com_error" tag at
        # the handler boundary. Record them here so a failed click can be told
        # apart (element exposes no Invoke at all, vs a real COM HRESULT, vs a
        # disabled/stale control).
        logger.warning(
            "click_element Invoke() failed: matched=%r exc_type=%s "
            "hresult=%r is_com_error=%s repr=%.200s",
            winner.name,
            type(exc).__name__,
            getattr(exc, "hresult", None),
            self._com_error_predicate(exc),
            repr(exc),
        )

        if isinstance(exc, InvokePatternUnavailable):
            # The control exposed no resolvable UIA Invoke pattern. This is not
            # a COM error -- labelling it ``invoke_com_error`` would claim a COM
            # HRESULT that never existed. Before failing, try the MSAA
            # LegacyIAccessible DoDefaultAction press fallback (wh-l4h.1.17):
            # some controls expose no UIA InvokePattern but DO expose an MSAA
            # default action. The fallback is attempted ONLY here -- i.e. ONLY
            # when InvokePattern is STRUCTURALLY unavailable. When InvokePattern
            # is present but Invoke() returns a COM error (the branches below),
            # DoDefaultAction is NEVER tried: the world has already been
            # disturbed by the Invoke attempt, and an MSAA retry could
            # double-fire. The clear-winner rule only selects controls whose
            # cached Invoke pattern was present, so reaching here is off the
            # happy path; the fallback never fakes a press.
            return self._attempt_do_default_action(winner, snap, query)

        if not self._com_error_predicate(exc):
            # Not a real COM error: cannot be proven side-effect-free, so it can
            # never reach the allowlist or any coordinate-click fallback.
            return self._fail(winner, "invoke_com_error")
        hresult = _hresult_of(exc)
        allowlisted = is_no_side_effect_hresult(hresult)  # type: ignore[arg-type]

        if allowlisted:
            # Branch 2: no-side-effect HRESULT.
            if not self._coord_eligible(winner, query):
                return self._fail(winner, "invoke_com_error")
            return self._coordinate_fallback(
                winner, snap, fail_reason="invoke_then_sendinput_failed"
            )

        # Branch 3: any other COM exception.
        if (
            self._enable_coordinate_click_on_com_error
            and self._coord_eligible(winner, query)
        ):
            return self._coordinate_fallback(
                winner, snap, fail_reason="invoke_then_sendinput_failed"
            )
        # Fail closed: a non-allowlisted COM error with the knob False (or a
        # match that fails the stronger eligibility check) does NOT
        # coordinate-click.
        return self._fail(winner, "invoke_com_error")

    def _attempt_do_default_action(
        self,
        winner: ElementMatch,
        snap: SnapshotForeground,
        query: ElementQuery,
    ) -> ClickResult:
        """MSAA LegacyIAccessible DoDefaultAction press fallback (wh-l4h.1.17).

        Reached ONLY from the ``InvokePatternUnavailable`` branch -- i.e. ONLY
        when UIA InvokePattern is structurally unavailable. It is NEVER reached
        when InvokePattern is present but ``Invoke()`` returned a COM error;
        that path stays fail-closed below in ``_handle_invoke_error``.

        Honesty contract (the reason this fallback is careful):
        ``accDoDefaultAction`` can FIRE the control's default action and only
        THEN return a non-success HRESULT (a self-dismissing button, a dialog
        that tears itself down). A non-success return is therefore NOT proof
        the press did not happen. So:

        * Success (seam returns) -> ``ok`` via ``invoke`` (a real press through
          the MSAA pattern, not a coordinate click). Telemetry tag ``dda_ok``.
        * ``DoDefaultActionUnavailable`` (no MSAA press path) or
          ``NoDefaultAction`` (pattern present, nothing to fire) -> the
          STRUCTURAL-ABSENCE coordinate fallback (wh-explorer-navpane-click).
          Both are provably nothing-fired states: ``NoDefaultAction`` is
          raised BEFORE ``accDoDefaultAction`` is called, and
          ``DoDefaultActionUnavailable`` means the pattern never resolved --
          so a coordinate click cannot double-fire. ``NoDefaultAction`` has a
          THIRD raise site (wh-explorer-navpane-click.1.2): the
          ``CurrentDefaultAction`` READ itself failed (chained ``__cause__``),
          a transient-instability signal rather than a true structural
          absence. It is DELIBERATELY treated the same: the read is a
          property get (nothing-fired still holds), a control with a broken
          MSAA implementation is squarely in the motivating class, and the
          full re-verification plus the click-point hit-test stand in front
          of the click. Pinned by
          ``test_no_default_action_with_chained_cause_still_coordinate_clicks``. It goes through the SAME
          ``_coord_eligible`` gate and the SAME ``_coordinate_fallback``
          re-verification as every other coordinate path, with NO knob: the
          ``enable_coordinate_click_on_com_error`` knob covers unproven
          side-effect states, which these are not (they are strictly safer
          than the allowlisted-HRESULT branch below, where a call was at
          least attempted). Telemetry tags ``dda_unavailable_then_coord`` /
          ``dda_no_default_action_then_coord``; a coordinate click that does
          not land reports ``dda_unavailable_then_sendinput_failed`` /
          ``dda_no_default_action_then_sendinput_failed``. A match that fails
          the eligibility gate keeps the original fail-closed reasons
          (``dda_unavailable`` / ``dda_no_default_action``). Live motivation:
          every File Explorer navigation-pane folder exposes no UIA Invoke
          pattern and the pinned Quick Access items expose an EMPTY MSAA
          default action, so "click N" on them always failed; SelectionItem
          .Select() was verified live to move the tree highlight WITHOUT
          navigating (Win11 WinUI tree), leaving the coordinate click as the
          only honest press path.
        * A COM error whose HRESULT is on the no-side-effect allowlist AND that
          passes the stronger ``_coord_eligible`` check -> gated coordinate
          fallback. Telemetry tag ``dda_no_side_effect_then_coord``. It is
          gated by the SAME ``is_no_side_effect_hresult`` allowlist
          (inside the COM-error branch) AND the SAME ``_coord_eligible`` gate as
          the Invoke path. If that gated coordinate retry itself fails to land,
          the reason is ``dda_no_side_effect_then_sendinput_failed`` -- distinct
          from the honesty-boundary ``dda_no_default_action_failed`` below, so a
          delivery failure (DDA proven side-effect-free, coordinate click could
          not land) is not conflated with a may-have-fired DDA failure. This
          mirrors the Invoke path's ``invoke_then_sendinput_failed`` (a short
          send is still its own ``sendinput_short`` tag inside
          ``_coordinate_fallback``).
        * ANY other DoDefaultAction failure -- a non-COM exception, or a COM
          error NOT on the allowlist (the HONESTY boundary), or an allowlisted
          code that fails the stronger eligibility gate -- fails closed under
          ``dda_no_default_action_failed`` with NO coordinate click. The
          ``enable_coordinate_click_on_com_error`` knob deliberately does NOT
          open a DoDefaultAction coordinate click for a non-allowlisted HRESULT:
          a non-success ``accDoDefaultAction`` return may already have fired the
          action, so coordinate-clicking would double-fire.
        """
        try:
            self._do_default_action_fn(winner.control_ref)
        except DoDefaultActionUnavailable as exc:
            logger.warning(
                "click_element DoDefaultAction unavailable: matched=%r repr=%.200s",
                winner.name,
                repr(exc),
            )
            return self._structural_absence_coordinate_fallback(
                winner, snap, query, structural_reason="dda_unavailable"
            )
        except NoDefaultAction as exc:
            logger.warning(
                "click_element no MSAA default action: matched=%r repr=%.200s",
                winner.name,
                repr(exc),
            )
            return self._structural_absence_coordinate_fallback(
                winner, snap, query, structural_reason="dda_no_default_action"
            )
        except Exception as exc:  # noqa: BLE001 -- MSAA raises broad exceptions
            logger.warning(
                "click_element DoDefaultAction() failed: matched=%r "
                "exc_type=%s hresult=%r is_com_error=%s repr=%.200s",
                winner.name,
                type(exc).__name__,
                getattr(exc, "hresult", None),
                self._com_error_predicate(exc),
                repr(exc),
            )
            # Gate 0 (mirrors the Invoke path): only a real COM error may be
            # consulted against the no-side-effect allowlist. A non-COM
            # exception that merely carries a ``.hresult`` attribute is treated
            # as a non-allowlisted failure -- fail closed, never coordinate-click
            # (reviewer_1 finding wh-9f3t.28.1 applied to the DDA seam).
            if self._com_error_predicate(exc):
                hresult = _hresult_of(exc)
                if is_no_side_effect_hresult(hresult):  # type: ignore[arg-type]
                    # Proven side-effect-free: the documented spec guarantees
                    # the call did NOT perform the action. A gated coordinate
                    # click may retry it -- subject to the SAME stronger
                    # eligibility gate as the Invoke path.
                    if self._coord_eligible(winner, query):
                        logger.info(
                            "click_element DoDefaultAction side-effect-free "
                            "failure, falling through to coordinate click "
                            "(dda_no_side_effect_then_coord): matched=%r "
                            "hresult=%r",
                            winner.name,
                            hresult,
                        )
                        return self._coordinate_fallback(
                            winner,
                            snap,
                            fail_reason="dda_no_side_effect_then_sendinput_failed",
                        )
                    # Allowlisted but the match fails the stronger eligibility
                    # gate -> fail closed, no coordinate click.
                    return self._fail(winner, "dda_no_default_action_failed")
            # HONESTY boundary: a non-success DoDefaultAction HRESULT that is NOT
            # on the allowlist (or a non-COM exception) might have FIRED the
            # action. Never assume the press succeeded, never coordinate-click
            # (that would double-fire). Fail closed and surface a notice.
            return self._fail(winner, "dda_no_default_action_failed")
        # Seam returned normally: the MSAA default action fired successfully.
        # This is a real press, so report it as ``invoke`` (the press path),
        # not ``coordinate``. Telemetry tag for the path: ``dda_ok``.
        logger.debug(
            "click_element DoDefaultAction succeeded (dda_ok): matched=%r",
            winner.name,
        )
        return ClickResult(
            outcome="ok",
            reason=None,
            matched_name=winner.name,
            clicked_via="invoke",
        )

    def _structural_absence_coordinate_fallback(
        self,
        winner: ElementMatch,
        snap: SnapshotForeground,
        query: ElementQuery,
        *,
        structural_reason: str,
    ) -> ClickResult:
        """Coordinate-click when BOTH press patterns are structurally absent.

        Reached only from ``_attempt_do_default_action``'s two structural
        branches (wh-explorer-navpane-click): InvokePattern was structurally
        unavailable AND the MSAA path either never resolved
        (``dda_unavailable``) or resolved with an EMPTY default action, raised
        BEFORE ``accDoDefaultAction`` was called (``dda_no_default_action``).
        In both states nothing has fired, so a coordinate click cannot
        double-press -- these are provably side-effect-free, strictly safer
        than the allowlisted-HRESULT paths that already coordinate-click.
        Hence NO ``enable_coordinate_click_on_com_error`` knob: that knob
        exists for unproven side-effect states.

        The SAME ``_coord_eligible`` gate applies (a coincidental-substring
        match must not click an unrelated region), and ``_coordinate_fallback``
        re-runs the FULL pre-click verification before sending the click. An
        ineligible match keeps the original fail-closed ``structural_reason``
        so the permanent-inability notice still names the true cause.
        """
        if not self._coord_eligible(winner, query):
            return self._fail(winner, structural_reason)
        logger.info(
            "click_element %s, both press patterns structurally absent, "
            "falling through to coordinate click (%s_then_coord): matched=%r",
            structural_reason,
            structural_reason,
            winner.name,
        )
        return self._coordinate_fallback(
            winner,
            snap,
            fail_reason=f"{structural_reason}_then_sendinput_failed",
        )

    def _coordinate_fallback(
        self,
        winner: ElementMatch,
        snap: SnapshotForeground,
        *,
        fail_reason: str,
    ) -> ClickResult:
        """Re-verify, hit-test the click point, then coordinate-click it.

        Per v5 branches 2/3: re-run the FULL pre-click verification block; if
        it fails, return ITS reason (not the Invoke reason) -- the world moved
        between the Invoke attempt and the fallback. If verification passes,
        coordinate-click the fresh centre; a short send -> ``sendinput_short``,
        any other coordinate-click failure -> ``fail_reason``.

        Click-point hit-test (wh-explorer-navpane-click.1.1): none of the five
        verification steps can detect an always-on-top window overlapping the
        target WITHOUT owning foreground (a picture-in-picture player, an
        on-top utility) -- the occluder never takes foreground and the
        control's own rectangle is unchanged, yet the OS would deliver the
        real mouse events to the occluder. And the numbered badges paint on
        TOPMOST click-through windows, so badge N renders ABOVE such an
        occluder, actively inviting the click. So before sending, the point is
        hit-tested through ``window_at_point_fn`` (production:
        ``WindowFromPoint`` -> ``GetAncestor(GA_ROOT)``, physical screen
        coordinates -- the same space as the UIA rect centre): the root window
        at the point must be the winner's OWN top-level window --
        ``winner.source_window_hwnd`` for a popup-owned winner (a menu is its
        own root window), else the verified foreground ``snap.window``. A
        mismatch, a zero/failed lookup, or a raising seam (including the
        un-injected placeholder) refuses under ``click_point_obstructed`` with
        NO input sent. One extra Win32 query, only on the already-cold
        coordinate path; guards all three coordinate callers at once.

        Second layer (wh-explorer-navpane-click.1.4): the root comparison is
        blind to a SAME-ROOT occluder (an in-window overlay -- a Chromium
        in-page modal, a same-process floating panel -- shares the expected
        top-level window). ``point_hits_winner_fn`` (production:
        ``uia_walker.point_hits_winner`` = ``ElementFromPoint`` plus a bounded
        bidirectional ancestor comparison) must confirm the element at the
        point is the winner, inside its subtree, or one of its containers
        (weak accessibility implementations legitimately report coarse
        container elements at a point, so a container answer is accepted --
        that keeps classic Win32 apps clickable at the cost of not catching
        same-root occluders inside such apps). False, a raise, or the
        un-injected placeholder refuses under the same
        ``click_point_obstructed``; the check runs AFTER the root comparison
        so the cheap Win32 query short-circuits cross-root occluders without
        a COM call.
        """
        verdict = self._verify(winner, snap)
        if verdict is not None:
            return self._fail(winner, verdict)
        centre_x, centre_y = self._last_rect
        expected_root = winner.source_window_hwnd or snap.window
        try:
            root_at_point = self._window_at_point_fn(centre_x, centre_y)
        except Exception as exc:  # noqa: BLE001 -- a real Win32 seam can raise
            logger.warning(
                "click_element coordinate hit-test failed, refusing "
                "(click_point_obstructed): matched=%r point=(%d, %d) "
                "repr=%.200s",
                winner.name,
                centre_x,
                centre_y,
                repr(exc),
            )
            return self._fail(winner, "click_point_obstructed")
        if root_at_point != expected_root:
            logger.warning(
                "click_element click point belongs to another window, "
                "refusing (click_point_obstructed): matched=%r "
                "point=(%d, %d) expected_root=%#x root_at_point=%#x",
                winner.name,
                centre_x,
                centre_y,
                expected_root,
                root_at_point,
            )
            return self._fail(winner, "click_point_obstructed")
        # Second layer (wh-explorer-navpane-click.1.4): the root comparison
        # above cannot see a SAME-ROOT occluder -- an in-window overlay (a
        # Chromium in-page modal, a same-process floating panel) shares the
        # expected top-level window. Ask UI Automation which ELEMENT is at
        # the point; it must resolve to the winner, a descendant, or one of
        # its containers. Runs second on purpose: the Win32 root comparison
        # is cheap and refuses cross-root occluders without a COM call.
        try:
            point_hits_winner = self._point_hits_winner_fn(
                winner, centre_x, centre_y
            )
        except Exception as exc:  # noqa: BLE001 -- a real UIA seam can raise
            logger.warning(
                "click_element UIA point-hits-winner check failed, refusing "
                "(click_point_obstructed): matched=%r point=(%d, %d) "
                "repr=%.200s",
                winner.name,
                centre_x,
                centre_y,
                repr(exc),
            )
            return self._fail(winner, "click_point_obstructed")
        if not point_hits_winner:
            logger.warning(
                "click_element element at click point is not the winner or "
                "a relative of it, refusing (click_point_obstructed): "
                "matched=%r point=(%d, %d)",
                winner.name,
                centre_x,
                centre_y,
            )
            return self._fail(winner, "click_point_obstructed")
        # The coordinate-click seam is a real SendInput/Win32 call in production;
        # it can raise (OSError, RuntimeError, a ctypes error) instead of
        # returning (succeeded, events_sent). An uncaught raise here would
        # propagate out of click() and break the one-response click contract,
        # dropping the user-visible failure notice (reviewer_1 finding
        # wh-9f3t.28.2). Map any seam exception to fail_reason so click() always
        # returns a ClickResult.
        try:
            succeeded, events_sent = self._coordinate_click_fn(centre_x, centre_y)
        except Exception:  # noqa: BLE001 -- a real SendInput/Win32 seam can raise
            return self._fail(winner, fail_reason)
        # A short send is its OWN reason (v5 ``sendinput_short``), distinct from
        # a generic coordinate-click failure. SendInput reports how many events
        # it injected; fewer than the expected mouse-down+mouse-up pair means
        # the OS dropped part of the click, which is checked BEFORE the generic
        # failure so a partial click never masquerades as
        # ``invoke_then_sendinput_failed``.
        if events_sent < self._expected_click_events:
            return self._fail(winner, "sendinput_short")
        if not succeeded:
            return self._fail(winner, fail_reason)
        return ClickResult(
            outcome="ok",
            reason=None,
            matched_name=winner.name,
            clicked_via="coordinate",
        )

    # -- stronger coordinate-click eligibility (v5) --------------------------

    @staticmethod
    def _coord_eligible(winner: ElementMatch, query: ElementQuery) -> bool:
        """The v5 stronger coordinate-click eligibility check.

        Tighter than the general find() predicate (which accepts a bare
        substring+role match). The match qualifies for ANY coordinate-click
        fallback only if it has at least one of:

        * name exact match (case-insensitive) to the query name, OR
        * name starts-with match (case-insensitive), OR
        * role match AND ``is_enabled`` True.

        A bare substring+role match (without exact / starts-with / enabled-role)
        FAILS this check -- coordinate-clicking a coincidental-substring label
        could click an unrelated region (v5 finding 6 / reviewer_2 finding 5).
        """
        q_name = (query.name or "").casefold().strip()
        m_name = (winner.name or "").casefold().strip()

        if q_name and m_name:
            if m_name == q_name:
                return True
            if m_name.startswith(q_name):
                return True

        # Role match AND enabled. A role match with the control disabled is not
        # enough (it would already have failed pre-click IsEnabled anyway, but
        # the eligibility gate states this explicitly).
        #
        # The role comparison is locale-invariant (wh-l4h.1.15): query.role is a
        # canonical UIA control-type NAME, so it is mapped to its numeric id and
        # compared against winner.control_type_id -- the locale-invariant id the
        # walker reads off the control. On non-English Windows the walker
        # supplies a localized role STRING that would never equal the canonical
        # English name, but the numeric id is the same in every locale. Falls
        # back to the localized-string casefold comparison when
        # winner.control_type_id == 0 (the unknown sentinel) or query.role is not
        # in NAME_TO_CONTROL_TYPE_ID, so behavior never regresses below today's.
        if query.role is not None:
            queried_id = NAME_TO_CONTROL_TYPE_ID.get(query.role)
            if queried_id is not None and winner.control_type_id != 0:
                role_match = winner.control_type_id == queried_id
            else:
                role_match = (winner.role or "").casefold() == query.role.casefold()
            if role_match and winner.is_enabled:
                return True

        return False

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _fail(winner: ElementMatch, reason: str) -> ClickResult:
        return ClickResult(
            outcome="execution_failed",
            reason=reason,
            matched_name=winner.name,
            clicked_via=None,
        )


__all__ = [
    "ClickExecutor",
    "ClickResult",
    "DoDefaultActionUnavailable",
    "ForegroundProbe",
    "NoDefaultAction",
    "SnapshotForeground",
]
