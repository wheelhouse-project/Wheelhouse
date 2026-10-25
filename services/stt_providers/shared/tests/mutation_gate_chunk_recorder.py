"""Mutation gate for the recording mode of the shared audio path
(wh-first-sound-lost.23, acceptance item 31).

The code is shared_stt/chunk_recorder.py (the state machine, the limits in
samples, the lead-in trim, end_now, deliver) and the recording branch of
AudioProcessor in shared_stt/audio_processor.py (the feed call, the three
verdicts, the reset before the callback, end_recording_now). The tests are
tests/test_audio_processor_recording.py. The lead_word_* commands of
WSForwarder are in mutation_gate_ws_forwarder_lead_word.py.

Run it from services/stt_providers/shared:

    python tests/mutation_gate_chunk_recorder.py            # the whole set
    python tests/mutation_gate_chunk_recorder.py name       # one, by name filter
    python tests/mutation_gate_chunk_recorder.py --check    # patterns only
    python tests/mutation_gate_chunk_recorder.py --equivalent
        # runs EQUIVALENT_MUTANTS; every one of them must be reported SURVIVED

The launcher starts pytest directly under the Google provider's interpreter,
never through ``uv run`` (see mutation_gate_lead_in_gain.py for the reasons).
A pattern of one line gets a final newline added to both texts, so it matches
a whole line. A pattern of several lines is used as written.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent
GOOGLE = PROVIDERS / "google_stt_server"

RECORDER = SHARED / "shared_stt" / "chunk_recorder.py"
PROCESSOR = SHARED / "shared_stt" / "audio_processor.py"
TESTS = "tests/test_audio_processor_recording.py"


def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    python = GOOGLE / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for {service}: {python} does not exist. Run "
            f".claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:cacheprovider",
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


def m(name, old, new, expect, file=RECORDER):
    if "\n" not in old.rstrip("\n"):
        if not old.endswith("\n"):
            old += "\n"
        if not new.endswith("\n"):
            new = new + "\n" if new else ""
    return {
        "name": name,
        "service": SHARED,
        "test_file": TESTS,
        "file": file,
        "old": old,
        "new": new,
        "expect": expect,
    }


def p(name, old, new, expect):
    return m(name, old, new, expect, file=PROCESSOR)


# --- Test names ----------------------------------------------------------
RAW_SAVED = "test_saved_samples_equal_input_while_agc_changes_the_gain"
EVERY_CHUNK = "test_vad_and_agc_run_on_every_chunk_while_recording"
LEAD_300 = "test_lead_in_is_trimmed_to_300_ms"
NO_TEXT = "test_hello_types_no_text_and_runs_no_command"
CONTINUES = "test_recognition_continues_with_the_next_chunk"
ONE_TAKE = "test_one_request_makes_one_take"
END_1S = "test_ends_1s_after_the_last_speech_chunk"
TAIL_RESTART = "test_speech_in_the_tail_restarts_the_1s_count"
END_4S = "test_ends_4s_after_the_gate_opened"
MAX_FROM_GATE = "test_max_counts_from_the_gate_not_from_the_request"
NO_SPEECH_10 = "test_no_speech_within_10s_ends_with_no_capture"
AUDIO_NOT_WALL = "test_the_ten_seconds_count_audio_not_wall_time"
END_RESETS = "test_the_end_resets_the_utterance_state"
RESET_BEFORE_CB = "test_process_chunk_resets_the_utterance_state_before_the_callback"
STARTS_NEXT = "test_starts_at_the_next_chunk"
SECOND_REFUSED = "test_a_second_request_while_busy_is_refused"
WAITS_UTT = "test_start_waits_for_the_utterance_in_progress"
CANCEL_BEFORE = "test_cancel_before_start_means_it_never_starts"
CANCEL_REC = "test_cancel_while_recording_ends_it_without_a_result"
CANCEL_CLEAN = "test_a_cancelled_capture_leaves_clean_utterance_state"
CBX_PROC = "test_process_chunk_does_not_reset_after_a_cancel_before_speech"
CBX_END = "test_end_recording_now_does_not_reset_after_a_cancel_before_speech"
CB_FAIL = "test_a_failing_finish_callback_never_stops_the_audio_loop"
NO_RECORDER = "test_a_processor_without_a_recorder_never_records"
ERN_GATE = "test_gate_opened_ends_the_capture_as_end_silence"
ERN_NOGATE = "test_gate_never_opened_ends_as_no_speech"
ERN_PENDING = "test_a_request_that_is_only_pending_stays_pending"
ERN_IDLE = "test_idle_does_nothing"
ERN_RESET_BEFORE = "test_the_end_resets_the_utterance_state_before_the_callback"
ERN_CANCELLED = "test_a_cancelled_capture_delivers_nothing"
ERN_NO_REC = "test_a_processor_without_a_recorder_ignores_the_call"
SECOND_CLEAN = "test_a_second_take_starts_with_a_clean_capture"
END_AFTER_TAKE = "test_end_recording_now_after_a_take_does_nothing"
START_FAIL = "test_a_failing_start_callback_never_stops_the_take"
DELIVER_ONCE = "test_a_finished_capture_is_delivered_once"
QUIET = "test_a_recorder_without_a_finish_callback_ends_quietly"
WRITABLE = "test_the_saved_samples_are_a_writable_int16_array"
CANCEL_RACE = "test_a_cancel_between_the_quick_check_and_the_lock_means_it_never_starts"
LOCK_USE = "test_a_chunk_is_taken_with_the_lock_held_and_a_callback_runs_without_it"

MUTATIONS = [
    # ---- construction: the limits in samples --------------------------------
    m("lead-in-in-samples", "        self._lead_in_bytes = lead_in_ms * sample_rate // 1000 * 2",
      "        self._lead_in_bytes = lead_in_ms * sample_rate // 1000", [LEAD_300]),
    m("lead-in-ten-times", "        self._lead_in_bytes = lead_in_ms * sample_rate // 1000 * 2",
      "        self._lead_in_bytes = lead_in_ms * sample_rate // 100 * 2", [LEAD_300]),
    m("end-silence-halved", "        self._end_silence_samples = int(end_silence_s * sample_rate)",
      "        self._end_silence_samples = int(end_silence_s * sample_rate) // 2", [END_1S]),
    m("end-silence-doubled", "        self._end_silence_samples = int(end_silence_s * sample_rate)",
      "        self._end_silence_samples = int(end_silence_s * sample_rate) * 2", [END_1S]),
    m("max-halved", "        self._max_samples = int(max_s * sample_rate)",
      "        self._max_samples = int(max_s * sample_rate) // 2", [END_4S]),
    m("max-doubled", "        self._max_samples = int(max_s * sample_rate)",
      "        self._max_samples = int(max_s * sample_rate) * 2", [END_4S]),
    m("no-speech-halved", "        self._no_speech_samples = int(no_speech_timeout_s * sample_rate)",
      "        self._no_speech_samples = int(no_speech_timeout_s * sample_rate) // 2", [NO_SPEECH_10]),
    m("no-speech-doubled", "        self._no_speech_samples = int(no_speech_timeout_s * sample_rate)",
      "        self._no_speech_samples = int(no_speech_timeout_s * sample_rate) * 2", [NO_SPEECH_10]),
    m("callbacks-swapped", "        self._on_start = on_start\n        self._on_finish = on_finish",
      "        self._on_start = on_finish\n        self._on_finish = on_start", [STARTS_NEXT]),

    # ---- _start_capture: a second take starts clean -------------------------
    m("start-capture-keeps-the-gate", "        self._gate = False",
      "        self._gate = getattr(self, \"_gate\", False)", [SECOND_CLEAN]),
    m("start-capture-keeps-the-wait", "        self._waited = 0",
      "        self._waited = getattr(self, \"_waited\", 0)", [SECOND_CLEAN]),

    # ---- request, cancel, busy, gate_opened ---------------------------------
    m("request-refuses-only-when-both", "            if self._pending or self._active:",
      "            if self._pending and self._active:", [SECOND_REFUSED]),
    m("request-never-refuses", "            if self._pending or self._active:",
      "            if False:", [SECOND_REFUSED]),
    m("request-sets-no-flag", "            self._pending = True", "            pass", [STARTS_NEXT]),
    m("request-answers-false", "            return True", "            return False", [STARTS_NEXT]),
    m("cancel-keeps-the-pending-request",
      "            self._pending = False\n            if self._active:\n                self._cancel = True",
      "            if self._active:\n                self._cancel = True", [CANCEL_BEFORE]),
    m("cancel-ignores-a-running-capture",
      "            self._pending = False\n            if self._active:\n                self._cancel = True",
      "            self._pending = False\n            if False:\n                self._cancel = True", [CANCEL_REC]),
    m("busy-needs-both", "        return self._pending or self._active", "        return self._pending and self._active",
      [ERN_PENDING]),
    m("busy-only-pending", "        return self._pending or self._active", "        return self._pending",
      [CANCEL_REC]),
    m("busy-only-active", "        return self._pending or self._active", "        return self._active",
      [ERN_PENDING]),
    m("gate-opened-always-false", "        return self._gate", "        return False", [CANCEL_CLEAN]),
    m("gate-opened-always-true", "        return self._gate", "        return True", [CBX_PROC]),
    m("gate-opened-inverted", "        return self._gate", "        return not self._gate", [CBX_PROC]),

    # ---- feed ---------------------------------------------------------------
    m("feed-quick-check-needs-both", "        if not (self._pending or self._active):",
      "        if not (self._pending and self._active):", [STARTS_NEXT]),
    m("feed-cancel-test-is-or", "            if self._active and self._cancel:",
      "            if self._active or self._cancel:", [END_1S]),
    m("feed-abort-keeps-the-capture-active",
      "            if self._active and self._cancel:\n                self._active = False\n",
      "            if self._active and self._cancel:\n                pass\n", [CANCEL_REC]),
    m("feed-abort-answers-consumed",
      "                self._chunks = []\n                return FEED_ABORTED\n            if not self._active:",
      "                self._chunks = []\n                return FEED_CONSUMED\n            if not self._active:",
      [CANCEL_CLEAN]),
    m("feed-abort-answers-skip",
      "                self._chunks = []\n                return FEED_ABORTED\n            if not self._active:",
      "                self._chunks = []\n                return FEED_SKIP\n            if not self._active:",
      [CANCEL_CLEAN]),
    m("feed-start-ignores-the-open-gate", "                if not self._pending or gate_open:",
      "                if not self._pending:", [WAITS_UTT]),
    m("feed-start-needs-both", "                if not self._pending or gate_open:",
      "                if not self._pending and gate_open:", [WAITS_UTT]),
    m("feed-start-ignores-a-dropped-request", "                if not self._pending or gate_open:",
      "                if gate_open:", [CANCEL_RACE]),
    m("feed-waiting-answers-consumed",
      "                if not self._pending or gate_open:\n                    return FEED_SKIP",
      "                if not self._pending or gate_open:\n                    return FEED_CONSUMED", [WAITS_UTT]),
    m("feed-start-keeps-the-request",
      "                self._pending = False\n                self._active = True\n",
      "                self._active = True\n", [ONE_TAKE]),
    m("feed-start-sets-no-active",
      "                self._pending = False\n                self._active = True\n",
      "                self._pending = False\n                pass\n", [SECOND_REFUSED]),
    m("feed-start-keeps-the-old-capture",
      "                self._cancel = False\n                self._start_capture()\n                started = True",
      "                self._cancel = False\n                started = True", [SECOND_CLEAN]),
    m("feed-start-not-announced",
      "                self._start_capture()\n                started = True",
      "                self._start_capture()\n                started = False", [STARTS_NEXT]),
    m("feed-start-callback-every-chunk", "        if started and self._on_start is not None:",
      "        if started or self._on_start is not None:", [ONE_TAKE]),
    m("feed-start-callback-never", "        if started and self._on_start is not None:",
      "        if started and self._on_start is None:", [STARTS_NEXT]),
    m("feed-start-callback-error-escapes",
      "            except Exception:\n                logger.exception(\"Recording start callback failed\")",
      "            except KeyError:\n                logger.exception(\"Recording start callback failed\")", [START_FAIL]),
    m("feed-take-without-the-lock",
      "        with self._lock:\n            return self._take(pcm_bytes, is_speech, lead_in_buffer)",
      "        if True:\n            return self._take(pcm_bytes, is_speech, lead_in_buffer)", [LOCK_USE]),
    m("feed-take-sees-speech-always", "            return self._take(pcm_bytes, is_speech, lead_in_buffer)",
      "            return self._take(pcm_bytes, True, lead_in_buffer)", [NO_SPEECH_10]),
    m("feed-take-sees-inverted-speech", "            return self._take(pcm_bytes, is_speech, lead_in_buffer)",
      "            return self._take(pcm_bytes, not is_speech, lead_in_buffer)", [END_1S]),

    # ---- end_now ------------------------------------------------------------
    m("end-now-cancel-test-inverted", "            if self._cancel:\n                self._active = False\n",
      "            if not self._cancel:\n                self._active = False\n", [ERN_GATE]),
    m("end-now-cancel-keeps-the-capture-active",
      "            if self._cancel:\n                self._active = False\n",
      "            if self._cancel:\n                pass\n", [ERN_CANCELLED]),
    m("end-now-cancel-answers-finished",
      "                self._chunks = []\n                return FEED_ABORTED\n            return self._end(",
      "                self._chunks = []\n                return FEED_FINISHED\n            return self._end(", [CBX_END]),
    m("end-now-cancel-answers-skip",
      "                self._chunks = []\n                return FEED_ABORTED\n            return self._end(",
      "                self._chunks = []\n                return FEED_SKIP\n            return self._end(", [ERN_CANCELLED]),
    m("end-now-reasons-swapped",
      "            return self._end(END_SILENCE if self._gate else END_NO_SPEECH)",
      "            return self._end(END_NO_SPEECH if self._gate else END_SILENCE)", [ERN_GATE, ERN_NOGATE]),
    m("end-now-always-silence",
      "            return self._end(END_SILENCE if self._gate else END_NO_SPEECH)",
      "            return self._end(END_SILENCE)", [ERN_NOGATE]),
    m("end-now-always-no-speech",
      "            return self._end(END_SILENCE if self._gate else END_NO_SPEECH)",
      "            return self._end(END_NO_SPEECH)", [ERN_GATE]),

    # ---- _take: counting, lead-in, the three limits -------------------------
    m("take-counts-bytes", "        n = len(pcm_bytes) // 2", "        n = len(pcm_bytes)", [END_1S]),
    m("take-waited-counts-one", "        self._waited += n", "        self._waited += 1", [NO_SPEECH_10]),
    m("take-waited-not-added", "        self._waited += n", "        self._waited = n", [NO_SPEECH_10]),
    m("take-gate-test-inverted", "        if not self._gate:", "        if self._gate:", [LEAD_300, NO_SPEECH_10]),
    m("take-lead-in-not-kept", "                lead_in_buffer.add(pcm_bytes)", "                pass", [LEAD_300]),
    m("take-no-speech-strict", "                if self._waited >= self._no_speech_samples:",
      "                if self._waited > self._no_speech_samples:", [NO_SPEECH_10]),
    m("take-no-speech-inverted", "                if self._waited >= self._no_speech_samples:",
      "                if self._waited < self._no_speech_samples:", [NO_SPEECH_10]),
    m("take-no-speech-ends-as-silence", "                    return self._end(END_NO_SPEECH)",
      "                    return self._end(END_SILENCE)", [NO_SPEECH_10]),
    m("take-waiting-answers-skip", "                return FEED_CONSUMED", "                return FEED_SKIP", [NO_TEXT]),
    m("take-lead-in-dropped", "            lead = lead_in_buffer.get_lead_in()", "            lead = b\"\"", [LEAD_300]),
    m("take-lead-in-not-trimmed", "            if len(lead) > self._lead_in_bytes:",
      "            if len(lead) < self._lead_in_bytes:", [LEAD_300]),
    m("take-lead-in-first-part-kept", "                lead = lead[len(lead) - self._lead_in_bytes:]",
      "                lead = lead[:self._lead_in_bytes]", [LEAD_300]),
    m("take-lead-in-last-part-dropped", "                lead = lead[len(lead) - self._lead_in_bytes:]",
      "                lead = lead[self._lead_in_bytes:]", [LEAD_300]),
    m("take-gate-not-opened", "            self._gate = True", "            pass", [RAW_SAVED]),
    m("take-lead-in-only", "            self._chunks = [lead, pcm_bytes] if lead else [pcm_bytes]",
      "            self._chunks = [lead] if lead else [pcm_bytes]", [RAW_SAVED]),
    m("take-first-chunk-before-lead-in",
      "            self._chunks = [lead, pcm_bytes] if lead else [pcm_bytes]",
      "            self._chunks = [pcm_bytes, lead] if lead else [pcm_bytes]", [LEAD_300]),
    m("take-lead-in-not-in-capture", "            self._chunks = [lead, pcm_bytes] if lead else [pcm_bytes]",
      "            self._chunks = [pcm_bytes]", [LEAD_300]),
    m("take-max-counts-without-the-first-chunk", "            self._since_open = n",
      "            self._since_open = 0", [END_4S]),
    m("take-chunk-not-kept", "            self._chunks.append(pcm_bytes)", "            pass", [RAW_SAVED]),
    m("take-max-counts-one-per-chunk", "            self._since_open += n", "            self._since_open += 1", [END_4S]),
    m("take-trailing-never-reset", "            self._trailing = 0 if is_speech else self._trailing + n",
      "            self._trailing = self._trailing + n", [TAIL_RESTART]),
    m("take-trailing-reset-on-silence", "            self._trailing = 0 if is_speech else self._trailing + n",
      "            self._trailing = 0 if not is_speech else self._trailing + n", [END_1S]),
    m("take-trailing-counts-one", "            self._trailing = 0 if is_speech else self._trailing + n",
      "            self._trailing = 0 if is_speech else self._trailing + 1", [END_1S]),
    m("take-trailing-counts-last-chunk", "            self._trailing = 0 if is_speech else self._trailing + n",
      "            self._trailing = 0 if is_speech else n", [END_1S]),
    m("take-end-silence-strict", "        if self._trailing >= self._end_silence_samples:",
      "        if self._trailing > self._end_silence_samples:", [END_1S]),
    m("take-end-silence-inverted", "        if self._trailing >= self._end_silence_samples:",
      "        if self._trailing < self._end_silence_samples:", [END_1S]),
    m("take-end-silence-as-max", "            return self._end(END_SILENCE)", "            return self._end(END_MAX)", [END_1S]),
    m("take-max-strict", "        if self._since_open >= self._max_samples:",
      "        if self._since_open > self._max_samples:", [END_4S]),
    m("take-max-inverted", "        if self._since_open >= self._max_samples:",
      "        if self._since_open < self._max_samples:", [END_4S]),
    m("take-max-as-silence", "            return self._end(END_MAX)", "            return self._end(END_SILENCE)", [END_4S]),
    m("take-answers-skip",
      "            return self._end(END_MAX)\n        return FEED_CONSUMED",
      "            return self._end(END_MAX)\n        return FEED_SKIP", [NO_TEXT]),

    # ---- _end and deliver ---------------------------------------------------
    m("end-keeps-samples-after-no-speech", "        if end != END_NO_SPEECH:", "        if True:", [NO_SPEECH_10]),
    m("end-no-samples", "        if end != END_NO_SPEECH:", "        if False:", [RAW_SAVED]),
    m("end-samples-test-inverted", "        if end != END_NO_SPEECH:", "        if end == END_NO_SPEECH:", [NO_SPEECH_10]),
    m("end-samples-read-only", '            samples = np.frombuffer(b"".join(self._chunks), dtype="<i2").copy()',
      '            samples = np.frombuffer(b"".join(self._chunks), dtype="<i2")', [WRITABLE]),
    m("end-samples-big-endian", '            samples = np.frombuffer(b"".join(self._chunks), dtype="<i2").copy()',
      '            samples = np.frombuffer(b"".join(self._chunks), dtype=">i2").copy()', [RAW_SAVED]),
    m("end-outcome-waited-zero", "        self._outcome = Recording(samples, end, self._waited)",
      "        self._outcome = Recording(samples, end, 0)", [NO_SPEECH_10]),
    m("end-outcome-reason-fixed", "        self._outcome = Recording(samples, end, self._waited)",
      "        self._outcome = Recording(samples, END_SILENCE, self._waited)", [END_4S]),
    m("end-stays-active", "        self._active = False\n        return FEED_FINISHED",
      "        return FEED_FINISHED", [ERN_GATE]),
    m("end-answers-consumed", "        return FEED_FINISHED", "        return FEED_CONSUMED", [END_RESETS]),
    m("deliver-keeps-the-outcome", "        outcome, self._outcome = self._outcome, None",
      "        outcome = self._outcome", [DELIVER_ONCE]),
    m("deliver-needs-both", "        if outcome is None or self._on_finish is None:",
      "        if outcome is None and self._on_finish is None:", [QUIET]),
    m("deliver-ignores-a-missing-callback", "        if outcome is None or self._on_finish is None:",
      "        if outcome is None:", [QUIET]),
    m("deliver-ignores-a-missing-outcome", "        if outcome is None or self._on_finish is None:",
      "        if self._on_finish is None:", [DELIVER_ONCE]),
    m("deliver-error-escapes",
      "        except Exception:\n            logger.exception(\"Recording finish callback failed\")",
      "        except KeyError:\n            logger.exception(\"Recording finish callback failed\")", [CB_FAIL]),

    # ---- AudioProcessor.end_recording_now -----------------------------------
    p("processor-end-now-without-recorder-goes-on",
      "        if recorder is None:\n            return\n        verdict = recorder.end_now()",
      "        if recorder is None:\n            pass\n        verdict = recorder.end_now()", [ERN_NO_REC]),
    p("processor-end-now-finished-test-inverted",
      "        verdict = recorder.end_now()\n        if verdict == FEED_FINISHED:",
      "        verdict = recorder.end_now()\n        if verdict != FEED_FINISHED:", [ERN_GATE]),
    p("processor-end-now-no-reset",
      "            self._reset_for_new_utterance()\n            recorder.deliver()\n        elif",
      "            recorder.deliver()\n        elif", [ERN_RESET_BEFORE]),
    p("processor-end-now-no-deliver",
      "            recorder.deliver()\n        elif verdict == FEED_ABORTED",
      "            pass\n        elif verdict == FEED_ABORTED", [ERN_GATE]),
    p("processor-end-now-deliver-before-reset",
      "            self._reset_for_new_utterance()\n            recorder.deliver()\n        elif",
      "            recorder.deliver()\n            self._reset_for_new_utterance()\n        elif", [ERN_RESET_BEFORE]),
    p("processor-end-now-abort-resets-without-the-gate", "        elif verdict == FEED_ABORTED and recorder.gate_opened:",
      "        elif verdict == FEED_ABORTED:", [CBX_END]),
    p("processor-end-now-resets-on-a-stale-gate", "        elif verdict == FEED_ABORTED and recorder.gate_opened:",
      "        elif verdict == FEED_ABORTED or recorder.gate_opened:", [END_AFTER_TAKE]),
    p("processor-end-now-resets-on-any-gate", "        elif verdict == FEED_ABORTED and recorder.gate_opened:",
      "        elif recorder.gate_opened:", [END_AFTER_TAKE]),
    p("processor-end-now-abort-no-reset",
      "        elif verdict == FEED_ABORTED and recorder.gate_opened:\n            self._reset_for_new_utterance()",
      "        elif verdict == FEED_ABORTED and recorder.gate_opened:\n            pass", [ERN_CANCELLED]),

    # ---- AudioProcessor.process_chunk ---------------------------------------
    p("processor-recorder-never-fed", "        if recorder is not None:\n            verdict = recorder.feed(",
      "        if False:\n            verdict = recorder.feed(", [STARTS_NEXT]),
    p("processor-feed-gets-the-agc-audio",
      "                pcm_bytes, is_speech, self._vad_gate_open, self.lead_in_buffer)",
      "                agc_pcm, is_speech, self._vad_gate_open, self.lead_in_buffer)", [RAW_SAVED]),
    p("processor-feed-gets-inverted-speech",
      "                pcm_bytes, is_speech, self._vad_gate_open, self.lead_in_buffer)",
      "                pcm_bytes, not is_speech, self._vad_gate_open, self.lead_in_buffer)", [END_1S]),
    p("processor-feed-gets-a-closed-gate",
      "                pcm_bytes, is_speech, self._vad_gate_open, self.lead_in_buffer)",
      "                pcm_bytes, is_speech, False, self.lead_in_buffer)", [WAITS_UTT]),
    p("processor-consumed-chunk-goes-on",
      "            if verdict == FEED_CONSUMED:\n                return\n",
      "            if verdict == FEED_CONSUMED:\n                pass\n", [NO_TEXT]),
    p("processor-finished-no-reset",
      "            if verdict == FEED_FINISHED:\n                self._reset_for_new_utterance()\n                recorder.deliver()\n                return\n",
      "            if verdict == FEED_FINISHED:\n                recorder.deliver()\n                return\n", [END_RESETS]),
    p("processor-finished-no-deliver",
      "            if verdict == FEED_FINISHED:\n                self._reset_for_new_utterance()\n                recorder.deliver()\n                return\n",
      "            if verdict == FEED_FINISHED:\n                self._reset_for_new_utterance()\n                return\n", [ONE_TAKE]),
    p("processor-finished-deliver-before-reset",
      "            if verdict == FEED_FINISHED:\n                self._reset_for_new_utterance()\n                recorder.deliver()\n                return\n",
      "            if verdict == FEED_FINISHED:\n                recorder.deliver()\n                self._reset_for_new_utterance()\n                return\n",
      [RESET_BEFORE_CB]),
    p("processor-finished-chunk-goes-on",
      "                recorder.deliver()\n                return\n            if verdict == FEED_ABORTED",
      "                recorder.deliver()\n            if verdict == FEED_ABORTED", [NO_TEXT]),
    p("processor-abort-resets-without-the-gate",
      "            if verdict == FEED_ABORTED and recorder.gate_opened:",
      "            if verdict == FEED_ABORTED:", [CBX_PROC]),
    p("processor-abort-resets-on-a-stale-gate",
      "            if verdict == FEED_ABORTED and recorder.gate_opened:",
      "            if verdict == FEED_ABORTED or recorder.gate_opened:", [CBX_PROC]),
    p("processor-abort-no-reset",
      "                # The cancelled capture had taken the lead-in buffer.\n                self._reset_for_new_utterance()",
      "                # The cancelled capture had taken the lead-in buffer.\n                pass", [CANCEL_CLEAN]),
]

# Mutants that no test can fail for. `--equivalent` runs them; each must be
# reported SURVIVED, and one that is caught is no longer equivalent.
EQUIVALENT_MUTANTS = [
    # The reset of the capture is repeated by the code that ends or starts it.
    m("eq-start-capture-keeps-since-open", "        self._since_open = 0",
      "        self._since_open = getattr(self, \"_since_open\", 0)", [SECOND_CLEAN]),
    m("eq-start-capture-keeps-trailing",
      "        self._since_open = 0\n        self._trailing = 0\n",
      "        self._since_open = 0\n        self._trailing = getattr(self, \"_trailing\", 0)\n",
      [SECOND_CLEAN]),
    m("eq-start-capture-keeps-chunks", "        self._chunks: list[bytes] = []",
      "        self._chunks: list[bytes] = getattr(self, \"_chunks\", [])", [SECOND_CLEAN]),
    # since_open and trailing are set when the gate opens; chunks are replaced
    # when the gate opens and emptied when a capture ends or is cancelled.
    # The cancel flag is set only while active, and the code that clears
    # active runs the abort or the end, so it is never stale at a start.
    m("eq-cancel-sets-the-flag-when-idle",
      "            self._pending = False\n            if self._active:\n                self._cancel = True",
      "            self._pending = False\n            if True:\n                self._cancel = True", [CANCEL_BEFORE]),
    m("eq-feed-abort-keeps-the-flag",
      "            if self._active and self._cancel:\n                self._active = False\n                self._cancel = False\n",
      "            if self._active and self._cancel:\n                self._active = False\n", [CANCEL_REC]),
    m("eq-feed-abort-keeps-the-chunks",
      "                self._chunks = []\n                return FEED_ABORTED\n            if not self._active:",
      "                return FEED_ABORTED\n            if not self._active:", [CANCEL_REC]),
    m("eq-feed-start-keeps-the-flag",
      "                self._active = True\n                self._cancel = False\n                self._start_capture()",
      "                self._active = True\n                self._start_capture()", [CANCEL_REC]),
    # The unlocked check at the top of feed() is a quick path: the locked code
    # below it answers FEED_SKIP in the same cases.
    m("eq-feed-no-quick-check", "        if not (self._pending or self._active):\n            return FEED_SKIP",
      "        if False:\n            return FEED_SKIP", [STARTS_NEXT]),
    # end_now() has the same quick path. Only the audio thread ends a capture
    # and end_now() runs on it, so the second check under the lock never sees
    # a capture that ended in between.
    m("eq-end-now-no-quick-check",
      "        if not self._active:  # cheap: the audio loop calls this for every skipped chunk\n            return FEED_SKIP",
      "        if False:\n            return FEED_SKIP", [ERN_IDLE]),
    m("eq-end-now-no-second-check",
      "        with self._lock:\n            if not self._active:\n                return FEED_SKIP\n            if self._cancel:",
      "        with self._lock:\n            if self._cancel:", [ERN_IDLE]),
    m("eq-end-now-abort-keeps-the-flag",
      "            if self._cancel:\n                self._active = False\n                self._cancel = False\n",
      "            if self._cancel:\n                self._active = False\n", [ERN_CANCELLED]),
    m("eq-end-now-abort-keeps-the-chunks",
      "                self._chunks = []\n                return FEED_ABORTED\n            return self._end(",
      "                return FEED_ABORTED\n            return self._end(", [ERN_CANCELLED]),
    # The lead-in buffer is emptied by the reset that ends the capture; the
    # buffer is not read while the gate of the recorder is open.
    m("eq-take-lead-in-buffer-kept", "            lead_in_buffer.clear()", "            pass", [END_RESETS]),
    # A lead of exactly lead_in_bytes slices from 0 to its end either way.
    m("eq-take-lead-in-at-the-limit", "            if len(lead) > self._lead_in_bytes:",
      "            if len(lead) >= self._lead_in_bytes:", [LEAD_300]),
    # An empty lead joins to nothing.
    m("eq-take-empty-lead-joined", "            self._chunks = [lead, pcm_bytes] if lead else [pcm_bytes]",
      "            self._chunks = [lead, pcm_bytes]", [LEAD_300]),
    # Reset to 0 when the gate opens; the value was 0 from _start_capture.
    m("eq-take-trailing-not-reset-at-open", "            self._trailing = 0", "            pass", [END_1S]),
    # The two limits can be reached on the same chunk; both deliver the same
    # samples, and the order decides only the label.
    m("eq-take-limits-in-the-other-order",
      "        if self._trailing >= self._end_silence_samples:\n            return self._end(END_SILENCE)\n"
      "        if self._since_open >= self._max_samples:\n            return self._end(END_MAX)\n",
      "        if self._since_open >= self._max_samples:\n            return self._end(END_MAX)\n"
      "        if self._trailing >= self._end_silence_samples:\n            return self._end(END_SILENCE)\n",
      [END_1S, END_4S]),
    # _end() empties the chunks; a start, a gate opening or a cancel replaces
    # or empties them before anything reads them.
    m("eq-end-keeps-the-chunks",
      "        self._outcome = Recording(samples, end, self._waited)\n        self._chunks = []\n",
      "        self._outcome = Recording(samples, end, self._waited)\n        pass\n", [END_1S]),
]


if __name__ == "__main__":
    if "--equivalent" in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != "--equivalent"]
        print(f"EQUIVALENT mutants: {len(EQUIVALENT_MUTANTS)}; each must print SURVIVED")
        sys.exit(run(EQUIVALENT_MUTANTS, argv))
    sys.exit(run(MUTATIONS))
