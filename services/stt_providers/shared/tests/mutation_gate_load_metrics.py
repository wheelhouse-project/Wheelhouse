"""Mutation gate for the wh-stt-load-metrics guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared with plain python
(the gate itself needs only the standard library; it invokes `uv run pytest`
per service as a subprocess):

    python tests/mutation_gate_load_metrics.py

The measurement this protects has one failure mode worse than being wrong: it
can be silently absent, or silently zero, and still print a line that looks
like an answer. Most mutations below attack exactly that -- an unattached log
handler, an unwired capture reader, an absent counter reported as 0, a
lifetime total reported as a window delta.

Three services are mutated, so each mutation names its own service and test
file: main.py in the Parakeet provider, and main.py in the Google provider,
can each only be exercised by that provider's own suite, which imports
`main`.

Discipline, per the mutation-gate skill: byte IO, patterns translated to each
file's own line endings, exactly-one-match required, compile check, expected
test names validated against real collection before the first mutation,
baseline required green, per-run timeout, suite-abort banner detected,
__pycache__ cleared with PYTHONDONTWRITEBYTECODE set, restore through
write_bytes in a finally that refuses to overwrite a concurrent edit.
Pattern, compile, timeout and abort problems are reported as ERRORS, never as
verdicts.

crewcut: this repeats the runner machinery that
tests/mutation_gate_process_priority.py already carries, which is the house
convention of one self-contained gate per bead. That gate additionally
journals its mutants for power-loss recovery; this one does not, because its
mutants are restored in a finally and the worktree has a single writer. To
remove the duplication: lift the runner half of either file into a
tests/mutation_gate_runner.py taking the per-mutation service and test file
this gate already uses, and have both import it.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent
PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server"
GOOGLE = PROVIDERS / "google_stt_server"

SHARED_TESTS = "tests/test_capture_load_metrics.py"
UTTERANCE_LINE_TESTS = "tests/test_utterance_load_line.py"
SEGMENT_TESTS = "tests/test_iteration_segments.py"
# The providers' readiness contract lives in this one file. It once
# held the PortAudio half as well, which is why the constant is
# named for the file rather than for WinRT.
CAPTURE_TESTS = "tests/test_winrt_capture.py"
FORWARDING_TESTS = "tests/test_capture_log_forwarding.py"
PARAKEET_TESTS = "tests/test_load_diagnostics_wiring.py"
GOOGLE_WIRING_TESTS = "tests/test_utterance_load_line_wiring.py"
# wh-forwarded-log-time-order: the queue-side keep/drop split and the head
# pending slot, and the reachability gate the plain handler inherited.
SURVIVAL_TESTS = "tests/test_log_frame_survival.py"
PLAIN_HANDLER_TESTS = "tests/test_plain_handler_liveness.py"

AUDIO_PROCESSOR = SHARED / "shared_stt" / "audio_processor.py"
DIAGNOSTICS = SHARED / "shared_audio" / "diagnostics.py"
WS_FORWARDER = SHARED / "shared_stt" / "ws_forwarder.py"
WINRT = SHARED / "shared_audio" / "capture" / "winrt_capture.py"
PARAKEET_MAIN = PARAKEET / "main.py"
GOOGLE_MAIN = GOOGLE / "main.py"
PROCEDURE_DOC = (SHARED.parents[2] / "docs" / "testing"
                 / "stt-cpu-load-test-procedure.md")

MUTATIONS = [
    # -- The line has to reach a log at all -----------------------------
    {
        # The mutation the boss required: drop the attachment that makes the
        # per-utterance line visible. Everything else still works, and the
        # measurement produces nothing.
        "name": "load-metrics-handler-not-attached",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "        logging.getLogger(LOAD_METRICS_LOGGER_NAME)"
               ".addHandler(ws_log_handler)",
        "new": "        pass",
        "expect": ["test_the_handler_is_attached_to_the_load_metrics_logger",
                   "test_a_load_diag_record_reaches_the_forwarder"],
    },
    {
        # The rejected option (a): forward the whole package. The two tests
        # above still pass; only the narrowness test catches this.
        "name": "handler-attached-to-the-whole-shared-stt-package",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "        logging.getLogger(LOAD_METRICS_LOGGER_NAME)"
               ".addHandler(ws_log_handler)",
        "new": '        logging.getLogger("shared_stt")'
               ".addHandler(ws_log_handler)",
        "expect": ["test_the_ordinary_processor_logger_stays_silent"],
    },
    {
        "name": "load-line-logged-on-the-silenced-module-logger",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            load_metrics_logger.info(",
        "new": "            logger.info(",
        "expect": ["test_the_line_is_logged_on_that_logger"],
    },

    # -- The capture numbers have to be this server's real numbers -------
    {
        "name": "processor-gets-no-capture-reader",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        # Anchored on the comment line above it. The round-9 periodic
        # reporter is built with a byte-identical keyword line a few
        # lines below, so the bare line matches twice and would mutate
        # that one instead (wh-stt-load-metrics.1.12 refresh).
        "old": "            # line still prints, with \"n/a\" for "
               "every capture number.\n"
               "            capture_stats=self.audio_capture.get_stats,",
        "new": "            # line still prints, with \"n/a\" for "
               "every capture number.\n"
               "            capture_stats=None,",
        "expect": ["test_the_processor_is_given_a_capture_stats_reader",
                   "test_the_reader_returns_this_servers_own_capture_stats"],
    },
    {
        # An absent counter reported as 0 is the reading that falsely rules
        # capture loss out, which is the whole decision this bead exists to
        # make.
        "name": "absent-counter-reports-zero-per-utterance",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Mutate the VALUE the guard returns, not the guard itself.
        # Disabling the guard let the next line, at_open[key], raise
        # KeyError; AudioProcessor swallowed it, no [load-diag] line
        # was written at all, and the catcher died in its _load_line
        # helper without ever reaching its own n/a assertion. That is
        # a catch nothing earned. Reporting 0 through the guard is
        # the wrong reading this entry is named for
        # (wh-portaudio-capture-removal.2.2).
        "old": "        if key not in at_open or key not in at_end:\n"
               "            deltas[key] = NOT_REPORTED",
        "new": "        if key not in at_open or key not in at_end:\n"
               "            deltas[key] = 0",
        "expect": [
            "test_the_utterance_line_says_n_a_for_a_key_the_provider_omits"],
    },
    {
        "name": "absent-counter-reports-zero-in-the-window-line",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Same repair as the per-utterance entry above, same reason:
        # disabling the guard left before[key] to raise KeyError at
        # diagnostics.py:1096, so the catcher never reached its own
        # assertion and the gate scored an unearned catch. The
        # pattern carries the comment line above the return because
        # "return 'n/a'" appears twice in this function
        # (wh-portaudio-capture-removal.2.2).
        "old": "                # \"measured, none happened\", which "
               "is a different fact.\n"
               "                return 'n/a'",
        "new": "                # \"measured, none happened\", which "
               "is a different fact.\n"
               "                return 0",
        "expect": [
            "test_the_window_summary_says_n_a_for_a_key_the_provider_omits"],
    },
    {
        # Lifetime totals instead of the utterance's own difference: every
        # utterance after the first then looks worse than it was.
        "name": "utterance-counters-report-lifetime-totals",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        deltas[key] = max(0, after - before)",
        "new": "        deltas[key] = after",
        "expect": ["test_overflow_is_the_delta_across_the_utterance",
                   "test_a_second_utterance_starts_its_counters_again"],
    },
    {
        "name": "window-counters-report-lifetime-totals",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "            return max(0, b - a)",
        "new": "            return b",
        "expect": [
            "test_capture_counters_are_window_deltas_not_lifetime_totals"],
    },

    # -- Window bookkeeping ---------------------------------------------
    {
        # Inheriting the closing reading makes one early spike permanent.
        "name": "window-high-water-mark-carries-over",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Round 10 inserted the readiness carry-over between these
        # two lines, so the pattern had to grow to keep describing
        # the code (wh-stt-load-metrics.1.13 refresh).
        # Round 12 inserted the frame-gap reset below these two lines,
        # so the pattern had to grow again (wh-stt-load-metrics.1.16
        # refresh).
        "old": "        self._q_max = 0\n"
               "        self._ready_all_window = ready_now\n"
               "        self._max_frame_gap_s = 0.0",
        "new": "        self._q_max = self._q_now\n"
               "        self._ready_all_window = ready_now\n"
               "        self._max_frame_gap_s = 0.0",
        "expect": ["test_the_high_water_mark_starts_again_each_window"],
    },
    {
        "name": "queue-depth-reports-the-last-sample-not-the-peak",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "                if depth > self._q_max:\n"
               "                    self._q_max = depth",
        "new": "                self._q_max = depth",
        "expect": ["test_queue_depth_is_the_high_water_mark_of_the_window"],
    },

    # -- The loop has to drive the reporter every iteration --------------
    {
        "name": "loop-never-records-an-iteration",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                if self._load_reporter is not None:",
        "new": "                if False:",
        "expect": ["test_the_loop_records_an_iteration",
                   "test_the_loop_logs_the_returned_lines",
                   "test_an_iteration_is_recorded_even_when_no_audio_arrives"],
    },
    {
        # A key mismatch between main.py and config.toml leaves the flag
        # permanently false: the periodic line never appears and a load test
        # measures nothing while reporting no error.
        "name": "debug-flag-key-misspelled",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": '    return bool(config.get("debug", {})'
               '.get("log_load_diagnostics", False))',
        "new": '    return bool(config.get("debug", {})'
               '.get("log_load_diagnostic", False))',
        "expect": ["test_the_flag_is_read_from_the_debug_section",
                   "test_the_tracked_config_declares_the_key_main_reads"],
    },
    {
        "name": "debug-flag-read-from-the-wrong-section",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": '    return bool(config.get("debug", {})'
               '.get("log_load_diagnostics", False))',
        "new": '    return bool(config.get("engine", {})'
               '.get("log_load_diagnostics", False))',
        "expect": ["test_the_flag_is_read_from_the_debug_section",
                   "test_the_tracked_config_declares_the_key_main_reads"],
    },
    {
        "name": "flag-helper-always-on",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": '    return bool(config.get("debug", {})'
               '.get("log_load_diagnostics", False))',
        "new": "    return True",
        "expect": ["test_the_flag_defaults_to_false",
                   "test_a_debug_section_without_the_key_is_false"],
    },
    {
        "name": "reporter-never-created",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "            if log_load_diagnostics else None",
        "new": "            if False else None",
        "expect": ["test_the_flag_creates_a_reporter"],
    },

    # -- The counters in the callback ------------------------------------

    # -- Engine timing ----------------------------------------------------
    {
        # A chunk count times a nominal duration is not the audio the engine
        # was given, and engine_ratio is only meaningful against the latter.
        "name": "audio-ms-counts-chunks-not-samples",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._load_engine_audio_samples += len(pcm_bytes) // 2",
        "new": "        self._load_engine_audio_samples += 480",
        "expect": ["test_audio_ms_follows_the_bytes_fed_to_the_engine"],
    },
    {
        "name": "engine-time-not-accumulated",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._load_engine_calls += 1\n"
               "        self._load_engine_s_total += _load_elapsed",
        "new": "        self._load_engine_calls += 1\n"
               "        self._load_engine_s_total = _load_elapsed",
        "expect": ["test_engine_time_is_measured_around_each_engine_call"],
    },
    {
        # The forced endpoint's finalize() is a full inference over the whole
        # utterance. Untimed, engine_ratio reads near zero on a kind=force
        # line, which the procedure document maps to no inference lag.
        "name": "forced-endpoint-inference-not-timed",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            _load_t0 = time.perf_counter()\n"
               "            finalize()\n"
               "            _load_elapsed = time.perf_counter() - _load_t0\n"
               "            self._load_engine_calls += 1\n"
               "            self._load_engine_s_total += _load_elapsed\n"
               "            if _load_elapsed > self._load_engine_s_max:\n"
               "                self._load_engine_s_max = _load_elapsed",
        "new": "            finalize()",
        "expect": [
            "test_engine_time_includes_the_final_inference",
            "test_the_final_inference_can_be_the_longest_one",
            "test_the_final_inference_is_counted_as_an_engine_call",
            "test_the_ratio_crosses_one_when_the_final_inference_is_slow",
        ],
    },

    # -- The counters the deltas are built from ---------------------------
    {
        # The baseline read at gate-open time already includes every frame
        # the callback lost while the lead-in was being captured, so the
        # subtraction erases the loss at the onset of speech.
        "name": "capture-baseline-taken-at-gate-open",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "                baseline = self._capture_baseline()",
        "new": "                baseline = self._read_capture_stats()",
        "expect": [
            "test_an_overflow_during_the_lead_in_reaches_the_line",
            "test_a_status_flag_during_the_lead_in_reaches_the_line",
            "test_a_queue_full_drop_during_the_lead_in_reaches_the_line",
        ],
    },
    {
        # A history that survives the endpoint makes the next utterance
        # inherit this one's baseline and report the same loss twice.
        "name": "capture-history-survives-the-endpoint",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        # report the same loss a second time.
        self._load_capture_history.clear()""",
        "new": """        # report the same loss a second time.
        pass""",
        "expect": ["test_the_previous_utterance_loss_is_not_counted_again"],
    },
    {
        # Keeping only the newest snapshot is the same defect as reading at
        # gate-open time, one chunk earlier.
        "name": "capture-history-keeps-only-the-newest",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        return self._load_capture_history[0][1]",
        "new": "        return self._load_capture_history[-1][1]",
        "expect": [
            "test_an_overflow_during_the_lead_in_reaches_the_line",
            "test_a_status_flag_during_the_lead_in_reaches_the_line",
            "test_a_queue_full_drop_during_the_lead_in_reaches_the_line",
        ],
    },
    {
        # wh-stt-load-metrics.1.5. The defect itself: trimming the history by
        # the consumer thread's clock instead of by the audio the lead-in
        # buffer keeps. A drained backlog stamps every chunk of the burst
        # with the same reading, so nothing is trimmed and the baseline
        # reaches back past audio that buffer already threw away.
        "name": "capture-history-trimmed-by-consumer-clock",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        # Re-anchored for wh-stt-load-metrics.1.8: the seal check now sits
        # between the append and the trim, so the old contiguous pattern no
        # longer matches. The seal block is carried through unchanged --
        # this mutation is about the trim rule alone.
        "old": """        self._load_capture_history.append((len(pcm_bytes), stats))
        self._load_capture_history_bytes += len(pcm_bytes)
        if (self._load_capture_history_bytes
                > self._lead_in_capacity_bytes):
            # LeadInBuffer.add evicts while its total EXCEEDS the same
            # capacity, so the audio sampled here outgrowing it means that
            # buffer has already dropped its oldest chunk, and the entry at
            # the front of this deque was read before every chunk the buffer
            # still holds. That is a genuine baseline even when no seed
            # reading was available (wh-stt-load-metrics.1.6).
            #
            # The trim below cannot carry this: it keeps one entry MORE than
            # the lead-in holds, so it first pops a full chunk later than the
            # buffer's own first eviction, and the line printed n/a for that
            # extra chunk while the numbers were already there
            # (wh-stt-load-metrics.1.8). Sealing any earlier is equally wrong
            # -- a lead-in filled EXACTLY evicts nothing, so its first chunk
            # is still retained and no entry precedes it.
            self._load_capture_baseline_sealed = True
        while (len(self._load_capture_history) > 1
               and (self._load_capture_history_bytes
                    - self._load_capture_history[0][0])
               > self._lead_in_capacity_bytes):
            self._load_capture_history_bytes -= (
                self._load_capture_history.popleft()[0])""",
        "new": """        now = time.monotonic()
        self._load_capture_history.append((now, stats))
        cutoff = now - self._lead_in_capacity_bytes / (self.sample_rate * 2)
        if (self._load_capture_history_bytes
                > self._lead_in_capacity_bytes):
            # LeadInBuffer.add evicts while its total EXCEEDS the same
            # capacity, so the audio sampled here outgrowing it means that
            # buffer has already dropped its oldest chunk, and the entry at
            # the front of this deque was read before every chunk the buffer
            # still holds. That is a genuine baseline even when no seed
            # reading was available (wh-stt-load-metrics.1.6).
            #
            # The trim below cannot carry this: it keeps one entry MORE than
            # the lead-in holds, so it first pops a full chunk later than the
            # buffer's own first eviction, and the line printed n/a for that
            # extra chunk while the numbers were already there
            # (wh-stt-load-metrics.1.8). Sealing any earlier is equally wrong
            # -- a lead-in filled EXACTLY evicts nothing, so its first chunk
            # is still retained and no entry precedes it.
            self._load_capture_baseline_sealed = True
        while (len(self._load_capture_history) > 1
               and self._load_capture_history[1][0] <= cutoff):
            self._load_capture_history.popleft()""",
        "expect": [
            "test_loss_before_the_retained_lead_in_is_not_charged",
            "test_the_history_keeps_the_lead_in_buffers_chunks_and_one_more",
            "test_the_history_cannot_grow_past_the_lead_in_it_covers",
        ],
    },
    {
        # A fixed entry cap cannot cover a lead-in of more chunks than the
        # cap, so loss from the early part of that lead-in stays in the
        # baseline and never reaches the line.
        "name": "capture-history-capped-at-a-fixed-count",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._load_capture_history: deque = deque()",
        "new": "        self._load_capture_history: deque = deque(maxlen=128)",
        "expect": ["test_a_long_lead_in_of_small_chunks_is_covered_end_to_end"],
    },
    {
        # Counting chunks instead of audio bytes understates the window, so
        # the trim never fires and the baseline runs away backwards.
        "name": "capture-history-measures-chunks-not-audio",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._load_capture_history_bytes += len(pcm_bytes)",
        "new": "        self._load_capture_history_bytes += 1",
        "expect": [
            "test_loss_before_the_retained_lead_in_is_not_charged",
            "test_the_history_keeps_the_lead_in_buffers_chunks_and_one_more",
            "test_the_history_cannot_grow_past_the_lead_in_it_covers",
        ],
    },
    {
        # Trimming to exactly the lead-in drops the one older entry the
        # baseline is taken from, so loss during the oldest retained chunk
        # sits in the baseline and subtracts to zero.
        "name": "capture-history-drops-the-baseline-entry",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """               and (self._load_capture_history_bytes
                    - self._load_capture_history[0][0])
               > self._lead_in_capacity_bytes):""",
        "new": """               and self._load_capture_history_bytes
               > self._lead_in_capacity_bytes):""",
        "expect": ["test_loss_in_the_oldest_retained_chunk_is_charged"],
    },
    {
        # Clearing the deque without clearing the audio total leaves the
        # next utterance's window collapsed to its newest snapshot.
        "name": "capture-history-bytes-survive-the-endpoint",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        self._load_capture_history.clear()
        self._load_capture_history_bytes = 0
        # wh-stt-load-metrics.1.6: the closing read for this utterance has""",
        "new": """        self._load_capture_history.clear()
        pass
        # wh-stt-load-metrics.1.6: the closing read for this utterance has""",
        "expect": ["test_a_second_utterance_charges_its_own_lead_in_loss"],
    },
    {
        # wh-stt-load-metrics.1.6. Without the construction reading, the
        # first utterance's oldest history entry is its own first chunk,
        # which already contains whatever that chunk lost.
        "name": "capture-history-not-seeded-at-construction",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        # wh-stt-load-metrics.1.6: True once the oldest kept entry is known
        # to predate every chunk the lead-in buffer still holds. Until then
        # there is no baseline to subtract and the line says n/a.
        self._load_capture_baseline_sealed = False
        self._seed_capture_history()""",
        "new": """        # wh-stt-load-metrics.1.6: True once the oldest kept entry is known
        # to predate every chunk the lead-in buffer still holds. Until then
        # there is no baseline to subtract and the line says n/a.
        self._load_capture_baseline_sealed = False""",
        "expect": [
            "test_one_silent_chunk_that_lost_a_frame_reaches_the_line",
            "test_a_lead_in_that_never_fills_still_charges_its_first_chunk",
            "test_a_lead_in_filled_exactly_still_charges_its_first_chunk",
        ],
    },
    {
        # The same defect for every utterance after the first: without a
        # reading taken when the previous one ended, a short lead-in has no
        # entry preceding its own opening audio.
        "name": "capture-history-not-seeded-after-the-endpoint",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        self._load_capture_baseline_sealed = False
        self._seed_capture_history()
        self.current_utterance_id += 1""",
        "new": """        self._load_capture_baseline_sealed = False
        self.current_utterance_id += 1""",
        "expect": ["test_the_second_utterance_gets_its_own_preceding_snapshot"],
    },
    {
        # Guessing with the post-chunk snapshot instead of saying n/a is the
        # false all-clear this whole measurement exists to avoid.
        "name": "capture-baseline-guesses-when-no-seed-exists",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        if (not self._load_capture_baseline_sealed
                or not self._load_capture_history):
            return None""",
        "new": """        if not self._load_capture_history:
            return None""",
        "expect": ["test_no_preceding_snapshot_reports_unknown_not_zero"],
    },
    {
        # A reader that was unavailable at construction has to recover once
        # the lead-in buffer evicts, or the line says n/a for ever.
        # Re-anchored for wh-stt-load-metrics.1.8: the seal moved out of the
        # trim loop, so the old anchor (the assignment followed by the
        # _seed_capture_history definition) no longer exists.
        "name": "capture-baseline-never-sealed",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """            self._load_capture_baseline_sealed = True
        while (len(self._load_capture_history) > 1""",
        "new": """            pass
        while (len(self._load_capture_history) > 1""",
        "expect": [
            "test_a_late_reader_recovers_once_the_buffer_evicts",
            "test_the_seal_lands_on_the_lead_in_buffers_own_first_eviction",
        ],
    },
    {
        # wh-stt-load-metrics.1.8, the defect itself. Sealing on the trim's
        # stricter rule keeps one entry MORE than the lead-in holds, so the
        # seal arrives a full chunk after the buffer's own first eviction and
        # the line says n/a while the delta is already available.
        "name": "capture-baseline-sealed-a-chunk-late",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        if (self._load_capture_history_bytes
                > self._lead_in_capacity_bytes):""",
        "new": """        if (self._load_capture_history_bytes
                - self._load_capture_history[0][0]
                > self._lead_in_capacity_bytes):""",
        "expect": [
            "test_the_seal_lands_on_the_lead_in_buffers_own_first_eviction",
        ],
    },
    {
        # The other side of the same boundary. LeadInBuffer.add evicts only
        # when its total EXCEEDS capacity, so a lead-in filled exactly still
        # retains its first chunk and no entry precedes it. Sealing on >=
        # hands back a baseline that already contains that chunk's loss --
        # the wh-stt-load-metrics.1.6 defect, one chunk wide.
        "name": "capture-baseline-sealed-a-chunk-early",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        if (self._load_capture_history_bytes
                > self._lead_in_capacity_bytes):""",
        "new": """        if (self._load_capture_history_bytes
                >= self._lead_in_capacity_bytes):""",
        "expect": [
            "test_one_chunk_before_that_eviction_still_reports_unknown",
        ],
    },
    {
        # wh-stt-load-metrics.1.9. Without a baseline of its own the first
        # utterance keeps the construction reading, taken from a capture
        # provider whose stream did not exist yet, so it withholds the two
        # counters that live on the stream object.
        "name": "capture-baseline-never-seeded-before-capture-starts",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "        self.audio_processor."
               "seed_capture_baseline_before_capture_starts()",
        "new": "        pass",
        "expect": [
            "test_start_seeds_the_capture_baseline",
            "test_the_baseline_is_taken_before_the_capture_provider_starts",
        ],
    },
    {
        # wh-stt-load-metrics.1.10, the defect itself. Order is the whole of
        # the fix: taking the baseline after start() returns lets the callback
        # fill the queue first, and nothing discards that queue, so the first
        # utterance is given audio whose loss is already inside its baseline.
        "name": "capture-baseline-seeded-after-the-microphone-opens",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        # Re-anchored 2026-09-05. The pattern still carried
        # logger.info("Audio capture started") after the start() call; that
        # line moved inside the startup announcement under
        # wh-provider-ready-handshake criterion 3, so the pattern stopped
        # matching and the sweep reported it as an error. The behaviour it
        # protects is unchanged: the baseline must be taken before the
        # microphone opens.
        "old": """        self.audio_processor.seed_capture_baseline_before_capture_starts()

        self.audio_capture.start()""",
        "new": """        self.audio_capture.start()

        self.audio_processor.seed_capture_baseline_before_capture_starts()""",
        "expect": [
            "test_the_baseline_is_taken_before_the_capture_provider_starts",
        ],
    },
    {
        # The zero baseline must cover the counters the pre-stream reader
        # cannot answer. Taking a reading instead of stating the zero puts
        # the four-key shape back and returns the .1.9 defect.
        "name": "capture-zero-baseline-replaced-by-a-reading",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        self._load_capture_history.append(
            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))
        self._load_capture_baseline_sealed = True""",
        "new": """        self._seed_capture_history()""",
        "expect": [
            "test_the_callback_counters_are_reported_not_withheld",
            "test_the_zero_baseline_needs_no_working_reader",
        ],
    },
    {
        # The zero baseline is a new start, not an extra entry. Keeping the
        # construction reading in front of it makes that older entry the
        # baseline again and undoes the whole change.
        "name": "capture-zero-baseline-appended-behind-an-older-one",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        self._load_capture_history.clear()
        self._load_capture_history_bytes = 0
        self._load_capture_history.append(
            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))""",
        "new": """        self._load_capture_history.append(
            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))""",
        "expect": [
            "test_the_zero_baseline_replaces_any_earlier_reading",
        ],
    },
    {
        # A zero baseline that is not sealed is not a baseline: _capture_
        # baseline answers None and the line says n/a for all three fields.
        "name": "capture-zero-baseline-never-sealed",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))
        self._load_capture_baseline_sealed = True""",
        "new": """            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))
        self._load_capture_baseline_sealed = False""",
        "expect": [
            "test_the_callback_counters_are_reported_not_withheld",
            "test_the_zero_baseline_needs_no_working_reader",
        ],
    },
    {
        # The line and the zero baseline must subtract the same counters. A
        # counter the line reports but the baseline omits loses its baseline
        # and reads n/a forever on the first utterance.
        "name": "capture-line-reports-a-key-the-zero-baseline-omits",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))",
        "new": "            (0, dict.fromkeys(CAPTURE_DELTA_KEYS[:2], 0)))",
        "expect": [
            "test_a_queue_full_drop_before_the_first_read_is_charged",
        ],
    },
    {
        # The seal belongs to one utterance. Carrying it past the endpoint
        # lets the next utterance treat its own first chunk as a baseline.
        "name": "capture-baseline-seal-survives-the-endpoint",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": """        self._load_capture_baseline_sealed = False
        self._seed_capture_history()
        self.current_utterance_id += 1""",
        "new": """        self._seed_capture_history()
        self.current_utterance_id += 1""",
        "expect": [
            "test_a_failed_reseed_does_not_leave_the_previous_seal_standing",
        ],
    },
    {
        # wh-stt-load-metrics.1.7. The defect itself: close the stall
        # tracker's window only at the boundaries that can print a line, so
        # a window whose capture reading was missing banks its stalls for
        # the next window that can.
        "name": "stall-window-closed-only-when-the-line-prints",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Refreshed for wh-whole-outage-metric, which lifted the snapshot
        # into its own statement so the outage record can be read between it
        # and the summary. The mutation is the same one: close the stall
        # window only when the line can print.
        "old": """        stall_snap = self.stall_tracker.snapshot_and_reset_window()""",
        "new": """        _can_print = self._at_window_start is not None and stats is not None
        stall_snap = (self.stall_tracker.snapshot_and_reset_window()
                      if _can_print else
                      {'stalls': self.stall_tracker.stall_count,
                       'max_gap_ms': self.stall_tracker.max_gap_ms})""",
        "expect": ["test_a_stall_in_an_unmeasured_window_is_not_reported_later"],
    },
    {
        # Reading the counters without clearing them makes every later
        # window repeat the first window's stall.
        "name": "stall-window-never-closed",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Refreshed for wh-whole-outage-metric; see the mutation above.
        "old": """        stall_snap = self.stall_tracker.snapshot_and_reset_window()""",
        "new": """        stall_snap = {'stalls': self.stall_tracker.stall_count,
                      'max_gap_ms': self.stall_tracker.max_gap_ms}""",
        "expect": ["test_a_stall_is_not_repeated_in_the_next_reported_window"],
    },
    # -- Recognizer time is not scheduler starvation (round 9) -----------
    {
        # The defect itself: the gap between two record_iteration() calls is
        # a whole loop iteration, so it contains the recognizer call the
        # iteration made. Reporting it whole names a busy loop starved.
        "name": "stall-gap-counts-the-loops-own-work",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        gap = max(0.0, (now - last) - busy_s)",
        "new": "        gap = now - last",
        "expect": [
            "test_a_slow_engine_call_produces_no_stall_line",
            "test_the_window_line_does_not_count_a_slow_engine_call_as_a_stall",
            "test_a_part_starved_part_working_gap_reports_only_the_starved_part",
        ],
    },
    {
        # The clamp, replaced by the size of the overshoot. The two clocks
        # differ, so a work reading marginally past its gap must read as no
        # gap at all rather than as a gap of the overshoot.
        "name": "stall-gap-uses-the-size-of-a-work-overshoot",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        gap = max(0.0, (now - last) - busy_s)",
        "new": "        gap = abs((now - last) - busy_s)",
        "expect": ["test_work_time_beyond_the_gap_does_not_go_negative"],
    },
    {
        # The reader exists but the provider never passes it: the tracker
        # falls back to the plain wall-clock gap and the defect returns with
        # every unit test of the tracker still green.
        "name": "parakeet-reporter-gets-no-work-reader",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                busy_seconds=lambda: self._load_work_s,\n",
        "new": "",
        "expect": [
            "test_the_reporter_is_given_the_loops_own_work_reader",
            "test_the_reader_follows_the_loop_rather_than_a_snapshot",
        ],
    },
    {
        # A reader that raises leaves the gap unmeasurable. Returning zero
        # and carrying on names starvation without knowing, which is the
        # defect this round fixed.
        "name": "stall-work-reader-failure-claims-starvation-anyway",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            logger.debug("[load-diag] work time unavailable: '
               '%s", e)\n            self.stall_tracker.reset()\n',
        "new": '            logger.debug("[load-diag] work time unavailable: '
               '%s", e)\n',
        "expect": ["test_a_raising_work_reader_cannot_produce_a_stall_claim"],
    },
    {
        # The reader is cumulative for the processor's life. Treating a
        # missing first reading as zero charges the first gap with every
        # second the recognizer has ever run, which silences a real stall at
        # startup for as long as that history is larger than the gap.
        "name": "stall-first-gap-charged-with-the-whole-history",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        busy_s = (0.0 if self._busy_at_last is None\n"
               "                  else max(0.0, busy_now - self._busy_at_last))",
        "new": "        busy_s = max(0.0, busy_now - "
               "(self._busy_at_last or 0.0))",
        "expect": ["test_the_first_work_reading_is_not_charged_to_any_gap"],
    },
    # -- Every kind of loop work is subtracted, not only the engine's -----
    {
        # The defect itself: the loop does the work and never records it, so
        # the whole of it stays in the gap and is named CPU starvation. This
        # is what the recognizer-only counter did for the VAD, the AGC, the
        # keep-warm decode, and the forced endpoint's own finalize.
        "name": "loop-does-not-count-process-chunk-as-work",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                self.audio_processor.process_chunk(chunk)\n"
               "                self._load_work_s += time.perf_counter() "
               "- _load_t0",
        "new": "                self.audio_processor.process_chunk(chunk)",
        "expect": [
            "test_the_time_inside_process_chunk_is_counted_as_work",
            "test_the_counter_only_ever_grows",
        ],
    },
    {
        # The idle branch never reaches AudioProcessor, so no recognizer
        # counter could ever have seen it. openWakeWord runs a model on
        # every frame while transcription is off.
        "name": "loop-does-not-count-the-wake-word-frame",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                    self._load_work_s += time.perf_counter() "
               "- _load_t0\n                    continue",
        "new": "                    continue",
        "expect": [
            "test_the_time_inside_a_wake_word_frame_is_counted_as_work"],
    },
    {
        # The loop charges its own wait for audio to its own work. That wait
        # is where a starved loop sits, so counting it erases the very gap
        # the stall field exists to report -- a machine on its knees then
        # reports stalls=0.
        "name": "loop-counts-its-wait-for-the-microphone-as-work",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                chunk = self.audio_capture.read(timeout=0.02)\n"
               "                if chunk is None:\n"
               "                    continue",
        "new": "                _load_wait_t0 = time.perf_counter()\n"
               "                chunk = self.audio_capture.read(timeout=0.02)\n"
               "                if chunk is None:\n"
               "                    self._load_work_s += time.perf_counter() "
               "- _load_wait_t0\n"
               "                    continue",
        "expect": [
            "test_waiting_for_the_microphone_is_not_counted_as_work"],
    },
    {
        # The counter restarts inside the loop. The reporter differences it,
        # so the drop reads as negative work, clamps to zero, and charges
        # the whole of the next gap to starvation.
        "name": "loop-work-counter-restarts-each-iteration",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                _load_t0 = time.perf_counter()\n",
        "new": "                _load_t0 = time.perf_counter()\n"
               "                self._load_work_s = 0.0\n",
        "expect": ["test_the_counter_only_ever_grows"],
    },
    {
        # The document's own reading, restored to what it said before this
        # round. The number is then right and the guide still sends the
        # reader to the wrong follow-up fix.
        "name": "procedure-document-reads-a-slow-inference-as-starvation",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        # Round 12 removed this bullet's closing engine_ratio sentence,
        # which named a field that never held the work
        # (wh-stt-load-metrics.1.17 refresh).
        #
        # Re-anchored for wh-capture-load-gaps.1.2. The pattern used to
        # start at "  failures. ", the words that preceded this sentence
        # until the 2026-09-03 edit put the CPU-measurement sentence there
        # instead ("...so read this number with a CPU measurement beside
        # it."). It had been reporting pattern-not-found rather than a
        # verdict ever since; the current wording is already at this
        # branch's base, so the staleness predates the branch and no full
        # sweep had run to expose it. It now starts at the sentence, which
        # is the text both catching tests actually read.
        "old": "  The loop times its own work and subtracts it before that "
               "comparison, so\n  nothing the loop was busy doing is counted "
               "here. That covers\n  the recognizer, the Silero VAD and the "
               "AGC that run on every chunk, the\n  keep-warm decode during "
               "silence, and the wake word model, which runs while\n  "
               "transcription is off and never reaches the recognizer at "
               "all.\n",
        "new": "",
        "expect": [
            "test_the_stalls_bullet_says_recognizer_time_is_excluded",
            "test_the_stalls_bullet_covers_work_that_is_not_the_recognizer",
        ],
    },
    {
        # The guide names the recognizer only. An operator told nothing about
        # the wake-word model reads a slow one as whole-machine starvation,
        # which is the wrong follow-up fix -- the same wrong-direction
        # reading the numbers themselves were fixed to prevent.
        "name": "procedure-document-omits-the-work-that-is-not-the-recognizer",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": ", and the wake word model, which runs while\n  "
               "transcription is off and never reaches the recognizer at "
               "all",
        "new": "",
        "expect": [
            "test_the_stalls_bullet_covers_work_that_is_not_the_recognizer"],
    },
    # -- A dead capture source is not a clean window (round 10) -----------
    {
        # The defect itself: a window whose microphone never ran reports six
        # zeros, which is what a healthy quiet window reports too.
        "name": "unavailable-capture-window-reports-clean-numbers",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Round 12 added the third term to this condition
        # (wh-stt-load-metrics.1.16 refresh). Refreshed again for
        # wh-whole-outage-metric, which moved the condition out of _summary
        # into _capture_unavailable so the window line and the cross-window
        # outage record read one test and cannot drift apart. What the
        # mutation does is unchanged: no window is ever marked.
        # wh-portaudio-capture-removal dropped a fourth catcher,
        # test_a_window_over_that_provider_is_marked_unavailable, which ran
        # this end to end over the real sounddevice adapter. That adapter is
        # deleted. Its WinRT twin, the last name below, is the same end-to-end
        # path over the one surviving provider, so the mutation keeps a
        # real-adapter catcher and not only the mocked ones.
        "old": "        return (not self._ready_all_window\n"
               "                or not self._frames_arrived(before, after)\n"
               "                or self._capture_interrupted())\n",
        "new": "        return False\n",
        "expect": [
            "test_a_window_with_capture_never_ready_is_marked_unavailable",
            "test_that_window_reports_no_capture_number_at_all",
            "test_a_window_over_a_dead_winrt_provider_is_marked_unavailable",
        ],
    },
    {
        # Readiness read only at the moment the window closes. A window that
        # began against a dead provider then reports clean, and its baseline
        # is the reading that makes every delta wrong.
        "name": "capture-readiness-checked-only-at-the-window-close",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._ready_all_window = self._ready_all_window "
               "and ready_now",
        "new": "        self._ready_all_window = ready_now",
        "expect": [
            "test_a_window_that_began_before_capture_was_ready_is_marked"],
    },
    {
        # The next window starts optimistic instead of carrying the reading
        # that closed this one. That reading is also the next window's
        # baseline, so the window after a dead one reports clean.
        "name": "capture-readiness-resets-optimistically-each-window",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._q_max = 0\n"
               "        self._ready_all_window = ready_now",
        "new": "        self._q_max = 0\n"
               "        self._ready_all_window = True",
        "expect": [
            "test_a_reading_that_goes_unready_at_the_close_marks_both_windows",
            "test_the_window_whose_baseline_came_from_a_dead_provider_is_marked",
        ],
    },
    {
        # A readiness reader that raises answers "ready". Claiming a working
        # microphone without knowing is this defect exactly.
        "name": "capture-readiness-failure-claims-ready-anyway",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            logger.debug("[load-diag] capture readiness '
               'unavailable: %s", e)\n            return False',
        "new": '            logger.debug("[load-diag] capture readiness '
               'unavailable: %s", e)\n            return True',
        "expect": [
            "test_a_readiness_reader_that_raises_does_not_claim_clean_capture"],
    },
    {
        # The reader exists but the provider never passes it, so every unit
        # test of the reporter stays green while production reports zeros
        # for a microphone that never opened.
        "name": "parakeet-reporter-gets-no-readiness-reader",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                capture_ready=lambda: "
               "self.audio_capture.wait_ready(\n"
               "                    timeout=0.0),\n",
        "new": "",
        "expect": [
            "test_the_reporter_is_given_a_capture_readiness_reader",
            "test_the_reader_asks_this_servers_own_capture_provider",
            "test_the_reader_never_blocks_the_consumer_loop",
        ],
    },
    {
        # The reader blocks. It runs once per 20 ms iteration of the very
        # loop this bead measures, so a WinRT setup still in flight would
        # make the reporter create the stalls it is there to observe.
        "name": "parakeet-readiness-reader-blocks-the-consumer-loop",
        "service": PARAKEET,
        "test_file": PARAKEET_TESTS,
        "file": PARAKEET_MAIN,
        "old": "                capture_ready=lambda: "
               "self.audio_capture.wait_ready(\n"
               "                    timeout=0.0),",
        "new": "                capture_ready=lambda: "
               "self.audio_capture.wait_ready(),",
        "expect": ["test_the_reader_never_blocks_the_consumer_loop"],
    },
    {
        # The marker without its reading. An operator who meets an unknown
        # word in the log is no better off than one who read a wrong number.
        "name": "procedure-document-does-not-explain-the-marker",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        # Was the WHOLE bullet, because an early version that removed only
        # the opening words left the body in place and survived. That
        # pattern went stale when the bullet was reworded (it still quoted
        # "so stop the test and fix the microphone", which the document
        # replaced with "One such window does not spoil the run"), and it
        # reported pattern-not-found rather than a verdict; the wording was
        # already current at this branch's base, so the staleness predates
        # this branch and had gone unseen because no full sweep had run
        # since (wh-capture-load-gaps.1.2).
        #
        # Refreshed to the one line the catching test can actually see:
        # test_the_reading_guide_covers_the_unavailable_marker partitions
        # the document on "- **`capture=unavailable`", so retitling the
        # bullet leaves it nothing to find. That is also the drift the test
        # exists for -- the marker renamed in the code and the guide left
        # naming the old one -- and it cannot go stale on a body edit.
        "old": "- **`capture=unavailable` means this window measured no "
               "microphone at all.**\n",
        "new": "- **A window can measure no microphone at all.**\n",
        "expect": ["test_the_reading_guide_covers_the_unavailable_marker"],
    },
    # -- A microphone that died after setup is not a clean window (r11) ---
    {
        # The defect itself: the window trusts the one-time setup handshake,
        # which no provider ever takes back, so a device unplugged mid-test
        # reports drops=0 for ten seconds in which nothing was captured.
        "name": "dead-microphone-window-reports-clean-numbers",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Round 12 added the third term. This mutation drops BOTH frame
        # terms, not just the endpoint one: the round-12 span rule also
        # marks a window whose counter never moved, so removing the
        # endpoint check alone would leave every catcher green and report
        # a survivor for code nothing broke (wh-stt-load-metrics.1.16
        # refresh).
        # Refreshed for wh-whole-outage-metric: the condition moved into
        # _capture_unavailable. Both frame terms still go, for the round-12
        # reason recorded above.
        "old": "        return (not self._ready_all_window\n"
               "                or not self._frames_arrived(before, after)\n"
               "                or self._capture_interrupted())\n",
        "new": "        return not self._ready_all_window\n",
        "expect": [
            "test_a_window_whose_frame_counter_never_moved_is_marked",
            "test_the_handshake_answering_ready_does_not_save_it",
            "test_the_loop_numbers_survive_a_window_with_no_frames",
            "test_a_counter_that_went_backwards_marks_the_window",
        ],
    },
    {
        # This was "frame-check-accepts-a-counter-that-did-not-move",
        # flipping > to >=. Round 12 made that flip unobservable: the span
        # rule marks a frozen counter on its own, so both catchers stayed
        # green and the mutation could only ever report a false pass. What
        # the endpoint check still uniquely catches is a provider that
        # restarted, whose counter moved but went backwards, so the
        # mutation now removes that (wh-stt-load-metrics.1.16).
        "name": "frame-check-accepts-a-provider-that-restarted",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        return b > a\n",
        "new": "        return True\n",
        "expect": ["test_a_counter_that_went_backwards_marks_the_window"],
    },
    {
        # A provider that does not report the counter is called dead. Not
        # knowing is not evidence, and this blanks numbers really measured.
        "name": "frame-check-marks-a-provider-that-omits-the-counter",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if not isinstance(a, int) or not isinstance(b, int):\n"
               "            return True\n",
        "new": "        if not isinstance(a, int) or not isinstance(b, int):\n"
               "            return False\n",
        "expect": [
            "test_a_provider_that_does_not_count_frames_reads_as_before"],
    },
    {
        # The whole added paragraph, not its opening words: a mutation that
        # left the body in place kept every claim the test checks, which is
        # how the round-10 document mutation first survived.
        "name": "procedure-document-does-not-explain-the-dead-microphone",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        # Pattern refreshed for wh-stt-load-metrics.2, which rewrote this
        # paragraph: the guide no longer says the readiness answer is
        # sticky. The behaviour the mutation protects is unchanged, so
        # the mutation stays and only its text moved.
        "old": "\n  A window is marked for a second reason as well: **no "
               "frames arrived in it.**\n"
               "  Asking the provider covers more than it used to: a "
               "microphone unplugged\n"
               "  mid-test, or a capture graph that starts failing, now "
               "stops answering\n"
               "  \"ready\" and marks the window it died in "
               "(wh-stt-load-metrics.2). Each\n"
               "  provider answers from the signals its own backend gives "
               "it: the\n"
               "  sounddevice path asks PortAudio on every reading whether "
               "the stream is\n"
               "  still active, and the WinRT path reports its capture "
               "thread ending or\n"
               "  three consecutive frame polls raising "
               "(wh-stt-load-metrics.2.1.1). What\n"
               "  that question still cannot see is a device that is "
               "alive by every\n"
               "  measure the provider has and delivering nothing \u2014 "
               "a muted or stalled\n"
               "  microphone that the audio backend goes on calling "
               "active. Both\n"
               "  providers count every frame they receive, silence "
               "included, so a frame\n"
               "  count that does not move across a whole window is "
               "direct evidence that\n"
               "  no microphone was measured in it. If the counter is "
               "missing from a\n"
               "  provider's numbers the window is read as before, because "
               "not knowing is not\n"
               "  evidence of a dead device (wh-stt-load-metrics.1.15).\n",
        "new": "",
        "expect": [
            "test_the_reading_guide_covers_a_microphone_that_died_later",
            "test_the_reading_guide_no_longer_calls_the_handshake_sticky",
        ],
    },
    # -- A capture path that opened and then died (wh-stt-load-metrics.2) --
    {
        # The defect itself on the WinRT side: _setup_ok alone, which the
        # capture thread sets once and nothing ever clears.
        "name": "winrt-readiness-goes-back-to-the-setup-flag",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        return self._setup_ok and self._capture_alive",
        "new": "        return self._setup_ok",
        # Catcher renamed for wh-stt-load-metrics.2.1.1:
        # test_winrt_stops_being_ready_when_the_poll_raises mocked
        # _poll_frames itself with side_effect=RuntimeError, a state the
        # real loop cannot reach, and was replaced by the test below that
        # drives the real loop.
        "expect": [
            "test_winrt_stops_being_ready_when_the_capture_loop_ends",
            "test_winrt_stops_being_ready_when_every_frame_poll_raises",
            "test_a_winrt_graph_that_dies_marks_the_window",
        ],
    },
    {
        # The capture thread's finally is the only place that learns the
        # graph is going away. A wait_ready() that reads a flag nobody
        # clears is the same defect wearing a second variable.
        "name": "winrt-capture-thread-never-reports-its-own-end",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        # Pattern re-indented for wh-stt-load-metrics.2.1.2, which moved
        # the finally's body inside a cycle guard. The behaviour is
        # unchanged, so the mutation stays and only its text moved.
        "old": "            self._capture_alive = False\n"
               "            # Set on every exit path",
        "new": "            pass\n"
               "            # Set on every exit path",
        # The failing-poll test is NOT a catcher here: it never lets the
        # thread leave, so the poll loop's own counter is what marks that
        # graph dead, not this line.
        "expect": [
            "test_winrt_stops_being_ready_when_the_capture_loop_ends",
            "test_a_winrt_graph_that_dies_marks_the_window",
        ],
    },
    {
        # The startup gate, which the liveness flag must not break: a graph
        # that came up has to answer ready while it is still polling.
        "name": "winrt-never-reports-a-live-graph",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            self._capture_alive = True\n"
               "            self._setup_done.set()",
        "new": "            self._setup_done.set()",
        "expect": [
            "test_winrt_is_ready_while_the_graph_runs",
            "test_wait_ready_true_after_successful_setup",
            "test_a_live_winrt_graph_still_reports_its_numbers",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.1. The poll loop catches every frame's
        # exception and keeps running, so the capture thread stays alive
        # through a graph that has stopped working and nothing about its
        # lifetime can report it. Without this line the whole finding is
        # back: a WinRT graph whose every poll raises answers ready.
        "name": "winrt-a-failing-poll-never-marks-the-graph-dead",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                        self._capture_alive = False\n"
               "                    time.sleep(0.1)",
        "new": "                        pass\n"
               "                    time.sleep(0.1)",
        "expect": [
            "test_winrt_stops_being_ready_when_every_frame_poll_raises",
            "test_winrt_is_ready_again_once_the_frame_polls_recover",
        ],
    },
    {
        # The other direction, and the reason the threshold is not one: a
        # single transient exception must not report an unavailable window
        # for a provider that is capturing normally.
        "name": "winrt-one-failed-poll-marks-the-graph-dead",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "POLL_FAILURES_BEFORE_DEAD = 3",
        "new": "POLL_FAILURES_BEFORE_DEAD = 1",
        "expect": [
            "test_winrt_stays_ready_through_one_failed_frame_poll",
        ],
    },
    {
        # A device that comes back must not need an external restart. The
        # loop keeps running through the failures, so recovery costs
        # nothing -- but only if the working poll writes the flag back.
        "name": "winrt-a-recovered-poll-never-comes-back",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        # Pattern refreshed after 1aaf66d9 put this write under
        # _lifecycle_lock (wh-stt-load-metrics.2.1.6). The lock stays in the
        # mutant so this mutation still tests only the liveness write; the
        # lock itself is what winrt-the-live-liveness-write-runs-outside-the-
        # lock covers.
        "old": "                with self._lifecycle_lock:\n"
               "                    if self._running and self._cycle == cycle:\n"
               "                        self._capture_alive = True",
        "new": "                with self._lifecycle_lock:\n"
               "                    if self._running and self._cycle == cycle:\n"
               "                        pass",
        "expect": [
            "test_winrt_is_ready_again_once_the_frame_polls_recover",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.2. stop() joins for 2.0s while setup
        # budgets two 5.0s WinRT calls, so a thread can outlive its own
        # stop() and rewrite the flags stop() cleared.
        "name": "winrt-a-stopped-cycle-still-writes-its-flags",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            if not self._running or self._cycle != cycle:\n"
               "                return False",
        "new": "            if self._cycle != cycle:\n"
               "                return False",
        "expect": [
            "test_a_capture_thread_that_outlives_stop_does_not_answer_ready",
        ],
    },
    {
        # The restart half of the same finding, which _running alone cannot
        # cover: start() sets it True again, so the OLD thread's check
        # passes and it answers the NEW cycle's handshake.
        "name": "winrt-a-stale-thread-answers-the-new-cycle",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            if not self._running or self._cycle != cycle:\n"
               "                return False",
        "new": "            if not self._running:\n"
               "                return False",
        "expect": [
            "test_a_stale_thread_does_not_answer_while_the_next_cycle_sets_up",
        ],
    },
    {
        # The worst of the cross-cycle damage: _setup_graph assigns
        # self._graph rather than returning it, so an unguarded finally
        # makes the stale thread stop and close the CURRENT cycle's graph.
        "name": "winrt-a-stale-finally-tears-down-the-new-cycle",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            if self._cycle != cycle:\n"
               "                return False\n\n"
               "            # Whichever way the thread leaves",
        "new": "            if False:\n"
               "                return False\n\n"
               "            # Whichever way the thread leaves",
        "expect": [
            "test_a_stale_capture_thread_does_not_disturb_the_next_cycle",
        ],
    },
    # -- A stale capture thread reaches the live cycle (round 13) --------
    {
        # The largest of the three gaps the round-1 cycle token missed: the
        # loop ran on _running alone, and a restart sets that True again, so
        # a thread blocked in get_frame past stop()'s bounded join resumed
        # and polled for the life of the process.
        "name": "winrt-the-poll-loop-runs-on-running-alone",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        while self._running and self._cycle == cycle:",
        "new": "        while self._running:",
        "expect": [
            "test_a_stale_poll_loop_ends_when_a_new_cycle_replaces_it",
        ],
    },
    {
        # Reading the node from the provider rather than from its own setup
        # is what let a late-finishing thread redirect the live loop onto a
        # graph nobody was reading.
        "name": "winrt-the-poll-loop-reads-the-shared-node",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                frame = frame_output.get_frame()",
        "new": "                frame = self._frame_output.get_frame()",
        "expect": [
            "test_a_stale_setup_does_not_make_the_live_cycle_report_itself_dead",
        ],
    },
    {
        # Leaving the loop at the top is not enough on its own: the poll that
        # was already inside get_frame still has a chunk to deliver, and it
        # would land in the counters the load reporter reads.
        "name": "winrt-an-in-flight-chunk-reaches-the-new-cycle",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        # Pattern refreshed after 1aaf66d9 added the _running half of this
        # guard (wh-stt-load-metrics.2.1.6). Only the cycle half is removed,
        # so a restart is still unguarded while a stop stays guarded, which
        # is what this mutation is named for. The _running half has its own
        # mutation, winrt-a-post-stop-chunk-still-reaches-the-queue.
        "old": "                            if not self._running or self._cycle != cycle:",
        "new": "                            if not self._running:",
        "expect": [
            "test_a_stale_iteration_in_flight_does_not_feed_the_new_cycle",
        ],
    },
    {
        # Publishing without the guard installs a stale thread's graph over
        # the live one, which is the state the old crewcut comment described
        # backwards.
        "name": "winrt-a-stale-thread-publishes-its-resources",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            if not self._running or self._cycle != cycle:\n"
               "                return False",
        "new": "            if False:\n"
               "                return False",
        # NOT test_a_stale_setup_does_not_make_the_live_cycle_report_itself_dead,
        # which was the obvious catcher and does not fail here. Once the poll
        # loop reads the node from its own argument, a stale thread that
        # publishes no longer redirects the live loop, so that test keeps
        # passing and only the direct test of the guard reports the change.
        "expect": [
            "test_publishing_a_stale_cycle_installs_nothing",
        ],
    },
    {
        # Tearing down whatever the provider currently holds, instead of what
        # this thread built: the stale thread then closes the live graph and
        # its own is left running with no reference to it.
        "name": "winrt-a-thread-tears-down-the-shared-graph",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            self._cleanup_graph(graph, mic_node, frame_output)",
        "new": "            self._cleanup_graph()",
        "expect": [
            "test_each_cycle_closes_the_graph_it_created",
            "test_a_stale_capture_thread_does_not_disturb_the_next_cycle",
        ],
    },
    {
        # The lock on the way out. Without it the cycle test and the writes
        # are two steps, and a thread descheduled between them writes into a
        # cycle that has since been replaced.
        "name": "winrt-the-lifecycle-lock-blocks-nothing",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        self._lifecycle_lock = threading.Lock()",
        "new": "        self._lifecycle_lock = __import__('contextlib').nullcontext()",
        # The two transition tests wrap whatever the constructor built, so
        # they no longer see this one (wh-stt-load-metrics.2.1.11). The
        # construction guard does, with no threads and no timing.
        "expect": [
            "test_the_lifecycle_lock_is_a_real_mutual_exclusion_lock",
        ],
    },
    {
        # The lock at the retirement transition itself, rather than at the
        # constructor. Without it no thread ever parks at the transition,
        # which is what the reworked test now requires before it reads any
        # state (wh-stt-load-metrics.2.1.11).
        "name": "winrt-the-retirement-transition-takes-no-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        with self._lifecycle_lock:\n"
               "            if self._cycle != cycle:\n"
               "                return False\n"
               "\n"
               "            # Whichever way the thread leaves, the graph is "
               "about to be",
        "new": "        with __import__('contextlib').nullcontext():\n"
               "            if self._cycle != cycle:\n"
               "                return False\n"
               "\n"
               "            # Whichever way the thread leaves, the graph is "
               "about to be",
        "expect": [
            "test_the_retirement_transition_holds_the_lifecycle_lock",
        ],
    },
    {
        # The same at the publish transition.
        "name": "winrt-the-publish-transition-takes-no-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        with self._lifecycle_lock:\n"
               "            # _running covers a plain stop(); the cycle "
               "number covers a",
        "new": "        with __import__('contextlib').nullcontext():\n"
               "            # _running covers a plain stop(); the cycle "
               "number covers a",
        "expect": [
            "test_the_publish_transition_holds_the_lifecycle_lock",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.9. The watermark records an attempt, and an
        # attempt can be lost before it is delivered. Without this reset a
        # lost report leaves the total equal to the watermark and that logger
        # never reports again. Since wh-forwarded-log-time-order defect 2 a
        # disconnect no longer discards a queued log frame, so the reachable
        # loss is process exit before delivery -- before the queue drains, or
        # with the frame in the pending slot, which stop() never waits on.
        # Pattern refreshed by wh-forwarded-log-time-order: the reset moved
        # out of emit() into the _on_disconnected_drop override when the
        # reachability gate went down to the base class. Same defect, same
        # catchers; only the anchor changed.
        "name": "capture-log-a-disconnect-keeps-the-suppression-watermark",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        self._reported_suppressed.clear()\n"
               "\n"
               "    def _forward_suppression_count",
        "new": "        pass\n"
               "\n"
               "    def _forward_suppression_count",
        "expect": [
            "test_a_disconnect_makes_the_next_window_report_the_count_again",
            "test_a_logger_that_falls_silent_is_reported_by_another",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.9. Only the logger that opened the window
        # reports, so a capture path that falls silent after its report was
        # lost is never carried by a path that keeps working.
        "name": "capture-log-only-the-triggering-logger-is-reported",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "            for name in sorted(self._suppressed_totals):",
        "new": "            for name in [n for n in [record.name] "
               "if n in self._suppressed_totals]:",
        "expect": [
            "test_a_logger_that_falls_silent_is_reported_by_another",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.10. The docstring that tells a maintainer
        # which condition catches which death. Restoring the stale sentence
        # sends them looking for a gap the readiness work already closed.
        "name": "diagnostics-the-unavailable-docstring-calls-readiness-sticky",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        The first catches a backend that reports its own death.",
        "new": "        The handshake answers ready for the life of the "
               "process, so the",
        "expect": [
            "test_the_unavailable_docstring_no_longer_calls_readiness_sticky",
        ],
    },
    {
        # Round 8 found the corrected sentence credited MicrophoneStream
        # with a wait_ready method it does not have. Restoring that text
        # removes the only mention of the class that really owns the
        # readiness answer, so both assertions in the guard test fire.
        # wh-portaudio-capture-removal deleted MicrophoneStream, which is
        # what makes the restored credit wrong twice over.
        "name": "diagnostics-the-unavailable-docstring-credits-the-wrong-class",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        when capture ends: WinRTAudioCapture answers False once its setup\n"
               "        wait expires, once",
        "new": "        when capture ends: MicrophoneStream asks PortAudio whether the\n"
               "        stream is still active. It answers False once its setup\n"
               "        wait expires, once",
        "expect": [
            "test_the_unavailable_docstring_names_the_class_that_answers",
        ],
    },
    {
        # Round 8 also left the readiness sentence listing two of the
        # three ways wait_ready answers no. Dropping the setup-wait
        # clause again is caught by that clause's own assertion, and by
        # no other, so the three assertions are proven separately.
        "name": "diagnostics-the-unavailable-docstring-omits-the-setup-wait",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "WinRTAudioCapture answers False once its setup\n"
               "        wait expires, once the capture thread has ended, or once",
        "new": "WinRTAudioCapture answers False once\n"
               "        the capture thread has ended, or once",
        "expect": [
            "test_the_unavailable_docstring_states_every_way_readiness_says_no",
        ],
    },
    {
        # The second of the three ways wait_ready answers no. Each way
        # has its own assertion in the guard test, so each needs its own
        # mutation; this one drops the ended-capture-thread clause.
        # It replaced a mutation against a 'started capture' qualifier
        # that belonged to the deleted PortAudio sentence
        # (wh-portaudio-capture-removal).
        "name": "diagnostics-the-unavailable-docstring-drops-the-ended-thread-case",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        wait expires, once the capture thread has ended, or once\n"
               "        POLL_FAILURES_BEFORE_DEAD",
        "new": "        wait expires, or once\n"
               "        POLL_FAILURES_BEFORE_DEAD",
        "expect": [
            "test_the_unavailable_docstring_states_every_way_readiness_says_no",
        ],
    },
    {
        # The third way, and the only one a maintainer cannot guess from
        # the other two: a capture thread that is still alive while every
        # frame poll raises. It replaced a mutation against the deleted
        # PortAudio never-opened case (wh-portaudio-capture-removal).
        "name": "diagnostics-the-unavailable-docstring-drops-the-failed-polls-case",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "once the capture thread has ended, or once\n"
               "        POLL_FAILURES_BEFORE_DEAD consecutive frame polls have raised. So",
        "new": "once the capture thread has ended. So",
        "expect": [
            "test_the_unavailable_docstring_states_every_way_readiness_says_no",
        ],
    },
    {
        # start() moves the cycle under the same lock. Leaving that outside
        # it lets a leaving thread see the old cycle number and the new
        # handshake at the same time.
        "name": "winrt-the-restart-moves-the-cycle-outside-the-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "            self._cycle += 1",
        "new": "            self._cycle += 0",
        "expect": [
            "test_a_stale_thread_does_not_answer_while_the_next_cycle_sets_up",
            "test_a_stale_poll_loop_ends_when_a_new_cycle_replaces_it",
            "test_each_cycle_closes_the_graph_it_created",
        ],
    },
    # -- An outage inside a window is not a measured window (round 12) ---
    {
        # The defect itself: the counter compared only at the two ends. One
        # frame just after the window opens makes that difference positive,
        # and the nine dead seconds after it report themselves as measured.
        "name": "endpoint-only-frame-check-passes-as-liveness-proof",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        # Refreshed for wh-whole-outage-metric: the condition moved into
        # _capture_unavailable, so the term is dropped from a return rather
        # than from an if.
        "old": "                or not self._frames_arrived(before, after)\n"
               "                or self._capture_interrupted())\n",
        "new": "                or not self._frames_arrived(before, after))\n",
        "expect": [
            "test_an_early_frame_then_a_dead_microphone_is_marked",
            "test_an_outage_that_recovers_is_still_marked",
            "test_the_marked_window_keeps_its_loop_numbers",
        ],
    },
    {
        # The counter is never read inside the window, so the only readings
        # that exist are the two ends again.
        "name": "frame-progress-not-read-inside-the-window",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._track_frame_progress(now, stats)\n",
        "new": "",
        "expect": [
            "test_an_early_frame_then_a_dead_microphone_is_marked",
            "test_an_outage_that_recovers_is_still_marked",
            "test_the_marked_window_keeps_its_loop_numbers",
        ],
    },
    {
        # The bound is so wide that no window can reach it. The measurement
        # is still taken and still printed, which is what makes this the
        # quiet form of the defect.
        "name": "outage-bound-so-wide-no-window-is-marked",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "_OUTAGE_WINDOW_FRACTION = 0.5\n",
        "new": "_OUTAGE_WINDOW_FRACTION = 5.0\n",
        "expect": [
            "test_an_early_frame_then_a_dead_microphone_is_marked",
            "test_an_outage_that_recovers_is_still_marked",
        ],
    },
    {
        # The opposite failure, and the more damaging one: a bound tight
        # enough that ordinary jitter blanks the capture fields, deleting
        # the overflow reading the whole procedure exists to read.
        "name": "outage-bound-so-tight-a-short-gap-marks-the-window",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "_OUTAGE_WINDOW_FRACTION = 0.5\n",
        "new": "_OUTAGE_WINDOW_FRACTION = 0.01\n",
        "expect": ["test_a_short_gap_leaves_the_window_measured"],
    },
    {
        # The gap is never reset, so one outage keeps condemning every
        # window after it, including the ones whose microphone recovered.
        "name": "gap-reading-carried-into-the-next-window",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._max_frame_gap_s = 0.0\n"
               "        self._frames_measured = False\n"
               "        return lines",
        "new": "        self._frames_measured = False\n"
               "        return lines",
        "expect": ["test_the_gap_reading_restarts_each_window"],
    },
    {
        # A provider that never reported the counter reports a zero gap,
        # which reads as "measured, no outage" -- the same false all-clear
        # every other capture field spells n/a to avoid.
        "name": "absent-frame-counter-reports-a-zero-gap",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        gap_ms = (f\"{self._max_frame_gap_s * 1000:.0f}\"\n"
               "                  if self._frames_measured else 'n/a')\n",
        "new": "        gap_ms = f\"{self._max_frame_gap_s * 1000:.0f}\"\n",
        "expect": [
            "test_a_provider_that_omits_the_counter_reports_no_gap_reading"],
    },
    {
        # Frames keep arriving but the progress time is never moved on, so
        # a healthy microphone accrues the whole window as one gap. The
        # round-13 fix split this condition in two, so the pattern moved with
        # it and the mutation is now the inverted test
        # (wh-stt-load-metrics.1.20 refresh).
        "name": "arriving-frames-do-not-restart-the-gap",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if captured != self._frames_at_last:\n"
               "            self._frames_at_last = captured\n"
               "            self._frames_progress_at = now\n",
        "new": "        if captured == self._frames_at_last:\n"
               "            self._frames_at_last = captured\n"
               "            self._frames_progress_at = now\n",
        "expect": [
            "test_a_live_microphone_is_not_marked",
            "test_the_gap_reading_restarts_each_window",
        ],
    },
    {
        # The whole added paragraph, not its opening words.
        "name": "procedure-document-does-not-explain-the-partial-outage",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "\n  A window is marked for a third reason: **the frames "
               "stopped arriving inside\n"
               "  it.** One frame at either end makes the count move across "
               "the window\n"
               "  however long the silence between them is, so the two end "
               "readings alone\n"
               "  cannot see a microphone that delivered once and then died. "
               "The reporter\n"
               "  reads the count on every loop iteration instead, and "
               "prints the longest\n"
               "  span in which it did not move as `max_frame_gap_ms`. A "
               "span longer than\n"
               "  half the window blanks the capture fields, because they "
               "then describe less\n"
               "  than half the time they claim to cover. A shorter span "
               "leaves them alone:\n"
               "  under this test's own load a moment with no frame is "
               "expected, and blanking\n"
               "  there would delete the `overflow` reading that says what "
               "really happened.\n"
               "  `max_frame_gap_ms` reads `n/a` for a provider that does "
               "not report the\n"
               "  count, for the same reason the other fields do\n"
               "  (wh-stt-load-metrics.1.16).\n",
        "new": "",
        "expect": [
            "test_the_reading_guide_covers_a_partial_capture_outage"],
    },
    # -- A gap is measured inside its own window (round 13) ---------------
    {
        # The finding restored: the span is measured from the last arrival
        # however long ago it was, so silence the previous line already
        # reported is charged again to this window's half-window bound.
        "name": "cross-window-gap-decides-this-windows-capture",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        gap_start = self._frames_progress_at\n"
               "        if (self._window_start is not None\n"
               "                and gap_start < self._window_start):\n"
               "            gap_start = self._window_start\n"
               "        gap = now - gap_start\n",
        "new": "        gap_start = self._frames_progress_at\n"
               "        gap = now - gap_start\n",
        "expect": [
            "test_an_outage_ending_early_in_the_next_window_leaves_it_"
            "measured",
            "test_the_second_windows_gap_describes_only_that_window",
            "test_a_gap_is_never_longer_than_the_window_it_is_printed_for",
        ],
    },
    {
        # Clipped to the window start even when the frames that stopped
        # arrived inside the window. A healthy microphone never reaches this
        # line -- a moving counter returns before it -- so what breaks is
        # ordinary jitter: a short frozen span is measured from the start of
        # the window instead of the last frame, and condemns the window.
        "name": "gap-always-measured-from-the-window-start",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if (self._window_start is not None\n"
               "                and gap_start < self._window_start):\n"
               "            gap_start = self._window_start\n",
        "new": "        if self._window_start is not None:\n"
               "            gap_start = self._window_start\n",
        "expect": [
            "test_a_short_gap_leaves_the_window_measured",
            "test_an_outage_ending_early_in_the_next_window_leaves_it_"
            "measured",
        ],
    },
    {
        # The comparison reversed, which does both wrongs at once: an
        # arrival inside the window is thrown away, and a reading from
        # before it is kept.
        "name": "gap-clamp-applied-in-the-wrong-direction",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "                and gap_start < self._window_start):\n",
        "new": "                and gap_start > self._window_start):\n",
        "expect": [
            "test_a_short_gap_leaves_the_window_measured",
            "test_the_second_windows_gap_describes_only_that_window",
            "test_a_gap_is_never_longer_than_the_window_it_is_printed_for",
        ],
    },
    {
        # The whole added paragraph. The guide then describes a gap with no
        # rule for the boundary, which is the reading that produced the
        # defect.
        "name": "procedure-document-omits-the-cross-boundary-rule",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "\n  An outage that straddles a window boundary is measured "
               "in each window as\n"
               "  the part of it that fell inside that window. The reading "
               "of when the count\n"
               "  last moved survives the boundary, so the silence after it "
               "is not read as a\n"
               "  fresh start, but the seconds before the boundary belong to "
               "the line that\n"
               "  already reported them. Charging them again would blank a "
               "window whose\n"
               "  numbers describe most of its own time, and would print a "
               "hole longer than\n"
               "  the window it is printed for (wh-stt-load-metrics.1.18).\n",
        "new": "",
        "expect": ["test_the_guide_says_the_gap_belongs_to_its_own_window"],
    },
    # -- A reading is the only look at capture (round 14) -----------------
    {
        # The finding restored: the reading that sees the count move returns
        # before measuring, so the seconds since the previous reading are
        # charged to nothing and one long recognizer call reports as clean.
        "name": "a-moving-count-hides-the-seconds-since-the-last-look",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        gap_start = self._frames_progress_at\n"
               "        if (self._window_start is not None\n"
               "                and gap_start < self._window_start):\n"
               "            gap_start = self._window_start\n"
               "        gap = now - gap_start\n"
               "        if gap > self._max_frame_gap_s:\n"
               "            self._max_frame_gap_s = gap\n"
               "        if captured != self._frames_at_last:\n"
               "            self._frames_at_last = captured\n"
               "            self._frames_progress_at = now\n",
        "new": "        if captured != self._frames_at_last:\n"
               "            self._frames_at_last = captured\n"
               "            self._frames_progress_at = now\n"
               "            return\n"
               "        gap_start = self._frames_progress_at\n"
               "        if (self._window_start is not None\n"
               "                and gap_start < self._window_start):\n"
               "            gap_start = self._window_start\n"
               "        gap = now - gap_start\n"
               "        if gap > self._max_frame_gap_s:\n"
               "            self._max_frame_gap_s = gap\n",
        "expect": [
            "test_a_window_the_loop_only_looked_at_twice_is_not_measured",
            "test_the_window_says_how_long_it_went_unwatched",
            "test_a_look_away_in_the_middle_of_a_healthy_window_is_marked",
        ],
    },
    {
        # The whole added paragraph. The guide then describes a span between
        # frames rather than between readings, which is the reading that
        # makes a window flagged while the count moved look like a fault.
        "name": "procedure-document-omits-the-reading-interval-rule",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "\n  The span runs between READINGS, not between frames. A "
               "reading is the only\n"
               "  moment the loop learns anything about capture, so when one "
               "loop iteration\n"
               "  takes seconds -- a long recognizer call under load, a "
               "wake-word inference --\n"
               "  those are seconds nothing was observed, and they count "
               "toward the span in\n"
               "  the same way. A window the loop only looked at twice is "
               "therefore marked,\n"
               "  even though the count moved between those two looks\n"
               "  (wh-stt-load-metrics.1.20).\n",
        "new": "",
        "expect": [
            "test_the_guide_says_a_reading_is_the_only_thing_that_counts"],
    },
    # -- The guide names only the metrics that exist (round 12) -----------
    {
        # The sentence put back exactly as it stood. The numbers are then
        # right and the guide still sends the reader to a field that never
        # held the work.
        "name": "procedure-document-sends-other-work-to-engine-ratio",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "  transcription is off and never reaches the recognizer at "
               "all.\n",
        "new": "  transcription is off and never reaches the recognizer at "
               "all. A slow\n  inference of any of those belongs to "
               "`engine_ratio`, not to this field.\n",
        "expect": [
            "test_the_stalls_bullet_does_not_send_other_work_to_engine_ratio"],
    },
    {
        # The correction removed instead. The false claim is gone, but so is
        # the statement of what engine_ratio really counts.
        "name": "procedure-document-omits-what-engine-ratio-measures",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "\n  `engine_ratio` on the per-utterance line is the time inside "
               "`engine.process_audio` plus\n"
               "  `engine.finalize`, divided by the duration of the audio "
               "those calls\n",
        "new": "",
        "expect": [
            "test_the_guide_says_what_engine_ratio_actually_measures",
        ],
    },
    # -- One outage spanning windows is one event (wh-whole-outage-metric) -
    {
        # The defect itself: the outage is never recorded across windows, so
        # a minute-long outage is six marked lines and no statement of the
        # whole. Assigning None rather than deleting the call keeps the name
        # bound, so the mutant fails on the missing event instead of on a
        # NameError, which would read as a catch and prove nothing.
        "name": "outage-never-recorded-across-windows",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        outage = self._track_outage(now, stats)\n",
        "new": "        outage = None\n",
        "expect": [
            "test_an_outage_across_three_windows_produces_one_event",
            "test_the_event_says_when_the_outage_began",
            "test_the_event_says_when_the_outage_ended",
            "test_the_event_says_how_long_the_outage_lasted",
            "test_the_event_counts_the_windows_it_spans",
            "test_a_single_window_outage_produces_one_event",
            "test_the_event_is_not_repeated_once_it_is_reported",
            "test_the_event_line_comes_before_the_window_that_closed_it",
            "test_an_unmeasured_window_ends_the_event_rather_than_spanning_it",
            "test_every_field_of_the_event_line_is_read_in_the_document",
        ],
    },
    {
        # The event emitted per marked window instead of per run, which is
        # the per-window reporting this bead exists to replace. The single
        # window case is deliberately NOT expected to fail: one marked
        # window is one event either way, and naming it would report a
        # survivor for behaviour the mutation cannot change.
        "name": "outage-reported-once-per-window-not-once-per-run",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "            self._outage_end = now\n"
               "            self._outage_windows += 1\n"
               "            return None\n",
        "new": "            self._outage_end = now\n"
               "            self._outage_windows += 1\n"
               "            return self._close_outage(now)\n",
        "expect": [
            "test_an_outage_across_three_windows_produces_one_event",
            "test_the_event_says_when_the_outage_began",
            "test_the_event_says_when_the_outage_ended",
            "test_the_event_says_how_long_the_outage_lasted",
            "test_the_event_counts_the_windows_it_spans",
            "test_the_event_line_comes_before_the_window_that_closed_it",
            "test_every_field_of_the_event_line_is_read_in_the_document",
        ],
    },
    {
        # The start moved forward to every later marked window, so the event
        # reports the last window of the outage as the whole of it.
        "name": "outage-start-taken-from-the-last-window-not-the-first",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "            if self._outage_start is None:\n"
               "                self._outage_start = self._window_start\n",
        "new": "            if True:\n"
               "                self._outage_start = self._window_start\n",
        "expect": [
            "test_the_event_says_when_the_outage_began",
            "test_the_event_says_how_long_the_outage_lasted",
            "test_the_event_counts_the_windows_it_spans",
        ],
    },
    {
        # The outage state added to the window boundary reset, which is the
        # edit a later reader is most likely to make by matching the fields
        # around it. Surviving that reset is the whole point of the record.
        "name": "outage-state-reset-at-the-window-boundary",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._max_frame_gap_s = 0.0\n"
               "        self._frames_measured = False\n"
               "        return lines\n",
        "new": "        self._max_frame_gap_s = 0.0\n"
               "        self._frames_measured = False\n"
               "        self._outage_start = None\n"
               "        self._outage_end = None\n"
               "        self._outage_windows = 0\n"
               "        return lines\n",
        "expect": [
            "test_an_outage_across_three_windows_produces_one_event",
            "test_the_event_says_when_the_outage_began",
            "test_the_event_says_when_the_outage_ended",
            "test_the_event_says_how_long_the_outage_lasted",
            "test_the_event_counts_the_windows_it_spans",
            "test_a_single_window_outage_produces_one_event",
            "test_the_event_is_not_repeated_once_it_is_reported",
            "test_the_event_line_comes_before_the_window_that_closed_it",
            "test_an_unmeasured_window_ends_the_event_rather_than_spanning_it",
            "test_every_field_of_the_event_line_is_read_in_the_document",
        ],
    },
    {
        # A window with no capture reading spans the outage instead of
        # ending it, so one event claims seconds nothing measured. The
        # mutation returns early rather than dropping the None guards:
        # dropping them calls dict.get on None, and the AttributeError would
        # read as a catch while proving nothing about the rule.
        "name": "unmeasured-window-spans-the-outage",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if (before is not None and after is not None\n"
               "                and self._capture_unavailable(before, after)):"
               "\n",
        "new": "        if before is None or after is None:\n"
               "            return None\n"
               "        if self._capture_unavailable(before, after):\n",
        "expect": [
            "test_an_unmeasured_window_ends_the_event_rather_than_spanning_it",
        ],
    },
    {
        # The duration measured to the moment the line is written rather
        # than to the end of the outage, so every event is one window too
        # long -- the recovery window charged to the microphone.
        "name": "outage-duration-runs-to-the-line-not-to-the-outage-end",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            f"duration_s={self._outage_end - self._outage_start:.0f} "\n',
        "new": '            f"duration_s={now - self._outage_start:.0f} "\n',
        "expect": [
            "test_the_event_says_how_long_the_outage_lasted",
            "test_a_single_window_outage_produces_one_event",
            "test_an_unmeasured_window_ends_the_event_rather_than_spanning_it",
        ],
    },
    {
        # The length computed from the configured interval instead of from
        # the two recorded times. Every equal-window fixture agrees with
        # this, which is exactly why codex filed wh-whole-outage-metric.1.2:
        # a descheduled consumer closes its window late, and multiplying the
        # interval by the count then undercounts the hole the operator is
        # trying to measure.
        "name": "outage-duration-computed-as-interval-times-windows",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            f"duration_s={self._outage_end - self._outage_start:.0f} "\n',
        "new": '            f"duration_s={self._interval * self._outage_windows:.0f} "\n',
        "expect": [
            "test_windows_of_unequal_length_are_measured_not_multiplied",
        ],
    },
    {
        # The outage record narrowed to the frame-arrival signature alone,
        # leaving _summary on the shared _capture_unavailable so every
        # per-window line is unchanged. Codex filed this as
        # wh-whole-outage-metric.1.5: every other fixture in the class
        # freezes the counter, which satisfies all three signatures at once,
        # so a microphone that never started or that stopped for most of a
        # window would be marked and then get no event.
        "name": "outage-tracks-only-the-frame-counter-not-all-three-signs",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "                and self._capture_unavailable(before, after)):\n",
        "new": "                and not self._frames_arrived(before, after)):\n",
        "expect": [
            "test_a_readiness_failure_alone_opens_the_event",
            "test_an_interior_silence_alone_opens_the_event",
        ],
    },
    {
        # The opening inventory left at its old count while the section below
        # it shows three forms of line. Codex filed this as
        # wh-whole-outage-metric.1.6: the reading guide then contradicts
        # itself about the measurement it just added, and the operator meets
        # a line the document has just said does not exist.
        # Renamed from "...-counts-two-..." for wh-capture-load-gaps.1.2:
        # the reason line made the true count four, so the undercount this
        # mutation writes is now three.
        "name": "procedure-document-still-counts-three-load-diag-lines",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "Six log lines carry the measurement. Five are tagged `[load-diag]`;\n",
        "new": "Three log lines carry the measurement. Three are tagged `[load-diag]`\n",
        "expect": [
            "test_the_opening_inventory_counts_the_outage_line",
        ],
    },
    {
        # The start printed as a raw monotonic reading instead of counted
        # back from the line, which is a number with no meaning to a reader
        # and the whole reason the field is named start_s_ago.
        "name": "outage-start-printed-as-a-raw-monotonic-reading",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            f"start_s_ago={now - self._outage_start:.0f} "\n',
        "new": '            f"start_s_ago={self._outage_start:.0f} "\n',
        "expect": ["test_the_event_says_when_the_outage_began"],
    },
    {
        # The event without its reading. The WHOLE bullet, not its opening
        # words, for the reason recorded on the unavailable-marker mutation.
        "name": "procedure-document-does-not-explain-the-outage-event",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "- **`capture-outage` counts one outage across every window it spanned.**\n"
               "  A microphone that stays down for a minute prints six marked windows and no\n"
               "  statement of the whole, so the operator had to count the lines and\n"
               "  multiply -- and windows of unequal length cannot be counted that way at\n"
               "  all. The reporter records the run of marked windows and prints one line\n"
               "  when the outage ends. `duration_s` is the length of the whole outage, and\n"
               "  `windows` is how many marked windows it covered, which ties the event to\n"
               "  the lines above it in the log.\n"
               "\n"
               "  `start_s_ago` and `end_s_ago` are counted back from this line's own\n"
               "  timestamp, because the reporter's clock is monotonic: its origin means\n"
               "  nothing to a reader, while the timestamp the log writes on every line is\n"
               "  exactly what these two numbers are meant to be subtracted from. A line\n"
               "  stamped 14:03:10 reading `start_s_ago=40 end_s_ago=10` describes an outage\n"
               "  from 14:02:30 to 14:03:00.\n"
               "\n"
               "  Both times are window edges, not the moments the microphone stopped and\n"
               "  started. A window is marked when it holds a span with no frame longer than\n"
               "  half of it, so the outage really begins somewhere inside the first marked\n"
               "  window and ends somewhere inside the last. `windows` says how many windows\n"
               "  the event covers, which is how coarse its own two times are, and\n"
               "  `max_frame_gap_ms` on each marked line is the finer measurement.\n"
               "\n"
               "  The line is printed when the outage ends, so an outage **still running**\n"
               "  when you stop the test prints no event at all, and its marked windows are\n"
               "  the whole of what the log holds for it. A window whose capture reading was\n"
               "  missing entirely ends the run rather than spanning it, so an outage\n"
               "  interrupted by one unreadable window prints as two events: a reading the\n"
               "  provider could not give is not evidence of an outage and not evidence of\n"
               "  recovery, and one event across that gap would claim seconds nothing\n"
               "  measured (wh-whole-outage-metric).\n",
        "new": "",
        "expect": [
            "test_the_reading_guide_covers_the_outage_event",
            "test_the_guide_says_the_two_times_are_window_edges",
            "test_the_guide_says_an_outage_still_running_is_not_reported",
            "test_every_field_of_the_event_line_is_read_in_the_document",
        ],
    },
    {
        # The sentinel back to None. A capture thread whose setup raised
        # passes None for all three, and without a distinct default that
        # call means "tear down whatever the provider holds" -- which by
        # then can be a later cycle's live graph.
        "name": "winrt-a-failed-setup-tears-down-the-provider",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        if graph is _PROVIDER_RESOURCES:",
        "new": "        if graph is _PROVIDER_RESOURCES or graph is None:",
        "expect": [
            "test_a_setup_that_failed_does_not_close_a_later_cycles_graph",
        ],
    },
    {
        # The graph left open when a later setup call fails. Nothing else
        # can reach it, so it holds the microphone until the process exits.
        "name": "winrt-a-partial-setup-leaks-its-graph",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        except Exception:\n"
               "            self._cleanup_graph(graph, None, None)\n"
               "            raise\n",
        "new": "        except Exception:\n"
               "            raise\n",
        "expect": ["test_a_partial_setup_closes_the_graph_it_created"],
    },
    {
        # stop() and close() back under one try. A graph whose stop() raises
        # then never gets the call that actually releases the device.
        "name": "winrt-a-graph-that-will-not-stop-is-never-closed",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "        try:\n"
               "            graph.stop()\n"
               "        except Exception as e:\n"
               '            logger.warning(f"Graph stop error: {e}")\n'
               "\n"
               "        try:\n"
               "            graph.close()\n",
        "new": "        try:\n"
               "            graph.stop()\n"
               "            graph.close()\n"
               "        except Exception as e:\n"
               '            logger.warning(f"Graph stop error: {e}")\n'
               "\n"
               "        try:\n"
               "            pass\n",
        "expect": ["test_a_graph_that_will_not_stop_is_still_closed"],
    },
    {
        # The per-chunk guard back to the cycle alone. stop() never moves
        # the cycle, so a poll blocked in get_frame past its bounded join
        # writes audio and counters into a provider meant to be silent.
        "name": "winrt-a-post-stop-chunk-still-reaches-the-queue",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                            if not self._running or self._cycle != cycle:",
        "new": "                            if self._cycle != cycle:",
        "expect": [
            "test_a_poll_that_lands_after_stop_does_not_reach_the_queue",
        ],
    },
    {
        # The chunk write's lock removed while its guard stays. The pair is
        # then two steps again, which is the whole of finding .2.1.5.
        "name": "winrt-the-chunk-write-runs-outside-the-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                        overflowed = False\n"
               "                        with self._lifecycle_lock:\n",
        "new": "                        overflowed = False\n"
               "                        with __import__('contextlib').nullcontext():\n",
        "expect": ["test_the_chunk_write_holds_the_lifecycle_lock"],
    },
    {
        # The healthy-poll liveness write outside the lock, so a thread
        # descheduled after its check can resurrect a retired cycle.
        "name": "winrt-the-live-liveness-write-runs-outside-the-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                consecutive_failures = 0\n"
               "                with self._lifecycle_lock:\n",
        "new": "                consecutive_failures = 0\n"
               "                with __import__('contextlib').nullcontext():\n",
        "expect": ["test_the_live_liveness_write_holds_the_lifecycle_lock"],
    },
    {
        # The failing-poll liveness write outside the lock. This is the
        # direction that blanks the load reporter's fields while audio is
        # still arriving, which is worse than the bug being fixed.
        "name": "winrt-the-dead-liveness-write-runs-outside-the-lock",
        "service": SHARED,
        "test_file": (CAPTURE_TESTS, SHARED_TESTS),
        "file": WINRT,
        "old": "                    if consecutive_failures >= POLL_FAILURES_BEFORE_DEAD:\n"
               "                        with self._lifecycle_lock:\n",
        "new": "                    if consecutive_failures >= POLL_FAILURES_BEFORE_DEAD:\n"
               "                        with __import__('contextlib').nullcontext():\n",
        "expect": ["test_the_dead_liveness_write_holds_the_lifecycle_lock"],
    },
    {
        # wh-stt-load-metrics.2.1.7. Records written while WheelHouse is
        # unreachable go into an unbounded queue that a run of INITIAL
        # connect failures never clears, and that queue is the one
        # transcripts use.
        "name": "capture-log-a-disconnected-record-is-queued-anyway",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if not self._wheelhouse_is_reachable():",
        "new": "        if False:",
        "expect": [
            "test_a_record_written_while_disconnected_is_not_forwarded",
            "test_the_records_dropped_while_disconnected_are_reported",
            "test_a_disconnected_drop_does_not_consume_the_window_budget",
        ],
    },
    {
        # Bounded but never silent: an outage that cost records must say so
        # on the way back up.
        "name": "capture-log-the-outage-is-never-reported",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if sum(self._disconnected_drops.values()) != (\n"
               "                self._reported_disconnected_total):\n"
               "            self._forward_disconnected_drops(record)",
        "new": "        if False:\n"
               "            self._forward_disconnected_drops(record)",
        "expect": [
            "test_the_records_dropped_while_disconnected_are_reported",
            "test_the_report_names_each_logger_that_lost_records",
            "test_a_lost_outage_report_is_recovered_by_the_next_one",
        ],
    },
    {
        # Dropping is safe only on a definite no. A forwarder that cannot
        # answer must still be forwarded to, or a broken accessor silences
        # the capture path -- the failure this handler exists to remove.
        "name": "capture-log-an-unanswerable-forwarder-is-treated-as-down",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Re-anchored 2026-09-05. a5b22fb9 lifted this body out of
        # WebSocketLogHandler._wheelhouse_is_reachable into the
        # module-level wheelhouse_can_receive_logs, which the handler and
        # AudioProcessor now share, so the indent changed from eight and
        # twelve spaces to four and eight. The getattr line is carried in
        # the pattern to keep it unmistakable.
        "old": "        return bool(getattr(forwarder, 'is_connected', True))\n"
               "    except Exception:\n"
               "        return True\n",
        "new": "        return bool(getattr(forwarder, 'is_connected', True))\n"
               "    except Exception:\n"
               "        return False\n",
        "expect": [
            "test_a_forwarder_that_cannot_answer_is_treated_as_connected",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.13. Put the capture-thread install back
        # outside the lifecycle lock, where it was before this fix.
        # _running is then True for a moment with no thread in place, and a
        # stop() arriving there joins nothing and returns while the thread
        # is launched behind it.
        "name": "winrt-start-installs-its-thread-after-the-lock",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": '            self._start_time = time.time()\n'
               '\n'
               '            # Start capture in background thread, under the same lock stop()\n'
               '            # holds while it takes that thread away. Releasing the lock here\n'
               '            # first left a window in which _running was already True and no\n'
               '            # thread existed yet: a stop() arriving in it cleared _running,\n'
               "            # found the previous cycle's reference or None, joined nothing,\n"
               '            # and returned as though the capture were down -- after which\n'
               '            # this thread went on to build a WinRT graph\n'
               '            # (wh-stt-load-metrics.2.1.13).\n'
               '            self._capture_thread = threading.Thread(\n'
               '                target=self._capture_loop,\n'
               '                args=(cycle,),\n'
               '                daemon=True,\n'
               '                name="WinRTAudioCapture"\n'
               '            )\n'
               '            self._capture_thread.start()\n',
        "new": '        self._start_time = time.time()\n'
               '\n'
               '        self._capture_thread = threading.Thread(\n'
               '            target=self._capture_loop,\n'
               '            args=(cycle,),\n'
               '            daemon=True,\n'
               '            name="WinRTAudioCapture"\n'
               '        )\n'
               '        self._capture_thread.start()\n',
        "expect": [
            "test_a_stop_waits_for_the_start_to_install_its_capture_thread",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.13, the mirror image. Read and clear the
        # thread reference after the lock, so a stop still joining the old
        # cycle can null the thread the next cycle just installed.
        "name": "winrt-stop-takes-the-thread-after-the-lock",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": "            capture_thread = self._capture_thread\n"
               "            self._capture_thread = None\n"
               "\n"
               "            # Inside the same transition, so no cycle can begin between the\n"
               "            # clear and the release. Clearing after the join left the queue\n"
               "            # unguarded for the length of a bounded 2.0s wait: a start()\n"
               "            # took the released lock, advanced the cycle, and installed a\n"
               "            # thread whose chunks passed both write guards, after which\n"
               "            # this clear erased audio the running cycle had captured. The\n"
               "            # loss was counted nowhere, because _drops moves only on\n"
               "            # queue.Full (wh-stt-load-metrics.2.1.15).\n"
               "            with self._q.mutex:\n"
               "                self._q.queue.clear()\n"
               "\n"
               "        # The join is deliberately outside the lock: the thread it waits on\n"
               "        # takes the same lock on its way out, so joining under it would\n"
               "        # deadlock until the timeout expired every single time.\n"
               "        if capture_thread:\n"
               "            capture_thread.join(timeout=2.0)\n",
        # The queue clear stays inside the lock, so this mutation still
        # isolates one change: where the thread reference is read.
        "new": "            with self._q.mutex:\n"
               "                self._q.queue.clear()\n"
               "\n"
               "        # The join is deliberately outside the lock: the thread it waits on\n"
               "        # takes the same lock on its way out, so joining under it would\n"
               "        # deadlock until the timeout expired every single time.\n"
               "        if self._capture_thread:\n"
               "            self._capture_thread.join(timeout=2.0)\n"
               "            self._capture_thread = None\n",
        "expect": [
            "test_a_stop_that_is_joining_does_not_take_the_next_cycles_thread",
        ],
    },
    {
        # Drop the join itself. The lock ordering is untouched, so the
        # window assertion still holds; what goes is stop()'s promise that
        # the capture thread is finished when it returns.
        "name": "winrt-stop-returns-without-joining",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": "        if capture_thread:\n"
               "            capture_thread.join(timeout=2.0)\n",
        "new": "        if capture_thread:\n"
               "            pass\n",
        "expect": [
            "test_a_stop_waits_for_the_start_to_install_its_capture_thread",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.15. Put the queue clear back after the
        # join, outside the lock. A start() that lands during that
        # bounded wait writes for its own cycle, and this clear erases
        # what it wrote.
        "name": "winrt-stop-clears-the-queue-after-its-join",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": "            # Inside the same transition, so no cycle can begin between the\n"
               "            # clear and the release. Clearing after the join left the queue\n"
               "            # unguarded for the length of a bounded 2.0s wait: a start()\n"
               "            # took the released lock, advanced the cycle, and installed a\n"
               "            # thread whose chunks passed both write guards, after which\n"
               "            # this clear erased audio the running cycle had captured. The\n"
               "            # loss was counted nowhere, because _drops moves only on\n"
               "            # queue.Full (wh-stt-load-metrics.2.1.15).\n"
               "            with self._q.mutex:\n"
               "                self._q.queue.clear()\n"
               "\n"
               "        # The join is deliberately outside the lock: the thread it waits on\n"
               "        # takes the same lock on its way out, so joining under it would\n"
               "        # deadlock until the timeout expired every single time.\n"
               "        if capture_thread:\n"
               "            capture_thread.join(timeout=2.0)\n",
        "new": "        # The join is deliberately outside the lock: the thread it waits on\n"
               "        # takes the same lock on its way out, so joining under it would\n"
               "        # deadlock until the timeout expired every single time.\n"
               "        if capture_thread:\n"
               "            capture_thread.join(timeout=2.0)\n"
               "\n"
               "        with self._q.mutex:\n"
               "            self._q.queue.clear()\n",
        "expect": [
            "test_a_stop_that_is_joining_cannot_erase_the_next_cycles_audio",
            "test_a_stop_cannot_clear_after_it_releases_the_lifecycle_lock",
        ],
    },
    {
        # The other direction: stop() keeps the lock but stops clearing
        # at all, so a restarted capture opens onto the previous
        # cycle's stale audio.
        "name": "winrt-stop-never-clears-the-queue",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": "            with self._q.mutex:\n"
               "                self._q.queue.clear()\n",
        "new": "            pass\n",
        "expect": [
            "test_stop_clears_queue_when_running",
            "test_a_stop_that_is_joining_cannot_erase_the_next_cycles_audio",
            "test_a_stop_cannot_clear_after_it_releases_the_lifecycle_lock",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.16. The move the .15 test cannot see:
        # dedent the clear out of the lock but leave it BEFORE the join.
        # It then runs before the join sets its event, so the restart
        # the .15 test drives lands after it and its chunk survives.
        # Only a test whose foothold is the release itself fails here.
        "name": "winrt-stop-clears-the-queue-after-the-lock",
        "service": SHARED,
        "test_file": CAPTURE_TESTS,
        "file": WINRT,
        "old": "            # Inside the same transition, so no cycle can begin between the\n"
               "            # clear and the release. Clearing after the join left the queue\n"
               "            # unguarded for the length of a bounded 2.0s wait: a start()\n"
               "            # took the released lock, advanced the cycle, and installed a\n"
               "            # thread whose chunks passed both write guards, after which\n"
               "            # this clear erased audio the running cycle had captured. The\n"
               "            # loss was counted nowhere, because _drops moves only on\n"
               "            # queue.Full (wh-stt-load-metrics.2.1.15).\n"
               "            with self._q.mutex:\n"
               "                self._q.queue.clear()\n"
               "\n",
        "new": "\n"
               "        with self._q.mutex:\n"
               "            self._q.queue.clear()\n"
               "\n",
        "expect": [
            "test_a_stop_cannot_clear_after_it_releases_the_lifecycle_lock",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.14. Spend a slot on every record the
        # handler was handed, whether or not it left. A few call sites with
        # unrenderable args then exhaust the window and the real capture
        # warnings are suppressed.
        "name": "capture-log-a-record-that-never-left-spends-a-slot",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if self._send_record(record):\n"
               "            window['forwarded'] += 1\n",
        "new": "        window['forwarded'] += 1\n"
               "        self._send_record(record)\n",
        "expect": [
            "test_a_record_that_could_not_be_formatted_leaves_the_budget_whole",
        ],
    },
    {
        # The other direction: report every send as a failure. Nothing ever
        # spends a slot, so the cap stops bounding a storm at all.
        "name": "capture-log-a-sent-record-spends-nothing",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "                trace_id=self.forwarder._current_trace_id,\n"
               "            )\n"
               "            return True\n",
        "new": "                trace_id=self.forwarder._current_trace_id,\n"
               "            )\n"
               "            return False\n",
        "expect": [
            "test_a_forwarded_record_still_spends_a_slot",
        ],
    },
    {
        # A send outcome is not a licence to swallow the failure. Dropping
        # handleError leaves a broken call site invisible everywhere.
        "name": "capture-log-a-format-failure-is-swallowed",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Re-anchored 2026-09-05. note_record_dropped(record) now sits
        # between the comment and this call (wh-capture-load-gaps.2.3), so
        # the pattern anchors on the call and the return. The mutation
        # still removes exactly handleError.
        "old": "            note_record_dropped(record)\n"
               "            self.handleError(record)\n"
               "            return False\n",
        "new": "            note_record_dropped(record)\n"
               "            return False\n",
        "expect": [
            "test_the_failure_is_still_reported_rather_than_swallowed",
            "test_the_plain_handler_reports_a_format_failure",
        ],
    },
    {
        # emit() keeps its own contract. Every other provider attaches the
        # plain handler, and logging.Handler.handle reads emit's answer as
        # nothing; growing a return value there is a change to a shared path
        # this bead has no reason to make.
        # Pattern refreshed by wh-forwarded-log-time-order: emit() now ends
        # with the same _send_record call, but the next method after it is
        # _drop_while_unreachable rather than _send_record. Same defect,
        # same catcher; only the anchor changed.
        "name": "capture-log-emit-grows-a-return-value",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        self._send_record(record)\n"
               "\n"
               "    def _drop_while_unreachable",
        "new": "        return self._send_record(record)\n"
               "\n"
               "    def _drop_while_unreachable",
        "expect": [
            "test_the_plain_handler_reports_a_format_failure",
        ],
    },
    {
        # The accessor the whole backlog bound reads. Answering yes always
        # disables the bound in silence.
        "name": "ws-forwarder-always-claims-a-connection",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        return self._ws_connected\n",
        "new": "        return True\n",
        "expect": [
            "test_a_forwarder_that_never_connected_is_not_connected",
        ],
    },

    # -- wh-forwarded-log-time-order defect 2: the queue keeps log frames --
    {
        # The whole split. Sorting every frame into the cleared pile is the
        # behaviour before this bead: a disconnect took the forwarded log
        # records with the stale transcripts, and the disconnect is often
        # the event the reader of wheelhouse.log is trying to explain.
        "name": "log-survival-the-clear-takes-the-log-frames-too",
        "service": SHARED,
        "test_file": SURVIVAL_TESTS,
        "file": WS_FORWARDER,
        # The kept.append line is part of the pattern on purpose: the send
        # failure branch tests the same type at deeper indentation, and the
        # shallower snippet is a SUBSTRING of that deeper line.
        "old": '                if msg_data.get("type") == "log":\n'
               "                    kept.append(msg_data)",
        "new": "                if False:\n"
               "                    kept.append(msg_data)",
        "expect": [
            "test_clear_queue_keeps_log_frames_in_order",
            "test_clear_queue_still_drops_transcripts_and_lifecycle",
            "test_a_failed_transcript_frame_drops_with_the_stale_clear",
        ],
    },
    {
        # The other half of the split. Keeping everything would send words
        # from before the outage to WheelHouse as if they were current
        # speech, which is the flood _clear_queue exists to stop.
        "name": "log-survival-the-clear-keeps-the-stale-transcripts",
        "service": SHARED,
        "test_file": SURVIVAL_TESTS,
        "file": WS_FORWARDER,
        "old": "                else:\n"
               "                    cleared += 1",
        "new": "                else:\n"
               "                    kept.append(msg_data)",
        "expect": [
            "test_clear_queue_still_drops_transcripts_and_lifecycle",
            "test_a_failed_transcript_frame_drops_with_the_stale_clear",
        ],
    },
    {
        # Kept is not enough; the order has to hold. Reversing is the
        # cheapest way to get frames that all survive and still read wrong.
        "name": "log-survival-the-kept-frames-go-back-reversed",
        "service": SHARED,
        "test_file": SURVIVAL_TESTS,
        "file": WS_FORWARDER,
        "old": "            for msg_data in kept:",
        "new": "            for msg_data in reversed(kept):",
        "expect": [
            "test_clear_queue_keeps_log_frames_in_order",
        ],
    },
    {
        # The head pending slot, unread. The frame survives in the slot and
        # is never sent, so the reconnect delivers everything except the one
        # record the outage cost.
        "name": "log-survival-the-pending-slot-is-never-read",
        "service": SHARED,
        "test_file": SURVIVAL_TESTS,
        "file": WS_FORWARDER,
        "old": "                        if self._pending_log is not None:",
        "new": "                        if False:",
        "expect": [
            "test_a_failed_log_frame_sends_first_on_reconnect",
        ],
    },
    {
        # The original defect, exactly: a failed log frame goes back on the
        # TAIL. It now survives the clear, so this one is purely about
        # order -- the frame arrives after everything queued behind it.
        "name": "log-survival-a-failed-log-frame-requeues-at-the-tail",
        "service": SHARED,
        "test_file": SURVIVAL_TESTS,
        "file": WS_FORWARDER,
        "old": "                                self._pending_log = msg_data",
        "new": "                                await self._queue.put(msg_data)",
        "expect": [
            "test_a_failed_log_frame_sends_first_on_reconnect",
        ],
    },

    # -- wh-forwarded-log-time-order criterion 4: the plain handler gate --
    {
        # The gate itself. Without it a provider that starts while
        # WheelHouse is down queues every record it writes into an unbounded
        # queue that a run of INITIAL connect failures never clears, and
        # sends the backlog ahead of the first live transcript.
        "name": "plain-handler-queues-while-wheelhouse-is-unreachable",
        "service": SHARED,
        "test_file": PLAIN_HANDLER_TESTS,
        "file": WS_FORWARDER,
        "old": "        if self._drop_while_unreachable(record):\n"
               "            return\n"
               "        self._report_disconnected_drops_if_due(record)\n"
               "        self._send_record(record)",
        "new": "        self._send_record(record)",
        "expect": [
            "test_a_record_written_while_unreachable_is_not_queued",
            "test_drops_are_counted_per_logger",
            "test_the_first_record_after_reconnect_reports_the_total",
        ],
    },
    {
        # PER LOGGER, not one total. A single counter still bounds the
        # backlog, but the report then cannot say where the loss was, which
        # is the half a reader needs.
        "name": "plain-handler-counts-every-drop-in-one-bucket",
        "service": SHARED,
        "test_file": PLAIN_HANDLER_TESTS,
        "file": WS_FORWARDER,
        "old": "            self._disconnected_drops[record.name] = (\n"
               "                self._disconnected_drops.get(record.name, 0) + 1)",
        "new": "            self._disconnected_drops['all'] = (\n"
               "                self._disconnected_drops.get('all', 0) + 1)",
        "expect": [
            "test_drops_are_counted_per_logger",
            "test_the_first_record_after_reconnect_reports_the_total",
        ],
    },
    {
        # The hook that lets the rate-limited subclass add its watermark
        # reset to the shared gate. Dropping the call silently reverts
        # wh-stt-load-metrics.2.1.9 while the gate still looks whole.
        "name": "plain-handler-the-drop-hook-is-never-called",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "            self._on_disconnected_drop()",
        "new": "            pass",
        "expect": [
            "test_a_disconnect_makes_the_next_window_report_the_count_again",
            "test_a_logger_that_falls_silent_is_reported_by_another",
        ],
    },
    {
        # Two handlers now write outage reports into one wheelhouse.log.
        # Losing the capture subclass's own subject makes them
        # indistinguishable to a reader counting capture records.
        "name": "plain-handler-the-capture-report-loses-its-subject",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "    DROP_REPORT_SUBJECT = 'capture-path records'",
        "new": "    DROP_REPORT_SUBJECT = 'forwarded log records'",
        # Not test_the_report_names_each_logger_that_lost_records: that one
        # reads the per-logger detail in the parentheses, which this
        # mutation leaves alone. The two below are the ones that count
        # capture-path records by name.
        "expect": [
            "test_a_lost_outage_report_is_recovered_by_the_next_one",
            "test_a_recovery_with_no_new_drops_does_not_repeat_the_report",
        ],
    },
    {
        # The budget is PER LOGGER. One shared budget lets a storm on
        # shared_audio.microphone suppress OverflowMonitor's summary, which
        # is the one record the load investigation needs.
        "name": "capture-log-one-budget-for-the-whole-tree",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Both keys, not just the lookup. Changing the lookup alone gives
        # every record a fresh window and so no budget at all, which four
        # unrelated tests catch; only changing the write as well produces the
        # single shared budget this mutation is named for.
        "old": "        window = self._windows.get(record.name)\n"
               "        if window is None or now - window['start'] >= self._window_seconds:\n"
               "            suppressed = 0 if window is None else window['suppressed']\n"
               "            window = {'start': now, 'forwarded': 0, 'suppressed': 0}\n"
               "            self._windows[record.name] = window\n",
        "new": "        window = self._windows.get('shared')\n"
               "        if window is None or now - window['start'] >= self._window_seconds:\n"
               "            suppressed = 0 if window is None else window['suppressed']\n"
               "            window = {'start': now, 'forwarded': 0, 'suppressed': 0}\n"
               "            self._windows['shared'] = window\n",
        "expect": [
            "test_the_overflow_summary_survives_a_microphone_storm",
        ],
    },
    {
        # The window never reopens, so the first five records of the run are
        # the only ones that ever reach the wheelhouse log.
        "name": "capture-log-the-window-never-reopens",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if window is None or now - window['start'] >= self._window_seconds:",
        "new": "        if window is None:",
        "expect": [
            "test_the_next_window_forwards_again",
        ],
    },
    {
        # No bound at all, which is the flood this handler exists to stop.
        "name": "capture-log-the-budget-never-runs-out",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if window['forwarded'] >= self._max_records_per_window:",
        "new": "        if False:",
        "expect": [
            "test_records_past_the_cap_are_not_forwarded",
        ],
    },
    {
        # A suppressed window that says nothing. Bounded and silent is the
        # one outcome this class refuses.
        "name": "capture-log-a-suppressed-window-says-nothing",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Refreshed, not deleted, for round 7: the report call moved
        # inside the all-loggers loop (wh-stt-load-metrics.2.1.9), so the
        # pattern names its new shape. The behaviour it protects is the same.
        "old": "                if running_total != self._reported_suppressed.get(name, 0):\n"
               "                    self._forward_suppression_count(record, name,\n"
               "                                                    running_total)",
        "new": "                if False:\n"
               "                    self._forward_suppression_count(record, name,\n"
               "                                                    running_total)",
        # Only the test that asserts the line is PRESENT can catch this.
        # test_a_window_that_suppressed_nothing_adds_no_line asserts an absent
        # line, which removing the report only makes more true.
        "expect": [
            "test_a_suppressed_window_says_how_many_it_dropped",
        ],
    },
    {
        # time.time() can jump backward on an NTP correction, which would
        # suppress the capture path for an unbounded period while frames
        # keep dropping.
        "name": "capture-log-the-window-uses-a-wall-clock",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "                 clock: Callable[[], float] = time.monotonic):",
        "new": "                 clock: Callable[[], float] = time.time):",
        "expect": [
            "test_the_default_clock_is_monotonic",
        ],
    },
    {
        # wh-stt-load-metrics.2.1.8. Clearing the drop counts when the report
        # is QUEUED loses them, because the report can still be lost after it
        # is queued. Since wh-forwarded-log-time-order defect 2 that loss is
        # process exit before delivery -- before the queue drains, or with the
        # frame in the pending slot that stop() never waits on -- not a
        # discard by _clear_queue, which now keeps log frames.
        "name": "capture-log-the-drop-counts-are-cleared-on-report",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        counts = dict(self._disconnected_drops)\n"
               "        total = sum(counts.values())\n"
               "        self._reported_disconnected_total = total",
        "new": "        counts = dict(self._disconnected_drops)\n"
               "        self._disconnected_drops = {}\n"
               "        total = sum(counts.values())\n"
               "        self._reported_disconnected_total = total",
        "expect": [
            "test_a_lost_outage_report_is_recovered_by_the_next_one",
        ],
    },
    {
        # Cumulative must not mean repeated. Comparing against a constant
        # instead of the watermark reports on every record after any outage.
        "name": "capture-log-the-outage-report-repeats-forever",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        if sum(self._disconnected_drops.values()) != (\n"
               "                self._reported_disconnected_total):",
        "new": "        if sum(self._disconnected_drops.values()) != (\n"
               "                0):",
        "expect": [
            "test_a_recovery_with_no_new_drops_does_not_repeat_the_report",
        ],
    },
    {
        # The watermark write itself. Without it the report repeats on every
        # record for the rest of the run.
        "name": "capture-log-the-outage-watermark-is-never-written",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        self._reported_disconnected_total = total\n",
        "new": "        pass\n",
        "expect": [
            "test_a_recovery_with_no_new_drops_does_not_repeat_the_report",
        ],
    },
    {
        # The suppression total must ACCUMULATE. Assigning the closing
        # window's count discards every earlier window, which is exactly what
        # a lost report needs the total to survive.
        "name": "capture-log-the-suppression-total-is-not-cumulative",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "                self._suppressed_totals[record.name] = (\n"
               "                    self._suppressed_totals.get(record.name, 0) + suppressed)",
        "new": "                self._suppressed_totals[record.name] = suppressed",
        "expect": [
            "test_a_lost_suppression_report_is_recovered_by_the_next_one",
        ],
    },
    {
        # Same repetition failure on the suppression side.
        "name": "capture-log-the-suppression-report-repeats-forever",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Refreshed for round 7: same comparison, new indentation and
        # key, because the loop now walks every logger.
        "old": "                if running_total != self._reported_suppressed.get(name, 0):",
        "new": "                if running_total != 0:",
        "expect": [
            "test_a_window_that_adds_no_suppression_does_not_repeat_the_report",
        ],
    },
    {
        # The suppression watermark write.
        "name": "capture-log-the-suppression-watermark-is-never-written",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        # Refreshed for round 7: the helper takes the logger name as an
        # argument now, because any logger's rollover reports the others.
        "old": "        self._reported_suppressed[logger_name] = suppressed\n",
        "new": "        pass\n",
        "expect": [
            "test_a_window_that_adds_no_suppression_does_not_repeat_the_report",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps criterion 1: _read_capture_stats says WHICH of
    # its three None cases fired. Before that change two of the three were
    # silent and the third logged at debug on the module logger, so the
    # 2026-08-30 loaded run lost capture readings with no cause in the log.
    # ---------------------------------------------------------------
    {
        # The missing-reader case silent again, which is how it shipped.
        # A provider that was never given a reader then looks exactly like
        # one whose reader is failing.
        "name": "capture-reason-a-missing-reader-says-nothing",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": '            self._note_capture_unreadable("no-reader")\n',
        "new": '            pass\n',
        "expect": [
            "test_a_missing_reader_names_itself",
            "test_a_missing_reader_says_so_once_for_the_whole_process",
            "test_the_reason_is_on_the_loadmetrics_logger",
            "test_the_reason_line_is_not_the_per_utterance_load_line",
        ],
    },
    {
        # The non-dict case folded back into the silent one-liner it was.
        "name": "capture-reason-a-non-dict-answer-says-nothing",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": ("        if not isinstance(stats, dict):\n"
                "            self._note_capture_unreadable(\n"
                '                "non-dict", type(stats).__name__)\n'
                "            return None\n"
                "        return stats\n"),
        "new": "        return stats if isinstance(stats, dict) else None\n",
        "expect": [
            "test_a_non_dict_reader_names_itself",
            "test_a_second_reason_is_named_in_its_own_right",
        ],
    },
    {
        # The exception text dropped. Which exception it is decides where
        # to look next, so the line without it names a reason and answers
        # nothing.
        "name": "capture-reason-the-exception-text-is-dropped",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": '                "reader-raised", f"{type(e).__name__}: {e}")\n',
        "new": '                "reader-raised")\n',
        "expect": ["test_a_raising_reader_names_itself"],
    },
    {
        # The line moved back to the module logger. This is the exact
        # defect the bead was filed for: Parakeet sets
        # logging.getLogger("shared_stt").propagate = False with no
        # handler, so a record there reaches no log at all.
        "name": "capture-reason-reported-on-the-silenced-module-logger",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        # Re-anchored 2026-09-05. The call now passes the delivery
        # receipt as a third argument (wh-capture-load-gaps.2.3), so the
        # second line ends with a comma and the old two-line pattern no
        # longer matched. The first two lines are all this mutation needs.
        "old": ("        load_metrics_logger.info(\n"
                '            "[load-diag-capture] unavailable: %s%s", counts, suffix,\n'),
        "new": ("        logger.info(\n"
                '            "[load-diag-capture] unavailable: %s%s", counts, suffix,\n'),
        "expect": [
            "test_the_reason_is_on_the_loadmetrics_logger",
            "test_a_missing_reader_names_itself",
        ],
    },
    {
        # The rate bound removed, by making the window zero rather than by
        # deleting the guard: _read_capture_stats runs on two per-chunk
        # paths, about 33 times a second at 30 ms chunks, so an unbounded
        # line buries the band it exists to explain.
        "name": "capture-reason-the-rate-bound-is-gone",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "                < CAPTURE_REASON_LOG_INTERVAL_S):\n",
        "new": "                < 0.0):\n",
        "expect": [
            "test_a_burst_of_one_reason_yields_one_line",
            "test_the_constructor_reading_starts_the_rate_window",
        ],
    },
    {
        # The counts never cleared, so every line carries the lifetime
        # total instead of what this window suppressed -- the same
        # lifetime-total-as-a-delta mistake several older mutations here
        # attack on the capture numbers.
        "name": "capture-reason-counts-are-never-cleared",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._capture_reason_counts.clear()\n",
        "new": "        pass\n",
        "expect": [
            "test_the_next_line_counts_what_it_suppressed",
            "test_a_second_reason_is_named_in_its_own_right",
        ],
    },
    {
        # The prefix back to the per-utterance one. This is not cosmetic:
        # _load_line() asserts a run has exactly one [load-diag] line, so
        # the shared prefix makes a reason line read as a measurement.
        "name": "capture-reason-wears-the-per-utterance-load-diag-prefix",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        # Re-anchored at f3d49a46: that fix gave the call a third
        # argument, so the line no longer ends the call with ")".
        "old": ('            "[load-diag-capture] unavailable: %s%s"'
                ', counts, suffix,\n'),
        "new": ('            "[load-diag] capture-stats unavailable: %s%s"'
                ', counts, suffix,\n'),
        "expect": [
            "test_the_reason_line_is_not_the_per_utterance_load_line",
            "test_a_missing_reader_names_itself",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps criterion 1b: the same three None cases in
    # CaptureLoadReporter._read, the reader behind the window lines and the
    # capture-outage event. A window whose readings all failed used to be
    # completely silent, which is the 13-of-83 gap of the 2026-08-30 run.
    # ---------------------------------------------------------------
    {
        "name": "window-reason-a-missing-reader-says-nothing",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '            self._note_unreadable("no-reader")\n',
        "new": '            pass\n',
        "expect": [
            "test_a_window_with_no_reader_names_it",
            "test_a_missing_reader_names_itself_in_one_window_only",
            "test_the_reason_speaks_where_the_window_line_cannot",
        ],
    },
    {
        "name": "window-reason-a-non-dict-answer-says-nothing",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": ("        if not isinstance(stats, dict):\n"
                '            self._note_unreadable("non-dict", type(stats).__name__)\n'
                "            return None\n"
                "        return stats\n"),
        "new": "        return stats if isinstance(stats, dict) else None\n",
        "expect": [
            "test_a_window_given_a_non_dict_names_it",
            "test_two_reasons_in_one_window_are_both_named",
        ],
    },
    {
        "name": "window-reason-the-exception-text-is-dropped",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": ('            self._note_unreadable("reader-raised",\n'
                '                                  f"{type(e).__name__}: {e}")\n'),
        "new": '            self._note_unreadable("reader-raised")\n',
        "expect": ["test_a_window_whose_reader_raised_names_it"],
    },
    {
        # The counts never cleared, so every window reports the lifetime
        # total instead of what that window saw -- the same
        # lifetime-total-as-a-delta mistake the capture deltas guard against.
        "name": "window-reason-counts-are-never-cleared",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._unreadable_counts = {}\n",
        "new": "        pass\n",
        "expect": ["test_each_window_counts_only_its_own_readings"],
    },
    {
        # Built and then dropped on the floor: the window goes back to being
        # silent, which is the defect itself.
        "name": "window-reason-is-never-returned-to-the-caller",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": ("        reason = self._unreadable_line()\n"
                "        if reason:\n"
                "            lines.append(reason)\n"),
        "new": "        self._unreadable_line()\n",
        "expect": [
            "test_a_window_with_no_reader_names_it",
            "test_a_raising_reader_produces_no_summary_and_no_exception",
            "test_two_reasons_in_one_window_are_both_named",
        ],
    },
    {
        # The prefix back to the debug text it replaced, which collides with
        # what the test helpers and logparse.py treat as a window line.
        "name": "window-reason-wears-the-load-diag-prefix",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": '        return f"[load-diag-capture] unavailable: {counts}{suffix}"\n',
        "new": '        return f"[load-diag] capture stats unavailable: {counts}{suffix}"\n',
        "expect": [
            "test_the_reason_speaks_where_the_window_line_cannot",
            "test_a_window_with_no_reader_names_it",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps.1.1: a missing reader is a fact about how the
    # provider was built, not a reading that failed. Reported once per
    # instance in both readers. Before this, distil_medium_en -- which
    # ships with no capture reader on purpose -- wrote the line every
    # five seconds for the life of the process.
    # ---------------------------------------------------------------
    {
        # The guard read but never obeyed, which is the flood itself.
        "name": "capture-reason-a-missing-reader-is-said-again-and-again",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            if self._capture_no_reader_reported:\n",
        "new": "            if False:\n",
        # test_a_missing_reader_names_itself was an expected catcher until
        # wh-capture-load-gaps.2.1. It builds the processor inside its own
        # capture block and reads once; the constructor no longer reports a
        # missing reader, so it now sees exactly one line whether the latch
        # holds or not. The replacement takes 200 readings after the
        # handler is attached, which a broken latch turns into 200 lines.
        "expect": [
            "test_a_missing_reader_says_so_once_for_the_whole_process",
            "test_the_line_is_still_said_only_once_once_the_handler_is_there",
        ],
    },
    {
        # The latch never latches. Same flood, a different line, and the
        # one a careless edit is likelier to produce.
        "name": "capture-reason-the-missing-reader-latch-never-closes",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            self._capture_no_reader_reported = True\n",
        "new": "            pass\n",
        # Same masking as the mutation above, same replacement catcher.
        "expect": [
            "test_a_missing_reader_says_so_once_for_the_whole_process",
            "test_the_line_is_still_said_only_once_once_the_handler_is_there",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps.2.1: the one no-reader report must be spent
    # where the provider's log handler can hear it. Both providers build
    # the processor in their own __init__ and attach that handler later,
    # in start(), so a report made from the constructor reaches the
    # provider's stdout and nothing else -- and the latch then suppresses
    # every later reading, leaving wheelhouse.log with no line at all.
    # ---------------------------------------------------------------
    {
        # The pre-handler guard read but never obeyed. The constructor
        # spends the one allowed line on a logger nothing is listening to.
        "name": "capture-reason-the-constructor-spends-the-one-no-reader-line",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        # Re-anchored 2026-09-05. The fix for wh-capture-load-gaps.2.2
        # moved this test out of _note_capture_unreadable and into
        # _a_no_reader_line_would_be_heard, where it sits at eight spaces
        # rather than twelve.
        "old": ("        if self._capture_read_before_provider_handler:\n"
                "            return False\n"),
        "new": ("        if False:\n"
                "            return False\n"),
        "expect": [
            "test_the_line_reaches_a_handler_attached_after_construction",
            "test_the_line_is_still_said_only_once_once_the_handler_is_there",
        ],
    },
    {
        # The flag is never cleared, so every reading looks like the
        # constructor's and the line is never said at all. This is the
        # opposite failure to the one above and the likelier typo.
        "name": "capture-reason-the-pre-handler-flag-is-never-cleared",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        self._capture_read_before_provider_handler = False\n",
        "new": "        pass\n",
        "expect": [
            "test_the_line_reaches_a_handler_attached_after_construction",
            "test_the_line_is_still_said_only_once_once_the_handler_is_there",
            "test_a_missing_reader_names_itself",
            "test_a_missing_reader_says_so_once_for_the_whole_process",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps.2.2. Attached is not the same question as able
    # to deliver. Both providers attach the handler and then keep going
    # without waiting for the WebSocket, so a report spent while
    # is_connected is still False is dropped by the handler and the latch
    # closes on a line nobody received.
    # ---------------------------------------------------------------
    {
        # The delivery check is gone entirely: the guard becomes the
        # once-only latch it was before either finding was filed.
        "name": "capture-reason-the-delivery-check-is-gone",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            if not self._a_no_reader_line_would_be_heard():\n",
        "new": "            if False:\n",
        "expect": [
            "test_a_reading_taken_before_the_connection_says_nothing",
            "test_the_deferred_readings_are_not_counted_into_the_line",
            "test_the_line_reaches_a_handler_attached_after_construction",
        ],
    },
    {
        # Handler attachment is still checked, but reachability is not:
        # exactly the state 1c9f8ac9 left behind, which is the defect
        # codex found in round 2.
        #
        # The two catchers this mutation had until f3d49a46
        # (test_a_reading_taken_before_the_connection_says_nothing and
        # test_the_deferred_readings_are_not_counted_into_the_line) no
        # longer fail under it, and the full sweep at f3d49a46 reported
        # it as a survivor for that reason. They are not stale and they
        # are not wrong: f3d49a46 added the delivery receipt, so the
        # written line is dropped by the handler, the latch stays open,
        # the counts are cleared, and a later reading delivers exactly
        # the one no-reader=1 line both tests assert. What the preflight
        # still owns by itself is that nothing is WRITTEN at all while
        # the WebSocket is down, so the catcher below watches the logger
        # instead of the forwarder.
        "name": "capture-reason-a-disconnected-wheelhouse-is-written-to-anyway",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        return wheelhouse_can_receive_logs(self.forwarder)\n",
        "new": "        return True\n",
        "expect": [
            "test_nothing_is_written_at_all_while_wheelhouse_cannot_be_reached",
        ],
    },
    {
        # The fail-open default flipped. A forwarder that cannot answer
        # now reads as unreachable, so the one line is withheld forever --
        # the silence these handlers exist to remove.
        "name": "capture-reason-a-silent-forwarder-is-read-as-unreachable",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": WS_FORWARDER,
        "old": "        return bool(getattr(forwarder, 'is_connected', True))\n",
        "new": "        return bool(getattr(forwarder, 'is_connected', False))\n",
        "expect": [
            "test_a_forwarder_that_cannot_answer_is_treated_as_reachable",
        ],
    },
    {
        # The handler stops asking the shared answer. The processor and
        # the handler would then disagree about the same forwarder, which
        # is the drift that lifting the function out was meant to prevent.
        "name": "capture-log-the-handler-stops-asking-the-shared-answer",
        "service": SHARED,
        "test_file": FORWARDING_TESTS,
        "file": WS_FORWARDER,
        "old": "        return wheelhouse_can_receive_logs(self.forwarder)\n",
        "new": "        return True\n",
        "expect": [
            "test_a_record_written_while_disconnected_is_not_forwarded",
            "test_the_records_dropped_while_disconnected_are_reported",
        ],
    },
    {
        # The latch closes where it used to, on the preflight answer,
        # before the handler has said anything. That is exactly the state
        # a5b22fb9 left behind and the defect codex found in round 3: the
        # connection can close between the two reads.
        "name": "capture-reason-the-latch-is-spent-before-the-handover",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "            receipt = new_delivery_receipt()\n",
        "new": ("            receipt = new_delivery_receipt()\n"
                "            self._capture_no_reader_reported = True\n"),
        "expect": [
            "test_a_connection_lost_after_the_check_does_not_spend_the_line",
            "test_a_send_that_raises_leaves_the_line_to_be_said_again",
        ],
    },
    {
        # The receipt is read but its answer is ignored, so a record every
        # handler refused still spends the one report.
        "name": "capture-reason-a-dropped-record-still-spends-the-line",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "        if receipt is not None and not receipt['dropped']:\n",
        "new": "        if receipt is not None:\n",
        "expect": [
            "test_a_connection_lost_after_the_check_does_not_spend_the_line",
            "test_a_send_that_raises_leaves_the_line_to_be_said_again",
        ],
    },
    {
        # The receipt starts already marked, so no delivery can ever spend
        # the report and the line repeats every window -- the flood
        # wh-capture-load-gaps.1.1 removed, reached from the other side.
        "name": "capture-reason-a-delivered-line-never-spends-the-latch",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": WS_FORWARDER,
        "old": "    return {'dropped': False}\n",
        "new": "    return {'dropped': True}\n",
        "expect": [
            "test_a_missing_reader_says_so_once_for_the_whole_process",
            "test_the_line_is_still_said_only_once_after_the_connection",
            "test_a_later_outage_does_not_reopen_the_delivered_line",
        ],
    },
    {
        # The receipt never reaches the handler, so nothing can mark it and
        # every record reads as delivered.
        "name": "capture-reason-the-receipt-never-reaches-the-handler",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": AUDIO_PROCESSOR,
        "old": "                   else {DELIVERY_RECEIPT_ATTR: receipt}))\n",
        "new": "                   else None))\n",
        "expect": [
            "test_a_connection_lost_after_the_check_does_not_spend_the_line",
            "test_a_send_that_raises_leaves_the_line_to_be_said_again",
        ],
    },
    {
        # An unreachable WheelHouse drops the record and says nothing about
        # it, so the writer spends its one report on a record nobody got.
        # The line above is carried because the same call appears at the
        # same indent on three paths in this file.
        "name": "capture-log-a-dropped-record-leaves-no-mark",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": WS_FORWARDER,
        "old": ("                self._disconnected_drops.get(record.name, 0)"
                " + 1)\n"
                "            note_record_dropped(record)\n"),
        "new": ("                self._disconnected_drops.get(record.name, 0)"
                " + 1)\n"),
        "expect": [
            "test_a_connection_lost_after_the_check_does_not_spend_the_line",
        ],
    },
    {
        # A send that raised reached nobody either. This is the case no
        # look at the connection can see, because the connection was up.
        "name": "capture-log-a-failed-send-leaves-no-mark",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": WS_FORWARDER,
        "old": ("            # Don't let logging errors crash the "
                "application\n"
                "            note_record_dropped(record)\n"),
        "new": ("            # Don't let logging errors crash the "
                "application\n"),
        "expect": [
            "test_a_send_that_raises_leaves_the_line_to_be_said_again",
        ],
    },
    {
        # The rate limit is the third way a handler ends without handing
        # the record over. It cannot carry the no-reader line today, but an
        # unmarked receipt must mean no handler reported a failure.
        "name": "capture-log-a-suppressed-record-leaves-no-mark",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": WS_FORWARDER,
        "old": ("            window['suppressed'] += 1\n"
                "            note_record_dropped(record)\n"),
        "new": "            window['suppressed'] += 1\n",
        "expect": [
            "test_a_suppressed_record_is_marked_dropped_too",
        ],
    },
    {
        # The document stops saying WHERE the one line appears. An
        # operator who checks the top of the log then reports it absent,
        # which is the exact confusion this finding was about.
        "name": "document-does-not-say-where-the-no-reader-line-appears",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": ("  process launches, so look for it below the startup lines "
                "rather than at\n  the very top of the log."),
        "new": "  process launches.",
        "expect": [
            "test_the_reading_guide_has_a_bullet_for_the_reason_line",
        ],
    },
    {
        "name": "window-reason-a-missing-reader-is-said-in-every-window",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "            if self._no_reader_reported:\n",
        "new": "            if False:\n",
        "expect": [
            "test_a_missing_reader_names_itself_in_one_window_only",
            "test_a_window_with_no_reader_names_it",
        ],
    },
    {
        "name": "window-reason-the-missing-reader-latch-never-closes",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": DIAGNOSTICS,
        "old": "            self._no_reader_reported = True\n",
        "new": "            pass\n",
        "expect": [
            "test_a_missing_reader_names_itself_in_one_window_only",
            "test_a_window_with_no_reader_names_it",
        ],
    },
    # ---------------------------------------------------------------
    # wh-capture-load-gaps.1.2: the operator's reading of the new line.
    # The document is what David runs the loaded procedure from, so a
    # line the guide does not read is a line the run cannot report.
    # ---------------------------------------------------------------
    {
        # The bullet gone, which is how the branch shipped it: the line
        # appears in the log with nothing anywhere to read it by.
        "name": "document-does-not-read-the-unreadable-reason-line",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "- **`[load-diag-capture] unavailable:` says why a reading failed.**",
        "new": "- **Some other line says why a reading failed.**",
        "expect": [
            "test_the_reading_guide_has_a_bullet_for_the_reason_line",
        ],
    },
    {
        # The once-per-process sentence dropped. An operator then reads a
        # single no-reader line as one failure at one moment, when it is a
        # permanent fact about how that provider was built.
        "name": "document-does-not-say-no-reader-is-said-once",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        "old": "  `no-reader` is **printed once** for the life of the process and then\n",
        "new": "  `no-reader` is reported for the life of the process and then\n",
        "expect": [
            "test_the_reading_guide_has_a_bullet_for_the_reason_line",
        ],
    },
    {
        # One reason word renamed in the prose, so the guide reads a word
        # the line never prints and says nothing about one it does. This
        # is the drift the field-coverage tests exist for, and it is what a
        # rename in the code without a document edit looks like.
        "name": "document-reads-a-reason-word-the-line-never-prints",
        "service": SHARED,
        "test_file": SHARED_TESTS,
        "file": PROCEDURE_DOC,
        # Both occurrences: the bullet names `non-dict` in the list of
        # reasons and again in the sentence about `(last: ...)`, so renaming
        # one leaves the other for the coverage test to find and the
        # mutation survives while looking like a real rename.
        "old": "  `non-dict` means the reader answered with something that is not a set of\n"
               "  readings. Each is followed by how many readings failed that way since\n"
               "  the previous line -- a count for this line, not a running total.\n"
               "  `(last: ...)` carries the newest detail behind those counts: the\n"
               "  exception type and message for `reader-raised`, the type that came back\n"
               "  for `non-dict`, and nothing at all for `no-reader`, which has no detail\n"
               "  to give (wh-capture-load-gaps).\n",
        "new": "  `bad-answer` means the reader answered with something that is not a set of\n"
               "  readings. Each is followed by how many readings failed that way since\n"
               "  the previous line -- a count for this line, not a running total.\n"
               "  `(last: ...)` carries the newest detail behind those counts: the\n"
               "  exception type and message for `reader-raised`, the type that came back\n"
               "  for `bad-answer`, and nothing at all for `no-reader`, which has no detail\n"
               "  to give (wh-capture-load-gaps).\n",
        "expect": [
            "test_every_reason_the_line_prints_is_read_in_the_document",
        ],
    },
    # -- The Google per-utterance line (wh-stt-load-metrics.4) -----------
    {
        # No answer at either end reported as a clean 0. That is the
        # reading that rules capture loss out, from a run that measured
        # nothing at all.
        "name": "an-unmeasured-capture-counter-reads-as-zero",
        "service": SHARED,
        "test_file": UTTERANCE_LINE_TESTS,
        "file": DIAGNOSTICS,
        "old": "        return dict.fromkeys(CAPTURE_DELTA_KEYS, NOT_REPORTED)",
        "new": "        return dict.fromkeys(CAPTURE_DELTA_KEYS, 0)",
        "expect": ["test_no_reading_at_either_end_answers_nothing"],
    },
    {
        # The utterance window never reopened. Every line then reports the
        # stalls of the whole session so far, so each utterance carries the
        # ones before it.
        "name": "the-utterance-stall-window-is-never-opened",
        "service": SHARED,
        "test_file": UTTERANCE_LINE_TESTS,
        "file": DIAGNOSTICS,
        "old": "            self._stall_tracker.snapshot_and_reset_utterance()",
        "new": "            pass",
        "expect": ["test_the_stall_fields_come_from_the_utterance_window"],
    },
    {
        # A diagnostic allowed to end the loop it observes. The capture
        # reader is the provider's own, and a device that goes away raises.
        "name": "a-capture-reader-failure-stops-the-consumer-loop",
        "service": SHARED,
        "test_file": UTTERANCE_LINE_TESTS,
        "file": DIAGNOSTICS,
        "old": "        except Exception:\n            return None\n"
               "        return stats if isinstance(stats, dict) else None",
        "new": "        except Exception:\n            raise\n"
               "        return stats if isinstance(stats, dict) else None",
        "expect": [
            "test_a_capture_reader_that_raises_costs_only_the_capture_fields"],
    },
    {
        # An engine timing invented for a provider that runs no engine.
        # engine_ratio=0.00 is the reading that rules inference lag out,
        # and no Google run has ever measured it.
        "name": "the-google-line-invents-an-engine-ratio",
        "service": SHARED,
        "test_file": UTTERANCE_LINE_TESTS,
        "file": DIAGNOSTICS,
        "old": "            f'engine_ratio={NOT_REPORTED}')",
        "new": "            f'engine_ratio=0.00')",
        "expect": ["test_the_engine_fields_are_never_numbers"],
    },
    # -- wh-stt-load-metrics.4: the Google provider's wiring -------------
    # Every mutation below leaves a server that starts, transcribes, and
    # writes a [load-diag] line. What it loses is the line's meaning: a
    # window that never opened, a depth read twice, a kind that is always
    # GOOGLE_FINAL. That is the failure this bead exists to prevent.
    {
        "name": "google-window-never-opened",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                self.load_metrics.start()",
        "new": "                pass",
        "expect": ["test_a_new_utterance_opens_the_window"],
    },
    {
        # The numbers are collected and never printed.
        "name": "google-line-never-logged",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for .4.1.1: the write moved into the drain,
        # which reads the queued pair rather than the live fields.
        "old": "                logger.info(self.load_metrics.finish("
               "utterance_id, reason))",
        "new": "                self.load_metrics.finish("
               "utterance_id, reason)",
        "expect": ["test_finalizing_reads_the_window_and_logs_the_line"],
    },
    {
        # A duplicate doubles every utterance in a parsed run, and the
        # judge's ratios and worst-case figures are computed per line.
        "name": "google-line-logged-twice",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for .4.1.1.
        "old": "                logger.info(self.load_metrics.finish("
               "utterance_id, reason))",
        "new": "                line = self.load_metrics.finish("
               "utterance_id, reason)\n"
               "                logger.info(line)\n"
               "                logger.info(line)",
        "expect": ["test_the_line_is_written_once"],
    },
    {
        # kind= is how a NO_TEXT_TIMEOUT utterance is told from a clean
        # one, which is the whole point of joining a stall to an utterance.
        "name": "google-line-always-reports-a-clean-ending",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for .4.1.1: the reason travels through the queue.
        "old": "self.load_metrics.finish(utterance_id, reason))",
        "new": "self.load_metrics.finish(utterance_id, 'GOOGLE_FINAL'))",
        "expect": ["test_the_reason_is_the_kind_the_line_reports"],
    },
    {
        # A line on a stale finalization reports an utterance that did not
        # just end, and takes the next one's window with it.
        "name": "google-line-written-on-a-stale-finalization",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if self.state != UtteranceState.ACTIVE:\n"
               "            if self.config.debug.log_lifecycle:\n"
               '                logger.info(f"[fsm] Stale finalization '
               'trigger ignored for utt_id={self.current_utterance_id} '
               '(state={self.state.name})")',
        "new": "        if self.load_metrics:\n"
               "            self.load_metrics.finish(\n"
               "                self.current_utterance_id, reason)\n"
               "        if self.state != UtteranceState.ACTIVE:\n"
               "            if self.config.debug.log_lifecycle:\n"
               '                logger.info(f"[fsm] Stale finalization '
               'trigger ignored for utt_id={self.current_utterance_id} '
               '(state={self.state.name})")',
        "expect": ["test_a_stale_finalization_writes_no_line"],
    },
    {
        # The one-read promise. Two reads disagree most under exactly the
        # load these lines exist to measure.
        "name": "google-queue-read-a-second-time-for-the-line",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        utterance_load.sample(depth)",
        "new": "        utterance_load.sample(mic.get_queue_size())",
        "expect": ["test_the_queue_is_read_once_for_both"],
    },
    {
        # q_max, q_mean and q_n all read 0 for every utterance, which is
        # the reading that falsely rules a full capture queue out.
        "name": "google-line-never-sampled",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    if utterance_load is not None:\n"
               "        utterance_load.sample(depth)",
        "new": "    if utterance_load is not None:\n"
               "        pass",
        "expect": ["test_the_depth_reaches_the_line_and_the_tracker"],
    },
    {
        # The periodic [stall] line disappears from the log while the
        # tracker goes on counting: the regression the new line could
        # cause in the measurement that already worked.
        "name": "google-stall-message-swallowed",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for G1a: the tracker is recorded here only when
        # no reporter is running, so the line sits one level in.
        "old": "        stall_msg = stall_tracker.record(depth)\n"
               "        raw_lines = [stall_msg] if stall_msg else []",
        "new": "        stall_tracker.record(depth)\n"
               "        raw_lines = []",
        "expect": ["test_the_stall_message_is_passed_back"],
    },
    {
        # A diagnostic that can end an utterance. ValueError rather than a
        # deleted try, so the mutant still parses and still runs.
        "name": "google-window-failure-stops-the-utterance",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                self.load_metrics.start()\n"
               "            except Exception as e:",
        "new": "                self.load_metrics.start()\n"
               "            except ValueError as e:",
        "expect": ["test_a_window_that_cannot_open_does_not_end_the_utterance"],
    },
    {
        "name": "google-line-failure-stops-the-utterance",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for .4.1.1: the guard now sits in the drain.
        # Narrowed rather than deleted, so this stays distinct from
        # a-drain-failure-breaks-the-iteration, which removes it.
        "old": "                logger.info(self.load_metrics.finish("
               "utterance_id, reason))\n"
               "            except Exception as e:",
        "new": "                logger.info(self.load_metrics.finish("
               "utterance_id, reason))\n"
               "            except ValueError as e:",
        "expect": [
            "test_a_line_writer_that_raises_does_not_end_the_utterance"],
    },
    {
        # The construction inside main(), which no test can drive. These
        # three are caught by the source-presence tests, and they are the
        # reason those tests exist: an unwired reporter prints a complete
        # line whose every capture number reads n/a.
        "name": "google-loop-builds-no-reporter",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    utterance_load = UtteranceLoadMetrics(\n"
               "        capture_stats=mic.get_stats, "
               "stall_tracker=stall_tracker)",
        "new": "    utterance_load = None",
        "expect": ["test_the_loop_builds_one_reporter",
                   "test_the_reporter_reads_the_provider_capture_counters"],
    },
    {
        "name": "google-reporter-gets-no-capture-reader",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        capture_stats=mic.get_stats, stall_tracker=stall_tracker)",
        "new": "        capture_stats=None, stall_tracker=stall_tracker)",
        "expect": ["test_the_reporter_reads_the_provider_capture_counters"],
    },
    {
        # The load reporter's own capture reader. Named apart from
        # google-reporter-gets-no-capture-reader above, which breaks the
        # same argument on the UtteranceLoadMetrics construction.
        "name": "google-load-reporter-gets-no-capture-reader",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            capture_stats=mic.get_stats,",
        "new": "            capture_stats=None,",
        "expect": [
            "test_the_load_reporter_reads_the_provider_capture_counters"],
    },
    {
        "name": "google-manager-never-gets-the-reporter",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                                     load_metrics=utterance_load)",
        "new": "                                     load_metrics=None)",
        "expect": ["test_the_manager_is_given_the_reporter"],
    },
    {
        # The pre-change call site, restored. The line stops being fed and
        # the stall tracker keeps working, so only the source count sees it.
        "name": "google-loop-reads-the-queue-outside-the-helper",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for G1a: the call site now passes the reporter.
        "old": "            for stall_line in sample_consumer_iteration(\n"
               "                    mic, stall_tracker, utterance_load, segments,\n"
               "                    reporter=load_reporter):\n"
               "                logger.info(stall_line)",
        "new": "            stall_msg = stall_tracker.record(mic.get_queue_size())\n"
               "            if stall_msg:\n"
               "                logger.info(stall_msg)",
        "expect": [
            "test_the_loop_takes_its_queue_depth_through_the_one_helper"],
    },
    # -- wh-stt-load-metrics.4 G3: which call the loop waited on ---------
    # The stall tracker says a stall happened. These say where the time
    # went, and the two answers -- blocked in a call, or not running at
    # all -- need opposite fixes. A mutation here leaves a loop that
    # still reports its stalls and points the reader at the wrong one.
    {
        # The subtle one. Reading the breakdown after the reopen reports
        # an iteration that has not happened yet, so every stall looks
        # like a loop that stalled on nothing.
        "name": "segments-read-after-the-iteration-is-reopened",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for G1a: the breakdown is now placed by matching
        # the stall prefix among the lines the reporter returns.
        "old": "    for line in raw_lines:\n"
               "        lines.append(line)\n"
               "        if segments is not None and line.startswith(STALL_PREFIX):\n"
               "            lines.append(segments.report())\n"
               "    if segments is not None:\n"
               "        segments.start()",
        "new": "    if segments is not None:\n"
               "        segments.start()\n"
               "    for line in raw_lines:\n"
               "        lines.append(line)\n"
               "        if segments is not None and line.startswith(STALL_PREFIX):\n"
               "            lines.append(segments.report())",
        "expect": ["test_the_breakdown_describes_the_iteration_that_stalled"],
    },
    {
        # Never reopened: the accumulators run on from the last stall, so
        # the next breakdown is the sum of every iteration since.
        "name": "segments-never-reopened",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    if segments is not None:\n"
               "        segments.start()\n"
               "    return lines",
        "new": "    if segments is not None:\n"
               "        pass\n"
               "    return lines",
        "expect": ["test_every_iteration_is_opened_even_when_none_stalled"],
    },
    {
        "name": "segments-breakdown-never-logged",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for G1a.
        "old": "        if segments is not None and line.startswith(STALL_PREFIX):\n"
               "            lines.append(segments.report())",
        "new": "        if segments is not None and line.startswith(STALL_PREFIX):\n"
               "            pass",
        "expect": ["test_the_breakdown_follows_the_stall_message"],
    },
    {
        # A breakdown on every iteration is twenty lines a second, which
        # buries the run it was meant to explain.
        "name": "segments-breakdown-on-every-iteration",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        # Refreshed for G1a.
        "old": "    lines = []\n"
               "    for line in raw_lines:",
        "new": "    lines = []\n"
               "    if segments is not None:\n"
               "        lines.append(segments.report())\n"
               "    for line in raw_lines:",
        "expect": ["test_a_quiet_iteration_says_nothing",
                   "test_a_quiet_iteration_is_not_asked_for_a_breakdown"],
    },
    {
        # The prime suspect on a Google run: the send back-pressuring
        # when the streamer's audio queue fills. Untimed, its cost lands
        # in unaccounted and reads as CPU starvation instead.
        "name": "google-send-not-timed",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                    with segments.timing('send'):\n"
               "                        for chunk in valid_chunks:",
        "new": "                    if True:\n"
               "                        for chunk in valid_chunks:",
        "expect": ["test_the_send_to_google_is_timed"],
    },
    {
        "name": "google-mic-read-not-timed",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            with segments.timing('mic_read'):\n"
               "                audio_frame = mic.read(timeout=0.05)",
        "new": "            if True:\n"
               "                audio_frame = mic.read(timeout=0.05)",
        "expect": ["test_the_microphone_read_is_timed"],
    },
    {
        "name": "google-response-drain-not-timed",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            segments.add('responses',\n"
               "                         (time.perf_counter() - "
               "responses_started) * 1000)",
        "new": "            pass",
        "expect": ["test_the_response_drain_is_timed"],
    },
    {
        # Back behind the debug flag. A stall that only happens under
        # load is not one anybody reproduces with a flag turned on after
        # the fact, so two of the five segments would read 0.0 on every
        # real run and the send would never be compared against them.
        "name": "vad-and-agc-timing-back-behind-the-debug-flag",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            t0 = time.perf_counter()\n"
               "            raw_is_speech = vad.is_speech(audio_frame)\n"
               "            t1 = time.perf_counter()",
        "new": "            if cfg.debug.log_overflow_diagnostics:\n"
               "                t0 = time.perf_counter()\n"
               "            raw_is_speech = vad.is_speech(audio_frame)\n"
               "            if cfg.debug.log_overflow_diagnostics:\n"
               "                t1 = time.perf_counter()",
        "expect": [
            "test_the_vad_and_agc_measurements_are_taken_every_iteration"],
    },
    {
        "name": "google-loop-builds-no-segment-timer",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    segments = IterationSegments()",
        "new": "    segments = None",
        "expect": ["test_the_loop_builds_one_segment_timer"],
    },

    # -- wh-stt-load-metrics.4 G1a: the Google loop's load reporter -----
    # The periodic "[load-diag] window=" line is what the load-test tool
    # waits for before it starts a run. Without it a Google run cannot be
    # measured by that tool at all, and every mutation here leaves a loop
    # that still looks wired: it logs its stalls, it logs its overflow
    # block, and the one line the tool needs never appears -- or appears
    # carrying numbers taken from a different iteration than the ones
    # beside it.
    {
        # Off for every run, whatever the config says.
        "name": "google-load-reporter-never-built",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if cfg.debug.log_load_diagnostics else None",
        "new": "        if False else None",
        "expect": ["test_the_reporter_is_gated_by_the_flag"],
    },
    {
        # Two trackers apply one threshold twice and report the same
        # stall in two places with different numbers, because each holds
        # its own last-progress time and its own window.
        "name": "google-builds-a-second-stall-tracker",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    stall_tracker = (\n"
               "        load_reporter.stall_tracker if load_reporter "
               "else LoopStallTracker())",
        "new": "    stall_tracker = LoopStallTracker()",
        "expect": ["test_one_detector_serves_both_windows"],
    },
    {
        # Built and never used: no window line, and the reporter's own
        # tracker is recorded by nobody, so the [stall] line goes quiet
        # as well.
        "name": "google-reporter-not-given-to-the-helper",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                    reporter=load_reporter):",
        "new": "                    reporter=None):",
        "expect": ["test_the_loop_hands_the_reporter_to_the_one_helper"],
    },
    {
        # The [stall] figure goes back to plain wall time, which counts
        # the loop's own work as starvation and reports a stall on every
        # slow iteration.
        "name": "google-reporter-not-given-the-loops-work-total",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            busy_seconds=lambda: segments.work_seconds,",
        "new": "            busy_seconds=lambda: 0.0,",
        "expect": ["test_the_reporter_is_given_the_loops_own_work_total"],
    },
    {
        # Capture reported ready before it is: the frameless windows
        # while the microphone opens then read as starvation.
        "name": "google-reporter-not-told-when-capture-is-ready",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            capture_ready=lambda: mic.wait_ready(timeout=0.0),",
        "new": "            capture_ready=lambda: True,",
        "expect": ["test_the_reporter_knows_when_capture_is_ready"],
    },
    {
        # Both summaries of one tracker, back together. Reading the
        # window empties it, so the reporter's line would report a window
        # this call had already taken -- stalls reported as zero on
        # exactly the runs that had them.
        "name": "google-overflow-block-still-empties-the-stall-window",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "                vad_times_ms.clear()\n"
               "                agc_times_ms.clear()",
        "new": "                stall_tracker.snapshot_and_reset_window()\n"
               "                vad_times_ms.clear()\n"
               "                agc_times_ms.clear()",
        "expect": ["test_the_overflow_block_no_longer_summarises_stalls"],
    },
    {
        # The one-reading rule, broken on the reporter's side. The
        # reporter's window line and the per-utterance line then carry
        # two different depths for one iteration, and they disagree most
        # under the load both lines exist to measure.
        "name": "the-helper-reads-the-queue-again-under-a-reporter",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        raw_lines = reporter.record_iteration()\n"
               "        depth = reporter.last_queue_depth",
        "new": "        raw_lines = reporter.record_iteration()\n"
               "        depth = mic.get_queue_size()",
        "expect": ["test_the_queue_is_not_read_a_second_time",
                   "test_the_utterance_line_gets_the_reading_the_reporter_took"],
    },
    {
        # record_iteration already recorded the tracker. A second record
        # counts every iteration twice, so the gap the [stall] line
        # reports is measured from a progress mark the same iteration set.
        "name": "the-helper-records-the-tracker-again-under-a-reporter",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        depth = reporter.last_queue_depth\n"
               "    else:",
        "new": "        depth = reporter.last_queue_depth\n"
               "        stall_tracker.record(depth)\n"
               "    else:",
        "expect": ["test_the_tracker_is_not_recorded_twice",
                   "test_the_loop_takes_its_queue_depth_through_the_one_helper"],
    },
    {
        # A breakdown after every line the reporter returns, including
        # the periodic window summary -- so the breakdown of one
        # iteration is printed under a line describing ten seconds.
        "name": "the-breakdown-follows-every-line-not-only-the-stall",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if segments is not None and "
               "line.startswith(STALL_PREFIX):",
        "new": "        if segments is not None:",
        "expect": ["test_the_breakdown_follows_the_stall_and_nothing_else"],
    },
    {
        # Everything the reporter produced is dropped: the window line,
        # the outage line, and the stall message with them.
        "name": "the-reporters-lines-are-never-logged",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    for line in raw_lines:\n"
               "        lines.append(line)",
        "new": "    for line in raw_lines[:0]:\n"
               "        lines.append(line)",
        "expect": ["test_every_line_the_reporter_returns_is_logged",
                   "test_the_breakdown_follows_the_stall_and_nothing_else",
                   "test_the_stall_message_is_passed_back"],
    },
    {
        # The per-utterance line stops being fed the moment the reporter
        # is switched on, so q_max, q_mean and q_n read 0 on exactly the
        # runs that were started to measure them.
        "name": "the-utterance-line-goes-blank-once-the-reporter-is-on",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "    if utterance_load is not None:\n"
               "        utterance_load.sample(depth)",
        "new": "    if utterance_load is not None and reporter is None:\n"
               "        utterance_load.sample(depth)",
        "expect": [
            "test_the_utterance_line_gets_the_reading_the_reporter_took"],
    },

    # -- the line is written on the loop's own thread (.4.1.1) ----------
    {
        # The defect as filed: the EOS fallback timer's thread reads and
        # zeroes counters the loop is writing that instant, so a stall
        # that coincides with a finalization appears on no line at all.
        "name": "eos-finalize-writes-the-line-on-the-timer-thread",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if self.load_metrics:\n"
               "            finalized_utterance_id = "
               "self.current_utterance_id\n"
               "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))",
        "new": "        if self.load_metrics:\n"
               "            try:\n"
               "                logger.info(self.load_metrics.finish(\n"
               "                    self.current_utterance_id, reason))\n"
               "            except Exception as e:\n"
               "                logger.warning(f'[load-diag] no line for '\n"
               "                               f'UTT-"
               "{self.current_utterance_id}: '\n"
               "                               f'{type(e).__name__}: {e}')",
        "expect": [
            "test_a_finalize_from_another_thread_writes_no_line_there",
            "test_a_new_utterance_drains_before_it_opens_the_window"],
    },
    {
        # The check that decides which thread may write. Inverted, the
        # timer thread writes and the loop thread queues, which is the
        # defect with the two threads swapped.
        "name": "the-loop-thread-check-is-inverted",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if threading.get_ident() == "
               "self._load_metrics_thread:",
        "new": "        if threading.get_ident() != "
               "self._load_metrics_thread:",
        "expect": [
            "test_a_finalize_from_another_thread_writes_no_line_there",
            "test_a_finalize_on_the_loop_thread_still_writes_it_there",
            "test_a_new_utterance_drains_before_it_opens_the_window"],
    },
    {
        # start() zeroes the window a queued finish() has still to read,
        # and the next utterance can begin in the same iteration the
        # timer finalized in. Without this drain that line reports the
        # new utterance's empty window under the old utterance's id.
        "name": "a-new-utterance-does-not-drain-first",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        self.emit_pending_load_lines()\n"
               "\n"
               "        # wh-stt-load-metrics.4: open this utterance's "
               "load window.",
        "new": "        pass\n"
               "\n"
               "        # wh-stt-load-metrics.4: open this utterance's "
               "load window.",
        "expect": ["test_a_new_utterance_drains_before_it_opens_the_window"],
    },
    {
        # A line queued by the timer thread and never drained is a
        # measurement collected and thrown away.
        "name": "the-loop-never-drains-the-queued-line",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            utterance_mgr.emit_pending_load_lines()",
        "new": "            pass",
        "expect": ["test_the_loop_drains_every_iteration"],
    },
    {
        # Two entries for one utterance double it in every parsed run.
        "name": "the-finalize-queues-the-line-twice",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))",
        "new": "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))\n"
               "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))",
        "expect": [
            "test_the_deferred_line_is_written_once",
            "test_the_loop_writes_the_deferred_line_when_it_drains",
            "test_a_finalize_on_the_loop_thread_still_writes_it_there",
            "test_the_line_is_written_once"],
    },

    # -- where the request is published, and when it is read
    # -- (.4.1.3, .4.1.4) ----------------------------------------------
    {
        # The defect as filed. The request moves below the state
        # transition, which is the moment the consumer loop may close
        # this utterance and open the next one. Both statements move
        # rather than one being copied, so a placement test cannot pass
        # on a stray original left behind.
        "name": "the-load-line-is-queued-after-the-state-transition",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "        if self.load_metrics:\n"
               "            finalized_utterance_id = "
               "self.current_utterance_id\n"
               "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))\n"
               "\n"
               "        # Transition state and mark utterance as closed\n"
               "        self.state = UtteranceState.FINALIZED\n"
               "        self.closed_utterances.add("
               "self.current_utterance_id)\n",
        "new": "        # Transition state and mark utterance as closed\n"
               "        self.state = UtteranceState.FINALIZED\n"
               "        self.closed_utterances.add("
               "self.current_utterance_id)\n"
               "        if self.load_metrics:\n"
               "            finalized_utterance_id = "
               "self.current_utterance_id\n"
               "            self._pending_load_lines.append(\n"
               "                (finalized_utterance_id, reason))\n",
        "expect": [
            "test_a_reopen_at_the_state_transition_keeps_"
            "the_ended_utterances_id"],
    },
    {
        # The drain back above the sample. The stall that spans the
        # iteration an EOS finalization landed in is then read out of
        # the utterance window before the detector records it, and the
        # next start() discards it, so it reaches no line.
        "name": "the-loop-drains-before-it-samples",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            for stall_line in sample_consumer_iteration(\n"
               "                    mic, stall_tracker, utterance_load, "
               "segments,\n"
               "                    reporter=load_reporter):\n"
               "                logger.info(stall_line)\n"
               "\n"
               "            # wh-stt-load-metrics.4.1.1: the EOS fallback "
               "timer runs on\n"
               "            # its own thread and only queues its line. It "
               "is written\n"
               "            # here, on the thread that samples the same "
               "counters, so the\n"
               "            # two cannot overlap.\n"
               "            utterance_mgr.emit_pending_load_lines()\n",
        "new": "            utterance_mgr.emit_pending_load_lines()\n"
               "\n"
               "            for stall_line in sample_consumer_iteration(\n"
               "                    mic, stall_tracker, utterance_load, "
               "segments,\n"
               "                    reporter=load_reporter):\n"
               "                logger.info(stall_line)\n",
        "expect": [
            "test_the_loop_samples_the_iteration_before_it_drains",
            "test_a_stall_on_the_finalizing_iteration_reaches_that_line"],
    },
    {
        # Every other reader failure in this feature costs its own
        # numbers and nothing else. Unguarded, a drain that raises
        # breaks the loop iteration that drained it.
        "name": "a-drain-failure-breaks-the-iteration",
        "service": GOOGLE,
        "test_file": GOOGLE_WIRING_TESTS,
        "file": GOOGLE_MAIN,
        "old": "            try:\n"
               "                logger.info(self.load_metrics.finish("
               "utterance_id, reason))\n"
               "            except Exception as e:\n"
               "                logger.warning(f'[load-diag] no line for '\n"
               "                               f'UTT-{utterance_id}: '\n"
               "                               f'{type(e).__name__}: {e}')",
        "new": "            logger.info(self.load_metrics.finish("
               "utterance_id, reason))",
        "expect": [
            "test_a_deferred_writer_that_raises_does_not_stop_the_loop"],
    },

    # -- the segment timer itself ---------------------------------------
    {
        # The whole point of the unaccounted figure: it is the
        # descheduling answer, and it has to be able to win.
        "name": "unaccounted-cannot-be-the-worst",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        worst = max(\n"
               "            list(self._ms.items()) + "
               "[('unaccounted', unaccounted)],\n"
               "            key=lambda pair: pair[1])[0]",
        "new": "        worst = max(\n"
               "            list(self._ms.items()),\n"
               "            key=lambda pair: pair[1])[0]",
        "expect": ["test_a_loop_that_was_not_running_names_no_call"],
    },
    {
        # Unaccounted that forgets to subtract the parts always wins, so
        # every stall reads as CPU starvation and no call is ever named.
        "name": "unaccounted-counts-the-whole-iteration",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        unaccounted = max(0.0, iter_ms - total)",
        "new": "        unaccounted = iter_ms",
        "expect": ["test_a_blocking_send_is_named"],
    },
    {
        # The send loop runs once per chunk and a lead-in burst sends
        # several in one iteration.
        "name": "a-segment-entered-twice-keeps-only-the-last",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        self._ms[name] += value",
        "new": "        self._ms[name] = value",
        "expect": ["test_a_segment_entered_twice_adds_up"],
    },
    {
        # The send raises when the stream dies, and that is exactly the
        # iteration whose timing matters most.
        "name": "a-raising-body-loses-its-timing",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        try:\n"
               "            yield\n"
               "        finally:\n"
               "            self.add(name, (self._clock() - started) "
               "* 1000.0)",
        "new": "        yield\n"
               "        self.add(name, (self._clock() - started) * 1000.0)",
        "expect": ["test_a_raising_body_still_records_its_time"],
    },
    {
        # A caller typo must not add a field the parser has never seen.
        "name": "an-unknown-segment-name-becomes-a-field",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if name not in self._ms:\n            return",
        "new": "        if name not in self._ms:\n"
               "            self._ms[name] = 0.0",
        "expect": ["test_an_unknown_segment_name_is_ignored"],
    },
    {
        # A NaN prints and poisons the comparison that names the worst.
        "name": "a-nan-or-negative-measurement-is-kept",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if not math.isfinite(value) or value < 0.0:\n"
               "            return",
        "new": "        if False:\n            return",
        "expect": ["test_a_negative_or_absent_measurement_is_dropped"],
    },
    {
        "name": "starting-again-keeps-the-previous-iteration",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        for name in self.NAMES:\n"
               "            self._ms[name] = 0.0\n"
               "        self._started = self._clock()",
        "new": "        self._started = self._clock()",
        "expect": ["test_starting_again_forgets_the_previous_iteration"],
    },
    {
        # The first stall can be reported before the first full
        # iteration, and a traceback out of a diagnostic takes the loop.
        "name": "a-report-before-any-start-raises",
        "service": SHARED,
        "test_file": SEGMENT_TESTS,
        "file": DIAGNOSTICS,
        "old": "        if self._started is None:\n            iter_ms = total",
        "new": "        if False:\n            iter_ms = total",
        "expect": ["test_a_report_before_any_start_answers_rather_than_raising"],
    },
]



def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def _clear_pycache(service: Path) -> None:
    for d in service.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _targets(test_file):
    """A mutation's test_file, as the list of paths to hand pytest.

    A str for the usual one-suite mutation, a tuple when one behaviour
    is guarded in two suites at once. Tuples rather than lists because
    main() uses (service, test_file) as a set key.
    """
    return [test_file] if isinstance(test_file, str) else list(test_file)


def _run_pytest(service: Path, test_file):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        ["uv", "run", "pytest", *_targets(test_file), "-rf", "-q", "-p", "no:randomly"],
        cwd=service, env=env, capture_output=True, text=True, timeout=300,
    )


def _collected_names(service: Path, test_file):
    run = subprocess.run(
        ["uv", "run", "pytest", *_targets(test_file), "--collect-only", "-q"],
        cwd=service, capture_output=True, text=True, timeout=300,
    )
    names = set()
    for line in run.stdout.splitlines():
        if "::" in line:
            names.add(re.sub(r"\[.*\]$", "", line.rsplit("::", 1)[-1]))
    return names


def _failed_names(output: str):
    return {
        re.sub(r"\[.*\]$", "", line.split("::")[-1].split()[0])
        for line in output.splitlines()
        if line.startswith("FAILED")
    }


def _restore(path: Path, original: bytes, mutated: bytes, name: str):
    """Put the pre-run bytes back; return an error string, or None.

    Never overwrite bytes this run did not write: a save landing from an
    editor while pytest ran would otherwise be replaced by the stale
    snapshot and lost.
    """
    try:
        current = path.read_bytes()
    except OSError as e:
        return (f"{name}: cannot read {path} before restore; "
                f"the MUTANT MAY REMAIN in source: {e}")
    if current == original:
        return None
    if current != mutated:
        return (f"{name}: {path} changed while pytest ran; refusing to "
                "overwrite the concurrent edit with the stale pre-run "
                "snapshot -- reconcile the file by hand")
    try:
        path.write_bytes(original)
    except OSError as e:
        return f"{name}: restore failed; the MUTANT REMAINS in {path}: {e}"
    return None


def main() -> int:
    only = sys.argv[1:]
    selected = [m for m in MUTATIONS
                if not only or any(a in m["name"] for a in only)]
    if only and not selected:
        print(f"ERROR no mutation name matches {only}")
        return 1
    skipped = len(MUTATIONS) - len(selected)

    errors, survivors = [], []
    suites = {(m["service"], m["test_file"]) for m in selected}

    # Validate every expected name against real collection first: a renamed
    # test can never appear in the failed set, so its mutation would report
    # as a survivor while the test that catches it sits there passing.
    for service, test_file in sorted(suites, key=lambda s: str(s[0])):
        real = _collected_names(service, test_file)
        if not real:
            errors.append(f"{test_file}: collected no tests in {service.name}")
            continue
        for m in selected:
            if (m["service"], m["test_file"]) != (service, test_file):
                continue
            for name in m["expect"]:
                if name not in real:
                    errors.append(
                        f"{m['name']}: expected test {name} does not exist "
                        f"in {test_file}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    for service, test_file in sorted(suites, key=lambda s: str(s[0])):
        _clear_pycache(service)
        base = _run_pytest(service, test_file)
        if base.returncode != 0:
            print(f"ERROR baseline not green for {service.name}/{test_file}; "
                  "refusing to mutate")
            print(base.stdout[-2000:])
            return 1
        print(f"baseline green: {service.name}/{test_file}")

    for m in selected:
        path, service, test_file = m["file"], m["service"], m["test_file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count != 1:
            problem = ("pattern not found" if count == 0
                       else f"pattern ambiguous ({count} matches)")
            errors.append(f"{m['name']}: {problem}")
            print(f"ERROR {m['name']}: {problem}")
            continue
        mutated = data.replace(old, new, 1)
        # Only Python is compiled. A markdown mutant has no syntax to
        # check, and compiling it would report every document mutation as a
        # SyntaxError -- which reads as "caught" and proves nothing
        # (wh-stt-load-metrics.1.12).
        if path.suffix == ".py":
            try:
                compile(mutated.decode("utf-8"), str(path), "exec")
            except SyntaxError as e:
                errors.append(f"{m['name']}: mutant does not compile: {e}")
                print(f"ERROR {m['name']}: mutant does not compile: {e}")
                continue

        _clear_pycache(service)
        path.write_bytes(mutated)
        timed_out = False
        try:
            run = _run_pytest(service, test_file)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error = _restore(path, data, mutated, m["name"])
            _clear_pycache(service)
        if restore_error:
            errors.append(restore_error)
            print(f"ERROR {restore_error}")
            print("aborting remaining mutations: source no longer matches "
                  "what this run snapshotted")
            break
        if timed_out:
            errors.append(f"{m['name']}: pytest timed out")
            print(f"ERROR {m['name']}: pytest timed out")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        failed = _failed_names(out)
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(f"{m['name']}: expected {missing}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
        else:
            print(f"caught {m['name']} by {sorted(failed & set(m['expect']))}")

    print(f"scope: ran {len(selected)} of {len(MUTATIONS)} mutations, "
          f"skipped {skipped} by name filter, "
          f"{len(survivors)} survivors, {len(errors)} errors")
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
