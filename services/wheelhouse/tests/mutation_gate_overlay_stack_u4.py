"""Mutation evidence for the U4 boundary tests (wh-full-refactor.4.1, stage A).

Each mutation breaks one rule of UIActionHandler (ui/ui_action_handler.py) that
a row IW-n of docs/design/overlay-stack-requirements.md states. The name of a
mutation starts with the lowercase row id, and its expect list names the
boundary tests of tests/test_overlay_stack_requirements_u4.py that must fail.

The shared runner in services/stt_providers/shared/tests/mutation_gate_runner.py
owns the disciplines of the mutation-gate skill (exactly-one-match, line-start
match, compile check, collected-name check, green baseline, timeouts, bytecode
clearing, byte-exact restore). This file only declares the table, in the form
of tests/mutation_gate_overlay_requirements.py.

An EXPECTED SURVIVOR is declared with survives= and its reason.

Run from services/wheelhouse:
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_u4.py --check
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_u4.py
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_u4.py iw-20 iw-31
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Run pytest with this interpreter, so a worktree builds no new venv."""
    command = [
        sys.executable, "-m", "pytest", *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_runner_failed_names = runner._failed_names


def _failed_names(output):
    """The runner's failed-name set, with the parameter id cut at the bracket."""
    return {name.split("[", 1)[0] for name in _runner_failed_names(output)}


runner._failed_names = _failed_names

MUTATIONS = []
_U4 = "test_overlay_stack_requirements_u4.py"
FILE = "ui/ui_action_handler.py"
_NW = "ui/overlay_snapshot_actions.py"


def sel(*names):
    """The pytest node ids of the named U4 tests."""
    return tuple(f"tests/{_U4}::{name}" for name in names)


def add(name, old, new, *expect, survives=None, file=FILE):
    """One mutation of ``file``. ``expect`` are the catcher tests."""
    # The selection is the catchers plus one sanity test that must stay green.
    sanity = "test_iw_2_walk_names_the_window_it_read"
    suite = tuple(dict.fromkeys(sel(*expect, sanity)))
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


# ===========================================================================
# start_overlay_walk (IW-1 to IW-19)
# ===========================================================================

add("iw-2-walk-reply-drops-the-process-id",
    "            read_identity.update(\n"
    "                foreground_window=foreground.foreground_window,\n"
    "                foreground_pid=foreground.foreground_pid,\n",
    "            read_identity.update(\n"
    "                foreground_window=foreground.foreground_window,\n"
    "                foreground_pid=0,\n",
    "test_iw_2_walk_names_the_window_it_read", file=_NW)

add("iw-5-walk-off-by-config-reports-automation-unavailable",
    '                _emit(_failed("execution_failed", "disabled_by_config"))\n',
    '                _emit(_failed("execution_failed", "automation_unavailable"))\n',
    "test_iw_5_walk_with_the_overlay_off_is_refused_and_reads_nothing", file=_NW)

add("iw-6-walk-unavailable-reports-disabled",
    '                    _emit(_failed("execution_failed", "automation_unavailable"))\n',
    '                    _emit(_failed("execution_failed", "disabled_by_config"))\n',
    "test_iw_6_walk_with_automation_unavailable_says_so_and_does_not_retry", file=_NW)

add("iw-7-walk-deadline-is-far-in-the-future",
    "                if click_cfg is not None\n"
    "                else None\n"
    "            )\n"
    "            walk_deadline: Optional[float] = (\n"
    "                dequeue_monotonic + (walk_deadline_ms / 1000.0)\n",
    "                if click_cfg is not None\n"
    "                else None\n"
    "            )\n"
    "            walk_deadline: Optional[float] = (\n"
    "                dequeue_monotonic + (walk_deadline_ms / 1000.0) + 1000.0\n",
    "test_iw_7_walk_past_its_deadline_is_execution_failed", file=_NW)

add("iw-8-walk-deadline-counts-backward-from-dequeue",
    "                if click_cfg is not None\n"
    "                else None\n"
    "            )\n"
    "            walk_deadline: Optional[float] = (\n"
    "                dequeue_monotonic + (walk_deadline_ms / 1000.0)\n",
    "                if click_cfg is not None\n"
    "                else None\n"
    "            )\n"
    "            walk_deadline: Optional[float] = (\n"
    "                dequeue_monotonic - (walk_deadline_ms / 1000.0)\n",
    "test_iw_8_walk_deadline_follows_the_config_value", file=_NW)

add("iw-8-walk-deadline-has-no-pre-walk-margin",
    "_WALK_DEADLINE_MARGIN_MS = PRE_WALK_MARGIN_MS\n",
    "_WALK_DEADLINE_MARGIN_MS = 0\n",
    "test_iw_8_walk_deadline_follows_the_config_value",
    file="ui/click_config.py")

add("iw-7-walk-failure-reason-is-always-walk-failed",
    '                    walk.reason or "walk_failed",\n',
    '                    "walk_failed",\n',
    "test_iw_7_walk_past_its_deadline_is_execution_failed", file=_NW)

add("iw-9-transient-error-reports-walk-failed",
    '            _emit(_failed("execution_failed", "transient_com_error"))\n',
    '            _emit(_failed("execution_failed", "walk_failed"))\n',
    "test_iw_9_walk_hit_by_a_transient_window_error_is_execution_failed",
    "test_iw_18_settle_walk_with_no_list_to_read_is_execution_failed", file=_NW)

add("iw-10-unexpected-error-reports-walk-failed",
    '            _emit(_failed("error", "unexpected_error", status="error"))\n'
    "\n"
    "    def _get_overlay_walk_finder(self):",
    '            _emit(_failed("error", "walk_failed", status="error"))\n'
    "\n"
    "    def _get_overlay_walk_finder(self):",
    "test_iw_10_walk_hit_by_an_unexpected_error_is_a_status_error", file=_NW)

add("iw-14-settle-never-reports-an-unchanged-list",
    "                unchanged = held_summary is not None\n",
    "                unchanged = False\n",
    "test_iw_14_settle_walk_that_matches_the_held_list_answers_with_the_held_id", file=_NW)

add("iw-14-settle-answers-with-the-fresh-id-and-summary",
    "                snapshot_id = compare_snapshot_id\n"
    "                summary = held_summary\n",
    "                pass\n",
    "test_iw_14_settle_walk_that_matches_the_held_list_answers_with_the_held_id", file=_NW)

add("iw-17-settle-ignores-the-window-creation-time",
    "            current_foreground_window_creation_time=(\n"
    "                foreground.foreground_window_creation_time\n"
    "            ),\n"
    "        )\n"
    "        if held is None:\n",
    "        )\n"
    "        if held is None:\n",
    "test_iw_17_settle_walk_does_not_reuse_a_held_list_from_another_window", file=_NW)

# ===========================================================================
# pin_snapshot (IW-20 to IW-30)
# ===========================================================================

add("iw-20-pin-reports-not-pinned",
    '                _emit(status="ok", reason=None, pinned=True)\n',
    '                _emit(status="ok", reason=None, pinned=False)\n',
    "test_iw_20_pin_of_a_stored_snapshot_is_pinned", file=_NW)

add("iw-22-pin-invalid-request-is-a-status-ok",
    "                    overlay_session_id, snapshot_id, paint_generation,\n"
    "                )\n"
    '                _emit(status="error", reason="invalid_request", pinned=False)\n',
    "                    overlay_session_id, snapshot_id, paint_generation,\n"
    "                )\n"
    '                _emit(status="ok", reason="invalid_request", pinned=False)\n',
    "test_iw_22_pin_with_malformed_fields_is_invalid_request", file=_NW)

add("iw-23-pin-off-by-config-reports-unknown-snapshot",
    "                    overlay_session_id, snapshot_id,\n"
    "                )\n"
    '                _emit(status="ok", reason="disabled_by_config", pinned=False)\n',
    "                    overlay_session_id, snapshot_id,\n"
    "                )\n"
    '                _emit(status="ok", reason="unknown_snapshot", pinned=False)\n',
    "test_iw_23_pin_with_the_overlay_off_is_refused", file=_NW)

add("iw-24-pin-accepts-the-same-session-as-stale",
    "                if overlay_session_id < latest_session:\n",
    "                if overlay_session_id <= latest_session:\n",
    "test_iw_26_pin_with_the_same_generation_is_accepted_again", file=_NW)

add("iw-24-pin-accepts-an-older-session",
    "                if overlay_session_id < latest_session:\n",
    "                if overlay_session_id > latest_session:\n",
    "test_iw_24_pin_from_an_older_session_is_stale_session", file=_NW)

add("iw-25-pin-rejects-the-same-generation",
    "                        and paint_generation < latest_gen):\n",
    "                        and paint_generation <= latest_gen):\n",
    "test_iw_26_pin_with_the_same_generation_is_accepted_again", file=_NW)

add("iw-25-pin-accepts-an-older-generation",
    "                        and paint_generation < latest_gen):\n",
    "                        and paint_generation > latest_gen):\n",
    "test_iw_25_pin_with_an_older_generation_is_stale_generation", file=_NW)

add("iw-28-pin-never-moves-the-bar",
    "            self._latest_pin_watermark = (overlay_session_id, new_gen)\n",
    "            pass\n",
    "test_iw_25_pin_with_an_older_generation_is_stale_generation",
    "test_iw_28_pin_of_a_missing_snapshot_still_raises_the_generation_bar", file=_NW)

add("iw-27-pin-keeps-the-old-session-generation-bar",
    "            if (watermark is not None\n"
    "                    and overlay_session_id == watermark[0]):\n",
    "            if (watermark is not None\n"
    "                    and True):\n",
    "test_iw_27_pin_from_a_newer_session_resets_the_generation_bar", file=_NW)

# ===========================================================================
# unpin_snapshot and refresh_overlay_snapshot (IW-31 to IW-43)
# ===========================================================================

add("iw-32-unpin-unknown-snapshot-reports-ok",
    '                reason=None if present else "unknown_snapshot",\n',
    "                reason=None,\n",
    "test_iw_32_unpin_of_an_unknown_snapshot_is_unknown_snapshot", file=_NW)

add("iw-33-unpin-invalid-request-is-a-status-ok",
    '                _emit(status="error", reason="invalid_request")\n',
    '                _emit(status="ok", reason="invalid_request")\n',
    "test_iw_33_unpin_with_malformed_fields_is_invalid_request", file=_NW)

add("iw-34-unpin-off-by-config-reports-ok",
    '                _emit(status="ok", reason="disabled_by_config")\n',
    '                _emit(status="ok", reason=None)\n',
    "test_iw_34_unpin_with_the_overlay_off_is_refused", file=_NW)

add("iw-39-refresh-unknown-snapshot-reports-ok",
    '                reason=None if refreshed else "unknown_snapshot",\n',
    "                reason=None,\n",
    "test_iw_39_refresh_of_an_unknown_snapshot_is_unknown_snapshot", file=_NW)

add("iw-38-refresh-reports-not-found",
    "                pinned=refreshed,\n",
    "                pinned=False,\n",
    "test_iw_38_refresh_of_a_stored_snapshot_reports_it_found", file=_NW)

add("iw-40-refresh-invalid-request-is-a-status-ok",
    '                    "without touching the store (snapshot=%r)", snapshot_id,\n'
    "                )\n"
    '                _emit(status="error", reason="invalid_request", pinned=False)\n',
    '                    "without touching the store (snapshot=%r)", snapshot_id,\n'
    "                )\n"
    '                _emit(status="ok", reason="invalid_request", pinned=False)\n',
    "test_iw_40_refresh_with_a_non_string_id_is_invalid_request", file=_NW)

add("iw-42-refresh-off-by-config-reports-unknown-snapshot",
    '                    "short-circuiting (snapshot=%s)", snapshot_id,\n'
    "                )\n"
    '                _emit(status="ok", reason="disabled_by_config", pinned=False)\n',
    '                    "short-circuiting (snapshot=%s)", snapshot_id,\n'
    "                )\n"
    '                _emit(status="ok", reason="unknown_snapshot", pinned=False)\n',
    "test_iw_42_refresh_with_the_overlay_off_is_refused", file=_NW)

# ===========================================================================
# show_numbered_overlay (IW-44 to IW-52)
# ===========================================================================

add("iw-45-show-ignores-the-filter",
    "            keep = set(item_id_filter) if item_id_filter is not None else None\n",
    "            keep = None\n",
    "test_iw_45_show_with_a_filter_keeps_only_those_items_renumbered_from_one",
    "test_iw_46_show_with_an_empty_filter_is_no_targets_with_an_empty_list", file=_NW)

add("iw-46-show-empty-result-is-reported-ok",
    "            if not summary.items:\n",
    "            if False:\n",
    "test_iw_46_show_with_an_empty_filter_is_no_targets_with_an_empty_list",
    "test_iw_47_show_with_a_filter_that_matches_nothing_is_no_targets", file=_NW)

add("iw-48-show-expired-reason-is-renamed",
    '                _emit(_build("snapshot_expired", "stale_snapshot_id"))\n',
    '                _emit(_build("snapshot_expired", "expired"))\n',
    "test_iw_48_show_of_an_unknown_snapshot_is_snapshot_expired", file=_NW)

add("iw-50-show-accepts-a-non-string-snapshot-id",
    "                and isinstance(snapshot_id, str)\n"
    "                and (\n"
    "                    item_id_filter is None\n",
    "                and (\n"
    "                    item_id_filter is None\n",
    "test_iw_50_show_with_malformed_fields_is_invalid_request", file=_NW)

add("iw-51-show-off-by-config-reports-unavailable",
    '                _emit(_build("execution_failed", "disabled_by_config"))\n',
    '                _emit(_build("execution_failed", "automation_unavailable"))\n',
    "test_iw_51_show_with_the_overlay_off_is_refused", file=_NW)

add("iw-52-show-unavailable-reports-disabled",
    '                    _emit(_build("execution_failed", "automation_unavailable"))\n',
    '                    _emit(_build("execution_failed", "disabled_by_config"))\n',
    "test_iw_52_show_with_automation_unavailable_says_so", file=_NW)

# ===========================================================================
# click_snapshot_item (IW-53 to IW-73)
# ===========================================================================

add("iw-54-click-invalid-request-reports-expired",
    '                _emit(_failed("invalid_request"))\n',
    '                _emit(_failed("snapshot_expired"))\n',
    "test_iw_54_click_with_a_missing_or_bad_id_is_invalid_request", file=_NW)

add("iw-56-click-off-by-config-reports-unavailable",
    '                    "short-circuiting (trace_id=%s)", safe_trace,\n'
    "                )\n"
    '                _emit(_failed("disabled_by_config"))\n',
    '                    "short-circuiting (trace_id=%s)", safe_trace,\n'
    "                )\n"
    '                _emit(_failed("automation_unavailable"))\n',
    "test_iw_56_click_with_the_overlay_off_is_refused", file=_NW)

add("iw-57-click-unavailable-reports-disabled",
    "                        safe_trace,\n"
    "                    )\n"
    '                    _emit(_failed("automation_unavailable"))\n',
    "                        safe_trace,\n"
    "                    )\n"
    '                    _emit(_failed("disabled_by_config"))\n',
    "test_iw_57_click_with_automation_unavailable_says_so", file=_NW)

add("iw-58-click-expired-snapshot-reports-item-not-found",
    '                _emit(_failed("snapshot_expired", snapshot_id_out=snapshot_id))\n',
    '                _emit(_failed("item_not_found", snapshot_id_out=snapshot_id))\n',
    "test_iw_58_click_on_an_unknown_snapshot_is_snapshot_expired", file=_NW)

add("iw-60-click-takes-the-first-item-when-the-id-is-unknown",
    "                (m for m in snapshot.matches if m.item_id == item_id), None\n",
    "                (m for m in snapshot.matches if True), None\n",
    "test_iw_60_click_on_an_item_not_in_the_snapshot_is_item_not_found", file=_NW)

add("iw-61-click-reports-no-refusal-reason",
    "                item_id, snapshot_id, safe_trace,\n"
    "            )\n"
    "            _emit(_failed(\n"
    '                click_result.reason or "invoke_com_error",\n',
    "                item_id, snapshot_id, safe_trace,\n"
    "            )\n"
    "            _emit(_failed(\n"
    '                "invoke_com_error",\n',
    "test_iw_61_click_on_a_disabled_control_is_refused_with_the_executor_reason",
    "test_iw_62_click_on_a_control_that_moved_is_refused_as_bounds_stale", file=_NW)

add("iw-84-a-non-allowlisted-com-error-reports-a-failed-mouse-send",
    "        # match that fails the stronger eligibility check) does NOT\n"
    "        # coordinate-click.\n"
    '        return self._fail(winner, "invoke_com_error")\n',
    "        # match that fails the stronger eligibility check) does NOT\n"
    "        # coordinate-click.\n"
    '        return self._fail(winner, "invoke_then_sendinput_failed")\n',
    "test_iw_84_click_whose_press_fails_is_refused_as_invoke_com_error",
    file="ui/click_executor.py")

add("iw-84-a-press-error-that-is-not-a-com-error-reports-a-failed-mouse-send",
    "            # never reach the allowlist or any coordinate-click fallback.\n"
    '            return self._fail(winner, "invoke_com_error")\n',
    "            # never reach the allowlist or any coordinate-click fallback.\n"
    '            return self._fail(winner, "invoke_then_sendinput_failed")\n',
    "test_iw_84_click_whose_press_fails_is_refused_as_invoke_com_error",
    file="ui/click_executor.py")

add("iw-87-show-unexpected-error-reports-walk-failed",
    '            _emit(_build("error", "unexpected_error", status="error"))\n',
    '            _emit(_build("error", "walk_failed", status="error"))\n',
    "test_iw_87_show_hit_by_an_unexpected_error_is_a_status_error", file=_NW)

add("iw-88-click-unexpected-error-reports-snapshot-expired",
    "                safe_trace, snapshot_id, item_id, exc, exc_info=True,\n"
    "            )\n"
    '            _emit(_failed("invoke_com_error"))\n',
    "                safe_trace, snapshot_id, item_id, exc, exc_info=True,\n"
    "            )\n"
    '            _emit(_failed("snapshot_expired"))\n',
    "test_iw_88_click_hit_by_an_unexpected_error_is_refused_as_invoke_com_error", file=_NW)

add("iw-64-click-ignores-the-gesture",
    "        return ClickGesture(value)\n",
    "        return DEFAULT_GESTURE\n",
    "test_iw_64_click_with_a_right_click_gesture_uses_the_mouse_not_invoke",
    "test_iw_65_click_with_a_double_click_gesture_clicks_twice")

add("iw-66-click-fails-on-an-unknown-gesture",
    "    except (TypeError, ValueError):\n"
    "        logger.warning(\n"
    '            "click_snapshot_item: unrecognised gesture',
    "    except TypeError:\n"
    "        logger.warning(\n"
    '            "click_snapshot_item: unrecognised gesture',
    "test_iw_66_click_with_an_unknown_gesture_uses_invoke")

add("iw-67-click-refuses-a-newer-session",
    "                resolved_pair[0] < last[0]\n",
    "                resolved_pair[0] > last[0]\n",
    "test_iw_67_click_from_a_list_already_clicked_is_stale_overlay_generation",
    "test_iw_68_click_from_a_newer_generation_or_session_is_accepted", file=_NW)

add("iw-67-click-accepts-the-same-generation",
    "                    and resolved_pair[1] <= last[1]\n",
    "                    and resolved_pair[1] < last[1]\n",
    "test_iw_67_click_from_a_list_already_clicked_is_stale_overlay_generation", file=_NW)

add("iw-67-click-never-records-its-list",
    "                    self._last_executed_click_pair = resolved_pair\n",
    "                    pass\n",
    "test_iw_67_click_from_a_list_already_clicked_is_stale_overlay_generation", file=_NW)

add("iw-69-click-records-its-list-before-the-press",
    "            click_result = executor.click(match, snap_fg, query, badge_pick=True)\n",
    "            click_result = executor.click(match, snap_fg, query, badge_pick=True)\n"
    "            if resolved_pair is not None:\n"
    "                self._last_executed_click_pair = resolved_pair\n",
    "test_iw_69_a_refused_click_does_not_use_up_its_list", file=_NW)

add("iw-72-click-reads-a-bool-as-a-session-id",
    "            if (\n"
    "                isinstance(overlay_session_id, int)\n"
    "                and not isinstance(overlay_session_id, bool)\n",
    "            if (\n"
    "                isinstance(overlay_session_id, int)\n",
    "test_iw_72_click_with_a_bad_list_pair_skips_the_stale_check_and_is_not_recorded", file=_NW)

# badge_pick=True matters only when the control has no Invoke pattern: the
# executor then prefers the guarded coordinate click over the MSAA default
# action (IW-82).
add("iw-63-click-does-not-ask-for-the-badge-pick-order",
    "            click_result = executor.click(match, snap_fg, query, badge_pick=True)\n",
    "            click_result = executor.click(match, snap_fg, query, badge_pick=False)\n",
    "test_iw_82_click_on_a_control_with_no_invoke_uses_the_mouse_not_the_default_action", file=_NW)

# A badge pick on a control that has Invoke presses it. The shell-owned rule
# sends a badge pick coordinate-first; without its owner test every badge pick
# would, and the mouse would click.
add("iw-63-every-badge-pick-goes-coordinate-first",
    "            badge_pick\n"
    "            and winner.source_window_is_shell\n",
    "            badge_pick\n",
    "test_iw_63_click_asks_the_executor_to_press_not_to_use_the_mouse",
    file="ui/click_executor.py")


# ===========================================================================
# Follow-up: one or more mutations for every row that has a boundary test.
# ===========================================================================

_EMIT_HEAD = ('            payload["action"] = action_name\n'
              "            try:\n"
              "                self._host.response_queue.put(payload)\n")
_EMIT_EXC = "            except Exception as exc:  # noqa: BLE001\n"
_EMIT_NOTE = (
    "                # Last-resort: log and drop. Without a response the Logic-side\n"
    "                # Future times out, which Logic already handles gracefully.\n"
)


def _emitter(name, note):
    """The reply-sending block of one handler, unique through its log text."""
    log = ("                logger.error(\n"
           f'                    "{name}: failed to enqueue response "\n')
    return _EMIT_HEAD, _EMIT_EXC + (_EMIT_NOTE if note else "") + log


# IW-74 and IW-75 hold for all six messages, so each message gets its own
# mutation: the closed-queue error escapes (IW-74), or the reply carries no
# action name (IW-75).
for _name, _note in (
    ("start_overlay_walk", True), ("pin_snapshot", True),
    ("unpin_snapshot", False), ("refresh_overlay_snapshot", False),
    ("show_numbered_overlay", True), ("click_snapshot_item", True),
):
    _head, _tail = _emitter(_name, _note)
    add(f"iw-74-{_name.replace('_', '-')}-lets-a-closed-queue-error-escape",
        _head + _tail,
        _head + _tail.replace("except Exception", "except KeyError", 1),
        "test_iw_74_a_closed_reply_queue_does_not_stop_the_handler",
        file=_NW)
    add(f"iw-75-{_name.replace('_', '-')}-reply-has-no-action",
        _head + _tail,
        _head.replace('            payload["action"] = action_name\n',
                      "            pass\n") + _tail,
        "test_iw_75_each_message_gets_exactly_one_reply_with_its_request_id",
        file=_NW)

add("iw-1-walk-reply-echoes-generation-0",
    "                paint_generation=paint_generation,\n"
    "                **read_identity,\n"
    "            ))\n"
    "        except _handler_module._TRANSIENT_WALK_ERRORS as exc:\n",
    "                paint_generation=0,\n"
    "                **read_identity,\n"
    "            ))\n"
    "        except _handler_module._TRANSIENT_WALK_ERRORS as exc:\n",
    "test_iw_1_walk_numbers_every_control_and_echoes_the_request", file=_NW)

add("iw-3-walk-of-an-empty-window-is-reported-ok",
    '                    status="ok",\n'
    '                    outcome="no_targets",\n',
    '                    status="ok",\n'
    '                    outcome="ok",\n',
    "test_iw_3_walk_of_a_window_with_no_controls_is_no_targets", file=_NW)

# The overlay walk filters non-interactive controls twice: the walker drops
# them when the finder passes query_has_role=True, and the finder filters the
# combined list again. Breaking either layer alone changes nothing observable,
# so this mutation breaks the shared predicate both layers call.
add("iw-4-interactive-filter-keeps-every-control-type",
    "    return control_type_id in INTERACTIVE_CONTROL_TYPE_IDS\n",
    "    return True\n",
    "test_iw_4_walk_skips_a_control_that_is_not_interactive",
    file="ui/uia_walker.py")

add("iw-11-transient-walk-errors-lack-comerror",
    "    _TRANSIENT_WALK_ERRORS: tuple[type[BaseException], ...] = (OSError, _COMError)\n",
    "    _TRANSIENT_WALK_ERRORS: tuple[type[BaseException], ...] = (OSError,)\n",
    "test_iw_11_walk_of_a_window_the_os_cannot_resolve_is_execution_failed")

add("iw-12-every-walk-gets-the-same-snapshot-id",
    "            for i, match in enumerate(clickable, start=1)\n"
    "        ]\n"
    '        snapshot_id = f"walk-{self._snapshot_salt}-{next(self._snapshot_counter)}"\n',
    "            for i, match in enumerate(clickable, start=1)\n"
    "        ]\n"
    '        snapshot_id = f"walk-{self._snapshot_salt}-fixed"\n',
    "test_iw_12_every_walk_gets_a_new_snapshot_id",
    file="ui/element_finder.py")

add("iw-13-settle-walk-reads-once",
    "            settled = settle_detector.wait_for_settled_window(\n"
    "                _read,\n"
    "                events_between=(\n"
    "                    listener.any_event_between if available else None\n"
    "                ),\n"
    "                now_ms=lambda: self._clock() * 1000.0,\n"
    "            )\n",
    "            _read()\n",
    "test_iw_13_settle_walk_reads_until_two_reads_agree", file=_NW)

add("iw-15-settle-walk-answers-with-the-held-id-when-the-list-differs",
    "        if settle_detector.signature_of(\n"
    "            held.matches\n"
    "        ) != settle_detector.signature_of(walk.snapshot.matches):\n"
    "            return walk, None\n",
    "        pass\n",
    "test_iw_15_settle_walk_that_differs_from_the_held_list_answers_fresh", file=_NW)

# Under this mutant the unknown held id reaches held.matches, the handler's
# catch-all answers status error, and the test's first assertion (outcome ok)
# fails on that reply.
add("iw-16-settle-walk-uses-a-held-list-that-is-gone",
    "        if held is None:\n"
    "            # The held snapshot expired (TTL) or was unpinned and evicted\n",
    "        if False:\n"
    "            # The held snapshot expired (TTL) or was unpinned and evicted\n",
    "test_iw_16_settle_walk_with_an_unknown_held_id_answers_fresh", file=_NW)

add("iw-19-settle-walk-drops-the-last-read-when-a-later-read-fails",
    "            if not last:\n"
    "                # Nothing completed, so there is no list to paint and nothing\n",
    "            if True:\n"
    "                # Nothing completed, so there is no list to paint and nothing\n",
    "test_iw_19_settle_walk_answers_with_the_last_read_when_a_later_read_fails", file=_NW)

add("iw-21-pin-unknown-snapshot-reports-stale-session",
    '                _emit(status="ok", reason="unknown_snapshot", pinned=False)\n',
    '                _emit(status="ok", reason="stale_session", pinned=False)\n',
    "test_iw_21_pin_of_an_unknown_snapshot_is_unknown_snapshot", file=_NW)

add("iw-29-pin-invalid-request-moves-the-generation-bar",
    "                    overlay_session_id, snapshot_id, paint_generation,\n"
    "                )\n"
    '                _emit(status="error", reason="invalid_request", pinned=False)\n',
    "                    overlay_session_id, snapshot_id, paint_generation,\n"
    "                )\n"
    "                self._latest_pin_watermark = (99, 99)\n"
    '                _emit(status="error", reason="invalid_request", pinned=False)\n',
    "test_iw_29_pin_with_malformed_fields_does_not_move_the_bar", file=_NW)

add("iw-30-stale-pin-still-pins-the-snapshot",
    '                    _emit(status="ok", reason="stale_generation", pinned=False)\n',
    "                    finder.pin(snapshot_id)\n"
    '                    _emit(status="ok", reason="stale_generation", pinned=False)\n',
    "test_iw_30_stale_pin_leaves_the_snapshot_unpinned", file=_NW)

add("iw-31-unpin-reports-pinned",
    '                # An unpin\'s resulting pin state is always "not pinned".\n'
    "                pinned=False,\n",
    '                # An unpin\'s resulting pin state is always "not pinned".\n'
    "                pinned=True,\n",
    "test_iw_31_unpin_of_a_pinned_snapshot_is_ok_and_not_pinned", file=_NW)

add("iw-35-unpin-is-held-back-by-the-pin-session-bar",
    "            present = finder.unpin(snapshot_id)\n",
    '            mark = getattr(self, "_latest_pin_watermark", None)\n'
    "            if mark is not None and overlay_session_id < mark[0]:\n"
    '                _emit(status="ok", reason="stale_session")\n'
    "                return\n"
    "            present = finder.unpin(snapshot_id)\n",
    "test_iw_35_unpin_is_not_held_back_by_the_pin_generation_bar", file=_NW)

add("iw-36-pin-does-not-pin-the-store-entry",
    "            pinned = finder.pin(snapshot_id)\n",
    '            pinned = snapshot_id != ""\n',
    "test_iw_36_a_pinned_snapshot_survives_store_eviction", file=_NW)

add("iw-37-unpin-does-not-clear-the-store-pin",
    "            present = finder.unpin(snapshot_id)\n",
    "            present = True\n",
    "test_iw_37_an_unpinned_snapshot_is_evicted_when_the_store_is_full", file=_NW)

add("iw-41-refresh-checks-the-session-id",
    "            if not isinstance(snapshot_id, str):\n"
    "                logger.error(\n"
    '                    "refresh_overlay_snapshot: invalid snapshot_id; rejecting "\n',
    "            if (not isinstance(snapshot_id, str)\n"
    "                    or not isinstance(overlay_session_id, int)):\n"
    "                logger.error(\n"
    '                    "refresh_overlay_snapshot: invalid snapshot_id; rejecting "\n',
    "test_iw_41_refresh_with_a_bad_session_id_is_not_rejected", file=_NW)

add("iw-43-refresh-is-held-back-by-the-pin-session-bar",
    "            refreshed = finder.refresh_snapshot_ttl(snapshot_id)\n",
    '            mark = getattr(self, "_latest_pin_watermark", None)\n'
    "            if mark is not None and overlay_session_id < mark[0]:\n"
    '                _emit(status="ok", reason="stale_session", pinned=False)\n'
    "                return\n"
    "            refreshed = finder.refresh_snapshot_ttl(snapshot_id)\n",
    "test_iw_43_refresh_is_not_held_back_by_the_pin_generation_bar", file=_NW)

add("iw-44-show-paints-only-the-first-item",
    "                summary, [m.item_id for m in survivors]\n",
    "                summary, [m.item_id for m in survivors][:1]\n",
    "test_iw_44_show_repaints_the_whole_stored_snapshot", file=_NW)

_IDENTITY_CALL = (
    "            snapshot = finder.get_snapshot(\n"
    "                snapshot_id,\n"
    "                current_foreground_window=foreground.foreground_window,\n"
    "                current_foreground_pid=foreground.foreground_pid,\n"
    "                current_foreground_process_name=(\n"
    "                    foreground.foreground_process_name\n"
    "                ),\n"
    "                current_foreground_window_creation_time=(\n"
    "                    foreground.foreground_window_creation_time\n"
    "                ),\n"
    "            )\n"
)
_NO_IDENTITY_CALL = "            snapshot = finder.get_snapshot(snapshot_id)\n"
_SHOW_LEAD = "                trace_id,\n            )\n\n"
_CLICK_LEAD = (
    '                        "for snapshot=%s: %s", snapshot_id, exc,\n'
    "                    )\n"
)

add("iw-49-show-ignores-a-changed-foreground-window",
    _SHOW_LEAD + _IDENTITY_CALL, _SHOW_LEAD + _NO_IDENTITY_CALL,
    "test_iw_49_show_after_the_foreground_window_changed_is_snapshot_expired",
    "test_iw_81_a_snapshot_dropped_by_a_foreground_change_stays_dropped", file=_NW)

add("iw-81-foreground-mismatch-does-not-drop-the-snapshot",
    "            self._drop(snapshot_id)\n"
    "            return None\n"
    "        # Hit: mark most-recently-used (move to the end of the recency order).\n",
    "            pass\n"
    "            return None\n"
    "        # Hit: mark most-recently-used (move to the end of the recency order).\n",
    "test_iw_81_a_snapshot_dropped_by_a_foreground_change_stays_dropped",
    file="ui/element_finder.py")

add("iw-53-click-reply-has-no-snapshot-id",
    "                    snapshot_id=snapshot_id,\n"
    "                    snapshot_summary=None,\n"
    "                    matched_name=click_result.matched_name,\n",
    "                    snapshot_id=None,\n"
    "                    snapshot_summary=None,\n"
    "                    matched_name=click_result.matched_name,\n",
    "test_iw_53_click_presses_the_named_item_and_reports_ok", file=_NW)

add("iw-55-click-bad-trace-id-becomes-bad",
    '        safe_trace = trace_id if isinstance(trace_id, str) else ""\n'
    "\n"
    '        def _emit(response: "ClickElementResponse") -> None:\n',
    '        safe_trace = trace_id if isinstance(trace_id, str) else "bad"\n'
    "\n"
    '        def _emit(response: "ClickElementResponse") -> None:\n',
    "test_iw_55_click_with_a_bad_trace_id_still_gets_a_string_trace_id", file=_NW)

add("iw-59-click-ignores-a-changed-foreground-window",
    _CLICK_LEAD + _IDENTITY_CALL, _CLICK_LEAD + _NO_IDENTITY_CALL,
    "test_iw_59_click_after_the_foreground_window_changed_is_snapshot_expired", file=_NW)

add("iw-70-click-without-a-list-pair-uses-the-pair-0-0",
    "            resolved_pair: Optional[tuple[int, int]] = None\n",
    "            resolved_pair: Optional[tuple[int, int]] = (0, 0)\n",
    "test_iw_70_click_without_a_list_pair_skips_the_stale_check", file=_NW)

add("iw-71-click-without-a-list-pair-resets-the-stale-check",
    '            if click_result.outcome == "ok":\n'
    "                if resolved_pair is not None:\n",
    '            if click_result.outcome == "ok":\n'
    "                if True:\n",
    "test_iw_71_click_without_a_list_pair_does_not_reset_the_stale_check", file=_NW)

add("iw-73-click-skips-the-stale-check-before-the-store-lookup",
    "            if resolved_pair is not None and last is not None and (\n",
    "            if False and resolved_pair is not None and last is not None and (\n",
    "test_iw_73_click_checks_the_list_pair_before_the_overlay_gate_and_the_store",
    "test_iw_67_click_from_a_list_already_clicked_is_stale_overlay_generation", file=_NW)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
