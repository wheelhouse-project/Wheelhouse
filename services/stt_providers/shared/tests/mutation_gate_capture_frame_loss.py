"""Mutation gate for the wh-stt-load-metrics.3 guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared with plain python
(the gate needs only the standard library; it invokes `uv run pytest` as a
subprocess):

    python tests/mutation_gate_capture_frame_loss.py
    python tests/mutation_gate_capture_frame_loss.py mmcss   # name filter

Two kinds of behaviour are protected here and they fail differently.

The MMCSS registration is a scheduling request that the audio path must
survive whether it succeeds or not, so most of its mutations attack the
"or not" half: a NULL handle read as success, an exception escaping into the
PortAudio callback, a handle reverted from the wrong thread or reverted
twice. A break in any of those costs a captured frame or a crash on the
audio thread, and neither shows up as a failed assertion anywhere else.

The four log lines are a different risk. A wrong line still prints, still
looks like an answer, and sent this investigation the wrong way for two
hours on 2026-09-03 -- so their mutations mostly restore the exact wording
that was wrong, rather than deleting anything.

Runner discipline lives in tests/mutation_gate_runner.py.

Last full sweep: 72 of 72 caught, 0 survivors, 0 errors, over a3358e5f on
2026-09-04, after codex round 5 on wh-audio-callback-log.2 filed nothing.
The ten baseline files were green before the first mutation. Recorded here
because a `--check` run is NOT a sweep: it proves only that every pattern
matches exactly once and every mutant parses, and it cannot see a survivor
that a later defence-in-depth fix has masked.
"""
import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path, but this keeps it working when the gate
# is invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]

MMCSS_TESTS = "tests/test_capture_mmcss.py"
CALLBACK_LOG_TESTS = "tests/test_capture_log_sink.py"
WORDING_TESTS = "tests/test_capture_log_wording.py"
RUNNER_TESTS = "tests/test_mutation_gate_runner.py"
WINRT_TESTS = "tests/test_winrt_capture.py"
FACTORY_TESTS = "tests/test_audio_capture_factory.py"
PRIORITY_TESTS = "tests/test_thread_priority.py"

# The runner mutates itself for these three. That is safe because the gate
# already imported it: the mutant on disk is what the pytest subprocess
# reads, while the restore in the outer finally runs from the module held
# in memory.
RUNNER = SHARED / "tests" / "mutation_gate_runner.py"

THREAD_PRIORITY = SHARED / "shared_audio" / "thread_priority.py"
OVERFLOW = SHARED / "shared_audio" / "overflow_monitor.py"
DIAGNOSTICS = SHARED / "shared_audio" / "diagnostics.py"
WINRT = SHARED / "shared_audio" / "capture" / "winrt_capture.py"
#: The wording guards read prose, and one piece of that prose is a
#: docstring in the guard file itself, so the gate mutates it there.
WORDING_FILE = SHARED / "tests" / "test_capture_log_wording.py"

# The monitor's rate-limited summary line, as the three adjacent
# f-strings it is written on. Two mutations below rewrite the whole
# line, so they share this pattern. It replaced a pattern reading
# 'by PortAudio in the last {window}s', which the backend-supplied
# wording removed (wh-stt-overflow-config-and-wording, criterion 4);
# the behaviour both mutations attack is unchanged.
SUMMARY_LINE = (
    '                f"[overflow] {overflow_count} audio input '
    'overflows in the "\n'
    '                f"last {self.config.window_seconds:.0f}s, '
    'measured at "\n'
    '                f"{self.config.overflow_source}"'
)

MUTATIONS = [
    # -- thread_priority.py: the Win32 surface ---------------------------
    {
        # ctypes gives None for a NULL c_void_p, but a driver can also hand
        # back 0, and both are the documented failure. An "is not None"
        # check would report a handle of 0 as a live registration and then
        # pass 0 to the revert.
        "name": "mmcss-zero-handle-read-as-success",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "        if not handle:",
        "new": "        if handle is None:",
        "expect": ["test_zero_handle_is_also_a_failure"],
    },
    {
        # Narrowed rather than deleted: deleting the arm would leave the
        # try with no handler and not compile, which reads as a catch and
        # proves nothing.
        "name": "mmcss-registration-exception-escapes",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "    except Exception as e:  # never break audio because "
               "scheduling failed\n"
               "        return MmcssRegistration(task, None, str(e))",
        "new": "    except ZeroDivisionError as e:\n"
               "        return MmcssRegistration(task, None, str(e))",
        "expect": ["test_exception_is_reported_not_raised"],
    },
    {
        # AvSetMmThreadCharacteristicsW requires a zeroed task index on a
        # thread's first registration.
        "name": "mmcss-task-index-not-zeroed",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "        task_index = ctypes.c_uint32(0)",
        "new": "        task_index = ctypes.c_uint32(7)",
        "expect": ["test_task_index_starts_at_zero",
                   "test_caller_may_choose_the_task_name"],
    },
    {
        "name": "mmcss-task-argument-ignored",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "        handle = _avrt.AvSetMmThreadCharacteristicsW(\n"
               "            task, ctypes.byref(task_index))",
        "new": "        handle = _avrt.AvSetMmThreadCharacteristicsW(\n"
               "            AVRT_TASK_PRO_AUDIO, ctypes.byref(task_index))",
        "expect": ["test_caller_may_choose_the_task_name"],
    },
    {
        # A missing avrt.dll must read as a failed registration, not as a
        # live handle the revert would later hand back to nothing.
        "name": "mmcss-unavailable-reported-as-success",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": '        return MmcssRegistration(task, None, '
               '"avrt.dll unavailable")',
        "new": "        return MmcssRegistration(task, 1, None)",
        "expect": ["test_no_avrt_reports_unavailable"],
    },
    {
        "name": "mmcss-revert-refusal-reported-as-success",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "        return bool("
               "_avrt.AvRevertMmThreadCharacteristics(handle))",
        "new": "        _avrt.AvRevertMmThreadCharacteristics(handle)\n"
               "        return True",
        "expect": ["test_os_refusal_returns_false"],
    },
    {
        "name": "mmcss-revert-exception-escapes",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        "old": "    except Exception as e:\n"
               '        logger.warning("[priority] MMCSS revert failed: %s"'
               ", e)",
        "new": "    except ZeroDivisionError as e:\n"
               '        logger.warning("[priority] MMCSS revert failed: %s"'
               ", e)",
        "expect": ["test_exception_returns_false"],
    },
    {
        "name": "mmcss-revert-without-avrt-claims-success",
        "service": SHARED,
        "test_file": MMCSS_TESTS,
        "file": THREAD_PRIORITY,
        # Anchored on the following try: the bare "if _avrt is None:" also
        # opens the registration function above.
        "old": "    if _avrt is None:\n        return False\n    try:",
        "new": "    if _avrt is None:\n        return True\n    try:",
        "expect": ["test_no_avrt_returns_false"],
    },

    # -- microphone.py: the registration on the callback thread ----------

    # -- microphone.py: the threshold line -------------------------------

    # -- overflow_monitor.py: the summary --------------------------------
    {
        # The exact clause that was wrong: the consumer being behind is a
        # different measurement (`drops`), and it was 0 for all 122
        # utterances on 2026-09-03 while 13 carried overflows.
        "name": "overflow-summary-blames-the-consumer",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": SUMMARY_LINE,
        "new": SUMMARY_LINE + '\n'
               '                f" -- frames are being dropped '
               '(audio consumer behind real time)"',
        "expect": ["test_summary_does_not_blame_the_consumer"],
    },
    {
        # A count with no window is not a measurement.
        "name": "overflow-summary-drops-the-window",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": SUMMARY_LINE,
        "new": '                f"[overflow] {overflow_count} audio '
               'input overflows "\n'
               '                f"recently, measured at "\n'
               '                f"{self.config.overflow_source}"',
        "expect": ["test_summary_names_the_count_the_window_and_the_source"],
    },

    # -- overflow_monitor.py: the restart request ------------------------
    {
        "name": "overflow-restart-line-announces-a-restart",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '            f"[overflow] Overflow threshold crossed; '
               'restart requested "',
        "new": '            f"[overflow] TRIGGERING RESTART "',
        "expect": ["test_monitor_line_does_not_announce_a_restart",
                   "test_monitor_line_names_the_request_and_the_attempt"],
    },
    {
        # Without the attempt number a reader cannot tell the first
        # request from the last one before the cap.
        "name": "overflow-restart-line-drops-the-attempt",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '            f"[overflow] Overflow threshold crossed; '
               'restart requested "\n'
               '            f"(attempt {self.restart_attempts}/"\n'
               '            f"{self.config.max_restart_attempts})")',
        "new": '            f"[overflow] Overflow threshold crossed; '
               'restart requested")',
        "expect": ["test_monitor_line_names_the_request_and_the_attempt"],
    },

    # -- overflow_monitor.py: the cap warning ----------------------------
    {
        # The whole of criterion 5: before this line the only record of the
        # cap was a DEBUG line, so the operator could not tell "the
        # overflows stopped" from "the monitor gave up and capture carried
        # on losing frames".
        "name": "cap-warning-never-logged",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": "            if not self._cap_warning_logged:",
        "new": "            if False:",
        "expect": ["test_cap_logs_one_warning"],
    },
    {
        # Once the cap is reached every later overflow reaches this branch,
        # so an unlatched warning repeats for the life of the process.
        "name": "cap-warning-repeats-on-every-overflow",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": "                self._cap_warning_logged = True",
        "new": "                pass",
        "expect": ["test_warning_is_not_repeated_on_later_overflows"],
    },
    {
        # A DEBUG line is what the operator already had, and could not see.
        #
        # Pattern refreshed for wh-audio-callback-log, which routed every
        # logging call in this class through _emit so none of them runs on
        # the audio callback thread. The behaviour attacked is unchanged --
        # the level this line is written at -- so the mutation stays and
        # only the call shape it matches moved.
        "name": "cap-warning-demoted-to-debug",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": "                self._emit(\n"
               "                    logging.WARNING,\n"
               '                    f"[overflow] Restart attempt limit '
               'reached "',
        "new": "                self._emit(\n"
               "                    logging.DEBUG,\n"
               '                    f"[overflow] Restart attempt limit '
               'reached "',
        "expect": ["test_cap_logs_one_warning"],
    },
    {
        "name": "cap-warning-claims-a-restart-happened",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '                    f"restarts will be requested and the '
               'capture stream is "\n'
               '                    f"left open, so capture continues '
               'degraded"',
        "new": '                    f"restarts will be requested; '
               'Restarting the capture stream now"',
        "expect": ["test_warning_does_not_claim_a_restart_happened",
                   "test_cap_logs_one_warning"],
    },
    {
        # The warning belongs to the cap, not to every threshold crossing.
        "name": "cap-branch-taken-before-the-cap",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": "        if self.restart_attempts >= "
               "self.config.max_restart_attempts:",
        "new": "        if self.restart_attempts >= 0:",
        "expect": ["test_no_warning_before_the_cap_is_reached",
                   "test_monitor_line_names_the_request_and_the_attempt"],
    },

    # -- diagnostics.py: the stall line ----------------------------------
    {
        # The other clause that was wrong: whole-machine CPU was 20-37%
        # during the 09:13 stalls, and an independent capture from the same
        # microphone lost nothing over the same minutes.
        "name": "stall-line-guesses-cpu-starvation",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": DIAGNOSTICS,
        "old": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress for "\n'
               '                f"{gap:.1f}s{depth}")',
        "new": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress for "\n'
               '                f"{gap:.1f}s{depth} '
               '(likely whole-machine CPU starvation)")',
        "expect": ["test_message_does_not_guess_at_cpu_starvation"],
    },
    {
        "name": "stall-line-drops-the-gap",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": DIAGNOSTICS,
        "old": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress for "\n'
               '                f"{gap:.1f}s{depth}")',
        "new": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress"\n'
               '                f"{depth}")',
        "expect": ["test_message_names_the_label_and_the_gap"],
    },
    {
        # The queue depth is how a reader tells a starved consumer from a
        # starved callback: a stall with a full queue is the consumer.
        "name": "stall-line-drops-the-queue-depth",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": DIAGNOSTICS,
        "old": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress for "\n'
               '                f"{gap:.1f}s{depth}")',
        "new": '        return (f"{STALL_PREFIX}{self._label} made '
               'no progress for "\n'
               '                f"{gap:.1f}s")',
        "expect": ["test_message_keeps_the_queue_depth_suffix"],
    },
    # -- mutation_gate_runner.py: the two tracked-target writes ----------
    {
        # Path.write_bytes truncates and then writes, so an interrupt
        # between those two steps leaves half a mutant in a tracked source
        # file -- bytes _restore recognises as somebody else's save and
        # refuses to repair (wh-stt-load-metrics.3.2.2).
        "name": "runner-apply-write-is-not-atomic",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": "            _atomic_write_bytes(path, mutated)",
        "new": "            path.write_bytes(mutated)",
        "expect": ["test_a_partial_mutant_write_is_not_left_on_disk",
                   "test_the_mutant_does_not_survive_an_interrupt_at_the_"
                   "write"],
    },
    {
        # The restore write has the identical shape, and an interrupt there
        # is worse: the retry reads the half-written file, calls it a
        # concurrent edit, and leaves it.
        "name": "runner-restore-write-is-not-atomic",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": "            _atomic_write_bytes(path, original)",
        "new": "            path.write_bytes(original)",
        "expect": ["test_a_partial_restore_write_is_retried_to_completion"],
    },
    {
        # The protection the atomic write must not weaken: bytes this run
        # did not write belong to whoever saved them.
        "name": "runner-restore-overwrites-a-concurrent-edit",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": "            if current != mutated:",
        "new": "            if False:",
        "expect": ["test_an_editor_save_is_refused_not_overwritten"],
    },
    {
        # Two gate runs in one checkout mutating the same source file. A
        # shared temporary name lets each move the other's bytes over the
        # target under its own rename, and lets each delete the file the
        # other is about to rename.
        "name": "runner-temp-file-not-owned-by-its-writer",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('    tmp = path.with_name('
                'f"{path.name}.mutation-gate.{os.getpid()}.tmp")'),
        "new": '    tmp = path.with_name(path.name + ".mutation-gate-tmp")',
        "expect": ["test_two_runs_do_not_share_one_temporary_file",
                   "test_a_concurrent_run_cannot_capture_this_runs_rename"],
    },
    {
        # A Ctrl+C at the restore call's own door. The mutant is already
        # published at that moment, so skipping the call leaves it in a
        # tracked source file.
        "name": "runner-restore-call-not-guarded",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('                    restore_error, deferred = _call_cleanup(\n'
                '                        _restore, path, data, mutated, '
                'm["name"])'),
        "new": ('                    restore_error = _restore(\n'
                '                        path, data, mutated, m["name"])'),
        "expect": ["test_the_restore_runs_when_the_interrupt_lands_at_its_"
                   "door"],
    },
    {
        # The same door on the call that stops a mutant .pyc being read
        # beside a source file already put back.
        "name": "runner-cache-clear-call-not-guarded",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('                    _, cache_deferred = _call_cleanup('
                '_clear_pycache, service)'),
        "new": '                    _clear_pycache(service)',
        "expect": ["test_the_cache_clear_runs_when_the_interrupt_lands_at_"
                   "its_door"],
    },
    {
        # The guard's own rule: retry ONLY a call whose body never ran.
        # Retrying one that finished repeats work these helpers must do
        # once, which is why the traceback is read rather than assumed.
        # Caught in the load-test suite, where the parametrised guarding
        # fixture runs one set of tests against all three copies.
        "name": "runner-cleanup-guard-retries-a-finished-call",
        "service": SHARED,
        "test_file": "tests/test_stt_load_test.py",
        "file": RUNNER,
        "old": ('                if (step.tb_frame.f_code is code\n'
                '                        and step.tb_lineno != '
                'code.co_firstlineno):'),
        "new": "                if False:",
        "expect": ["test_a_call_that_ran_and_raised_is_not_made_again"],
    },

    # -- winrt_capture.py: the same registration on the path the provider
    # actually runs once winsdk is installed --------------------------------
    {
        # The mutual exclusion. Windows hands an MMCSS thread's scheduling
        # to MMCSS, whose own control is AvSetMmThreadPriority, so calling
        # SetThreadPriority as well is a second, different request against
        # a thread MMCSS already manages -- not a stronger one.
        "name": "winrt-success-also-elevates",
        "service": SHARED,
        "test_file": WINRT_TESTS,
        "file": WINRT,
        "old": ('                    f"{mmcss.task!r} (handle '
                '{mmcss.handle:#x})")\n'
                '            else:'),
        "new": ('                    f"{mmcss.task!r} (handle '
                '{mmcss.handle:#x})")\n'
                '            if True:'),
        "expect": ["test_a_successful_registration_does_not_also_elevate"],
    },
    {
        # The failure MMCSS actually returns is a NULL handle, not an
        # exception. A fallback that skipped it would leave this thread
        # with no scheduling protection at all, and nothing would say so.
        "name": "winrt-null-handle-skips-the-fallback",
        "service": SHARED,
        "test_file": WINRT_TESTS,
        "file": WINRT,
        "old": ("                elevated = elevate_current_thread("
                "'time_critical')"),
        "new": "                elevated = False",
        "expect": ["test_a_null_handle_falls_back_to_thread_priority",
                   "test_a_registration_that_raises_falls_back_too"],
    },
    {
        # Only the registering thread may release its own registration, so
        # a loop that exits still holding one leaves the service holding a
        # handle nothing can release.
        "name": "winrt-registration-never-released",
        "service": SHARED,
        "test_file": WINRT_TESTS,
        "file": WINRT,
        "old": ("            if mmcss is not None and mmcss.handle is not "
                "None:\n"
                "                revert_current_thread_mmcss(mmcss.handle)"),
        "new": ("            if False:\n"
                "                revert_current_thread_mmcss(mmcss.handle)"),
        "expect": ["test_the_registration_is_released_when_the_loop_ends",
                   "test_the_registration_is_released_even_when_setup_"
                   "fails"],
    },
    {
        # The check that decides which capture path the provider gets. Every
        # other test in the factory file sets WINRT_AUDIO_AVAILABLE by hand,
        # so all of them keep passing when this check stops working.
        "name": "winrt-availability-always-false",
        "service": SHARED,
        "test_file": FACTORY_TESTS,
        "file": WINRT,
        "old": ("        from winsdk.windows.media.audio import AudioGraph"
                "  # noqa: F401\n"
                "        return True"),
        "new": ("        from winsdk.windows.media.audio import AudioGraph"
                "  # noqa: F401\n"
                "        return False"),
        "expect": ["test_an_installed_winsdk_makes_winrt_available"],
    },
    {
        # The opposite answer is the defect wh-capture-winrt-required
        # exists to stop: a machine without the winsdk wheel is told WinRT
        # is available, so the factory builds a WinRT capture that cannot
        # make a graph, instead of refusing to start and saying why.
        #
        # The second expected test was
        # test_the_factory_takes_sounddevice_when_winsdk_is_missing until
        # 2026-09-06. wh-capture-winrt-required deleted the fallback the
        # factory used to take, and that test with it, which left this
        # mutation naming a test that no longer collects -- the runner then
        # refused to start the whole gate. Its replacement,
        # test_a_missing_winsdk_makes_the_factory_refuse, is the same
        # end-to-end case under the rule that replaced the fallback: with
        # winsdk absent the factory must raise, so an availability check
        # that answers True makes it build a capture and the test fails.
        "name": "winrt-availability-always-true",
        "service": SHARED,
        "test_file": FACTORY_TESTS,
        "file": WINRT,
        "old": "    except ImportError:\n        return False",
        "new": "    except ImportError:\n        return True",
        "expect": ["test_a_failed_winsdk_import_makes_winrt_unavailable",
                   "test_a_missing_winsdk_makes_the_factory_refuse"],
    },

    # -- the process class printed beside the thread priority ---------------
    {
        # GetPriorityClass answers 0 on failure. Passed through, 0 reaches
        # the line as an unknown class and reads as a real reading.
        "name": "process-class-zero-reported-as-a-class",
        "service": SHARED,
        "test_file": PRIORITY_TESTS,
        "file": THREAD_PRIORITY,
        "old": "        return value or None",
        "new": "        return value",
        "expect": ["test_a_zero_return_is_reported_as_unavailable"],
    },
    {
        # The provider configures logging at INFO, so a DEBUG line is
        # discarded before it is written and no operator can tell from a log
        # whether the WinRT capture thread got its registration. Every other
        # WinRT test reads the mock calls rather than the log, so all of them
        # keep passing at either level. The mutation changes only the level:
        # anything that also skipped the call would fail those other tests
        # for an unrelated reason and earn a verdict it did not prove.
        "name": "winrt-mmcss-line-invisible-at-info",
        "service": SHARED,
        "test_file": WINRT_TESTS,
        "file": WINRT,
        "old": ("                logger.info(\n"
                "                    f\"Capture thread registered with MMCSS "
                "task \""),
        "new": ("                logger.debug(\n"
                "                    f\"Capture thread registered with MMCSS "
                "task \""),
        "expect": ["test_the_registration_line_prints_at_the_provider_"
                   "log_level"],
    },

    # -- the callback writes no log record at all -------------------------
    #
    # wh-audio-callback-log. These attack the sink parameter that keeps a
    # log write off a real-time thread: that the monitor honours a sink
    # when it is given one, and that the elevation fallback does the same.
    # The caller that passed a sink was MicrophoneStream, deleted by
    # wh-portaudio-capture-removal; both parameters are kept behind a
    # crewcut comment for the next real-time caller, and
    # tests/test_capture_log_sink.py is what still catches these four.
    {
        "name": "callback-log-monitor-ignores-its-sink",
        "service": SHARED,
        "test_file": CALLBACK_LOG_TESTS,
        "file": OVERFLOW,
        "old": "        if self._log_sink is None:",
        "new": "        if True:",
        "expect": [
            "test_a_burst_past_the_threshold_creates_no_log_record",
            "test_the_lines_are_handed_to_the_sink_instead_of_dropped",
            "test_the_sink_keeps_the_level_of_each_line",
        ],
    },
    {
        # The break that a handler on the capture tree's ROOT cannot see.
        # google_stt_server forwards from "shared_audio.overflow_monitor"
        # alone, so a summary re-emitted under the draining module's name
        # keeps reaching Parakeet and disappears from wheelhouse.log on
        # google. Only the test that watches google's own attachment point
        # fails here, which is exactly the point of asserting at both.
        "name": "callback-log-sink-given-the-wrong-logger",
        "service": SHARED,
        "test_file": CALLBACK_LOG_TESTS,
        "file": OVERFLOW,
        "old": "            self._log_sink(logger, level, message)",
        "new": ("            self._log_sink("
                "logging.getLogger(\"shared_audio.somewhere_else\"), "
                "level, message)"),
        "expect": ["test_the_sink_is_given_the_overflow_monitors_own_logger"],
    },

    # -- thread_priority.py: the last logging call the callback could
    # reach, and the parameter that moves it off that thread ------------
    {
        # The sink honoured at the call site but ignored inside the
        # helper. The caller looks right and the thread is still wrong.
        "name": "elevation-warning-ignores-the-sink",
        "service": SHARED,
        "test_file": CALLBACK_LOG_TESTS,
        "file": THREAD_PRIORITY,
        "old": ("        if log_sink is None:\n"
                "            logger.warning(message)"),
        "new": ("        if True:\n"
                "            logger.warning(message)"),
        "expect": [
            "test_a_refused_elevation_with_a_sink_creates_no_log_record",
            "test_a_raising_elevation_with_a_sink_creates_no_log_record"],
    },
    {
        # Deferred, not discarded. A guard that only counted records on
        # the callback thread would also pass if the diagnostic were
        # dropped, so the drain has to be seen to receive it.
        "name": "elevation-warning-dropped-instead-of-deferred",
        "service": SHARED,
        "test_file": CALLBACK_LOG_TESTS,
        "file": THREAD_PRIORITY,
        "old": ("        else:\n"
                "            log_sink(logger, logging.WARNING, message)"),
        "new": "        else:\n            pass",
        "expect": [
            "test_the_refusal_reaches_the_sink",
            "test_the_raised_error_reaches_the_sink"],
    },
    {
        # The other half of the same parameter. Every caller on an
        # ordinary thread passes no sink and still needs the line where
        # the failure happened; a sink-only helper would lose it for the
        # consumer loop and the WinRT poll loop both.
        "name": "elevation-warning-lost-without-a-sink",
        "service": SHARED,
        "test_file": PRIORITY_TESTS,
        "file": THREAD_PRIORITY,
        "old": ("        if log_sink is None:\n"
                "            logger.warning(message)"),
        "new": "        if log_sink is None:\n            pass",
        "expect": [
            "test_the_default_writes_the_refusal_here_and_now",
            "test_the_default_writes_the_raised_error_here_and_now"],
    },

    # -- overflow_monitor.py: the prose beside the corrected log lines.
    # A documentation-only change cannot fail a log assertion, which is
    # how these three drifted back in the first place -----------------
    {
        "name": "overflow-docstring-promises-a-restart",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": ("            True if a restart was requested, False "
                "otherwise. A request"),
        "new": ("            True if restart should be triggered, False "
                "otherwise. A request"),
        "expect": ["test_report_overflow_does_not_promise_a_restart"],
    },
    {
        "name": "should-restart-docstring-says-triggered",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": ('        """Decide whether this overflow should ask for a '
                'restart.'),
        "new": '        """Determine if restart should be triggered.',
        "expect": ["test_should_restart_describes_a_decision_to_ask"],
    },
    {
        "name": "success-path-comment-says-trigger",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": "        # All checks passed - request a restart",
        "new": "        # All checks passed - trigger restart",
        "expect": ["test_the_success_path_comment_says_request"],
    },
    {
        # The cooldown and the cap bound REQUESTS. Calling them restart
        # attempts says the monitor retries something it never performs.
        "name": "config-cooldown-comment-says-attempts",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": ("    # Minimum time between restart requests (prevents "
                "request loops)"),
        "new": "    # Minimum time between restart attempts (prevents loops)",
        "expect": ["test_the_config_fields_are_described_as_request_limits"],
    },
    {
        "name": "config-class-docstring-drops-request",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": ('    """Configuration for overflow detection and '
                'restart-request behavior."""'),
        "new": ('    """Configuration for overflow detection and restart '
                'behavior."""'),
        "expect": ["test_the_config_fields_are_described_as_request_limits"],
    },
    {
        # The guard file's own prose contradicted its opening text, which
        # is how this one survived the round that fixed the others.
        "name": "cap-warning-docstring-says-restarts",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": WORDING_FILE,
        "old": ('    """Exactly one WARNING when the attempt cap stops '
                'further requests.'),
        "new": ('    """Exactly one WARNING when the attempt cap stops '
                'further restarts.'),
        "expect": ["test_this_module_does_not_call_the_cap_a_restart_stop"],
    },
]


if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS))
