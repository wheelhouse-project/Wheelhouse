"""Wording helper for the click-notice advisory toast (wh-lstwt).

When a ``click_element`` voice command produces a non-ok outcome, the
GUI shows an advisory notice. This module is the single source of truth
for the user-facing wording so the GUI widget
(``click_notice_toast.py``) stays a thin, declarative consumer and the
wording can be updated with no GUI test churn.

The mapping reproduces the "User-visible notice wording" table from the
v5 design doc
(``docs/plans/2026-05-21-voice-element-clicking-design-v5.md``) exactly.
This is a NEW sibling of ``rejection_toast_wording.py``; it is NOT a
reuse of the rejection wording, because the click notice has different
inputs (spoken name, matched name(s), execution-failed reason) and no
Try-it-anyway semantics.

v5 "User-visible notice wording" table:

  | Outcome                                          | Wording |
  | ok                                               | (no notice) |
  | not_found                                        | "No match for '<spoken name>'." |
  | ambiguous                                        | "Found 'A' and 'B' -- be more specific." Up to notice_max_names names. |
  | execution_failed:disabled                        | "'<matched name>' is disabled." |
  | execution_failed:bounds_invalid                  | "WheelHouse couldn't click '<matched name>' -- it may have moved." |
  | execution_failed:foreground_changed              | "Window changed before WheelHouse could click '<matched name>'." |
  | execution_failed:foreground_verification_failed  | "WheelHouse couldn't verify the active window -- if you didn't switch apps, try clicking again." |
  | execution_failed:invoke_com_error                | "WheelHouse couldn't click '<matched name>' -- the control did not respond." |
  | execution_failed:invoke_then_sendinput_failed    | Same as invoke_com_error. |
  | execution_failed:sendinput_short                 | Same as invoke_com_error. |
  | execution_failed:target_moved_offscreen          | "'<matched name>' moved off screen." |
  | execution_failed:timeout                         | "WheelHouse timed out while clicking." |
  | execution_failed:popup_closed                    | "The menu closed before WheelHouse could click '<matched name>'." (name-less: "...could click it.") |
  | execution_failed:disabled_by_config              | "Voice clicking is disabled -- check config.toml [click]." |
  | execution_failed:automation_unavailable          | "Voice clicking is unavailable on this system." |
  | execution_failed:snapshot_expired                | "The numbered overlay has expired -- say the click command again to get fresh numbers." |
  | execution_failed:malformed_response              | "Something went wrong on the click command -- check the log for details and try again." |
  | execution_failed:malformed_query                 | Same as malformed_response. |
  | execution_failed:send_request_failed             | "WheelHouse couldn't send the click request." |
  | execution_failed:dda_unavailable                 | "'<matched name>' can't be clicked by voice." (wh-dda-notice-wording) |
  | execution_failed:dda_no_default_action           | Same as dda_unavailable. |
  | execution_failed:dda_no_default_action_failed    | Same as invoke_com_error. |
  | execution_failed:dda_no_side_effect_then_sendinput_failed | Same as invoke_com_error. |
  | execution_failed:dda_unavailable_then_sendinput_failed | Same as invoke_com_error. (wh-explorer-navpane-click) |
  | execution_failed:dda_no_default_action_then_sendinput_failed | Same as invoke_com_error. |
  | execution_failed:click_point_obstructed          | "WheelHouse couldn't click '<matched name>' -- another window may be covering it." (wh-explorer-navpane-click.1.1) |

(The retired ``invoke_pattern_unavailable`` tag was removed from the
invoke_com_error alias set in wh-dda-notice-wording: click_executor.py
documents that no producer has emitted it since wh-l4h.1.17, so it now
renders the neutral fallback like any unknown tag.)

The reasons disabled_by_config, snapshot_expired, malformed_response,
and send_request_failed are Logic-synthesised: Logic emits them itself
(wh-tab7j, wh-jfavj) rather than receiving them from the Input-process
executor. malformed_query IS emitted by the Input-process click_element
handler (when the query object is not an ElementQuery), but it is an
internal/IPC-corruption error of the same class as malformed_response,
so it shares that copy (wh-9f3t.59.3). None of these reasons embed the
matched name, so their wording is independent of ``matched_name``
(wh-g4oma).

``automation_unavailable`` (wh-n29v.74.1) is, unlike the four above,
Input-emitted: only the Input process knows the IUIAutomation root could
not be built, so ``click_element`` / ``start_overlay_walk`` set it when
``_get_click_element_finder`` short-circuits on the
``_AUTOMATION_UNAVAILABLE`` sentinel. Logic forwards it unchanged
(``reason`` is an open tag set on both ``click_element.py`` and
``click_notice.py``). It is also name-independent.
"""

from __future__ import annotations

from services.wheelhouse.shared.click_notice import ClickNoticeEvent

# The ambiguous notice lists the names in ``matched_names``. The cap to
# ``[click] notice_max_names`` (v5 default 3) is applied UPSTREAM by Logic
# before the event is constructed (wh-9f3t.14.1), so by the time the event
# reaches this helper ``matched_names`` is already the final list. The
# helper therefore imposes NO cap of its own -- it renders exactly the
# names it is handed. (An earlier draft hardcoded a cap of 2 here, which
# both diverged from the v5 config default of 3 and double-trimmed a list
# Logic had already trimmed.)

# Reasons whose wording is identical to ``invoke_com_error`` per the v5
# table ("Same as invoke_com_error").
_INVOKE_COM_ERROR_ALIASES = frozenset(
    {
        "invoke_com_error",
        "invoke_then_sendinput_failed",
        "sendinput_short",
        # wh-dda-notice-wording: the DoDefaultAction press was attempted and
        # failed -- the delivery-failure analogue of invoke_com_error.
        # Honesty note (accepted tradeoff, recorded on the bead): the
        # executor's contract says the press MAY actually have fired before
        # the failure was reported, so "did not respond" can slightly
        # overstate; both bead comments accept the copy as matching the
        # retired invoke_pattern_unavailable tag's wording.
        "dda_no_default_action_failed",
        # wh-dda-notice-wording: the no-side-effect DoDefaultAction succeeded
        # but the SendInput follow-through failed -- the analogue of
        # invoke_then_sendinput_failed, already an alias.
        "dda_no_side_effect_then_sendinput_failed",
        # wh-explorer-navpane-click: both press patterns were structurally
        # absent (nothing fired) and the structural coordinate fallback's
        # click did not land. A transient delivery failure -- the mechanism
        # (a coordinate click) exists, only this send failed -- so it shares
        # the invoke_com_error copy, NOT the permanent "can't be clicked"
        # copy of the plain dda_unavailable / dda_no_default_action reasons
        # (those now mean the match also failed the coordinate eligibility
        # gate, so no mechanism is available for it).
        "dda_unavailable_then_sendinput_failed",
        "dda_no_default_action_then_sendinput_failed",
    }
)

# wh-dda-notice-wording: the control exposes neither an Invoke pattern nor a
# resolvable default action, so a voice press can never work on it. The copy
# must read PERMANENT (saying the command again will not help), unlike the
# transient "did not respond" family above.
_DDA_PERMANENT_REASONS = frozenset(
    {
        "dda_unavailable",
        "dda_no_default_action",
    }
)


def compose_click_notice_wording(event: ClickNoticeEvent) -> str:
    """Render the exact v5 user-visible string for a click notice.

    Args:
        event: the :class:`ClickNoticeEvent` carrying the outcome, the
            execution-failed reason (when applicable), the spoken name,
            and the matched name(s).

    Returns:
        The single-line notice string from the v5 "User-visible notice
        wording" table. Always returns a usable string; never raises for
        a well-formed event. An unrecognized ``execution_failed`` reason
        falls back to NEUTRAL wording that asserts no specific cause
        (wh-9f3t.14.2), so a future tag like ``snapshot_expired`` does not
        inherit the invoke_com_error "the control did not respond" copy.
    """

    if event.outcome == "not_found":
        return f"No match for '{event.spoken_name}'."

    if event.outcome == "ambiguous":
        return _compose_ambiguous(event.matched_names)

    if event.outcome == "execution_failed":
        return _compose_execution_failed(event.reason, event.matched_name)

    # Defensive: an outcome outside the closed set should never reach
    # here (the schema rejects it), but return a usable string rather
    # than raise so the GUI never crashes on a future outcome value.
    return "WheelHouse couldn't complete the click."


def _compose_ambiguous(matched_names: tuple[str, ...]) -> str:
    """Render the ambiguous notice for the names handed by Logic.

    Renders every name in ``matched_names`` (the cap to notice_max_names
    is applied upstream by Logic, wh-9f3t.14.1). The v5 table example for
    two names is "Found 'Cancel' and 'Cancel and exit' -- be more
    specific." Three or more names read as a natural list:
    "Found 'A', 'B' and 'C' -- be more specific."

    wh-9f3t.14.3: the schema accepts an empty ``matched_names``, so the
    0- and 1-name cases must degrade gracefully rather than emit "Found
    -- be more specific." (double space) or an odd dangling join:

      * 0 names -> "Found multiple matches -- be more specific."
      * 1 name  -> "Found '<name>' -- be more specific."
    """

    quoted = [f"'{name}'" for name in matched_names]
    if len(quoted) == 0:
        return "Found multiple matches -- be more specific."
    if len(quoted) == 1:
        return f"Found {quoted[0]} -- be more specific."
    if len(quoted) == 2:
        joined = " and ".join(quoted)
    else:
        # 3+ names: comma-separate all but the last, then " and " the last
        # so the list reads naturally ("'A', 'B' and 'C'").
        joined = ", ".join(quoted[:-1]) + " and " + quoted[-1]
    return f"Found {joined} -- be more specific."


def _compose_execution_failed(reason: str | None, matched_name: str | None) -> str:
    """Render the execution_failed notice for the given reason tag."""

    name = matched_name if matched_name is not None else ""

    # Branches that embed the matched name into the string require a
    # non-empty name. An empty matched_name is schema-valid (the schema
    # accepts str-or-None, and "" is a str), and although Logic sends None
    # rather than "" when no name is available, this helper runs in the GUI
    # process across the IPC boundary and must stay robust against any
    # schema-valid input. Guarding each name-using branch with ``and name``
    # makes the empty-name case fall through to the neutral fallback below
    # (which already returns the generic string when ``name`` is empty),
    # instead of emitting odd quoted-empty-string text like "'' is disabled."
    # (reviewer_2 / deepseek finding wh-9f3t.16.1). The foreground-verification
    # and timeout branches do NOT use the name, so they keep their specific
    # wording even with an empty name.
    if reason == "overlay_numbers_changed":
        # wh-overlay-fixqueue-review.2: the renumber guard blocked a
        # "click N" spoken across a proactive refresh swap. Deliberately
        # name-independent -- the spoken text is a number, not a control
        # name, and the whole point is that the number's target changed.
        return "The numbers just updated -- check the number and say it again."
    if reason == "disabled" and name:
        return f"'{name}' is disabled."
    if reason == "bounds_invalid" and name:
        return f"WheelHouse couldn't click '{name}' -- it may have moved."
    if reason == "click_point_obstructed" and name:
        # wh-explorer-navpane-click.1.1: the pre-send hit-test found a
        # different top-level window under the click point (an always-on-top
        # occluder), or could not verify the point. "may be" keeps the copy
        # honest for the seam-failure case while naming the likely cause a
        # hands-free user can act on.
        return (
            f"WheelHouse couldn't click '{name}' -- another window may be "
            "covering it."
        )
    if reason == "foreground_changed" and name:
        return f"Window changed before WheelHouse could click '{name}'."
    if reason == "foreground_verification_failed":
        return (
            "WheelHouse couldn't verify the active window -- if you didn't "
            "switch apps, try clicking again."
        )
    if reason in _DDA_PERMANENT_REASONS and name:
        return f"'{name}' can't be clicked by voice."
    if reason in _INVOKE_COM_ERROR_ALIASES and name:
        return f"WheelHouse couldn't click '{name}' -- the control did not respond."
    if reason == "target_moved_offscreen" and name:
        return f"'{name}' moved off screen."
    if reason == "timeout":
        return "WheelHouse timed out while clicking."
    if reason == "popup_closed":
        # wh-n29v.71: an owned #32768 / UIA-Menu popup that closed between the
        # walk and the click. Unlike the name-using branches above, this branch
        # is NOT guarded with ``and name``: the cause (the menu vanished) is the
        # same whether or not we captured the item's name, so a name-less
        # popup_closed must still say the menu closed -- not collapse to the
        # generic neutral string. Name the item when we have one; otherwise use
        # "it".
        if name:
            return f"The menu closed before WheelHouse could click '{name}'."
        return "The menu closed before WheelHouse could click it."

    # Logic-synthesised reasons (wh-g4oma). None of these embed the matched
    # name, so they need no ``and name`` guard and keep their specific copy
    # even when matched_name is None or "".
    if reason == "disabled_by_config":
        return "Voice clicking is disabled -- check config.toml [click]."
    if reason == "automation_unavailable":
        # wh-n29v.74.1 (deepseek reviewer_2): the Input process could not build
        # the IUIAutomation root (COM / UIAutomationCore unavailable on a
        # degraded / headless / locked-down host). This is DISTINCT from
        # disabled_by_config: clicking IS enabled in config, so pointing the
        # user at config.toml [click] would be wrong. The cause is the machine,
        # not the config, so the copy names no control and is name-independent.
        return "Voice clicking is unavailable on this system."
    if reason == "snapshot_expired":
        return (
            "The numbered overlay has expired -- say the click command "
            "again to get fresh numbers."
        )
    if reason in ("malformed_response", "malformed_query"):
        return (
            "Something went wrong on the click command -- check the log "
            "for details and try again."
        )
    if reason == "send_request_failed":
        return "WheelHouse couldn't send the click request."

    # Unrecognized reason tag (including a None reason on an
    # execution_failed event) -- the schema deliberately leaves the tag
    # set open (a downstream slice may add a reason this helper does not
    # yet have an explicit branch for).
    # wh-9f3t.14.2: fall back to NEUTRAL wording that asserts no specific
    # cause. Borrowing the invoke_com_error "the control did not respond"
    # copy would tell the user a wrong, specific cause for a failure mode
    # the helper does not actually understand. Name the control when we
    # have it; otherwise stay fully generic.
    if name:
        return f"WheelHouse couldn't click '{name}'."
    return "WheelHouse couldn't complete the click."
