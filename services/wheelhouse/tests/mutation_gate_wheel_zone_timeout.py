"""Mutation gate for the thumb wheel zone and timeout fix (wh-wheel-zone-timeout-jump).

WHY THIS GATE EXISTS. Acceptance item 6 of that bead asks for a gate with no
survivors over the tests of items 1 and 2:

  1. Movement rolled at the left edge never changes the volume, and movement
     rolled in the volume zone never changes brightness, however long the
     batch waits behind a slow TV or speaker.
  2. After a timeout or offline result, a wheel step does not wait for the
     Samsung TV for a short named interval, and it takes the path of a
     timed-out step.

The behaviour lives in four files, and a fault in any one of them brings the
reported defect back: the batch stamp (hid_listener.py), the reader and the
zone decision (mouse_handler.py), the reset (thumb_wheel_filter.py), and the
interval (samsung_plugin.py).

WHAT IT COVERS, grouped by the behaviour each mutation defends:

  the batch stamp (handlers/hid_listener.py)
      batch-has-no-stamp, stamp-under-another-key, reader-is-dropped
      hid-send-site-loses-the-stamp, tail-site-loses-the-stamp   one site only
      stamp-read-once-at-construction, stamp-read-at-the-first-tick   read at
                                       the wrong time
      a-zero-position-is-not-stamped   X 0 is a real position
      stamp-is-zero-without-a-reader   the unstamped form must stay unstamped
  the reader (handlers/mouse_handler.py)
      handler-gives-no-reader, reader-keeps-the-start-position,
      reader-always-right
  the zone decision
      zone-reads-the-current-pointer   the reported defect
      a-stamp-of-zero-is-ignored, no-stamp-means-the-left-edge
      zone-comparison-inverted, zone-boundary-moves, zone-dispatch-inverted
  the runs of one drain
      both-zones-merge-into-one-run, only-the-first-run-acts,
      only-the-newest-run-is-kept, runs-act-in-reverse-order,
      a-run-acts-before-the-drain-is-final,
      a-run-that-nets-to-zero-still-acts, run-log-names-the-first-stamp
  the reset at a change of zone
      zone-reset-removed, zone-reset-on-every-batch, last-zone-not-recorded,
      the-first-zone-stays-recorded, last-zone-forgotten-between-drains,
      zone-reset-after-the-filter
  ThumbWheelFilter.reset (handlers/thumb_wheel_filter.py)
      reset-keeps-the-held-ticks, reset-keeps-the-gesture-open
  the skip (plugins/samsung_plugin.py)
      skip-check-removed, skip-check-inverted, skip-check-boundary
      skip-before-the-active-guard, skip-outside-the-lock
      skip-publishes-nothing, skip-falls-through-to-the-tv,
      skip-sends-the-step-to-the-tv, skip-reads-the-tv
      skip-publishes-the-wrong-delta, skip-drops-the-command-id,
      skip-uses-another-reason
      skip-sends-a-notice, skip-clears-the-stored-error,
      skip-extends-the-interval
  the retry time
      retry-not-set-for-timeout, retry-not-set-for-offline
      retry-set-for-a-rejection, retry-set-for-an-unavailable-identity
      retry-not-cleared-by-a-good-result, retry-not-cleared-by-another-error
      retry-read-before-the-result-returned, retry-time-is-in-the-past
      retry-interval-is-zero, retry-interval-is-an-hour
      no-answer-set-lacks-timeout, no-answer-set-lacks-offline,
      no-answer-set-gains-a-rejection, plugin-clock-is-stuck

WHAT ``caught`` MEANS. ``expect`` is the set of tests that must fail; the
shared runner reports a survivor when any of them passes. This gate prints
each run's ``FAILED`` summary lines, so the record shows WHICH assertion fired.
A mutation of a handler file runs the three handler test files, and a mutation
of the plugin runs the plugin test file.

Some catches read a missing value rather than a wrong one, by design. The
mutations that take the stamp away raise ``KeyError: 'x'`` at the test's own
read of the stamp, and ``handler-gives-no-reader`` raises
``KeyError: 'pointer_x'`` at the test's read of the keyword argument. The
missing key is the behaviour those tests pin.

``zone-comparison-inverted`` and ``zone-dispatch-inverted`` do not name the two
tests that wait behind a slow zone. Under those mutations the first batch goes
to the other handler, so those tests stop with ``TimeoutError`` at their own
wait and never reach their assertion on the zones.

Run from services/wheelhouse with an interpreter that has the service's
dependencies:

    python tests/mutation_gate_wheel_zone_timeout.py            # all
    python tests/mutation_gate_wheel_zone_timeout.py skip-      # by name
    python tests/mutation_gate_wheel_zone_timeout.py --check    # patterns
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
HANDLER_TESTS = (
    "tests/test_handlers/test_thumb_wheel_filter.py",
    "tests/test_handlers/test_mouse_handler.py",
    "tests/test_handlers/test_hid_listener.py",
)
PLUGIN_TESTS = "tests/test_plugins/test_samsung_plugin.py"
FILTER = SERVICE / "handlers/thumb_wheel_filter.py"
MOUSE = SERVICE / "handlers/mouse_handler.py"
HID = SERVICE / "handlers/hid_listener.py"
PLUGIN = SERVICE / "plugins/samsung_plugin.py"

spec = importlib.util.spec_from_file_location(
    "wheel_zone_timeout_shared_gate",
    ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run``.

    The same replacement as tests/mutation_gate_thumb_wheel_sensitivity.py,
    for the same reason: ``uv run`` builds a .venv inside a git worktree and
    makes the worktree hard to delete.
    """
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

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


def handler_mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=HANDLER_TESTS, file=file,
                old=old, new=new, expect=expect)


def plugin_mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=PLUGIN_TESTS, file=PLUGIN,
                old=old, new=new, expect=expect)


# ---- hid_listener.py ----------------------------------------------------
STAMP = (
    "        if self._pointer_x is not None:\n"
    '            event["x"] = self._pointer_x()\n'
)
STORE_READER = "        self._pointer_x = pointer_x\n"

# ---- mouse_handler.py ---------------------------------------------------
READER = "            self.loop, self.hid_event_queue, pointer_x=lambda: self.mouse_x\n"
BATCH_X = '                            x = e.get("x", self.mouse_x)\n'
ZONE_COMPARISON = "BRIGHTNESS_ZONE if x < self.side_offset else VOLUME_ZONE"
NEW_RUN = (
    "                            if not runs or runs[-1].zone != zone:\n"
    "                                runs.append(_WheelRun(zone, x))\n"
)
ACT_ON_RUNS = (
    "                for run in runs:\n"
    "                    if run.ticks == 0:\n"
)
RESET_AT_CHANGE = (
    "                            if self._last_wheel_zone not in (None, zone):\n"
    "                                self.thumb_wheel_filter.reset()\n"
)
RECORD_ZONE = "                            self._last_wheel_zone = zone\n"
FILTER_AND_ADD = (
    "                            run = runs[-1]\n"
    "                            run.last_x = x\n"
    "                            run.ticks += self.thumb_wheel_filter.filter_batch(\n"
    '                                delta, at=e.get("at")\n'
    "                            )\n"
)

# ---- thumb_wheel_filter.py ----------------------------------------------
RESET_DOC = '        """Forget the held ticks and close the gesture (a zone change)."""\n'

# ---- samsung_plugin.py --------------------------------------------------
GUARD = (
    "            if not self._active or self._state != PluginState.RUNNING or event.delta == 0:\n"
    "                return\n"
)
SKIP_CHECK = (
    "            if self._retry_after is not None and self._monotonic() < self._retry_after:\n"
)
SKIP_LOG = (
    '                logger.debug(f"Samsung TV not contacted: last result was {self._error}, "\n'
    '                             f"retry in {self._retry_after - self._monotonic():.1f}s")\n'
)
SKIP_PUBLISH = "                await self._publish_offline_overflow(event)\n"
SKIP_END = "                return\n            started = time.monotonic()\n"
SKIP_TAIL = SKIP_PUBLISH + SKIP_END


def _inline_overflow(delta="event.delta", reason="'device_offline'",
                     command_id="event.command_id"):
    """The skip's overflow written out, so one field changes at the skip alone.

    A mutation inside _publish_offline_overflow would also change the path of a
    timed-out step, and a catch there would not prove the skip's own test.
    """
    return (
        "                await self._event_bus.publish(BrightnessOverflowEvent(\n"
        f"                    delta={delta}, source_plugin=self.name, reason={reason},\n"
        f"                    command_id={command_id}))\n"
    )


RETRY_CONDITION = "                             if reason in _NO_ANSWER_ERRORS else None)\n"
GOOD_RESULT_CLEARS = (
    "        self._error = None\n"
    "        self._retry_after = None\n"
)
NO_ANSWER = "_NO_ANSWER_ERRORS = ('offline', 'timeout')\n"
INTERVAL = "OFFLINE_RETRY_INTERVAL_SECONDS = 10.0\n"
STEP_REACHES_THE_TV = (
    "            started = time.monotonic()\n"
    "            result = await self._control.change_brightness(event.delta)\n"
    "            if result.error:\n"
    "                # The notice below is sent only when the reason changes; this line\n"
    "                # records every failed attempt.\n"
    '                logger.warning(f"Samsung brightness attempt failed: reason={result.error}, "\n'
    '                               f"elapsed={time.monotonic() - started:.2f}s")\n'
    "                await self._report_error(result.error)\n"
)

STAMP_CATCHERS = [
    "test_batch_sent_from_the_hid_thread_carries_the_pointer_x",
    "test_tail_batch_carries_the_pointer_x",
    "test_the_stamp_is_read_when_the_batch_is_sent",
    "test_real_listener_stamp_decides_the_zone",
]
HELD_TICKS_CATCHERS = [
    "test_ticks_held_at_the_left_edge_never_reach_volume",
    "test_ticks_held_in_the_volume_zone_never_reach_brightness",
    "test_the_reset_works_across_two_drains",
    "test_a_zone_change_closes_an_open_gesture",
]
SKIP_CATCHERS = [
    "test_step_after_a_silent_tv_result_does_not_contact_the_tv",
    "test_skipped_step_takes_the_path_of_a_timed_out_step",
    "test_failed_read_at_start_opens_the_interval",
    "test_a_step_at_the_end_of_the_interval_contacts_the_tv",
    "test_the_interval_starts_when_the_wait_ended",
    "test_a_failed_retry_opens_a_new_interval",
]

MUTATIONS = [
    # ---- the stored position --------------------------------------------
    handler_mutation(
        "a-rapid-move-is-dropped", MOUSE,
        "        self.mouse_x = x\n",
        "        import time\n"
        "        if time.time() - getattr(self, '_stored_at', 0.0) > 0.1:\n"
        "            self._stored_at = time.time()\n"
        "            self.mouse_x = x\n",
        ["test_the_last_move_of_a_flick_is_kept"]),
    # ---- the batch stamp ------------------------------------------------
    handler_mutation(
        "batch-has-no-stamp", HID, STAMP, "", STAMP_CATCHERS),
    handler_mutation(
        "stamp-under-another-key", HID,
        '            event["x"] = self._pointer_x()\n',
        '            event["pointer_x"] = self._pointer_x()\n',
        STAMP_CATCHERS),
    handler_mutation(
        "reader-is-dropped", HID, STORE_READER,
        "        self._pointer_x = None\n",
        STAMP_CATCHERS),
    handler_mutation(
        "hid-send-site-loses-the-stamp", HID,
        "            event = self._batch_event(accumulated_delta)\n",
        "            event = self._batch_event(accumulated_delta)\n"
        '            event.pop("x", None)\n',
        ["test_batch_sent_from_the_hid_thread_carries_the_pointer_x"]),
    handler_mutation(
        "tail-site-loses-the-stamp", HID,
        "            self._safe_enqueue_event(self._batch_event(accumulated_delta))\n",
        "            event = self._batch_event(accumulated_delta)\n"
        '            event.pop("x", None)\n'
        "            self._safe_enqueue_event(event)\n",
        ["test_tail_batch_carries_the_pointer_x",
         "test_the_stamp_is_read_when_the_batch_is_sent",
         "test_real_listener_stamp_decides_the_zone"]),
    handler_mutation(
        "stamp-read-once-at-construction", HID, STORE_READER,
        "        first_x = pointer_x() if pointer_x is not None else None\n"
        "        self._pointer_x = (lambda: first_x) if pointer_x is not None else None\n",
        ["test_the_stamp_is_read_when_the_batch_is_sent"]),
    handler_mutation(
        "stamp-read-at-the-first-tick", HID,
        "            self.delta_accumulator += delta\n",
        "            self.delta_accumulator += delta\n"
        "            if self._pointer_x is not None:\n"
        "                tick_x = self._pointer_x()\n"
        "                self._pointer_x = lambda: tick_x\n",
        ["test_the_stamp_is_read_when_the_batch_is_sent"]),
    handler_mutation(
        "a-zero-position-is-not-stamped", HID, STAMP,
        "        x = self._pointer_x() if self._pointer_x is not None else None\n"
        "        if x:\n"
        '            event["x"] = x\n',
        ["test_a_pointer_at_the_left_screen_edge_is_still_stamped"]),
    handler_mutation(
        "stamp-is-zero-without-a-reader", HID, STAMP,
        '        event["x"] = self._pointer_x() if self._pointer_x is not None else 0\n',
        ["test_no_reader_leaves_the_key_out_of_a_batch",
         "test_no_reader_leaves_the_key_out_of_a_tail_batch"]),
    # ---- the reader -----------------------------------------------------
    handler_mutation(
        "handler-gives-no-reader", MOUSE, READER,
        "            self.loop, self.hid_event_queue\n",
        ["test_the_listener_reader_returns_the_current_mouse_x",
         "test_real_listener_stamp_decides_the_zone"]),
    handler_mutation(
        "reader-keeps-the-start-position", MOUSE, READER,
        "            self.loop, self.hid_event_queue, pointer_x=lambda x=self.mouse_x: x\n",
        ["test_the_listener_reader_returns_the_current_mouse_x"]),
    handler_mutation(
        "reader-always-right", MOUSE, READER,
        "            self.loop, self.hid_event_queue, pointer_x=lambda: 5000\n",
        ["test_the_listener_reader_returns_the_current_mouse_x",
         "test_real_listener_stamp_decides_the_zone"]),
    # ---- the zone decision ----------------------------------------------
    handler_mutation(
        "zone-reads-the-current-pointer", MOUSE, BATCH_X,
        "                            x = self.mouse_x\n",
        ["test_left_edge_movement_behind_a_tv_wait_never_reaches_volume",
         "test_volume_zone_movement_behind_a_wait_never_reaches_brightness",
         "test_real_listener_stamp_decides_the_zone",
         "test_the_stamp_decides_against_the_side_offset",
         "test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order"]),
    handler_mutation(
        "a-stamp-of-zero-is-ignored", MOUSE, BATCH_X,
        '                            x = e.get("x") or self.mouse_x\n',
        ["test_the_stamp_decides_against_the_side_offset"]),
    handler_mutation(
        "no-stamp-means-the-left-edge", MOUSE, BATCH_X,
        '                            x = e.get("x", 0)\n',
        ["test_routes_to_volume_when_mouse_right",
         "test_aggregates_multiple_queued_events"]),
    handler_mutation(
        "zone-comparison-inverted", MOUSE, ZONE_COMPARISON,
        "BRIGHTNESS_ZONE if x >= self.side_offset else VOLUME_ZONE",
        ["test_the_stamp_decides_against_the_side_offset",
         "test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_routes_to_brightness_when_mouse_left",
         "test_routes_to_volume_when_mouse_right"]),
    handler_mutation(
        "zone-boundary-moves", MOUSE, ZONE_COMPARISON,
        "BRIGHTNESS_ZONE if x <= self.side_offset else VOLUME_ZONE",
        ["test_the_stamp_decides_against_the_side_offset"]),
    handler_mutation(
        "zone-dispatch-inverted", MOUSE,
        "                    if run.zone == BRIGHTNESS_ZONE:\n",
        "                    if run.zone != BRIGHTNESS_ZONE:\n",
        ["test_the_stamp_decides_against_the_side_offset",
         "test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_routes_to_brightness_when_mouse_left",
         "test_routes_to_volume_when_mouse_right"]),
    # ---- the runs of one drain ------------------------------------------
    handler_mutation(
        "both-zones-merge-into-one-run", MOUSE,
        "                            if not runs or runs[-1].zone != zone:\n",
        "                            if not runs:\n",
        ["test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_a_run_that_nets_to_zero_reaches_no_zone",
         "test_every_drained_batch_is_filtered_before_the_first_zone_acts",
         "test_each_run_logs_its_zone_stamp_and_ticks"]),
    handler_mutation(
        "only-the-first-run-acts", MOUSE, ACT_ON_RUNS,
        "                for run in runs[:1]:\n"
        "                    if run.ticks == 0:\n",
        ["test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_a_run_that_nets_to_zero_reaches_no_zone",
         "test_every_drained_batch_is_filtered_before_the_first_zone_acts",
         "test_each_run_logs_its_zone_stamp_and_ticks"]),
    handler_mutation(
        "only-the-newest-run-is-kept", MOUSE,
        "                                runs.append(_WheelRun(zone, x))\n",
        "                                runs[:] = [_WheelRun(zone, x)]\n",
        ["test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_every_drained_batch_is_filtered_before_the_first_zone_acts",
         "test_each_run_logs_its_zone_stamp_and_ticks"]),
    handler_mutation(
        "runs-act-in-reverse-order", MOUSE, ACT_ON_RUNS,
        "                for run in reversed(runs):\n"
        "                    if run.ticks == 0:\n",
        ["test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order",
         "test_each_run_logs_its_zone_stamp_and_ticks"]),
    handler_mutation(
        "a-run-acts-before-the-drain-is-final", MOUSE, NEW_RUN,
        "                            if not runs or runs[-1].zone != zone:\n"
        "                                if runs and runs[-1].ticks != 0:\n"
        "                                    if runs[-1].zone == BRIGHTNESS_ZONE:\n"
        "                                        await self._handle_brightness_zone_event(runs[-1].ticks)\n"
        "                                    else:\n"
        "                                        await self._handle_volume_zone_event(runs[-1].ticks)\n"
        "                                    runs[-1].ticks = 0\n"
        "                                runs.append(_WheelRun(zone, x))\n",
        ["test_every_drained_batch_is_filtered_before_the_first_zone_acts"]),
    handler_mutation(
        "a-run-that-nets-to-zero-still-acts", MOUSE,
        "                    if run.ticks == 0:\n"
        "                        # Held below the dead zone or cancelled; nothing for\n"
        "                        # a zone to do.\n"
        "                        continue\n",
        "                    if run.ticks == 0:\n"
        "                        # Held below the dead zone or cancelled; nothing for\n"
        "                        # a zone to do.\n"
        "                        pass\n",
        ["test_a_run_that_nets_to_zero_reaches_no_zone"]),
    handler_mutation(
        "run-log-names-the-first-stamp", MOUSE,
        "                            run.last_x = x\n",
        "                            pass\n",
        ["test_each_run_logs_its_zone_stamp_and_ticks"]),
    # ---- the reset at a change of zone ----------------------------------
    handler_mutation(
        "zone-reset-removed", MOUSE, RESET_AT_CHANGE,
        "                            pass\n",
        HELD_TICKS_CATCHERS),
    handler_mutation(
        "zone-reset-on-every-batch", MOUSE,
        "                            if self._last_wheel_zone not in (None, zone):\n",
        "                            if True:\n",
        ["test_two_drains_in_one_zone_still_share_the_dead_zone",
         "test_queued_batches_share_the_dead_zone"]),
    handler_mutation(
        "last-zone-not-recorded", MOUSE, RECORD_ZONE,
        "                            pass\n",
        HELD_TICKS_CATCHERS),
    handler_mutation(
        "the-first-zone-stays-recorded", MOUSE, RECORD_ZONE,
        "                            if self._last_wheel_zone is None:\n"
        "                                self._last_wheel_zone = zone\n",
        ["test_the_new_zone_counts_its_own_ticks_after_a_change"]),
    handler_mutation(
        "last-zone-forgotten-between-drains", MOUSE,
        "                runs: List[_WheelRun] = []\n",
        "                runs: List[_WheelRun] = []\n"
        "                self._last_wheel_zone = None\n",
        ["test_the_reset_works_across_two_drains"]),
    # A placement mutation: the reset moves, and no copy stays above the filter.
    handler_mutation(
        "zone-reset-after-the-filter", MOUSE,
        RESET_AT_CHANGE + RECORD_ZONE + NEW_RUN + FILTER_AND_ADD,
        NEW_RUN + FILTER_AND_ADD + RESET_AT_CHANGE + RECORD_ZONE,
        HELD_TICKS_CATCHERS),
    # ---- ThumbWheelFilter.reset -----------------------------------------
    handler_mutation(
        "reset-keeps-the-held-ticks", FILTER,
        RESET_DOC + "        self._held_ticks = 0\n",
        RESET_DOC,
        ["test_reset_forgets_held_ticks",
         "test_ticks_held_at_the_left_edge_never_reach_volume",
         "test_ticks_held_in_the_volume_zone_never_reach_brightness",
         "test_the_reset_works_across_two_drains"]),
    handler_mutation(
        "reset-keeps-the-gesture-open", FILTER,
        RESET_DOC + "        self._held_ticks = 0\n"
        "        self._gesture_open = False\n",
        RESET_DOC + "        self._held_ticks = 0\n",
        ["test_reset_closes_an_open_gesture",
         "test_a_zone_change_closes_an_open_gesture"]),
    # ---- the skip -------------------------------------------------------
    plugin_mutation(
        "skip-check-removed", SKIP_CHECK,
        "            if False:\n",
        SKIP_CATCHERS),
    plugin_mutation(
        "skip-check-inverted", SKIP_CHECK,
        "            if self._retry_after is not None and self._monotonic() >= self._retry_after:\n",
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_a_step_at_the_end_of_the_interval_contacts_the_tv"]),
    plugin_mutation(
        "skip-check-boundary", SKIP_CHECK,
        "            if self._retry_after is not None and self._monotonic() <= self._retry_after:\n",
        ["test_a_step_at_the_end_of_the_interval_contacts_the_tv",
         "test_failed_read_at_start_opens_the_interval",
         "test_a_failed_retry_opens_a_new_interval"]),
    # Placement mutations: the skip moves, and no copy stays below the guard.
    plugin_mutation(
        "skip-before-the-active-guard",
        GUARD + SKIP_CHECK + SKIP_LOG + SKIP_TAIL,
        SKIP_CHECK + SKIP_LOG + SKIP_PUBLISH + "                return\n"
        + GUARD + "            started = time.monotonic()\n",
        ["test_a_zero_step_during_the_interval_publishes_nothing",
         "test_a_stopped_plugin_publishes_nothing_during_the_interval"]),
    plugin_mutation(
        "skip-outside-the-lock",
        "        async with self._lock:\n" + GUARD + SKIP_CHECK + SKIP_LOG + SKIP_TAIL,
        "        if self._retry_after is not None and self._monotonic() < self._retry_after:\n"
        '            logger.debug(f"Samsung TV not contacted: last result was {self._error}, "\n'
        '                         f"retry in {self._retry_after - self._monotonic():.1f}s")\n'
        "            await self._publish_offline_overflow(event)\n"
        "            return\n"
        "        async with self._lock:\n" + GUARD
        + "            started = time.monotonic()\n",
        ["test_a_step_waiting_behind_a_failing_step_does_not_contact_the_tv",
         "test_a_zero_step_during_the_interval_publishes_nothing",
         "test_a_stopped_plugin_publishes_nothing_during_the_interval"]),
    plugin_mutation(
        "skip-publishes-nothing", SKIP_TAIL, SKIP_END,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_skipped_step_takes_the_path_of_a_timed_out_step"]),
    plugin_mutation(
        "skip-falls-through-to-the-tv", SKIP_TAIL,
        SKIP_PUBLISH + "            started = time.monotonic()\n",
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_skipped_step_takes_the_path_of_a_timed_out_step"]),
    plugin_mutation(
        "skip-sends-the-step-to-the-tv", SKIP_TAIL,
        "                await self._control.change_brightness(event.delta)\n" + SKIP_TAIL,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_skipped_step_takes_the_path_of_a_timed_out_step"]),
    plugin_mutation(
        "skip-reads-the-tv", SKIP_TAIL,
        "                await self._control.get_brightness()\n" + SKIP_TAIL,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-publishes-the-wrong-delta", SKIP_TAIL,
        _inline_overflow(delta="-event.delta") + SKIP_END,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-drops-the-command-id", SKIP_TAIL,
        _inline_overflow(command_id="None") + SKIP_END,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-uses-another-reason", SKIP_TAIL,
        _inline_overflow(reason="'at_hardware_limit'") + SKIP_END,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-sends-a-notice", SKIP_TAIL,
        "                await self._event_bus.publish(SystemConfigurationErrorEvent(\n"
        "                    service_name='Samsung TV', error_message='silent', user_action='wait'))\n"
        + SKIP_TAIL,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-clears-the-stored-error", SKIP_TAIL,
        "                self._error = None\n" + SKIP_TAIL,
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "skip-extends-the-interval", SKIP_TAIL,
        "                self._retry_after = self._monotonic() + OFFLINE_RETRY_INTERVAL_SECONDS\n"
        + SKIP_TAIL,
        ["test_a_step_at_the_end_of_the_interval_contacts_the_tv"]),
    # ---- the retry time -------------------------------------------------
    plugin_mutation(
        "retry-not-set-for-timeout", RETRY_CONDITION,
        "                             if reason == 'offline' else None)\n",
        SKIP_CATCHERS),
    plugin_mutation(
        "retry-not-set-for-offline", RETRY_CONDITION,
        "                             if reason == 'timeout' else None)\n",
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_skipped_step_takes_the_path_of_a_timed_out_step",
         "test_failed_read_at_start_opens_the_interval"]),
    plugin_mutation(
        "retry-set-for-a-rejection", RETRY_CONDITION,
        "                             if reason in (*_NO_ANSWER_ERRORS, 'rejected') else None)\n",
        ["test_a_tv_that_answers_with_a_refusal_ends_the_interval",
         "test_a_refusal_at_restart_ends_the_interval"]),
    plugin_mutation(
        "retry-set-for-an-unavailable-identity", RETRY_CONDITION,
        "                             if reason in (*_NO_ANSWER_ERRORS, 'identity_unavailable')"
        " else None)\n",
        ["test_a_tv_that_answers_with_a_refusal_ends_the_interval",
         "test_a_refusal_at_restart_ends_the_interval"]),
    # Both catchers below use the read at start, the only result that arrives
    # while the end time still lies ahead. A step reaches the TV only at or after
    # that time, and the clock never goes back, so a step's own result cannot
    # show whether the end time was cleared.
    plugin_mutation(
        "retry-not-cleared-by-a-good-result", GOOD_RESULT_CLEARS,
        "        self._error = None\n",
        ["test_a_good_read_at_restart_ends_the_interval"]),
    plugin_mutation(
        "retry-not-cleared-by-another-error", RETRY_CONDITION,
        "                             if reason in _NO_ANSWER_ERRORS else self._retry_after)\n",
        ["test_a_refusal_at_restart_ends_the_interval"]),
    plugin_mutation(
        "retry-read-before-the-result-returned", STEP_REACHES_THE_TV,
        "            before_the_wait = self._monotonic()\n"
        + STEP_REACHES_THE_TV
        + "                if self._retry_after is not None:\n"
        "                    self._retry_after = before_the_wait + OFFLINE_RETRY_INTERVAL_SECONDS\n",
        ["test_the_interval_starts_when_the_wait_ended"]),
    plugin_mutation(
        "retry-time-is-in-the-past",
        "        self._retry_after = (self._monotonic() + OFFLINE_RETRY_INTERVAL_SECONDS\n",
        "        self._retry_after = (self._monotonic() - OFFLINE_RETRY_INTERVAL_SECONDS\n",
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_failed_read_at_start_opens_the_interval"]),
    plugin_mutation(
        "retry-interval-is-zero", INTERVAL,
        "OFFLINE_RETRY_INTERVAL_SECONDS = 0.0\n",
        ["test_step_after_a_silent_tv_result_does_not_contact_the_tv",
         "test_the_retry_interval_is_ten_seconds"]),
    plugin_mutation(
        "retry-interval-is-an-hour", INTERVAL,
        "OFFLINE_RETRY_INTERVAL_SECONDS = 3600.0\n",
        ["test_the_retry_interval_is_ten_seconds"]),
    plugin_mutation(
        "no-answer-set-lacks-timeout", NO_ANSWER,
        "_NO_ANSWER_ERRORS = ('offline',)\n",
        ["test_failures_never_report_success_or_fake_limit",
         "test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "no-answer-set-lacks-offline", NO_ANSWER,
        "_NO_ANSWER_ERRORS = ('timeout',)\n",
        ["test_failures_never_report_success_or_fake_limit",
         "test_step_after_a_silent_tv_result_does_not_contact_the_tv"]),
    plugin_mutation(
        "no-answer-set-gains-a-rejection", NO_ANSWER,
        "_NO_ANSWER_ERRORS = ('offline', 'timeout', 'rejected')\n",
        ["test_offline_software_dimming_matches_existing_sony_path",
         "test_failures_never_report_success_or_fake_limit",
         "test_a_tv_that_answers_with_a_refusal_ends_the_interval"]),
    plugin_mutation(
        "plugin-clock-is-stuck",
        "        self._monotonic: Callable[[], float] = time.monotonic\n",
        "        self._monotonic: Callable[[], float] = lambda: 0.0\n",
        ["test_the_plugin_clock_is_monotonic"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
