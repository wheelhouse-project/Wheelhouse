"""Mutation evidence for the U7 boundary tests (wh-full-refactor.6.1, stage A).

Each mutation breaks one rule of UIActionHandler (ui/ui_action_handler.py) for
the four pointer actions (click_point, click_at_pointer, move_pointer,
perform_drag) and for channel_probe, which the rows MP-n state. Since
wh-full-refactor.6.2 the bodies of the four pointer actions are in
MousePointerActions (ui/mouse_pointer_actions.py), and the mutations of those
bodies name that file (file=MPA_FILE). The name of a
mutation starts with the lowercase row id, and its expect list names the
boundary tests of tests/test_mouse_grid_requirements_u7.py that must fail.

The shared runner in services/stt_providers/shared/tests/mutation_gate_runner.py
owns the disciplines of the mutation-gate skill (exactly-one-match, line-start
match, compile check, collected-name check, green baseline, timeouts, bytecode
clearing, byte-exact restore). This file only declares the table, in the form
of tests/mutation_gate_overlay_stack_u4.py.

An EXPECTED SURVIVOR is declared with survives= and its reason. The runner
fails the sweep when any test of its selection fails under it.

Run from services/wheelhouse:
  .venv/Scripts/python.exe tests/mutation_gate_mouse_grid_u7.py --check
  .venv/Scripts/python.exe tests/mutation_gate_mouse_grid_u7.py
  .venv/Scripts/python.exe tests/mutation_gate_mouse_grid_u7.py mp-10 mp-12
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
_U7 = "test_mouse_grid_requirements_u7.py"
FILE = "ui/ui_action_handler.py"
# wh-full-refactor.6.2: the four pointer actions moved to MousePointerActions.
MPA_FILE = "ui/mouse_pointer_actions.py"

# Test names (the row id leads each name).
T1 = "test_mp_1_success_replies_ok_with_the_trace_id"
T2 = "test_mp_2_primitive_refusal_reaches_logic_as_execution_failed"
T3 = "test_mp_3_refusal_without_a_reason_is_unexpected_error"
T4 = "test_mp_4_primitive_that_raises_is_sendinput_error"
T5 = "test_mp_5_malformed_primitive_return_is_unexpected_error"
T6 = "test_mp_6_trace_id_that_is_not_a_string_becomes_empty"
T7 = "test_mp_7_release_failed_reaches_logic_unchanged"
T8 = "test_mp_8_one_reply_per_message_in_every_outcome"
T10 = "test_mp_10_grid_actions_refuse_when_the_grid_is_off"
T11 = "test_mp_11_grid_actions_run_when_only_an_overlay_key_is_off"
T12 = "test_mp_12_click_at_pointer_refuses_when_voice_clicking_is_off"
T13 = "test_mp_13_click_at_pointer_still_clicks_with_a_bad_grid_key"
T15 = "test_mp_15_click_point_passes_its_arguments_unconverted"
T16 = "test_mp_16_click_point_defaults_to_one_left_click"
T17 = "test_mp_17_click_at_pointer_clicks_where_the_pointer_is"
T18 = "test_mp_18_unreadable_pointer_sends_no_click"
T19 = "test_mp_19_move_pointer_moves_to_the_point_and_presses_nothing"
T20 = "test_mp_20_perform_drag_passes_the_points_and_the_duration"
T21 = "test_mp_21_perform_drag_uses_250_ms_when_logic_omits_the_duration"
T22 = "test_mp_22_probe_before_any_action_reports_no_record"
T23 = "test_mp_23_probe_trace_id_that_is_not_a_string_becomes_empty"
T24 = "test_mp_24_probe_reports_the_last_action_and_its_failure"
T25 = "test_mp_25_probe_reports_success_as_succeeded_true_and_no_reason"
T26 = "test_mp_26_probe_reports_only_the_most_recent_action"
T27 = "test_mp_27_a_refusal_by_config_is_also_recorded"
T28 = "test_mp_28_a_trace_id_that_is_not_a_string_is_recorded_as_empty"
T29 = "test_mp_29_the_record_survives_a_reply_that_could_not_be_queued"
T30 = "test_mp_30_a_probe_reply_that_could_not_be_queued_is_dropped_quietly"


def sel(*names):
    """The pytest node ids of the named U7 tests."""
    return tuple(f"tests/{_U7}::{name}" for name in names)


def add(name, old, new, *expect, survives=None, file=FILE):
    """One mutation of ``file``. ``expect`` are the catcher tests."""
    # The selection is the catchers plus one sanity test that must stay green
    # unless the mutation touches what it pins.
    sanity = "test_mp_9_unknown_message_keys_are_ignored"
    suite = tuple(dict.fromkeys(sel(*expect, sanity)))
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


# ===========================================================================
# The reply builder _emit_mouse_action_response and the record (MP-1 to MP-9,
# MP-24 to MP-29)
# ===========================================================================

add("mp-1-reply-status-is-inverted",
    '            status="ok" if succeeded else "error",\n',
    '            status="error" if succeeded else "ok",\n', T1, T2, file=MPA_FILE)

add("mp-1-reply-outcome-is-inverted",
    '            outcome="ok" if succeeded else "execution_failed",\n',
    '            outcome="execution_failed" if succeeded else "ok",\n', T1, T2, file=MPA_FILE)

add("mp-3-refusal-without-a-reason-keeps-the-empty-reason",
    '            reason=None if succeeded else (reason or "unexpected_error"),\n',
    "            reason=reason,\n", T3, file=MPA_FILE)

add("mp-2-reply-drops-the-primitive-reason",
    '            reason=None if succeeded else (reason or "unexpected_error"),\n',
    '            reason=None if succeeded else "unexpected_error",\n', T2, T7, file=MPA_FILE)

add("mp-6-reply-keeps-a-trace-id-that-is-not-a-string",
    '            trace_id=trace_id if isinstance(trace_id, str) else "",\n',
    "            trace_id=trace_id,\n", T6, file=MPA_FILE)

add("mp-1-reply-drops-the-action",
    '            "trace_id": payload["trace_id"],\n'
    '        }\n'
    '        if request_id is not None:\n'
    '            payload["request_id"] = request_id\n'
    '        payload["action"] = action_name\n',
    '            "trace_id": payload["trace_id"],\n'
    '        }\n'
    '        if request_id is not None:\n'
    '            payload["request_id"] = request_id\n'
    '        payload["action"] = ""\n', T1, file=MPA_FILE)

add("mp-1-reply-drops-the-request-id",
    '        if request_id is not None:\n'
    '            payload["request_id"] = request_id\n'
    '        payload["action"] = action_name\n',
    '        payload["action"] = action_name\n', T1, file=MPA_FILE)

add("mp-24-record-always-says-succeeded",
    '            "succeeded": bool(succeeded),\n',
    '            "succeeded": True,\n', T24, T27, file=MPA_FILE)

add("mp-24-record-drops-the-reason",
    '            "reason": payload["reason"],\n',
    '            "reason": None,\n', T24, T27, file=MPA_FILE)

add("mp-24-record-names-the-wrong-action",
    '        self.last_mouse_action_result = {\n'
    '            "action": action_name,\n',
    '        self.last_mouse_action_result = {\n'
    '            "action": "click_point",\n', T24, T26, T27, file=MPA_FILE)

add("mp-28-record-keeps-a-trace-id-that-is-not-a-string",
    '            "trace_id": payload["trace_id"],\n',
    '            "trace_id": trace_id,\n', T28, file=MPA_FILE)

add("mp-29-record-is-written-only-after-the-reply-is-queued",
    '        self.last_mouse_action_result = {\n'
    '            "action": action_name,\n'
    '            "succeeded": bool(succeeded),\n'
    '            "reason": payload["reason"],\n'
    '            "trace_id": payload["trace_id"],\n'
    '        }\n'
    '        if request_id is not None:\n'
    '            payload["request_id"] = request_id\n'
    '        payload["action"] = action_name\n'
    '        try:\n'
    '            self._host.response_queue.put(payload)\n',
    '        _record = {\n'
    '            "action": action_name,\n'
    '            "succeeded": bool(succeeded),\n'
    '            "reason": payload["reason"],\n'
    '            "trace_id": payload["trace_id"],\n'
    '        }\n'
    '        if request_id is not None:\n'
    '            payload["request_id"] = request_id\n'
    '        payload["action"] = action_name\n'
    '        try:\n'
    '            self._host.response_queue.put(payload)\n'
    '            self.last_mouse_action_result = _record\n', T29, file=MPA_FILE)

add("mp-29-reply-that-cannot-be-queued-escapes-the-handler",
    '        except Exception as exc:  # noqa: BLE001 -- log and drop\n'
    '            logger.error(\n'
    '                "%s: failed to enqueue response (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- log and drop\n'
    '            logger.error(\n'
    '                "%s: failed to enqueue response (trace_id=%s): %s",\n', T29, file=MPA_FILE)

# ===========================================================================
# channel_probe (MP-22 to MP-30)
# ===========================================================================

add("mp-22-probe-reply-status-is-not-ok",
    '            "status": "ok",\n'
    '            "trace_id": trace_id if isinstance(trace_id, str) else "",\n'
    '            "last_mouse_action": self.mouse_pointer.last_mouse_action_result,\n',
    '            "status": "error",\n'
    '            "trace_id": trace_id if isinstance(trace_id, str) else "",\n'
    '            "last_mouse_action": self.mouse_pointer.last_mouse_action_result,\n', T22)

add("mp-23-probe-keeps-a-trace-id-that-is-not-a-string",
    '            "trace_id": trace_id if isinstance(trace_id, str) else "",\n'
    '            "last_mouse_action": self.mouse_pointer.last_mouse_action_result,\n',
    '            "trace_id": trace_id,\n'
    '            "last_mouse_action": self.mouse_pointer.last_mouse_action_result,\n', T23)

add("mp-24-probe-reports-no-record",
    '            "last_mouse_action": self.mouse_pointer.last_mouse_action_result,\n',
    '            "last_mouse_action": None,\n', T24, T25, T26, T27)

add("mp-22-probe-reply-names-another-action",
    '            "action": "channel_probe",\n',
    '            "action": "probe",\n', T22)

add("mp-30-probe-reply-that-cannot-be-queued-escapes-the-handler",
    '        except Exception as exc:  # noqa: BLE001 -- log and drop\n'
    '            logger.error(\n'
    '                "channel_probe: failed to enqueue response (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- log and drop\n'
    '            logger.error(\n'
    '                "channel_probe: failed to enqueue response (trace_id=%s): %s",\n',
    T30)

# ===========================================================================
# The three SendInput wrappers (MP-4, MP-15, MP-19, MP-20)
# ===========================================================================

add("mp-15-click-wrapper-converts-the-point-to-integers",
    "        return click_point(x, y, button=button, click_count=click_count)\n",
    "        return click_point(\n"
    "            int(x), int(y), button=button, click_count=click_count)\n", T15)

add("mp-4-click-wrapper-lets-a-raise-escape",
    '    except Exception:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_click_point: seam failed", exc_info=True)\n',
    '    except KeyError:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_click_point: seam failed", exc_info=True)\n', T4)

add("mp-4-click-wrapper-reports-another-reason",
    '        logger.error("_win32_click_point: seam failed", exc_info=True)\n'
    '        return (False, "sendinput_error")\n',
    '        logger.error("_win32_click_point: seam failed", exc_info=True)\n'
    '        return (False, "unexpected_error")\n', T4)

add("mp-4-move-wrapper-lets-a-raise-escape",
    '    except Exception:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_move_pointer: seam failed", exc_info=True)\n',
    '    except KeyError:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_move_pointer: seam failed", exc_info=True)\n', T4)

add("mp-4-move-wrapper-reports-another-reason",
    '        logger.error("_win32_move_pointer: seam failed", exc_info=True)\n'
    '        return (False, "sendinput_error")\n',
    '        logger.error("_win32_move_pointer: seam failed", exc_info=True)\n'
    '        return (False, "unexpected_error")\n', T4)

add("mp-4-drag-wrapper-lets-a-raise-escape",
    '    except Exception:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_drag_pointer: seam failed", exc_info=True)\n',
    '    except KeyError:  # noqa: BLE001 -- fail soft, never propagate\n'
    '        logger.error("_win32_drag_pointer: seam failed", exc_info=True)\n', T4)

add("mp-4-drag-wrapper-reports-another-reason",
    '        logger.error("_win32_drag_pointer: seam failed", exc_info=True)\n'
    '        return (False, "sendinput_error")\n',
    '        logger.error("_win32_drag_pointer: seam failed", exc_info=True)\n'
    '        return (False, "unexpected_error")\n', T4)

add("mp-19-move-wrapper-swaps-the-coordinates",
    "        return move_pointer_to(x, y)\n",
    "        return move_pointer_to(y, x)\n", T19)

add("mp-20-drag-wrapper-drops-the-duration",
    "            start_x, start_y, end_x, end_y, duration_ms=duration_ms,\n",
    "            start_x, start_y, end_x, end_y,\n", T20)

add("mp-17-pointer-read-failure-answers-the-origin",
    '        logger.error("_win32_pointer_position: GetCursorPos failed",\n'
    '                     exc_info=True)\n'
    '        return None\n',
    '        logger.error("_win32_pointer_position: GetCursorPos failed",\n'
    '                     exc_info=True)\n'
    '        return (0, 0)\n', T18)

add("mp-17-pointer-read-skips-the-integer-conversion",
    "        return (int(x), int(y))\n",
    "        return (x, y)\n", T17,
    survives="The operating system answers GetCursorPos with integers, so no "
             "input a boundary test can fake distinguishes the int() call; a "
             "fake that answers strings is not an operating system input.")


# ===========================================================================
# click_point
# ===========================================================================

_CP = 'action_name = "click_point"'
_CP_GATE = (
    '                    "click_point: voice clicking disabled by config; "\n'
    '                    "short-circuiting (trace_id=%s)", trace_id,\n'
    '                )\n'
    '                self._emit_mouse_action_response(\n'
    '                    action_name=action_name, request_id=request_id,\n'
    '                    trace_id=trace_id, succeeded=False,\n'
    '                    reason="disabled_by_config",\n')

add("mp-10-click-point-gate-reads-enabled-only",
    '        action_name = "click_point"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "click_point"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().enabled:\n', T10, file=MPA_FILE)

add("mp-10-click-point-gate-is-removed",
    '        action_name = "click_point"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "click_point"\n'
    '        try:\n'
    '            if False:\n', T10, file=MPA_FILE)

add("mp-10-click-point-gate-reports-another-reason",
    _CP_GATE,
    _CP_GATE.replace("disabled_by_config\",", "unexpected_error\","), T10, T27, file=MPA_FILE)

add("mp-2-click-point-reply-drops-the-primitive-result",
    '            logger.info(\n'
    '                "click_point: point=(%r,%r) button=%s count=%r ok=%s "\n'
    '                "reason=%s (trace_id=%s)",\n'
    '                x, y, button, click_count, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,\n',
    '            logger.info(\n'
    '                "click_point: point=(%r,%r) button=%s count=%r ok=%s "\n'
    '                "reason=%s (trace_id=%s)",\n'
    '                x, y, button, click_count, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=True, reason=reason,\n', T2, T7, file=MPA_FILE)

add("mp-15-click-point-sends-the-click-count-as-one",
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, click_count,\n'
    '            )\n'
    '            logger.info(\n'
    '                "click_point: point=',
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, 1,\n'
    '            )\n'
    '            logger.info(\n'
    '                "click_point: point=', T15, file=MPA_FILE)

add("mp-15-click-point-sends-the-wrong-button",
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, click_count,\n'
    '            )\n'
    '            logger.info(\n'
    '                "click_point: point=',
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, "left", click_count,\n'
    '            )\n'
    '            logger.info(\n'
    '                "click_point: point=', T15, file=MPA_FILE)

add("mp-16-click-point-default-button-is-right",
    # The UIActionHandler.click_point delegator has its own default and sends
    # button=button to MousePointerActions.click_point, so the default that a
    # caller sees is the default of the delegator.
    '        button: str = "left",\n'
    '        click_count: int = 1,\n'
    '        trace_id: str = "",\n'
    '        request_id: Optional[str] = None,\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Click at a screen point; calls ``MousePointerActions.click_point``."""\n',
    '        button: str = "right",\n'
    '        click_count: int = 1,\n'
    '        trace_id: str = "",\n'
    '        request_id: Optional[str] = None,\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Click at a screen point; calls ``MousePointerActions.click_point``."""\n', T16)

add("mp-5-click-point-handler-lets-a-raise-escape",
    '        except Exception as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "click_point: unexpected error (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "click_point: unexpected error (trace_id=%s): %s",\n', T5, file=MPA_FILE)

add("mp-5-click-point-handler-reports-another-reason",
    '                "click_point: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="unexpected_error",\n',
    '                "click_point: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="sendinput_error",\n', T5, file=MPA_FILE)

add("mp-31-click-point-does-not-invalidate-the-shadow-buffer",
    '            # wh-review-pattern-fixes.8: a grid click can move focus, the\n'
    '            # caret, or the selection; invalidate before the dispatch (see\n'
    '            # the hotkey_action block comment for the full rationale).\n'
    '            self._host.buffer_manager.invalidate()\n',
    '            # wh-review-pattern-fixes.8: a grid click can move focus, the\n'
    '            # caret, or the selection; invalidate before the dispatch (see\n'
    '            # the hotkey_action block comment for the full rationale).\n'
    '            pass\n', T1,
    survives="MP-31 has no boundary test: the invalidation leaves the handler "
             "only as a later UI Automation re-sync on a dictation message, "
             "which needs a live focused control that no operating system "
             "fake in the harness provides.", file=MPA_FILE)

# ===========================================================================
# click_at_pointer
# ===========================================================================

_AP_GATE = (
    '                    "click_at_pointer: voice clicking disabled by config; "\n'
    '                    "short-circuiting (trace_id=%s)", trace_id,\n'
    '                )\n'
    '                self._emit_mouse_action_response(\n'
    '                    action_name=action_name, request_id=request_id,\n'
    '                    trace_id=trace_id, succeeded=False,\n'
    '                    reason="disabled_by_config",\n')

add("mp-12-click-at-pointer-gate-reads-the-grid-switch",
    '            if not self._host.get_validated_click_config().enabled:\n',
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    T13, file=MPA_FILE)

add("mp-12-click-at-pointer-gate-is-removed",
    '            if not self._host.get_validated_click_config().enabled:\n',
    '            if False:\n', T12, file=MPA_FILE)

add("mp-12-click-at-pointer-gate-reports-another-reason",
    _AP_GATE,
    _AP_GATE.replace("disabled_by_config\",", "unexpected_error\","), T12, T27, file=MPA_FILE)

add("mp-18-unreadable-pointer-is-not-checked",
    '            if position is None:\n',
    '            if False:\n', T18, file=MPA_FILE)

add("mp-18-unreadable-pointer-reports-another-reason",
    '                    reason="pointer_unavailable",\n',
    '                    reason="unexpected_error",\n', T18, file=MPA_FILE)

add("mp-17-click-at-pointer-clicks-at-the-origin",
    '            x, y = position\n',
    '            x, y = 0, 0\n', T17, file=MPA_FILE)

add("mp-17-click-at-pointer-sends-the-wrong-button",
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, click_count,\n',
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, "left", click_count,\n', T17, file=MPA_FILE)

add("mp-17-click-at-pointer-sends-the-click-count-as-one",
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, click_count,\n',
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            self._host.buffer_manager.invalidate()\n'
    '            succeeded, reason = self.mouse_click_seam(\n'
    '                x, y, button, 1,\n', T17, file=MPA_FILE)

add("mp-2-click-at-pointer-reply-drops-the-primitive-result",
    '            logger.info(\n'
    '                "click_at_pointer: point=(%r,%r) button=%s count=%r ok=%s "\n'
    '                "reason=%s (trace_id=%s)",\n'
    '                x, y, button, click_count, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,\n',
    '            logger.info(\n'
    '                "click_at_pointer: point=(%r,%r) button=%s count=%r ok=%s "\n'
    '                "reason=%s (trace_id=%s)",\n'
    '                x, y, button, click_count, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=True, reason=reason,\n', T2, T7, file=MPA_FILE)

add("mp-5-click-at-pointer-handler-lets-a-raise-escape",
    '        except Exception as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "click_at_pointer: unexpected error (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "click_at_pointer: unexpected error (trace_id=%s): %s",\n', T5, file=MPA_FILE)

add("mp-5-click-at-pointer-handler-reports-another-reason",
    '                "click_at_pointer: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="unexpected_error",\n',
    '                "click_at_pointer: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="sendinput_error",\n', T5, file=MPA_FILE)

add("mp-31-click-at-pointer-does-not-invalidate-the-shadow-buffer",
    '            # Same invalidation as click_point: a click can move focus, the\n'
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            self._host.buffer_manager.invalidate()\n',
    '            # Same invalidation as click_point: a click can move focus, the\n'
    '            # caret, or the selection (wh-review-pattern-fixes.8).\n'
    '            pass\n', T1,
    survives="MP-31 has no boundary test: the invalidation leaves the handler "
             "only as a later UI Automation re-sync on a dictation message, "
             "which needs a live focused control that no operating system "
             "fake in the harness provides.", file=MPA_FILE)

# ===========================================================================
# move_pointer
# ===========================================================================

_MV_GATE = (
    '                    "move_pointer: voice clicking disabled by config; "\n'
    '                    "short-circuiting (trace_id=%s)", trace_id,\n'
    '                )\n'
    '                self._emit_mouse_action_response(\n'
    '                    action_name=action_name, request_id=request_id,\n'
    '                    trace_id=trace_id, succeeded=False,\n'
    '                    reason="disabled_by_config",\n')

add("mp-10-move-pointer-gate-reads-enabled-only",
    '        action_name = "move_pointer"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "move_pointer"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().enabled:\n', T10, file=MPA_FILE)

add("mp-10-move-pointer-gate-is-removed",
    '        action_name = "move_pointer"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "move_pointer"\n'
    '        try:\n'
    '            if False:\n', T10, file=MPA_FILE)

add("mp-10-move-pointer-gate-reports-another-reason",
    _MV_GATE,
    _MV_GATE.replace("disabled_by_config\",", "unexpected_error\","), T10, T27, file=MPA_FILE)

add("mp-19-move-pointer-swaps-the-coordinates",
    "            succeeded, reason = self.mouse_move_seam(x, y)\n",
    "            succeeded, reason = self.mouse_move_seam(y, x)\n", T19, file=MPA_FILE)

add("mp-2-move-pointer-reply-drops-the-primitive-result",
    '            logger.info(\n'
    '                "move_pointer: point=(%r,%r) ok=%s reason=%s (trace_id=%s)",\n'
    '                x, y, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,\n',
    '            logger.info(\n'
    '                "move_pointer: point=(%r,%r) ok=%s reason=%s (trace_id=%s)",\n'
    '                x, y, succeeded, reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=True, reason=reason,\n', T2, T7, file=MPA_FILE)

add("mp-5-move-pointer-handler-lets-a-raise-escape",
    '        except Exception as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "move_pointer: unexpected error (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "move_pointer: unexpected error (trace_id=%s): %s",\n', T5, file=MPA_FILE)

add("mp-5-move-pointer-handler-reports-another-reason",
    '                "move_pointer: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="unexpected_error",\n',
    '                "move_pointer: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="sendinput_error",\n', T5, file=MPA_FILE)

# ===========================================================================
# perform_drag
# ===========================================================================

_DR_GATE = (
    '                    "perform_drag: voice clicking disabled by config; "\n'
    '                    "short-circuiting (trace_id=%s)", trace_id,\n'
    '                )\n'
    '                self._emit_mouse_action_response(\n'
    '                    action_name=action_name, request_id=request_id,\n'
    '                    trace_id=trace_id, succeeded=False,\n'
    '                    reason="disabled_by_config",\n')

add("mp-10-perform-drag-gate-reads-enabled-only",
    '        action_name = "perform_drag"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "perform_drag"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().enabled:\n', T10, file=MPA_FILE)

add("mp-10-perform-drag-gate-is-removed",
    '        action_name = "perform_drag"\n'
    '        try:\n'
    '            if not self._host.get_validated_click_config().grid_enabled_effective:\n',
    '        action_name = "perform_drag"\n'
    '        try:\n'
    '            if False:\n', T10, file=MPA_FILE)

add("mp-10-perform-drag-gate-reports-another-reason",
    _DR_GATE,
    _DR_GATE.replace("disabled_by_config\",", "unexpected_error\","), T10, T27, file=MPA_FILE)

add("mp-20-perform-drag-ignores-the-duration-from-logic",
    "                start_x, start_y, end_x, end_y, duration_ms,\n"
    "            )\n",
    "                start_x, start_y, end_x, end_y, 250,\n"
    "            )\n", T20, file=MPA_FILE)

add("mp-21-perform-drag-default-duration-is-500",
    "        duration_ms: int = 250,\n",
    "        duration_ms: int = 500,\n", T21)

add("mp-20-perform-drag-passes-the-end-point-as-the-start",
    "                start_x, start_y, end_x, end_y, duration_ms,\n"
    "            )\n",
    "                end_x, end_y, start_x, start_y, duration_ms,\n"
    "            )\n", T20, file=MPA_FILE)

add("mp-2-perform-drag-reply-drops-the-primitive-result",
    '                "reason=%s (trace_id=%s)",\n'
    '                start_x, start_y, end_x, end_y, duration_ms, succeeded,\n'
    '                reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,\n',
    '                "reason=%s (trace_id=%s)",\n'
    '                start_x, start_y, end_x, end_y, duration_ms, succeeded,\n'
    '                reason, trace_id,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=True, reason=reason,\n', T2, T7, file=MPA_FILE)

add("mp-5-perform-drag-handler-lets-a-raise-escape",
    '        except Exception as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "perform_drag: unexpected error (trace_id=%s): %s",\n',
    '        except KeyError as exc:  # noqa: BLE001 -- never-raise handler\n'
    '            logger.error(\n'
    '                "perform_drag: unexpected error (trace_id=%s): %s",\n', T5, file=MPA_FILE)

add("mp-5-perform-drag-handler-reports-another-reason",
    '                "perform_drag: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="unexpected_error",\n',
    '                "perform_drag: unexpected error (trace_id=%s): %s",\n'
    '                trace_id, exc, exc_info=True,\n'
    '            )\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=False,\n'
    '                reason="sendinput_error",\n', T5, file=MPA_FILE)

add("mp-31-perform-drag-does-not-invalidate-the-shadow-buffer",
    '            # wh-review-pattern-fixes.8: a drag can move focus, the caret,\n'
    '            # or the selection; invalidate before the dispatch (see the\n'
    '            # hotkey_action block comment for the full rationale).\n'
    '            self._host.buffer_manager.invalidate()\n',
    '            # wh-review-pattern-fixes.8: a drag can move focus, the caret,\n'
    '            # or the selection; invalidate before the dispatch (see the\n'
    '            # hotkey_action block comment for the full rationale).\n'
    '            pass\n', T1,
    survives="MP-31 has no boundary test: the invalidation leaves the handler "
             "only as a later UI Automation re-sync on a dictation message, "
             "which needs a live focused control that no operating system "
             "fake in the harness provides.", file=MPA_FILE)

# Row MP-11 (an overlay-only key leaves the grid on) has its own mutation in
# ui/click_config.py:

add("mp-11-grid-gate-reads-the-overlay-switch",
    '        return self.enabled and not any(\n'
    '            key in self.overlay_invalid_key for key in self._GRID_KEYS\n'
    '        )\n',
    '        return self.overlay_enabled_effective\n', T11, T10,
    file="ui/click_config.py")

add("mp-8-move-pointer-answers-twice",
    '            succeeded, reason = self.mouse_move_seam(x, y)\n'
    '            logger.info(\n'
    '                "move_pointer: point=',
    '            succeeded, reason = self.mouse_move_seam(x, y)\n'
    '            self._emit_mouse_action_response(\n'
    '                action_name=action_name, request_id=request_id,\n'
    '                trace_id=trace_id, succeeded=True, reason=None,\n'
    '            )\n'
    '            logger.info(\n'
    '                "move_pointer: point=', T8, file=MPA_FILE)

# Row MP-9 (unknown keys are ignored). input_proc passes every key of the
# message to the method by name. trace_id and request_id are named parameters,
# so a method without **kwargs still accepts every message of the table above.
# Only a message with an unknown key then fails, and test_mp_9 sends one
# ("surprise"). The call raises TypeError for that keyword: this is the rule
# that MP-9 states, not an unrelated crash.
T9 = "test_mp_9_unknown_message_keys_are_ignored"

add("mp-9-click-point-rejects-unknown-keys",
    '        request_id: Optional[str],\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Click at a physical-pixel screen point',
    '        request_id: Optional[str],\n'
    '    ) -> None:\n'
    '        """Click at a physical-pixel screen point', T9, file=MPA_FILE)

add("mp-9-click-at-pointer-rejects-unknown-keys",
    '        request_id: Optional[str],\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Click wherever the pointer is now',
    '        request_id: Optional[str],\n'
    '    ) -> None:\n'
    '        """Click wherever the pointer is now', T9, file=MPA_FILE)

add("mp-9-move-pointer-rejects-unknown-keys",
    '        request_id: Optional[str],\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Park the pointer at a physical-pixel point',
    '        request_id: Optional[str],\n'
    '    ) -> None:\n'
    '        """Park the pointer at a physical-pixel point', T9, file=MPA_FILE)

add("mp-9-perform-drag-rejects-unknown-keys",
    '        duration_ms: int = 250,\n'
    '        trace_id: str = "",\n'
    '        request_id: Optional[str] = None,\n'
    '        **kwargs,\n',
    '        duration_ms: int = 250,\n'
    '        trace_id: str = "",\n'
    '        request_id: Optional[str] = None,\n', T9)

# The mutation above changes the delegator of UIActionHandler. This one
# changes the area method that the delegator calls, as the three entries for
# the other actions do.
add("mp-9-area-perform-drag-rejects-unknown-keys",
    '        request_id: Optional[str],\n'
    '        **kwargs,\n'
    '    ) -> None:\n'
    '        """Drag from one physical-pixel point',
    '        request_id: Optional[str],\n'
    '    ) -> None:\n'
    '        """Drag from one physical-pixel point', T9, file=MPA_FILE)

# Row MP-14 (no [click] block: all four actions run). Two ways break it: the
# default of "enabled" in ClickConfig.from_raw, and the value that the handler
# reads when the config has no [click] block.
T14 = "test_mp_14_the_config_default_allows_every_action"

add("mp-14-enabled-default-is-false",
    '_DEFAULTS: dict[str, Any] = {\n'
    '    "enabled": True,\n',
    '_DEFAULTS: dict[str, Any] = {\n'
    '    "enabled": False,\n', T14,
    file="ui/click_config.py")

add("mp-14-missing-click-block-reads-as-disabled",
    '        automation root -- a pointer action needs neither.\n'
    '        """\n'
    '        cfg = getattr(self, "_click_config", None)\n'
    '        if cfg is None:\n'
    '            from ui.click_config import ClickConfig\n'
    '\n'
    '            cfg = ClickConfig.from_raw(self.config.get("click", {}))\n',
    '        automation root -- a pointer action needs neither.\n'
    '        """\n'
    '        cfg = getattr(self, "_click_config", None)\n'
    '        if cfg is None:\n'
    '            from ui.click_config import ClickConfig\n'
    '\n'
    '            cfg = ClickConfig.from_raw(\n'
    '                self.config.get("click", {"enabled": False}))\n', T14)


# ===========================================================================
# Defaults of the UIActionHandler delegators (wh-full-refactor.6.3, item 6)
# ===========================================================================
# The delegators in ui/ui_action_handler.py keep the defaults of the four
# pointer actions; MousePointerActions has none. Each mutation changes one
# default value of a delegator. Only the defaults that a test reads are
# here; the other defaults (x, y, start/end points, trace_id, request_id of
# each delegator) are not read by any U7 test, because every message sent
# through PointerSide carries them; those defaults are expected survivors
# at the end of this list. mp-16 (button) and mp-21 (duration_ms)
# are above.

add('mp-d-click-point-default-click-count-changed',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 1,\n',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 2,\n', T16)

add('mp-d-click-at-pointer-default-button-changed',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n',
    '    def click_at_pointer(\n        self,\n        button: str = "right",\n', T13)

add('mp-d-click-at-pointer-default-click-count-changed',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 1,\n',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 2,\n', T13)


# The defaults that no caller reads (wh-full-refactor.6.3, acceptance item 6).
_UNREAD_U7 = ("Logic sends this field in every message: the call sites of "
              "MouseGridController._dispatch_mouse_action give the points, "
              "_send_mouse_action adds trace_id, and WheelHouseApp.send_request "
              "adds request_id; so the shipped app never reads the default")


def _whole_file():
    """Run the last expected survivor against the whole U7 test file."""
    MUTATIONS[-1]["test_file"] = (f"tests/{_U7}",)


add('mp-d-click-point-default-x-changed',
    '    def click_point(\n        self,\n        x=None,\n',
    '    def click_point(\n        self,\n        x=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-click-point-default-y-changed',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n',
    '    def click_point(\n        self,\n        x=None,\n        y=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-click-point-default-trace-id-changed',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "default-trace",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-click-point-default-request-id-changed',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n        request_id: Optional[str] = None,\n',
    '    def click_point(\n        self,\n        x=None,\n        y=None,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n        request_id: Optional[str] = "default-request",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-click-at-pointer-default-trace-id-changed',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "default-trace",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-click-at-pointer-default-request-id-changed',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n        request_id: Optional[str] = None,\n',
    '    def click_at_pointer(\n        self,\n        button: str = "left",\n        click_count: int = 1,\n        trace_id: str = "",\n        request_id: Optional[str] = "default-request",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-move-pointer-default-x-changed',
    '    def move_pointer(\n        self,\n        x=None,\n',
    '    def move_pointer(\n        self,\n        x=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-move-pointer-default-y-changed',
    '    def move_pointer(\n        self,\n        x=None,\n        y=None,\n',
    '    def move_pointer(\n        self,\n        x=None,\n        y=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-move-pointer-default-trace-id-changed',
    '    def move_pointer(\n        self,\n        x=None,\n        y=None,\n        trace_id: str = "",\n',
    '    def move_pointer(\n        self,\n        x=None,\n        y=None,\n        trace_id: str = "default-trace",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-move-pointer-default-request-id-changed',
    '    def move_pointer(\n        self,\n        x=None,\n        y=None,\n        trace_id: str = "",\n        request_id: Optional[str] = None,\n',
    '    def move_pointer(\n        self,\n        x=None,\n        y=None,\n        trace_id: str = "",\n        request_id: Optional[str] = "default-request",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-start-x-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n',
    '    def perform_drag(\n        self,\n        start_x=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-start-y-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-end-x-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-end-y-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=None,\n',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=0,\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-trace-id-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=None,\n        duration_ms: int = 250,\n        trace_id: str = "",\n',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=None,\n        duration_ms: int = 250,\n        trace_id: str = "default-trace",\n',
    survives=_UNREAD_U7)
_whole_file()

add('mp-d-perform-drag-default-request-id-changed',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=None,\n        duration_ms: int = 250,\n        trace_id: str = "",\n        request_id: Optional[str] = None,\n',
    '    def perform_drag(\n        self,\n        start_x=None,\n        start_y=None,\n        end_x=None,\n        end_y=None,\n        duration_ms: int = 250,\n        trace_id: str = "",\n        request_id: Optional[str] = "default-request",\n',
    survives=_UNREAD_U7)
_whole_file()

def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
