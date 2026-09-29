"""Mutation gate for wh-mic-loss-capture-recovery.

The bead's acceptance criterion 4: a test fails when the recovery code is
removed. On 2026-09-24 the Scarlett Solo was "not present" in Windows for
0.9 s, get_frame() went on answering without raising, and the provider
polled a dead AudioGraph for over an hour until Wheelhouse was restarted.
The fix detects the loss two ways (the graph's UnrecoverableErrorOccurred
event, and CAPTURE_STALL_SECONDS with no samples), rebuilds the graph in
the same capture thread, retries every REBUILD_RETRY_SECONDS on the
cycle's stop event, and logs one loss line, reminders at most once per
STILL_UNAVAILABLE_LOG_SECONDS, and one recovery line naming the device.
Each mutation below puts one piece of the old behaviour back, or breaks
one of the boss rulings (C1 recovered means samples arrived, C2 a stop in
the retry wait returns at once, C4 the recovery line names the device).

Run it from services/stt_providers/shared, with the interpreter of a
provider venv that has wheelhouse-shared installed (the shared package has
no venv of its own in a worktree). ``sys.executable`` becomes the pytest
launcher, see LAUNCH below:

    ../sherpa_offline_parakeet_stt_server/.venv/Scripts/python.exe \
        tests/mutation_gate_mic_loss_recovery.py --check
    ../sherpa_offline_parakeet_stt_server/.venv/Scripts/python.exe \
        tests/mutation_gate_mic_loss_recovery.py

``--check`` answers the two offline questions -- does every pattern match
exactly once, and does every mutant still parse -- without running a test
or writing a file. It is NOT a sweep: it cannot see a survivor. Run the
gate in full before the final commit.

WHY THIS GATE REPLACES ``runner.LAUNCH``. The runner's default starts
pytest through ``scripts/run_tests.py``, which runs ``uv run pytest``. In a
git worktree ``uv`` builds a ``.venv`` inside the tree, and that .venv is
what makes a worktree undeletable. This gate launches ``<python> -m
pytest`` in the service directory instead, the same replacement
services/wheelhouse/tests/mutation_gate_pattern_manager_tree_changed.py
makes. Nothing else about the run changes: the same Windows Job ownership,
the same timeout, the same pre-launch marker, the same output parsing.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]

WINRT_CAPTURE = SHARED / "shared_audio" / "capture" / "winrt_capture.py"

WINRT_CAPTURE_TESTS = "tests/test_winrt_capture.py"


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


# The catching tests, all in tests/test_winrt_capture.py. Every one but the
# last is in class TestMicrophoneLossRecovery.
RESUMES = "test_a_graph_that_stops_delivering_is_rebuilt_and_capture_resumes"
EVENT = ("test_the_unrecoverable_error_event_rebuilds_without_waiting_"
         "for_a_stall")
RETRIES = "test_failed_rebuilds_retry_until_the_microphone_returns"
ONE_OUTAGE = "test_a_rebuilt_graph_that_never_delivers_stays_one_outage"
STOP_IN_WAIT = "test_stop_during_the_retry_wait_returns_promptly"
FULL_QUEUE = "test_a_full_queue_is_not_a_stall"
NO_ERROR_LINE = "test_a_rebuild_raises_the_same_words_without_the_error_line"
# wh-mic-false-loss-stall-check, class TestTheStallTestJudgesPollsNotTheClock.
GIL_WAIT = "test_a_gil_wait_after_the_sleep_is_not_a_loss"
EMPTY_POLLS = "test_empty_polls_for_the_stall_time_are_a_loss"
RAISING_POLLS = "test_raising_polls_for_the_stall_time_are_a_loss"


def _mutation(name, old, new, *expect):
    return {
        "name": name,
        "service": SHARED,
        "test_file": WINRT_CAPTURE_TESTS,
        "file": WINRT_CAPTURE,
        "old": old,
        "new": new,
        "expect": list(expect),
    }


# --------------------------------------------------------------------------
# Detection: the stall and the graph's own event
# --------------------------------------------------------------------------

DETECTION_MUTATIONS = [
    # The defect itself: a graph that answers every poll with nothing is
    # polled forever. The threshold is multiplied rather than the test
    # removed, so the loop still advances exactly as before.
    _mutation(
        "the-stall-never-ends-the-poll",
        "            if poll_started - last_samples_at >= "
        "CAPTURE_STALL_SECONDS:\n",
        "            if poll_started - last_samples_at >= "
        "CAPTURE_STALL_SECONDS * 1e9:\n",
        RESUMES, RETRIES, ONE_OUTAGE, STOP_IN_WAIT, EMPTY_POLLS,
        RAISING_POLLS,
    ),
    # wh-mic-false-loss-stall-check acceptance 5: the stall test reads the
    # time after the sleep again, so a thread that waited for the GIL past
    # the threshold is judged lost before the poll that would have
    # collected its audio (39 false losses on 2026-09-25).
    _mutation(
        "the-stall-test-reads-the-time-after-the-sleep",
        "            if poll_started - last_samples_at >= "
        "CAPTURE_STALL_SECONDS:\n",
        "            if _monotonic() - last_samples_at >= "
        "CAPTURE_STALL_SECONDS:\n",
        GIL_WAIT, EMPTY_POLLS, RAISING_POLLS,
    ),
    # Samples that arrive no longer reset the stall clock, so a working
    # microphone whose consumer has stopped reading is rebuilt anyway
    # (ruling C3). The full-queue test is the one that keeps samples
    # arriving for longer than the shrunk threshold.
    _mutation(
        "samples-do-not-reset-the-stall-clock",
        "                            last_samples_at = _monotonic()\n"
        "                            if not delivered:\n",
        "                            pass\n"
        "                            if not delivered:\n",
        FULL_QUEUE,
    ),
    # Signal A read by nobody: the event fires and the poll goes on.
    _mutation(
        "the-loss-event-is-never-checked",
        "            if loss is not None and loss.event.is_set():\n",
        "            if False:\n",
        EVENT,
    ),
    # Signal A never subscribed.
    _mutation(
        "nothing-subscribes-to-the-unrecoverable-error-event",
        "            loss.token = graph.add_unrecoverable_error_occurred("
        "loss.handler)\n",
        "            pass\n",
        EVENT,
    ),
]


# --------------------------------------------------------------------------
# The rebuild
# --------------------------------------------------------------------------

REBUILD_MUTATIONS = [
    # The loss is logged and the graph closed, but the thread then ends
    # instead of building a new graph: the 2026-09-24 outcome with a log
    # line added.
    _mutation(
        "a-loss-ends-the-thread-instead-of-rebuilding",
        "            rebuilt = self._rebuild(cycle, stop_event, outage)\n",
        "            rebuilt = None\n",
        RESUMES, EVENT, RETRIES, ONE_OUTAGE,
    ),
    # The lost graph is dropped without being closed.
    _mutation(
        "the-lost-graph-is-not-closed",
        "            self._forget_loss_handler(owned[0], loss)\n"
        "            self._cleanup_graph(*owned)\n",
        "            self._forget_loss_handler(owned[0], loss)\n"
        "            pass\n",
        RESUMES,
    ),
    # A loss leaves the provider answering ready for a graph that is gone.
    # Only the liveness write is removed; the cycle test and the other
    # writes stay.
    _mutation(
        "a-loss-does-not-take-readiness-back",
        "                return\n"
        "            self._capture_alive = False\n"
        "            self._graph = None\n",
        "                return\n"
        "            pass\n"
        "            self._graph = None\n",
        ONE_OUTAGE,
    ),
    # Ruling C2: the retry wait on time.sleep. time.sleep returns None, so
    # the mutant goes on to the loop's own cycle test after the full wait,
    # exactly as a sleep-based wait would.
    _mutation(
        "the-retry-wait-sleeps-instead-of-waiting-on-the-stop-event",
        "                if stop_event.wait(REBUILD_RETRY_SECONDS):\n",
        "                if time.sleep(REBUILD_RETRY_SECONDS):\n",
        STOP_IN_WAIT,
    ),
    # Ruling C2 from the other side: the event exists but stop() never
    # sets it.
    _mutation(
        "stop-does-not-set-the-stop-event",
        "            self._running = False\n"
        "            self._stop_event.set()\n",
        "            self._running = False\n"
        "            pass\n",
        STOP_IN_WAIT,
    ),
]


# --------------------------------------------------------------------------
# Ruling C1: recovered means samples arrived, not that a graph built
# --------------------------------------------------------------------------

READINESS_MUTATIONS = [
    # A rebuilt graph answers ready as soon as one poll comes through,
    # samples or not.
    _mutation(
        "a-rebuilt-graph-is-ready-before-it-delivers",
        "                if recovering is None or delivered:\n",
        "                if True:\n",
        ONE_OUTAGE,
    ),
    # The recovery line MOVED to the moment the rebuilt graph starts
    # polling: logged there, and the call at the first samples made a
    # no-op, so the mutant writes it once, in the wrong place. Readiness
    # (the `recovering is None or delivered` gate) is untouched.
    _mutation(
        "recovery-is-logged-when-the-graph-builds",
        "        recovering = getattr(self._poll_context, 'recovering', None)\n",
        "        recovering = getattr(self._poll_context, 'recovering', None)\n"
        "        if recovering is not None:\n"
        "            recovering.recovered()\n"
        "            recovering.recovered = lambda: None\n",
        ONE_OUTAGE,
    ),
]


# --------------------------------------------------------------------------
# The log lines: bounded while absent, and the device named on recovery
# --------------------------------------------------------------------------

LOG_MUTATIONS = [
    # A reminder on every failed attempt, every REBUILD_RETRY_SECONDS.
    _mutation(
        "the-reminder-has-no-period",
        "        if now - self.last_logged < STILL_UNAVAILABLE_LOG_SECONDS:\n",
        "        if False:\n",
        ONE_OUTAGE,
    ),
    # Every rebuild attempt writes the startup ERROR line again.
    _mutation(
        "the-rebuild-writes-the-error-line-every-attempt",
        "                if log_failure:\n"
        "                    logger.error(AUDIO_DEVICE_MISSING_LOG_LINE)\n",
        "                if True:\n"
        "                    logger.error(AUDIO_DEVICE_MISSING_LOG_LINE)\n",
        NO_ERROR_LINE,
    ),
    # Ruling C4, at the line: the name is known and not written.
    _mutation(
        "the-recovery-line-drops-the-device-name",
        "        device = f' on {self.device!r}' if self.device else ''\n",
        "        device = ''\n",
        RESUMES, RETRIES,
    ),
    # Ruling C4, at the source: the name is never read from the new graph.
    _mutation(
        "the-rebuild-never-records-the-device-name",
        "            outage.device = _device_name(triple[1])\n",
        "            outage.device = None\n",
        RESUMES, RETRIES,
    ),
]


MUTATIONS = (
    DETECTION_MUTATIONS
    + REBUILD_MUTATIONS
    + READINESS_MUTATIONS
    + LOG_MUTATIONS
)


if __name__ == "__main__":
    sys.exit(runner.run(MUTATIONS))
