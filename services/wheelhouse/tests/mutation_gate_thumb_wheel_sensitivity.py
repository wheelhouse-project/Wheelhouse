"""Mutation gate for the thumb wheel sensitivity fix (wh-mouse-wheel-sensitivity).

WHY THIS GATE EXISTS. The tests in the three files below were written before
the fix, but the first run stopped at a collection error (the filter module
did not exist yet), so the MouseHandler and HIDListener tests were never
seen red. Every protected behaviour is therefore broken here twice over: by
reverting the fix (no dead zone, no cap, the loop skips the filter, the tail
flush is never armed) and at the level of the INPUT the code depends on (the
direction of the dead-zone and gap comparisons, the sign of held ticks, the
cap value, which config key is read, which delay is armed).

WHAT IT COVERS, grouped by the behaviour each mutation defends:

  dead zone (handlers/thumb_wheel_filter.py)
      no-dead-zone                     every tick passes at once
      dead-zone-needs-one-tick-more    the crossing comparison is off by one
      held-ticks-lose-their-sign       a wobble that nets to zero opens
      gesture-never-opens              every batch must cross alone
      a-zero-batch-forgets-held-ticks  a zero batch is no longer inert
  gesture gap
      pause-keeps-held-ticks           a pause forgets nothing
      a-batch-at-exactly-the-gap-closes  the boundary comparison flips
      gap-measured-from-the-first-batch  the gap no longer follows the roll
      injected-clock-ignored           the filter reads its own clock
      arrival-time-ignored             the gap is measured at drain time
      clock-ignored-without-arrival-time  no stamp, no clock either
  per-batch cap
      no-cap                           a flick passes whole
      excess-is-carried                a flick keeps moving after it stops
      cap-is-one-tick-too-generous     the cap value is off by one
  settings
      dead-zone-not-clamped / cap-not-clamped / gap-not-clamped
      gap-in-the-wrong-unit            milliseconds become tenths
      dead-zone-truncation / cap-truncation  fractions round instead
      default-dead-zone-is-one / default-gap-is-fifty-ms /
      default-cap-is-unbounded         the shipped defaults drift
  the MouseHandler wiring (handlers/mouse_handler.py)
      loop-skips-the-filter            every batch reaches a zone raw
      a-held-batch-still-reaches-a-zone  a zero result is still routed
      filter-sees-the-drained-sum-not-each-batch  (.1.1) queued 50 ms
                                       batches are capped as one flick
      only-the-last-batch-counts       (.1.1) earlier batches' ticks drop
      arrival-time-not-passed-to-the-filter  (.1.1)
      routing-reads-the-raw-sum        (.1.2) cancelling raw deltas drop
                                       the ticks that passed
      brightness-cap-never-applies     (wh-brightness-wheel-one-step) one
                                       native TV step per command
      brightness-excess-is-carried-not-discarded  (same bead) the excess
                                       must not move the TV after the wheel stops
      brightness-cap-is-one-step-again (wh-brightness-wheel-two-steps) David
                                       chose two native steps per command
      brightness-steps-config-key-ignored (same bead) BRIGHTNESS_STEPS_PER_COMMAND
                                       must reach the cap
      brightness-steps-floor-removed   (same bead) a value below 1 must not
                                       disable the brightness zone
      dead-zone-key-ignored / gap-key-ignored / cap-key-ignored
      missing-dead-zone-key-falls-back-to-one / missing-gap-key-falls-back-
      to-zero / missing-cap-key-falls-back-to-unbounded
  the HIDListener tail flush (handlers/hid_listener.py)
      tail-flush-never-armed           the stale-tail defect returns
      tail-flush-waits-a-full-second   the wrong delay is armed
      the-wrong-callback-is-armed
      send-branch-also-arms-a-flush
      flush-keeps-the-held-delta / flush-leaves-the-old-window /
      flush-sends-an-empty-tail
      arm-keeps-the-earlier-timer
      stop-leaves-the-timer-armed
      closed-loop-raises               the shutdown race escapes
      batch-lacks-arrival-time / tail-lacks-arrival-time  (.1.1)
      arrival-time-from-wall-clock     (.1.1) the stamp is on the wrong scale

WHAT ``caught`` MEANS. ``expect`` is the set of tests that must fail; the
shared runner reports a survivor when any of them passes. Every mutation
runs all three test files in one pytest process, and this gate prints each
run's ``FAILED`` summary lines so the record shows WHICH assertion fired,
not only that one did. One catch is an exception rather than an assertion
by design: ``closed-loop-raises`` is caught by the RuntimeError escaping
``test_closed_loop_does_not_raise``, and that escape is exactly the
behaviour the test pins.

Run from services/wheelhouse:

    python tests/mutation_gate_thumb_wheel_sensitivity.py            # all
    python tests/mutation_gate_thumb_wheel_sensitivity.py no-cap     # one
    python tests/mutation_gate_thumb_wheel_sensitivity.py --check    # patterns
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TESTS = (
    "tests/test_handlers/test_thumb_wheel_filter.py",
    "tests/test_handlers/test_mouse_handler.py",
    "tests/test_handlers/test_hid_listener.py",
)
FILTER = SERVICE / "handlers/thumb_wheel_filter.py"
MOUSE = SERVICE / "handlers/mouse_handler.py"
HID = SERVICE / "handlers/hid_listener.py"

spec = importlib.util.spec_from_file_location(
    "thumb_wheel_shared_gate",
    ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

_shared_run_pytest = runner._run_pytest


def _run_pytest_and_show_reasons(service, test_file):
    """The shared run, plus each FAILED summary line with its reason.

    The runner prints only the caught names. The mutation-gate skill asks
    for the assertion that fired, so a failure upstream of the guarded
    behaviour cannot read as a catch.
    """
    result = _shared_run_pytest(service, test_file)
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith("FAILED "):
            print("    " + line.split("::", 1)[-1][:200])
    return result


runner._run_pytest = _run_pytest_and_show_reasons


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=file,
                old=old, new=new, expect=expect)


PER_BATCH = (
    "                            ticks += self.thumb_wheel_filter.filter_batch(\n"
    '                                delta, at=e.get("at")\n'
    "                            )\n"
)

MUTATIONS = [
    # ---- dead zone ------------------------------------------------------
    mutation("no-dead-zone", FILTER,
             "            if abs(self._held_ticks) < self.dead_zone_ticks:\n"
             "                return 0\n",
             "            if False:\n"
             "                return 0\n",
             ["test_single_tick_is_held",
              "test_ticks_below_dead_zone_are_held",
              "test_crossing_dead_zone_passes_held_ticks",
              "test_reversal_below_dead_zone_stays_closed",
              "test_negative_direction_is_symmetric",
              "test_gap_forgets_held_ticks",
              "test_gap_closes_an_open_gesture",
              "test_single_tick_reaches_no_zone",
              "test_filter_applies_to_brightness_zone_too"]),
    mutation("dead-zone-needs-one-tick-more", FILTER,
             "            if abs(self._held_ticks) < self.dead_zone_ticks:\n",
             "            if abs(self._held_ticks) <= self.dead_zone_ticks:\n",
             ["test_crossing_dead_zone_passes_held_ticks",
              "test_one_batch_can_cross_dead_zone_alone",
              "test_dead_zone_of_one_passes_every_tick",
              "test_dead_zone_crossing_reaches_volume_zone",
              "test_queued_batches_share_the_dead_zone"]),
    mutation("held-ticks-lose-their-sign", FILTER,
             "        self._held_ticks += delta\n",
             "        self._held_ticks += abs(delta)\n",
             ["test_reversal_below_dead_zone_stays_closed",
              "test_negative_direction_is_symmetric",
              "test_cap_limits_negative_batch",
              "test_dead_zone_of_one_passes_every_tick"]),
    mutation("gesture-never-opens", FILTER,
             "            self._gesture_open = True\n",
             "            self._gesture_open = False\n",
             ["test_open_gesture_passes_single_ticks",
              "test_batch_inside_gap_keeps_gesture_open",
              "test_batch_at_exactly_the_gap_keeps_gesture_open",
              "test_excess_is_discarded_not_carried",
              "test_batch_under_cap_passes_whole",
              "test_default_clock_is_monotonic"]),
    mutation("a-zero-batch-forgets-held-ticks", FILTER,
             "        if delta == 0:\n"
             "            return 0\n",
             "        if delta == 0:\n"
             "            self._held_ticks = 0\n"
             "            return 0\n",
             ["test_zero_batch_changes_nothing"]),
    # ---- gesture gap ----------------------------------------------------
    mutation("pause-keeps-held-ticks", FILTER,
             "            # Rule 2: the pause ended the previous gesture.\n"
             "            self._held_ticks = 0\n"
             "            self._gesture_open = False\n",
             "            # Rule 2: the pause ended the previous gesture.\n"
             "            pass\n",
             ["test_gap_forgets_held_ticks",
              "test_gap_closes_an_open_gesture",
              "test_gap_of_zero_closes_after_every_batch"]),
    mutation("a-batch-at-exactly-the-gap-closes", FILTER,
             "            and now - self._last_batch_at > self.gesture_gap_s\n",
             "            and now - self._last_batch_at >= self.gesture_gap_s\n",
             ["test_batch_at_exactly_the_gap_keeps_gesture_open"]),
    mutation("gap-measured-from-the-first-batch", FILTER,
             "        self._last_batch_at = now\n",
             "        if self._last_batch_at is None:\n"
             "            self._last_batch_at = now\n",
             ["test_gap_is_measured_from_the_previous_batch"]),
    mutation("injected-clock-ignored", FILTER,
             "        self._clock = clock\n",
             "        self._clock = time.monotonic\n",
             ["test_gap_forgets_held_ticks",
              "test_gap_closes_an_open_gesture"]),
    mutation("arrival-time-ignored", FILTER,
             "        now = self._clock() if at is None else at\n",
             "        now = self._clock()\n",
             ["test_arrival_time_governs_the_gap_not_the_clock",
              "test_arrival_time_can_close_the_gesture_before_the_clock_does"]),
    mutation("clock-ignored-without-arrival-time", FILTER,
             "        now = self._clock() if at is None else at\n",
             "        now = 0.0 if at is None else at\n",
             ["test_without_arrival_time_the_clock_is_used"]),
    # ---- per-batch cap --------------------------------------------------
    mutation("no-cap", FILTER,
             "        passed = max(-cap, min(cap, self._held_ticks))\n",
             "        passed = self._held_ticks\n",
             ["test_cap_limits_one_batch",
              "test_cap_limits_negative_batch",
              "test_cap_applies_to_the_dead_zone_crossing",
              "test_out_of_range_settings_are_clamped",
              "test_flick_is_capped_before_volume_zone"]),
    mutation("excess-is-carried", FILTER,
             "        self._held_ticks = 0\n"
             "        return passed\n",
             "        self._held_ticks -= passed\n"
             "        return passed\n",
             ["test_excess_is_discarded_not_carried"]),
    mutation("cap-is-one-tick-too-generous", FILTER,
             "        cap = self.max_ticks_per_batch\n",
             "        cap = self.max_ticks_per_batch + 1\n",
             ["test_cap_limits_one_batch",
              "test_cap_limits_negative_batch",
              "test_cap_applies_to_the_dead_zone_crossing",
              "test_out_of_range_settings_are_clamped",
              "test_flick_is_capped_before_volume_zone"]),
    # ---- settings -------------------------------------------------------
    mutation("dead-zone-not-clamped", FILTER,
             "        self.dead_zone_ticks = max(1, int(dead_zone_ticks))\n",
             "        self.dead_zone_ticks = int(dead_zone_ticks)\n",
             ["test_out_of_range_settings_are_clamped"]),
    mutation("cap-not-clamped", FILTER,
             "        self.max_ticks_per_batch = max(1, int(max_ticks_per_batch))\n",
             "        self.max_ticks_per_batch = int(max_ticks_per_batch)\n",
             ["test_out_of_range_settings_are_clamped"]),
    mutation("gap-not-clamped", FILTER,
             "        self.gesture_gap_s = max(0.0, float(gesture_gap_ms)) / 1000.0\n",
             "        self.gesture_gap_s = float(gesture_gap_ms) / 1000.0\n",
             ["test_out_of_range_settings_are_clamped"]),
    mutation("gap-in-the-wrong-unit", FILTER,
             "        self.gesture_gap_s = max(0.0, float(gesture_gap_ms)) / 1000.0\n",
             "        self.gesture_gap_s = max(0.0, float(gesture_gap_ms)) / 100.0\n",
             ["test_settings_are_kept",
              "test_reads_filter_settings_from_config",
              "test_missing_keys_use_shipped_defaults",
              "test_custom_keys_reach_the_filter",
              "test_gap_forgets_held_ticks",
              "test_gap_closes_an_open_gesture"]),
    mutation("dead-zone-truncation", FILTER,
             "        self.dead_zone_ticks = max(1, int(dead_zone_ticks))\n",
             "        self.dead_zone_ticks = max(1, round(dead_zone_ticks))\n",
             ["test_float_settings_are_truncated_to_whole_ticks"]),
    mutation("cap-truncation", FILTER,
             "        self.max_ticks_per_batch = max(1, int(max_ticks_per_batch))\n",
             "        self.max_ticks_per_batch = max(1, round(max_ticks_per_batch))\n",
             ["test_float_settings_are_truncated_to_whole_ticks"]),
    mutation("default-dead-zone-is-one", FILTER,
             "DEFAULT_DEAD_ZONE_TICKS = 3\n",
             "DEFAULT_DEAD_ZONE_TICKS = 1\n",
             ["test_defaults"]),
    mutation("default-gap-is-fifty-ms", FILTER,
             "DEFAULT_GESTURE_GAP_MS = 500\n",
             "DEFAULT_GESTURE_GAP_MS = 50\n",
             ["test_defaults"]),
    mutation("default-cap-is-unbounded", FILTER,
             "DEFAULT_MAX_TICKS_PER_BATCH = 3\n",
             "DEFAULT_MAX_TICKS_PER_BATCH = 100\n",
             ["test_defaults"]),
    # ---- the MouseHandler wiring ----------------------------------------
    mutation("loop-skips-the-filter", MOUSE,
             PER_BATCH,
             "                            ticks += delta\n",
             ["test_single_tick_reaches_no_zone",
              "test_flick_is_capped_before_volume_zone",
              "test_filter_applies_to_brightness_zone_too"]),
    mutation("a-held-batch-still-reaches-a-zone", MOUSE,
             "                if ticks == 0:\n"
             "                    # Held below the dead zone, cancelled, or no wheel\n"
             "                    # movement at all; nothing for a zone to do.\n"
             "                    continue\n",
             "                if ticks == 0:\n"
             "                    # Held below the dead zone, cancelled, or no wheel\n"
             "                    # movement at all; nothing for a zone to do.\n"
             "                    pass\n",
             ["test_single_tick_reaches_no_zone",
              "test_filter_applies_to_brightness_zone_too"]),
    mutation("brightness-cap-never-applies", MOUSE,
             "            if abs(steps_to_take) > self.brightness_max_per_command:\n",
             "            if abs(steps_to_take) > 100:\n",
             ["test_one_hardware_step_per_command",
              "test_one_hardware_step_per_command_when_dimming",
              "test_excess_ticks_are_discarded_not_carried"]),
    mutation("brightness-excess-is-carried-not-discarded", MOUSE,
             "                steps_to_take = (self.brightness_max_per_command if steps_to_take > 0\n"
             "                                 else -self.brightness_max_per_command)\n",
             "                capped = (self.brightness_max_per_command if steps_to_take > 0\n"
             "                          else -self.brightness_max_per_command)\n"
             "                self.brightness_accumulator += steps_to_take - capped\n"
             "                steps_to_take = capped\n",
             ["test_excess_ticks_are_discarded_not_carried"]),
    mutation("brightness-cap-is-one-step-again", MOUSE,
             "DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND = 2\n",
             "DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND = 1\n",
             ["test_one_hardware_step_per_command",
              "test_one_hardware_step_per_command_when_dimming",
              "test_three_ticks_pass_as_three_below_the_cap",
              "test_missing_key_uses_two_steps"]),
    mutation("brightness-steps-config-key-ignored", MOUSE,
             "        steps_per_command = max(1, int(config_service.get(\n"
             "            \"BRIGHTNESS_STEPS_PER_COMMAND\", DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND\n"
             "        )))\n",
             "        steps_per_command = DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND\n",
             ["test_three_steps_from_config", "test_one_step_from_config"]),
    mutation("brightness-steps-floor-removed", MOUSE,
             "        steps_per_command = max(1, int(config_service.get(\n"
             "            \"BRIGHTNESS_STEPS_PER_COMMAND\", DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND\n"
             "        )))\n",
             "        steps_per_command = int(config_service.get(\n"
             "            \"BRIGHTNESS_STEPS_PER_COMMAND\", DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND\n"
             "        ))\n",
             ["test_value_below_one_is_treated_as_one"]),
    mutation("routing-reads-the-raw-sum", MOUSE,
             "                if ticks == 0:\n"
             "                    # Held below the dead zone, cancelled, or no wheel\n",
             "                if total_delta == 0:\n"
             "                    # Held below the dead zone, cancelled, or no wheel\n",
             ["test_cancelling_raw_deltas_still_route_the_passed_ticks",
              "test_cancelling_raw_deltas_route_to_the_brightness_zone_too",
              "test_correction_behind_a_slow_zone_action_is_not_lost"]),
    mutation("filter-sees-the-drained-sum-not-each-batch", MOUSE,
             "                        total_delta += delta\n"
             "                        if delta != 0:\n"
             + PER_BATCH +
             "                    self.hid_event_queue.task_done()\n"
             "\n"
             "                if total_delta != 0 or ticks != 0:\n",
             "                        total_delta += delta\n"
             "                    self.hid_event_queue.task_done()\n"
             "\n"
             "                if total_delta != 0 or ticks != 0:\n"
             "                    ticks = self.thumb_wheel_filter.filter_batch(total_delta)\n",
             ["test_each_queued_batch_gets_its_own_cap",
              "test_queued_batch_arrival_times_reach_the_filter",
              "test_slow_zone_action_does_not_merge_later_batches"]),
    mutation("only-the-last-batch-counts", MOUSE,
             PER_BATCH,
             PER_BATCH.replace("ticks +=", "ticks ="),
             ["test_each_queued_batch_gets_its_own_cap",
              "test_slow_zone_action_does_not_merge_later_batches"]),
    mutation("arrival-time-not-passed-to-the-filter", MOUSE,
             '                                delta, at=e.get("at")\n',
             "                                delta, at=None\n",
             ["test_queued_batch_arrival_times_reach_the_filter"]),
    mutation("dead-zone-key-ignored", MOUSE,
             "            dead_zone_ticks=config_service.get(\n"
             "                \"THUMB_WHEEL_DEAD_ZONE_TICKS\", DEFAULT_DEAD_ZONE_TICKS\n"
             "            ),\n",
             "            dead_zone_ticks=DEFAULT_DEAD_ZONE_TICKS,\n",
             ["test_reads_filter_settings_from_config",
              "test_custom_keys_reach_the_filter"]),
    mutation("gap-key-ignored", MOUSE,
             "            gesture_gap_ms=config_service.get(\n"
             "                \"THUMB_WHEEL_GESTURE_GAP_MS\", DEFAULT_GESTURE_GAP_MS\n"
             "            ),\n",
             "            gesture_gap_ms=DEFAULT_GESTURE_GAP_MS,\n",
             ["test_custom_keys_reach_the_filter"]),
    mutation("cap-key-ignored", MOUSE,
             "            max_ticks_per_batch=config_service.get(\n"
             "                \"THUMB_WHEEL_MAX_TICKS_PER_BATCH\", DEFAULT_MAX_TICKS_PER_BATCH\n"
             "            ),\n",
             "            max_ticks_per_batch=DEFAULT_MAX_TICKS_PER_BATCH,\n",
             ["test_reads_filter_settings_from_config",
              "test_custom_keys_reach_the_filter"]),
    mutation("missing-dead-zone-key-falls-back-to-one", MOUSE,
             "                \"THUMB_WHEEL_DEAD_ZONE_TICKS\", DEFAULT_DEAD_ZONE_TICKS\n",
             "                \"THUMB_WHEEL_DEAD_ZONE_TICKS\", 1\n",
             ["test_missing_keys_use_shipped_defaults"]),
    mutation("missing-gap-key-falls-back-to-zero", MOUSE,
             "                \"THUMB_WHEEL_GESTURE_GAP_MS\", DEFAULT_GESTURE_GAP_MS\n",
             "                \"THUMB_WHEEL_GESTURE_GAP_MS\", 0\n",
             ["test_missing_keys_use_shipped_defaults"]),
    mutation("missing-cap-key-falls-back-to-unbounded", MOUSE,
             "                \"THUMB_WHEEL_MAX_TICKS_PER_BATCH\", DEFAULT_MAX_TICKS_PER_BATCH\n",
             "                \"THUMB_WHEEL_MAX_TICKS_PER_BATCH\", 100\n",
             ["test_missing_keys_use_shipped_defaults"]),
    # ---- the HIDListener tail flush -------------------------------------
    mutation("tail-flush-never-armed", HID,
             "        elif window_remaining is not None:\n"
             "            self._request_tail_flush(window_remaining)\n",
             "        elif window_remaining is not None:\n"
             "            pass\n",
             ["test_within_window_arms_a_tail_flush",
              "test_tail_reaches_queue_without_a_following_report"]),
    mutation("tail-flush-waits-a-full-second", HID,
             "                window_remaining = self.batch_interval - elapsed\n",
             "                window_remaining = 1.0\n",
             ["test_within_window_arms_a_tail_flush",
              "test_tail_reaches_queue_without_a_following_report"]),
    mutation("the-wrong-callback-is-armed", HID,
             "            self.loop.call_soon_threadsafe(self._arm_tail_flush, delay)\n",
             "            self.loop.call_soon_threadsafe(self._cancel_tail_flush, delay)\n",
             ["test_within_window_arms_a_tail_flush",
              "test_tail_reaches_queue_without_a_following_report"]),
    mutation("send-branch-also-arms-a-flush", HID,
             "        elif window_remaining is not None:\n",
             "        if True:\n",
             ["test_send_branch_does_not_arm_a_tail_flush"]),
    mutation("flush-keeps-the-held-delta", HID,
             "            self.delta_accumulator = 0\n"
             "            self.last_batch_time = time.time()\n",
             "            self.last_batch_time = time.time()\n",
             ["test_flush_tail_sends_held_delta",
              "test_tail_reaches_queue_without_a_following_report"]),
    mutation("flush-leaves-the-old-window", HID,
             "            self.last_batch_time = time.time()\n"
             "        if accumulated_delta != 0:\n",
             "        if accumulated_delta != 0:\n",
             ["test_flush_tail_starts_a_new_window"]),
    mutation("flush-sends-an-empty-tail", HID,
             "        if accumulated_delta != 0:\n"
             "            self._safe_enqueue_event({\n",
             "        if True:\n"
             "            self._safe_enqueue_event({\n",
             ["test_flush_tail_with_nothing_held_sends_nothing"]),
    mutation("arm-keeps-the-earlier-timer", HID,
             "        if self._tail_flush_handle is not None:\n"
             "            self._tail_flush_handle.cancel()\n"
             "        self._tail_flush_handle = self.loop.call_later(delay, self._flush_tail)\n",
             "        self._tail_flush_handle = self.loop.call_later(delay, self._flush_tail)\n",
             ["test_arm_replaces_an_earlier_timer"]),
    mutation("stop-leaves-the-timer-armed", HID,
             "        try:\n"
             "            self.loop.call_soon_threadsafe(self._cancel_tail_flush)\n"
             "        except RuntimeError:\n"
             "            pass  # Loop closed: no timer can fire any more.\n",
             "        pass\n",
             ["test_stop_cancels_an_armed_flush"]),
    mutation("batch-lacks-arrival-time", HID,
             '                "at": time.monotonic(),\n'
             "            }\n",
             "            }\n",
             ["test_batched_event_carries_its_arrival_time"]),
    mutation("arrival-time-from-wall-clock", HID,
             '                "at": time.monotonic(),\n'
             "            }\n",
             '                "at": time.time(),\n'
             "            }\n",
             ["test_batched_event_carries_its_arrival_time"]),
    mutation("tail-lacks-arrival-time", HID,
             '                "at": time.monotonic(),\n'
             "            })\n",
             "            })\n",
             ["test_tail_event_carries_its_arrival_time"]),
    mutation("closed-loop-raises", HID,
             "        except RuntimeError:\n"
             "            # The loop is closed: the Logic process is shutting down and the\n",
             "        except ValueError:\n"
             "            # The loop is closed: the Logic process is shutting down and the\n",
             ["test_closed_loop_does_not_raise"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
