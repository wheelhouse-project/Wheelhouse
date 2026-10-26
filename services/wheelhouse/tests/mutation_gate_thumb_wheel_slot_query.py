"""Mutation gate for the thumb wheel slot query (wh-thumb-wheel-not-detected).

WHAT IT GUARDS. handlers/hid_listener.py no longer keeps a fixed list of
thumb-wheel pages. It asks each receiver slot 1-6 for the THUMB_WHEEL
(0x2150) feature index with a read-only HID++ Root getFeature query that
carries software ID 0x0E (commit ce474844), accepts a thumb-wheel report only
from a slot and feature index that answered, and queries a slot with no
answer again when it sends a HID++ notification, at most once per slot in 5
seconds (commit bfb0e63d). Each mutation below breaks one changed behaviour
of `git diff e928302b..bfb0e63d -- services/wheelhouse/handlers/hid_listener.py`
and names the tests in tests/test_handlers/test_hid_listener.py that must
fail. The tests in class TestQueryLimitsAndFilters were added with this gate,
for the mutations that no earlier test caught. One example: round-ignores-stop
survived the first sweep, because test_blocked_send_delays_neither_start_nor_stop
looks 0.3 s after stop() while the round waits the unpatched 1 s before its
next send; test_round_sends_nothing_more_after_stop now catches it.

MUTATIONS LEFT OUT, each with the reason it cannot change what a caller sees
(read against the source at bfb0e63d):

  answer-falls-through       ``return True`` after our answer -> False. The
                             page of the answer is ``slot | 0x00 << 8``, below
                             0x100, and every recorded page is at least 0x100
                             (feature index 0 is never recorded), so the page
                             check fails; the trigger then refuses sub ID 0.
  error-falls-through        ``return True`` after an error -> False. Bytes
                             3-4 read as usage 0x0E00, which is neither
                             0xFF00 nor 0x0000, so no report can turn the
                             wheel; the trigger refuses sub ID 0x8F / 0xFF
                             (at least 0x80).
  link-up-ignores-stop       the stop check in the 0x41 branch. With it gone,
                             a link-up after stop() starts a round that
                             checks the stop event before its first send and
                             sends nothing; the "no THUMB_WHEEL" mark it
                             clears is never read after stop().
  trigger-ignores-stop       the stop check in _query_slot_after_notification
                             (hid_listener.py:485). Same reason: the round
                             it starts sends nothing after stop(), and its
                             finally clears the in-progress mark.
  trigger-ignores-answer     ``slot in self._thumb_wheel_features`` in the
                             trigger (hid_listener.py:489). A turn from an
                             answered slot takes the page branch and never
                             reaches the trigger; any other notification from
                             that slot reaches the round, whose filter drops
                             the slot before any send.
  one-no-thumb-wheel-check   each of the two reads of _no_thumb_wheel_slots
                             alone (trigger, hid_listener.py:490; round
                             filter, :540): the other read drops the slot
                             before any send. Mutation zero-answer-not-
                             remembered removes the state both read.
  nonzero-answer-keeps-mark  ``_no_thumb_wheel_slots.discard(slot)`` on a
                             nonzero answer (hid_listener.py:438). Both reads
                             of the mark sit beside a read of
                             _thumb_wheel_features, which then holds the
                             slot, so the stale mark is never the deciding
                             read; a later 0 answer adds the mark again and a
                             0x41 link-up clears it.
  pending-ignores-stop       the stop check before a round starts the query
                             for a link-up that arrived while it ran. The
                             round it starts checks the stop event before its
                             first send and sends nothing, and its finally
                             clears the in-progress mark.
  pending-mark-dropped       ``set(slots) - set(linked)`` -> ``slots``: the
                             mark is absent while the next link-up round
                             runs. Only a link-up or a notification inside
                             that round sees the change, and it then starts
                             one more round at once instead of after the
                             running round: no thumb wheel turn is lost and
                             the rounds stay one per link-up.
  error-ignores-byte-3       ``arr[3] == HIDPP_ROOT_FEATURE_INDEX`` in the
                             error branch. Wheelhouse is the only sender of
                             software ID 0xE and sends only to Root index 0,
                             so an error that carries 0x0E in byte 4 always
                             carries 0 in byte 3.

ONE MUTATION RUNS A NARROWER SELECTION. query-runs-on-the-calling-thread makes
every send run on the caller's thread. In
test_blocked_send_delays_neither_start_nor_stop that is six blocked sends of
5 s each plus six 1 s waits, 36 s, past the suite's 30 s pytest-timeout, which
would abort the whole run (suite-timeout-abort). It runs on its one expected
test only, test_trigger_does_not_block_the_reader_thread. The earlier
selection, class TestQueryAfterNotification, stopped working when
test_link_up_burst_starts_one_round_per_slot joined that class (.1.1): under
the mutant its 11 link-ups each block the test thread for 5 s.

WHAT ``caught`` MEANS. ``expect`` is the set of tests that must fail; the
shared runner reports a survivor when any of them passes. This gate prints
each run's FAILED summary lines with their reason, so the record shows WHICH
assertion fired. A parametrized test counts as caught when any case fails;
the printed lines name the case.

The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
owns pattern matching, the exactly-one-match check, the compile check, the
expected-name check, the green baseline, the per-run timeout, bytecode
clearing, and source restoration. This file replaces ``runner.LAUNCH`` as
tests/mutation_gate_volume_router_sonos_enabled.py does: the default launch
goes through ``uv run``, which builds a .venv inside a git worktree.

Run from services/wheelhouse with an interpreter that has the service's
dependencies (``sys.executable`` becomes the pytest launcher):

    python tests/mutation_gate_thumb_wheel_slot_query.py            # all
    python tests/mutation_gate_thumb_wheel_slot_query.py daemon     # by name
    python tests/mutation_gate_thumb_wheel_slot_query.py --check    # patterns
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the module docstring)."""
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
    """The shared run, plus each FAILED summary line with its reason, so a
    failure upstream of the guarded behaviour cannot read as a catch."""
    result = _shared_run_pytest(service, test_file)
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith("FAILED "):
            print("    " + line.split("::", 1)[-1][:240])
    return result


runner._run_pytest = _run_pytest_and_show_reasons

TEST = "tests/test_handlers/test_hid_listener.py"
NARROW = TEST + "::TestQueryAfterNotification::test_trigger_does_not_block_the_reader_thread"
HID = SERVICE / "handlers/hid_listener.py"

MUTATIONS = []


def add(name, old, new, *expect, test_file=TEST):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=test_file,
                          file=HID, old=old, new=new, expect=list(expect)))


# ---- only answered slot and feature pairs are accepted (ce474844) ----------
add("unanswered-page-accepted",
    "            if page in self._thumb_wheel_pages:\n",
    "            if True:\n",
    "test_notifications_dropped_without_a_getfeature_answer",
    "test_unanswered_slot_on_a_former_fixed_page_is_dropped",
    "test_answer_with_feature_index_zero_registers_nothing")
add("page-built-with-the-wrong-shift",
    "                s | (i << 8) for s, i in self._thumb_wheel_features.items()\n",
    "                s | (i << 4) for s, i in self._thumb_wheel_features.items()\n",
    "test_notifications_act_after_the_getfeature_answer",
    "test_query_round_opens_the_answered_slot")
add("answer-ignores-the-software-id",
    "        if sub_id == HIDPP_ROOT_FEATURE_INDEX and arr[3] == GET_FEATURE_FUNCTION_SWID:\n",
    "        if sub_id == HIDPP_ROOT_FEATURE_INDEX:\n",
    "test_options_plus_getfeature_answer_is_not_taken_as_ours")
add("software-id-is-options-plus",
    "HIDPP_SOFTWARE_ID = 0x0E\n",
    "HIDPP_SOFTWARE_ID = 0x0A\n",
    "test_query_sends_only_getfeature_with_its_own_software_id",
    "test_notifications_act_after_the_getfeature_answer")
add("answer-records-the-wrong-byte",
    "            self._record_thumb_wheel_answer(slot, arr[4])\n",
    "            self._record_thumb_wheel_answer(slot, arr[5])\n",
    "test_notifications_act_after_the_getfeature_answer")
add("answer-not-stored",
    "                self._thumb_wheel_features[slot] = feature_index\n",
    "                pass\n",
    "test_notifications_act_after_the_getfeature_answer",
    "test_query_round_opens_the_answered_slot")
add("zero-answer-keeps-the-old-index",
    "                self._thumb_wheel_features.pop(slot, None)\n",
    "                pass\n",
    "test_new_answer_for_a_slot_replaces_its_feature_index")
add("link-notification-falls-through",
    "                        self._slot_queries_in_progress.discard(slot)\n"
    "            return True\n",
    "                        self._slot_queries_in_progress.discard(slot)\n"
    "            return False\n",
    # Only the lost link shows the fall-through: after a link-up, the slot's
    # "in progress" mark (.1.1) stops the trigger from a second query.
    "test_link_lost_notification_sends_nothing")

# ---- the request bytes, slots 1-6, and padding -----------------------------
add("request-uses-function-1",
    "GET_FEATURE_FUNCTION_SWID = (0 << 4) | HIDPP_SOFTWARE_ID\n",
    "GET_FEATURE_FUNCTION_SWID = (1 << 4) | HIDPP_SOFTWARE_ID\n",
    "test_query_sends_only_getfeature_with_its_own_software_id")
add("request-asks-for-another-feature",
    "THUMB_WHEEL_FEATURE_ID = 0x2150\n",
    "THUMB_WHEEL_FEATURE_ID = 0x2151\n",
    "test_query_sends_only_getfeature_with_its_own_software_id")
add("request-sent-as-a-long-report",
    "                HIDPP_SHORT_REPORT_ID, slot, HIDPP_ROOT_FEATURE_INDEX, GET_FEATURE_FUNCTION_SWID,\n",
    "                HIDPP_LONG_REPORT_ID, slot, HIDPP_ROOT_FEATURE_INDEX, GET_FEATURE_FUNCTION_SWID,\n",
    "test_query_sends_only_getfeature_with_its_own_software_id")
add("request-trailing-byte-is-not-zero",
    "                THUMB_WHEEL_FEATURE_ID >> 8, THUMB_WHEEL_FEATURE_ID & 0xFF, 0x00,\n",
    "                THUMB_WHEEL_FEATURE_ID >> 8, THUMB_WHEEL_FEATURE_ID & 0xFF, 0x01,\n",
    "test_query_sends_only_getfeature_with_its_own_software_id")
add("request-not-padded",
    "            request += [0x00] * (device.hid_caps.output_report_byte_length - len(request))\n",
    "            pass\n",
    "test_request_is_padded_to_the_output_report_length")
add("slots-stop-at-5",
    "RECEIVER_SLOTS = range(1, 7)\n",
    "RECEIVER_SLOTS = range(1, 6)\n",
    "test_start_queries_slots_1_to_6",
    "test_link_up_notification_queries_that_slot")
add("slots-start-at-0",
    "RECEIVER_SLOTS = range(1, 7)\n",
    "RECEIVER_SLOTS = range(0, 7)\n",
    "test_start_queries_slots_1_to_6",
    "test_link_up_outside_slots_1_to_6_sends_nothing",
    "test_report_that_is_not_a_notification_starts_no_query")
add("query-sent-on-every-hidpp-interface",
    "            return caps.usage_page == HIDPP_USAGE_PAGE and caps.usage == HIDPP_SHORT_USAGE\n",
    "            return caps.usage_page == HIDPP_USAGE_PAGE\n",
    "test_query_is_sent_only_on_the_hidpp_short_interface")
add("start-sends-no-query",
    "        self._start_thumb_wheel_query(RECEIVER_SLOTS)\n",
    "        pass\n",
    "test_start_queries_slots_1_to_6")

# ---- the 0x41 link-up query ------------------------------------------------
add("link-lost-also-queries",
    "            link_up = not (arr[4] & LINK_NOT_ESTABLISHED)\n",
    "            link_up = True\n",
    "test_link_lost_notification_sends_nothing")
add("link-up-slot-range-removed",
    "            if link_up and slot in RECEIVER_SLOTS and not self._stop_event.is_set():\n",
    "            if link_up and not self._stop_event.is_set():\n",
    "test_link_up_outside_slots_1_to_6_sends_nothing")
add("link-up-keeps-the-no-thumb-wheel-mark",
    "                    self._no_thumb_wheel_slots.discard(slot)\n"
    "                    if slot in self._slot_queries_in_progress:\n",
    "                    pass\n"
    "                    if slot in self._slot_queries_in_progress:\n",
    "test_no_thumb_wheel_answer_stops_queries_until_link_up")
add("link-up-sends-no-query",
    "                    self._slot_queries_in_progress.add(slot)\n"
    "                if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n",
    "                    self._slot_queries_in_progress.add(slot)\n"
    "                if True:\n",
    "test_link_up_notification_queries_that_slot",
    "test_reconnect_answer_reopens_the_slot")

# ---- one link-up round per slot (wh-thumb-wheel-not-detected.1.1) ----------
add("link-up-burst-starts-a-round-each-time",
    "                        self._slot_link_ups_pending.add(slot)\n"
    "                        return True\n",
    "                        self._slot_link_ups_pending.add(slot)\n"
    "                        pass\n",
    "test_link_up_burst_starts_one_round_per_slot")
add("link-up-mark-never-set",
    "                    self._slot_queries_in_progress.add(slot)\n"
    "                if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n",
    "                    pass\n"
    "                if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n",
    "test_link_up_burst_starts_one_round_per_slot")
add("link-up-round-keeps-its-mark",
    "            if from_notification or owns_marks:\n",
    "            if from_notification:\n",
    "test_no_thumb_wheel_answer_stops_queries_until_link_up",
    "test_link_up_burst_starts_one_round_per_slot")
add("link-up-mark-kept-after-a-failed-start",
    "                if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n"
    "                    with self._hidpp_lock:\n"
    "                        self._slot_queries_in_progress.discard(slot)\n",
    "                if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n"
    "                    pass\n",
    "test_failed_link_up_start_leaves_the_slot_free")

# ---- a link-up during a query starts one query after it (.1.2) -------------
add("link-up-during-a-query-is-dropped",
    "                        self._slot_link_ups_pending.add(slot)\n",
    "                        pass\n",
    "test_link_up_burst_starts_one_round_per_slot",
    "test_replacement_mouse_linked_during_a_query_is_asked",
    "test_failed_start_after_a_query_leaves_the_slot_free")
add("pending-link-up-never-started",
    "                        linked = tuple(s for s in slots if s in self._slot_link_ups_pending)\n",
    "                        linked = ()\n",
    "test_link_up_burst_starts_one_round_per_slot",
    "test_replacement_mouse_linked_during_a_query_is_asked",
    "test_failed_start_after_a_query_leaves_the_slot_free")
add("pending-link-up-never-cleared",
    "                    self._slot_link_ups_pending.difference_update(slots)\n",
    "                    pass\n",
    "test_link_up_burst_starts_one_round_per_slot",
    "test_replacement_mouse_linked_during_a_query_is_asked")
add("pending-start-failure-keeps-the-mark",
    "                    if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n"
    "                        with self._hidpp_lock:\n"
    "                            self._slot_queries_in_progress.discard(slot)\n",
    "                    if not self._start_thumb_wheel_query((slot,), owns_marks=True):\n"
    "                        pass\n",
    "test_failed_start_after_a_query_leaves_the_slot_free")

# ---- the per-slot time limit (mutate the value, never remove the wait) -----
add("per-slot-wait-ten-times-longer",
    "            pending.answered.wait(HIDPP_QUERY_TIMEOUT)\n",
    "            pending.answered.wait(HIDPP_QUERY_TIMEOUT * 10)\n",
    "test_one_info_line_when_the_receiver_is_silent")
add("shipped-time-limit-thirty-seconds",
    "HIDPP_QUERY_TIMEOUT = 1.0 ",
    "HIDPP_QUERY_TIMEOUT = 30.0 ",
    "test_shipped_time_limit_is_one_second_per_slot")

# ---- each reply ends that slot's wait --------------------------------------
add("answer-does-not-end-the-wait",
    "            pending.feature_index = feature_index\n"
    "            pending.answered.set()\n",
    "            pending.feature_index = feature_index\n",
    "test_each_reply_ends_that_slot_wait_at_once")
add("answer-not-returned",
    "            pending.feature_index = feature_index\n"
    "            pending.answered.set()\n",
    "            pending.answered.set()\n",
    "test_info_line_for_each_slot_found",
    "test_error_for_another_program_does_not_end_the_wait")
add("error-does-not-end-the-wait",
    "            if pending is not None:\n"
    "                pending.answered.set()\n"
    "            return True\n",
    "            if pending is not None:\n"
    "                pass\n"
    "            return True\n",
    "test_each_reply_ends_that_slot_wait_at_once")
add("receiver-error-not-recognised",
    "        if (sub_id in (HIDPP_RECEIVER_ERROR, HIDPP_DEVICE_ERROR)\n",
    "        if (sub_id in (HIDPP_DEVICE_ERROR,)\n",
    "test_each_reply_ends_that_slot_wait_at_once")
add("device-error-not-recognised",
    "        if (sub_id in (HIDPP_RECEIVER_ERROR, HIDPP_DEVICE_ERROR)\n",
    "        if (sub_id in (HIDPP_RECEIVER_ERROR,)\n",
    "test_each_reply_ends_that_slot_wait_at_once")
add("error-ignores-the-software-id",
    "                and arr[3] == HIDPP_ROOT_FEATURE_INDEX\n"
    "                and arr[4] == GET_FEATURE_FUNCTION_SWID):\n",
    "                and arr[3] == HIDPP_ROOT_FEATURE_INDEX):\n",
    "test_error_for_another_program_does_not_end_the_wait")

# ---- what counts as found, and the INFO lines ------------------------------
add("zero-answer-counted-as-found",
    "                        if self._ask_thumb_wheel_feature_index(device, slot):\n",
    "                        if self._ask_thumb_wheel_feature_index(device, slot) is not None:\n",
    "test_round_where_every_answer_is_zero_logs_the_no_slot_line")
add("no-slot-line-never-logged",
    "                if not found:\n",
    "                if False:\n",
    "test_one_info_line_when_no_slot_answers",
    "test_one_info_line_when_the_receiver_is_silent",
    "test_round_where_every_answer_is_zero_logs_the_no_slot_line")
add("no-slot-line-at-debug",
    "                    logger.info(\n"
    "                        f\"Thumb wheel query: no receiver slot answered",
    "                    logger.debug(\n"
    "                        f\"Thumb wheel query: no receiver slot answered",
    "test_one_info_line_when_no_slot_answers",
    "test_one_info_line_when_the_receiver_is_silent")
add("found-line-at-debug",
    "            logger.info(\n"
    "                f\"Thumb wheel found:",
    "            logger.debug(\n"
    "                f\"Thumb wheel found:",
    "test_info_line_for_each_slot_found")

# ---- the query thread ------------------------------------------------------
add("query-thread-not-a-daemon",
    "                name=\"HIDPPThumbWheelQuery\",\n"
    "                daemon=True,\n",
    "                name=\"HIDPPThumbWheelQuery\",\n"
    "                daemon=False,\n",
    "test_query_thread_is_a_daemon")
add("round-ignores-stop",
    "                        if self._stop_event.is_set():\n"
    "                            logger.debug(\"Thumb wheel query ended",
    "                        if False:\n"
    "                            logger.debug(\"Thumb wheel query ended",
    "test_round_sends_nothing_more_after_stop")
add("query-runs-on-the-calling-thread",
    "            ).start()\n"
    "            return True\n",
    "            ).run()\n"
    "            return True\n",
    "test_trigger_does_not_block_the_reader_thread",
    test_file=NARROW)

# ---- the int16 direction rule and byte 3 = 0x00 ----------------------------
add("old-bolt-rule-restored",
    "                action_taken = False\n"
    "                if usage_id_from_report == USAGE_ID_FOR_UP_DIRECTION:\n",
    "                action_taken = delta != 0\n"
    "                if False:\n",
    "test_rotation_beyond_one_byte_is_dropped_as_on_ikon",
    "test_options_plus_answers_never_turn_the_wheel")
add("high-byte-sign-ignored-going-down",
    "                    if delta > 0:\n",
    "                    if delta != 0:\n",
    "test_rotation_beyond_one_byte_is_dropped_as_on_ikon")
add("high-byte-sign-ignored-going-up",
    "                    if delta < 0:\n",
    "                    if delta != 0:\n",
    "test_target_page_1303_wrong_direction_skipped")
add("byte-3-ignored",
    "            usage_id_from_report = arr[3] | (arr[4] << 8)\n",
    "            usage_id_from_report = arr[4] << 8\n",
    "test_options_plus_answers_never_turn_the_wheel")

# ---- the trigger filter (bfb0e63d) -----------------------------------------
add("trigger-takes-any-report-id",
    "        if (report_id not in (HIDPP_SHORT_REPORT_ID, HIDPP_LONG_REPORT_ID)\n"
    "                or slot not in RECEIVER_SLOTS\n",
    "        if (False\n"
    "                or slot not in RECEIVER_SLOTS\n",
    "test_report_that_is_not_a_notification_starts_no_query")
add("trigger-takes-any-slot",
    "                or slot not in RECEIVER_SLOTS\n"
    "                or not 0 < sub_id < HIDPP_FIRST_ANSWER_SUB_ID\n",
    "                or not 0 < sub_id < HIDPP_FIRST_ANSWER_SUB_ID\n",
    "test_report_that_is_not_a_notification_starts_no_query")
add("trigger-takes-answers-and-errors",
    "                or not 0 < sub_id < HIDPP_FIRST_ANSWER_SUB_ID\n",
    "                or not 0 < sub_id\n",
    "test_report_that_is_not_a_notification_starts_no_query")
add("trigger-takes-root-reports",
    "                or not 0 < sub_id < HIDPP_FIRST_ANSWER_SUB_ID\n",
    "                or not sub_id < HIDPP_FIRST_ANSWER_SUB_ID\n",
    "test_report_that_is_not_a_notification_starts_no_query")
add("trigger-takes-any-software-id",
    "                or fn_swid & 0x0F):\n",
    "                ):\n",
    "test_options_plus_answer_starts_no_query")
add("trigger-never-queries",
    "        if not self._start_thumb_wheel_query((slot,), from_notification=True):\n",
    "        if not True:\n",
    "test_timed_out_slot_is_queried_after_its_notification")

# ---- the 5 s per-slot limit ------------------------------------------------
add("interval-boundary-flipped",
    "            if last is not None and now - last < UNANSWERED_SLOT_QUERY_INTERVAL:\n",
    "            if last is not None and now - last <= UNANSWERED_SLOT_QUERY_INTERVAL:\n",
    "test_one_query_per_slot_in_five_seconds")
add("interval-four-seconds",
    "UNANSWERED_SLOT_QUERY_INTERVAL = 5.0\n",
    "UNANSWERED_SLOT_QUERY_INTERVAL = 4.0\n",
    "test_one_query_per_slot_in_five_seconds")
add("interval-start-not-recorded",
    "            self._slot_query_started[slot] = now\n",
    "            pass\n",
    "test_one_query_per_slot_in_five_seconds")
add("interval-clock-not-injected",
    "        now = self._monotonic()\n",
    "        now = time.monotonic()\n",
    "test_one_query_per_slot_in_five_seconds")

# ---- the in-progress mark --------------------------------------------------
add("in-progress-mark-ignored",
    "                    or slot in self._slot_queries_in_progress):\n",
    "                    ):\n",
    "test_no_second_query_while_one_is_in_progress")
add("in-progress-mark-never-set",
    "            self._slot_query_started[slot] = now\n"
    "            self._slot_queries_in_progress.add(slot)\n",
    "            self._slot_query_started[slot] = now\n"
    "            pass\n",
    "test_no_second_query_while_one_is_in_progress",
    "test_notification_round_skips_a_slot_answered_while_it_waited")
add("in-progress-mark-never-cleared",
    "                    self._slot_queries_in_progress.difference_update(set(slots) - set(linked))\n",
    "                    pass\n",
    "test_timed_out_slot_is_queried_after_its_notification",
    "test_one_query_per_slot_in_five_seconds")
add("in-progress-mark-kept-after-a-failed-start",
    "        if not self._start_thumb_wheel_query((slot,), from_notification=True):\n"
    "            with self._hidpp_lock:\n"
    "                self._slot_queries_in_progress.discard(slot)\n",
    "        if not self._start_thumb_wheel_query((slot,), from_notification=True):\n"
    "            pass\n",
    "test_failed_thread_start_leaves_the_slot_free_for_the_next_query")
add("failed-start-reported-as-started",
    "            logger.error(f\"Thumb wheel query not started: {e}\")\n"
    "            return False\n",
    "            logger.error(f\"Thumb wheel query not started: {e}\")\n"
    "            return True\n",
    "test_failed_thread_start_leaves_the_slot_free_for_the_next_query")
add("round-asks-a-slot-answered-while-it-waited",
    "                        asked = tuple(\n"
    "                            s for s in slots\n"
    "                            if s not in self._thumb_wheel_features\n"
    "                            and s not in self._no_thumb_wheel_slots)\n",
    "                        asked = slots\n",
    "test_notification_round_skips_a_slot_answered_while_it_waited")

# ---- the "no THUMB_WHEEL" mark ---------------------------------------------
add("zero-answer-not-remembered",
    "                self._no_thumb_wheel_slots.add(slot)\n",
    "                pass\n",
    "test_no_thumb_wheel_answer_stops_queries_until_link_up")


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
