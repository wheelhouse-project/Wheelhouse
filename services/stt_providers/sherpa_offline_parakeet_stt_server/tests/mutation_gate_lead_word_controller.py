"""Mutation gate for lead_word_controller.py and the wiring in main.py
(wh-first-sound-lost.23, acceptance item 31).

The code is the provider side of the 'Your training word' window: the seven
commands and their answers, the busy check, the stale-job check, the lock
that guards the job (and the two read paths that take none), the candidate
write, the recorder settings, and the timers of the capture. The tests are
tests/test_lead_word_controller.py. main.py is the other half: the builder of
the controller, the recorder given to the audio processor, the release that
ends a take, and the loggers that are forwarded. Its tests are
tests/test_lead_word_wiring.py and tests/test_engine_audio_wiring.py.

The four timer constants of lead_word_recorder.py (LEAD_IN_MS,
CAPTURE_END_SILENCE_S, CAPTURE_MAX_S, NO_SPEECH_TIMEOUT_S) are in this gate
because the controller tests pin them. The engine side (set_lead_word,
has_lead_word, the per-utterance copy) is in mutation_gate_lead_word.py.

Run it as lead_word_gate_common.py says. EQUIVALENT_MUTANTS holds the mutants
that no test can fail for; `--equivalent` runs them and each must be reported
SURVIVED.
"""
from __future__ import annotations

from lead_word_gate_common import CONTROLLER, MAIN, RECORDER, main, make

T = "tests/test_lead_word_controller.py"
W = "tests/test_lead_word_wiring.py"
L = "tests/test_engine_audio_wiring.py"
m = make(T, CONTROLLER)
mm = make(W, MAIN)
ml = make(L, MAIN)
mr = make(T, RECORDER)

# --- Test names ----------------------------------------------------------
ST_EMPTY = "test_status_with_no_recording"
ST_KEY = "test_status_reads_the_key_from_config_at_send_time"
ST_KEPT = "test_status_after_a_keep_reports_the_kept_recording"
CAP_FIELDS = "test_supported_loaded_recorded"
CAP_NEVER_RAISES = "test_never_raises"
CAP_BLOCKED = "test_a_blocked_check_does_not_block_capabilities"
REC_ORDER = "test_the_order_is_listening_checking_passed"
REC_MEASURES = "test_passed_carries_the_measures_and_the_key_path"
REC_CANDIDATE = "test_a_passed_recording_becomes_the_candidate_with_its_measures"
REC_NO_CHANGE = "test_the_candidate_never_changes_hello_wav"
REC_LOG = "test_the_log_line_holds_the_measures"
REC_DECODE_ALONE = "test_the_check_decode_gets_the_cut_alone_while_the_engine_holds_a_word"
REC_VAD = "test_each_check_makes_a_new_vad_with_the_silero_threshold"
REC_NO_TYPING = "test_recording_types_no_text_and_runs_no_command"
REC_CONTINUES = "test_recognition_continues_after_the_take"
REC_OLDER = "test_a_record_deletes_an_older_candidate_first"
RF_NO_SPEECH = "test_no_speech_in_10_seconds"
RF_ROW = "test_a_refused_row_deletes_the_candidate_and_names_the_reason"
RF_NOT_HELLO = "test_a_decode_that_is_not_hello_refuses"
RF_LOG = "test_refused_logs_only_the_decode_length"
RF_AFTER = "test_a_record_after_a_refusal_works"
BUSY_PENDING = "test_a_command_while_pending_is_refused"
BUSY_RECORDING = "test_a_command_while_recording_is_refused"
BUSY_CHECK = "test_a_command_during_the_check_is_refused"
BUSY_STATUS = "test_status_answers_during_a_recording"
DI_PENDING = "test_discard_while_pending_means_it_never_starts"
DI_RECORDING = "test_discard_while_recording_keeps_no_candidate_and_no_result"
DI_NEXT = "test_the_next_utterance_after_a_discard_is_typed_as_normal"
DI_STATUS_ERROR = "test_a_status_error_after_a_pass_answers_failed"
DI_CHECK = "test_discard_during_the_check_keeps_no_candidate_and_no_result"
DI_STALE = "test_a_take_answers_only_the_request_that_started_it"
DI_CANDIDATE = "test_discard_with_a_candidate_deletes_it"
DI_HELLO = "test_discard_never_changes_hello_wav"
DI_NOTHING = "test_discard_with_nothing_to_discard_answers_status"
K_ORDER = "test_the_order_record_passed_keep_is_listening_checking_passed_kept"
K_FIRST = "test_first_keep_files_key_and_engine"
K_REPLACEMENT = "test_a_replacement_keeps_the_old_recording_as_previous"
K_NONE = "test_keep_with_no_candidate_fails"
K_KEY_PATH = "test_passed_after_a_keep_reports_the_key_path"
K_FAILING = "test_a_failing_keep_step_leaves_the_old_word"
R_SWAPS = "test_restore_swaps_the_files_and_loads_the_word"
R_NONE = "test_restore_without_a_previous_recording_fails"
R_EARLIER = "test_restore_of_an_earlier_file_reports_that_hello_wav_exists"
OFF_ON_CURRENT = "test_off_and_on_report_that_hello_wav_exists"
OFF_FULL = "test_off_empties_the_key_keeps_the_files_and_clears_the_engine"
ON_FULL = "test_on_sets_the_key_and_loads_the_word"
ON_NONE = "test_on_with_no_recording_fails"
OFF_FAILED = "test_a_failed_off_reports_failed_and_keeps_the_word"
F_CHECK = "test_a_failing_check_sends_failed_and_frees_the_job"
F_WRITE = "test_a_candidate_write_failure_sends_failed"
F_COMMAND = "test_a_command_that_raises_sends_failed"
F_UNKNOWN = "test_an_unknown_command_sends_failed"
F_FORWARDER = "test_a_forwarder_that_raises_never_stops_the_audio_thread"
SR_ONLY_16K = "test_only_16_khz_builds"
SR_RECORDER = "test_the_recorder_gets_the_real_rate_and_a_lead_in_no_longer_than_the_buffer"
PTT_RELEASE = "test_the_release_ends_the_take_and_the_check_runs"
RS_LONG_BUFFER = "test_a_long_processor_buffer_still_gives_a_300_ms_lead_in"
RS_SHORT_BUFFER = "test_a_shorter_processor_buffer_limits_the_lead_in"
RS_END_1S = "test_the_capture_ends_1_second_after_the_last_speech_chunk"
RS_END_4S = "test_the_capture_ends_4_seconds_after_the_gate_opened"
NL_READS = "test_status_and_capabilities_answer_while_the_candidate_is_written"
NL_DISCARD = "test_a_discard_that_arrives_during_the_candidate_write_leaves_no_candidate"
NJ_CANCEL = "test_a_take_that_starts_without_a_job_is_cancelled"
BUSY_FIRST_RUNS = "test_a_refused_record_leaves_the_first_take_running"
BUSY_RECORDER = "test_a_recorder_that_is_already_busy_refuses_the_record_and_frees_the_job"
DI_AFTER_PENDING = "test_a_record_after_a_discard_while_pending_works"
DI_STALE_NONE = "test_a_take_whose_job_was_cleared_answers_nothing"
DI_STALE_FAIL = "test_a_check_that_fails_after_it_went_stale_answers_nothing_and_keeps_the_new_job"
RF_ROW_LOGGED = "test_no_speech_logs_its_row"
RF_APPEARED = "test_a_refusal_leaves_no_candidate_even_when_one_appeared_during_the_check"
RF_NO_KEY = "test_a_refusal_does_not_read_the_key"
REC_WHOLE = "test_the_check_runs_on_the_whole_capture"
REC_PLAIN = "test_measures_are_sent_and_saved_as_plain_numbers"
NL_KEY = "test_the_key_is_read_without_the_lock"
# wh-training-word-noisy-reject: the noise advice of a passed recording.
ADV_NOISY = "test_a_noisy_word_through_the_real_check_passes_with_the_advice"
ADV_QUIET = "test_a_quiet_room_passes_without_the_advice"
ADV_EDGE = "test_the_advice_starts_under_the_default_limit_of_30_db"
ADV_NO_SNR = "test_no_snr_measure_gives_no_advice"
ADV_CONFIG = "test_the_configured_limit_decides"
ADV_REFUSED = "test_a_refusal_carries_no_advice"
ADV_BAD = "test_a_limit_that_is_not_a_number_falls_back_to_30"
ADV_DEFAULT = "test_the_default_limit_is_the_recorder_constant"
ADV_INT = "test_an_integer_limit_is_used"

W_RECORDER = "test_the_processor_gets_the_controllers_recorder"
W_CALLBACKS = "test_the_forwarder_gets_the_callback_and_the_capabilities"
W_STORE = "test_the_store_uses_this_providers_config_and_the_appdata_folder"
W_CLIENT = "test_the_client_values_reach_the_controller"
W_MICROPHONE = "test_the_microphone_name_is_read_from_the_forwarder_at_each_call"
W_ENGINE = "test_the_engine_is_the_servers_engine"
W_OTHER_RATE = "test_another_rate_builds_no_controller"
W_NO_APPDATA = "test_no_localappdata_builds_no_controller_and_the_server_starts"
W_OFF_ENDS = "test_transcription_off_calls_end_recording_now"
W_ON_NOT = "test_transcription_on_processes_the_chunk_and_does_not_end_the_take"
W_FAILING_END = "test_a_failing_end_does_not_stop_the_wake_word_branch"
L_LINE = "test_a_training_word_line_reaches_the_forwarder"
W_ADV_KEY = "test_the_engine_noise_advice_key_reaches_the_controller"
W_ADV_DEFAULT = "test_without_the_noise_advice_key_the_limit_is_30_db"

MUTATIONS = [
    # ---- the timers and the lead-in of the capture (constants of the recorder)
    mr("lead-in-shorter", "LEAD_IN_MS = 300", "LEAD_IN_MS = 200", [RS_LONG_BUFFER]),
    mr("lead-in-longer", "LEAD_IN_MS = 300", "LEAD_IN_MS = 400", [RS_LONG_BUFFER]),
    mr("end-silence-shorter", "CAPTURE_END_SILENCE_S = 1.0", "CAPTURE_END_SILENCE_S = 0.5", [RS_END_1S]),
    mr("end-silence-longer", "CAPTURE_END_SILENCE_S = 1.0", "CAPTURE_END_SILENCE_S = 1.5", [RS_END_1S]),
    mr("capture-max-shorter", "CAPTURE_MAX_S = 4.0", "CAPTURE_MAX_S = 3.0", [RS_END_4S]),
    mr("capture-max-longer", "CAPTURE_MAX_S = 4.0", "CAPTURE_MAX_S = 5.0", [RS_END_4S]),
    mr("no-speech-timeout-shorter", "NO_SPEECH_TIMEOUT_S = 10.0", "NO_SPEECH_TIMEOUT_S = 5.0", [RF_NO_SPEECH]),
    mr("no-speech-timeout-longer", "NO_SPEECH_TIMEOUT_S = 10.0", "NO_SPEECH_TIMEOUT_S = 12.0", [RF_NO_SPEECH]),

    # ---- construction -------------------------------------------------------
    m("rate-check-only-below", "        if sample_rate != lead_word_recorder.SAMPLE_RATE:",
      "        if sample_rate < lead_word_recorder.SAMPLE_RATE:", [SR_ONLY_16K]),
    m("rate-check-removed", "        if sample_rate != lead_word_recorder.SAMPLE_RATE:",
      "        if False:", [SR_ONLY_16K]),
    m("vad-factory-ignored", "        self._vad_factory = vad_factory or lead_word_recorder.new_silero_vad",
      "        self._vad_factory = lead_word_recorder.new_silero_vad", [REC_VAD]),
    m("recorder-lead-in-ignores-the-buffer", "            lead_in_ms=min(lead_word_recorder.LEAD_IN_MS, vad_lead_in_ms),",
      "            lead_in_ms=lead_word_recorder.LEAD_IN_MS,", [RS_SHORT_BUFFER]),
    m("recorder-lead-in-is-the-buffer", "            lead_in_ms=min(lead_word_recorder.LEAD_IN_MS, vad_lead_in_ms),",
      "            lead_in_ms=vad_lead_in_ms,", [RS_LONG_BUFFER]),
    m("recorder-lead-in-takes-the-longer", "            lead_in_ms=min(lead_word_recorder.LEAD_IN_MS, vad_lead_in_ms),",
      "            lead_in_ms=max(lead_word_recorder.LEAD_IN_MS, vad_lead_in_ms),", [RS_SHORT_BUFFER]),
    m("recorder-end-silence-is-max", "            end_silence_s=lead_word_recorder.CAPTURE_END_SILENCE_S,\n            max_s=lead_word_recorder.CAPTURE_MAX_S,",
      "            end_silence_s=lead_word_recorder.CAPTURE_MAX_S,\n            max_s=lead_word_recorder.CAPTURE_END_SILENCE_S,", [RS_END_1S]),
    m("recorder-no-speech-timeout-is-max", "            no_speech_timeout_s=lead_word_recorder.NO_SPEECH_TIMEOUT_S,",
      "            no_speech_timeout_s=lead_word_recorder.CAPTURE_MAX_S,", [RF_NO_SPEECH]),
    m("recorder-start-and-finish-swapped", "            on_start=self._on_start,\n            on_finish=self._on_finish,",
      "            on_start=self._on_finish,\n            on_finish=self._on_start,", [REC_ORDER]),

    # ---- sending ------------------------------------------------------------
    m("send-error-escapes", "        except Exception:\n            logger.exception(f\"Cannot send the training-word result '{stage}'\")",
      "        except KeyError:\n            logger.exception(f\"Cannot send the training-word result '{stage}'\")", [F_FORWARDER]),
    m("send-drops-the-fields", "            self.forwarder.send_lead_word_result(request_id, stage, **fields)",
      "            self.forwarder.send_lead_word_result(request_id, stage)", [REC_MEASURES]),
    m("send-wrong-stage", "            self.forwarder.send_lead_word_result(request_id, stage, **fields)",
      "            self.forwarder.send_lead_word_result(request_id, 'status', **fields)", [REC_ORDER]),
    m("state-loaded-always-true", "            loaded=self.engine.has_lead_word(),", "            loaded=True,", [ST_EMPTY]),
    m("state-loaded-always-false", "            loaded=self.engine.has_lead_word(),", "            loaded=False,", [ST_KEPT]),
    m("state-recorded-is-microphone", "            recorded=state.recorded,\n            microphone=state.microphone,",
      "            recorded=state.microphone,\n            microphone=state.microphone,", [ST_KEPT]),
    m("state-microphone-is-recorded", "            recorded=state.recorded,\n            microphone=state.microphone,",
      "            recorded=state.recorded,\n            microphone=state.recorded,", [ST_KEPT]),
    m("state-previous-wrong", "            previous_exists=state.previous_exists,", "            previous_exists=False,", [K_REPLACEMENT]),
    # wh-first-sound-lost.23.1.1: hello.wav exists, also with no recorded date.
    m("state-current-wrong", "            current_exists=state.current_exists,", "            current_exists=False,",
      [ST_KEPT, R_EARLIER, OFF_ON_CURRENT]),
    m("state-current-from-the-date", "            current_exists=state.current_exists,",
      "            current_exists=bool(state.recorded),", [R_EARLIER]),
    m("state-key-path-always", "        if stage == \"status\":\n            fields[\"key_path\"] = state.key_path",
      "        if True:\n            fields[\"key_path\"] = state.key_path", [K_ORDER]),
    m("state-key-path-never", "        if stage == \"status\":\n            fields[\"key_path\"] = state.key_path",
      "        if False:\n            fields[\"key_path\"] = state.key_path", [ST_KEY]),
    m("state-key-path-wrong-value", "            fields[\"key_path\"] = state.key_path",
      "            fields[\"key_path\"] = state.recorded", [ST_KEY]),

    # ---- capabilities -------------------------------------------------------
    m("capabilities-not-supported", "                \"supported\": True,\n                \"loaded\": bool(self.engine.has_lead_word()),",
      "                \"supported\": False,\n                \"loaded\": bool(self.engine.has_lead_word()),", [CAP_FIELDS]),
    m("capabilities-loaded-always", "                \"loaded\": bool(self.engine.has_lead_word()),",
      "                \"loaded\": True,", [CAP_FIELDS]),
    m("capabilities-loaded-never", "                \"loaded\": bool(self.engine.has_lead_word()),",
      "                \"loaded\": False,", [CAP_FIELDS]),
    m("capabilities-recorded-lost", "                \"recorded\": self.store.status().recorded,",
      "                \"recorded\": \"\",", [CAP_FIELDS]),
    m("capabilities-error-escapes", "        except Exception:\n            logger.exception(\"Cannot read the training-word state\")",
      "        except KeyError:\n            logger.exception(\"Cannot read the training-word state\")", [CAP_NEVER_RAISES]),
    m("capabilities-fallback-not-supported", "            return {\"supported\": True, \"loaded\": False, \"recorded\": \"\"}",
      "            return {\"supported\": False, \"loaded\": False, \"recorded\": \"\"}", [CAP_NEVER_RAISES]),
    m("capabilities-fallback-loaded", "            return {\"supported\": True, \"loaded\": False, \"recorded\": \"\"}",
      "            return {\"supported\": True, \"loaded\": True, \"recorded\": \"\"}", [CAP_NEVER_RAISES]),
    m("capabilities-take-the-lock",
      "            return {\n                \"supported\": True,\n                \"loaded\": bool(self.engine.has_lead_word()),\n                \"recorded\": self.store.status().recorded,\n            }",
      "            with self._lock:\n                return {\n                    \"supported\": True,\n                    \"loaded\": bool(self.engine.has_lead_word()),\n                    \"recorded\": self.store.status().recorded,\n                }",
      [NL_READS]),

    # ---- commands -----------------------------------------------------------
    m("command-error-not-answered", "            self._send(request_id, \"failed\", reason=f\"Training word: {e}\")",
      "            pass", [F_COMMAND]),
    m("command-error-reason-lost", "            self._send(request_id, \"failed\", reason=f\"Training word: {e}\")",
      "            self._send(request_id, \"failed\", reason=\"\")", [F_COMMAND]),
    m("command-error-reaches-the-caller", "        except Exception as e:\n            logger.exception(f\"Training-word command {command} failed\")",
      "        except KeyError as e:\n            logger.exception(f\"Training-word command {command} failed\")", [F_COMMAND]),
    m("dispatch-status-wrong-stage", "            self._send_state(request_id, \"status\", self.store.status())\n        elif command == \"lead_word_record\":",
      "            self._send_state(request_id, \"kept\", self.store.status())\n        elif command == \"lead_word_record\":", [ST_EMPTY]),
    m("dispatch-status-takes-the-lock",
      "        if command == \"lead_word_status\":\n            self._send_state(request_id, \"status\", self.store.status())",
      "        if command == \"lead_word_status\":\n            with self._lock:\n                self._send_state(request_id, \"status\", self.store.status())",
      [NL_READS]),
    m("dispatch-record-is-discard", "        elif command == \"lead_word_record\":\n            self._record(request_id)",
      "        elif command == \"lead_word_record\":\n            self._discard(request_id)", [REC_ORDER]),
    m("dispatch-discard-is-record", "        elif command == \"lead_word_discard\":\n            self._discard(request_id)",
      "        elif command == \"lead_word_discard\":\n            self._record(request_id)", [DI_NOTHING]),
    m("dispatch-store-commands-ignored", "        elif command in _STORE_COMMANDS:\n            self._store_command(command, request_id)",
      "        elif False:\n            self._store_command(command, request_id)", [K_FIRST]),
    m("dispatch-unknown-silent", "            self._send(\n                request_id, \"failed\",\n                reason=f\"Unknown training-word command: {command}\")",
      "            pass", [F_UNKNOWN]),
    m("dispatch-unknown-reason", "                reason=f\"Unknown training-word command: {command}\")",
      "                reason=\"Unknown\")", [F_UNKNOWN]),

    # ---- record -------------------------------------------------------------
    m("busy-reason-changed", "BUSY_REASON = \"A recording is already in progress\"",
      "BUSY_REASON = \"Busy\"", [BUSY_PENDING]),
    m("record-busy-never", "            if self._job is not None:\n                busy = True",
      "            if False:\n                busy = True", [BUSY_FIRST_RUNS]),
    m("record-busy-always", "            if self._job is not None:\n                busy = True",
      "            if True:\n                busy = True", [REC_ORDER]),
    m("record-job-not-kept", "                self._job = _Job(request_id)\n                job = self._job",
      "                job = _Job(request_id)", [BUSY_PENDING]),
    m("record-job-request-id-lost", "                self._job = _Job(request_id)", "                self._job = _Job('')", [REC_ORDER]),
    m("record-busy-silent", "        if busy:\n            self._send(request_id, \"failed\", reason=BUSY_REASON)\n            return",
      "        if busy:\n            return", [BUSY_PENDING]),
    m("record-busy-goes-on", "            self._send(request_id, \"failed\", reason=BUSY_REASON)\n            return\n        try:",
      "            self._send(request_id, \"failed\", reason=BUSY_REASON)\n        try:", [BUSY_PENDING]),
    m("record-older-candidate-kept", "            self.store.discard_candidate()\n            if not self.recorder.request():",
      "            if not self.recorder.request():", [REC_OLDER]),
    m("record-request-ignored", "            if not self.recorder.request():\n                raise RuntimeError(BUSY_REASON)",
      "            self.recorder.request()", [BUSY_RECORDER]),
    m("record-request-inverted", "            if not self.recorder.request():",
      "            if self.recorder.request():", [REC_ORDER]),
    m("record-failure-keeps-the-job", "            with self._lock:\n                if self._job is job:\n                    self._job = None\n            logger.exception(\"Cannot start",
      "            logger.exception(\"Cannot start", [BUSY_RECORDER]),
    m("record-failure-silent",
      "            logger.exception(\"Cannot start the training-word recording\")\n            self._send(request_id, \"failed\", reason=str(e))",
      "            logger.exception(\"Cannot start the training-word recording\")\n            pass", [BUSY_RECORDER]),

    # ---- discard ------------------------------------------------------------
    m("discard-keeps-the-job", "        with self._lock:\n            self._job = None\n            self.store.discard_candidate()",
      "        with self._lock:\n            self.store.discard_candidate()", [DI_AFTER_PENDING]),
    m("discard-keeps-the-candidate", "            self._job = None\n            self.store.discard_candidate()\n        self.recorder.cancel()",
      "            self._job = None\n        self.recorder.cancel()", [DI_CANDIDATE]),
    m("discard-recorder-not-cancelled", "        self.recorder.cancel()\n        self._send_state(request_id, \"status\", self.store.status())",
      "        self._send_state(request_id, \"status\", self.store.status())", [DI_NEXT, DI_AFTER_PENDING]),
    m("discard-answer-wrong-stage", "        self.recorder.cancel()\n        self._send_state(request_id, \"status\", self.store.status())",
      "        self.recorder.cancel()\n        self._send_state(request_id, \"kept\", self.store.status())", [DI_NOTHING]),
    m("discard-answer-lost", "        self.recorder.cancel()\n        self._send_state(request_id, \"status\", self.store.status())",
      "        self.recorder.cancel()", [DI_NOTHING]),

    # ---- Keep, restore, off, on ---------------------------------------------
    m("store-busy-check-removed", "    def _store_command(self, command: str, request_id: str) -> None:\n        if self._job is not None:",
      "    def _store_command(self, command: str, request_id: str) -> None:\n        if False:", [BUSY_RECORDING]),
    m("store-busy-check-inverted", "    def _store_command(self, command: str, request_id: str) -> None:\n        if self._job is not None:",
      "    def _store_command(self, command: str, request_id: str) -> None:\n        if self._job is None:", [K_FIRST]),
    m("store-busy-reason-lost", "            self._send(request_id, \"failed\", reason=BUSY_REASON)\n            return\n        action, stage",
      "            self._send(request_id, \"failed\", reason=\"\")\n            return\n        action, stage", [BUSY_RECORDING]),
    m("store-busy-goes-on", "            self._send(request_id, \"failed\", reason=BUSY_REASON)\n            return\n        action, stage",
      "            self._send(request_id, \"failed\", reason=BUSY_REASON)\n        action, stage", [BUSY_RECORDING]),
    m("store-action-gets-no-engine-method", "            state = getattr(self.store, action)(self.engine.set_lead_word)",
      "            state = getattr(self.store, action)(lambda audio: None)", [K_FIRST]),
    m("store-error-escapes", "        except LeadWordStoreError as e:\n            self._send(request_id, \"failed\", reason=str(e))\n            return",
      "        except KeyError as e:\n            self._send(request_id, \"failed\", reason=str(e))\n            return", [K_NONE]),
    m("store-error-reason-lost", "            self._send(request_id, \"failed\", reason=str(e))\n            return\n        self._send_state(request_id, stage, state)",
      "            self._send(request_id, \"failed\", reason=\"\")\n            return\n        self._send_state(request_id, stage, state)", [K_NONE]),
    m("store-error-goes-on", "            self._send(request_id, \"failed\", reason=str(e))\n            return\n        self._send_state(request_id, stage, state)",
      "            self._send(request_id, \"failed\", reason=str(e))\n        self._send_state(request_id, stage, state)", [K_NONE]),
    m("store-answer-wrong-stage", "        self._send_state(request_id, stage, state)\n\n    # -- recording",
      "        self._send_state(request_id, \"status\", state)\n\n    # -- recording", [K_ORDER]),
    m("store-commands-keep-stage", "    \"lead_word_keep\": (\"keep\", \"kept\"),", "    \"lead_word_keep\": (\"keep\", \"restored\"),", [K_ORDER]),
    m("store-commands-keep-action", "    \"lead_word_keep\": (\"keep\", \"kept\"),", "    \"lead_word_keep\": (\"restore\", \"kept\"),", [K_FIRST]),
    m("store-commands-restore-stage", "    \"lead_word_restore\": (\"restore\", \"restored\"),", "    \"lead_word_restore\": (\"restore\", \"kept\"),", [R_SWAPS]),
    m("store-commands-restore-action", "    \"lead_word_restore\": (\"restore\", \"restored\"),", "    \"lead_word_restore\": (\"keep\", \"restored\"),", [R_SWAPS]),
    m("store-commands-off-stage", "    \"lead_word_off\": (\"turn_off\", \"off\"),", "    \"lead_word_off\": (\"turn_off\", \"on\"),", [OFF_FULL]),
    m("store-commands-off-action", "    \"lead_word_off\": (\"turn_off\", \"off\"),", "    \"lead_word_off\": (\"turn_on\", \"off\"),", [OFF_FULL]),
    m("store-commands-on-stage", "    \"lead_word_on\": (\"turn_on\", \"on\"),", "    \"lead_word_on\": (\"turn_on\", \"off\"),", [ON_FULL]),
    m("store-commands-on-action", "    \"lead_word_on\": (\"turn_on\", \"on\"),", "    \"lead_word_on\": (\"turn_off\", \"on\"),", [ON_FULL]),

    # ---- recording start (audio thread) -------------------------------------
    m("start-capture-job-not-set", "            job = self._job\n            self._capture_job = job",
      "            job = self._job", [REC_ORDER]),
    m("start-capture-job-is-none", "            job = self._job\n            self._capture_job = job",
      "            job = self._job\n            self._capture_job = None", [REC_ORDER]),
    m("start-no-job-not-cancelled", "            # A discard slipped in between the request and the first chunk.\n            self.recorder.cancel()\n            return",
      "            # A discard slipped in between the request and the first chunk.\n            return", [NJ_CANCEL]),
    m("start-no-job-goes-on", "            self.recorder.cancel()\n            return\n        self._send(job.request_id, \"listening\")",
      "            self.recorder.cancel()\n        self._send(job.request_id, \"listening\")", [NJ_CANCEL]),
    m("start-no-listening", "        self._send(job.request_id, \"listening\")", "        pass", [REC_ORDER]),
    m("start-listening-wrong-id", "        self._send(job.request_id, \"listening\")", "        self._send('', \"listening\")", [REC_ORDER]),

    # ---- the end of the capture (audio thread) ------------------------------
    m("finish-stale-job-answers", "            if self._job is not job:\n                # Discarded while recording. A new record can already be\n                # pending; this take must not answer it.\n                job = None",
      "            if False:\n                job = None", [DI_STALE]),
    m("finish-stale-job-always", "            if self._job is not job:\n                # Discarded while recording. A new record can already be\n                # pending; this take must not answer it.\n                job = None",
      "            if True:\n                job = None", [REC_ORDER]),
    m("finish-no-job-checked-by-equality", "            if self._job is not job:\n                # Discarded while recording.",
      "            if self._job != job and self._job is not None:\n                # Discarded while recording.", [DI_STALE_NONE]),
    m("finish-no-job-goes-on", "        if job is None:\n            return\n        try:\n            self._check_and_report(job, recording)",
      "        if job is None:\n            pass\n        try:\n            self._check_and_report(job, recording)", [DI_STALE, DI_STALE_NONE]),
    m("finish-error-not-answered", "            if current:\n                self._send(\n                    job.request_id, \"failed\",\n                    reason=f\"The check could not finish: {e}\")",
      "            if current:\n                pass", [F_CHECK]),
    m("finish-error-answered-when-stale", "            if current:\n                self._send(\n                    job.request_id, \"failed\",",
      "            if True:\n                self._send(\n                    job.request_id, \"failed\",", [DI_STALE_FAIL]),
    m("finish-error-job-kept", "                current = self._job is job\n                if current:\n                    self._job = None",
      "                current = self._job is job", [F_CHECK]),
    m("finish-error-job-ended-when-stale", "                current = self._job is job\n                if current:\n                    self._job = None",
      "                current = self._job is job\n                self._job = None", [DI_STALE_FAIL]),
    m("finish-error-reason-lost", "                    reason=f\"The check could not finish: {e}\")",
      "                    reason=\"\")", [F_CHECK]),
    m("finish-error-wrong-id", "                self._send(\n                    job.request_id, \"failed\",\n                    reason=f\"The check could not finish",
      "                self._send(\n                    '', \"failed\",\n                    reason=f\"The check could not finish", [F_CHECK]),

    # ---- the check and its report (audio thread) ----------------------------
    m("check-no-checking-message", "        self._send(job.request_id, \"checking\")", "        pass", [REC_ORDER]),
    m("check-no-speech-when-samples", "        if recording.samples is None:", "        if recording.samples is not None:", [RF_NO_SPEECH]),
    m("check-waited-ms-wrong-unit", "            waited_ms = recording.waited_samples * 1000 // self.recorder.sample_rate",
      "            waited_ms = recording.waited_samples // self.recorder.sample_rate", [RF_NO_SPEECH]),
    m("check-waited-ms-in-samples", "            waited_ms = recording.waited_samples * 1000 // self.recorder.sample_rate",
      "            waited_ms = recording.waited_samples", [RF_NO_SPEECH]),
    m("check-no-speech-regions", "                {\"capture_ms\": waited_ms, \"regions\": 0},", "                {\"capture_ms\": waited_ms, \"regions\": 1},", [RF_NO_SPEECH]),
    m("check-no-speech-no-capture-ms", "                {\"capture_ms\": waited_ms, \"regions\": 0},", "                {\"regions\": 0},", [RF_NO_SPEECH]),
    m("check-no-speech-row", "                lead_word_recorder.ROW_NO_SPEECH,\n                lead_word_recorder.REASONS[lead_word_recorder.ROW_NO_SPEECH],",
      "                lead_word_recorder.ROW_TOO_QUIET,\n                lead_word_recorder.REASONS[lead_word_recorder.ROW_NO_SPEECH],", [RF_ROW_LOGGED]),
    m("check-no-speech-reason", "                lead_word_recorder.REASONS[lead_word_recorder.ROW_NO_SPEECH],\n                {\"capture_ms\"",
      "                lead_word_recorder.REASONS[lead_word_recorder.ROW_TOO_QUIET],\n                {\"capture_ms\"", [RF_NO_SPEECH]),
    m("check-runs-on-the-wrong-audio", "                recording.samples,\n                self._vad_factory(self._silero_threshold),",
      "                recording.samples[:-512],\n                self._vad_factory(self._silero_threshold),", [REC_WHOLE]),
    m("check-vad-threshold-lost", "                self._vad_factory(self._silero_threshold),", "                self._vad_factory(0.5),", [REC_VAD]),
    m("check-decode-bypasses-engine-method", "                lambda cut: lead_word_recorder.decode_cut(self.engine, cut),",
      "                lambda cut: self.engine._recognize(lead_word_recorder.cut_to_float(cut)),", [REC_DECODE_ALONE]),
    m("check-log-line-lost", "        logger.info(lead_word_recorder.format_log_line(result))", "        pass", [REC_LOG]),
    m("check-measures-not-plain", "        measures = {k: _plain(v) for k, v in result.measures.items()}",
      "        measures = dict(result.measures)", [REC_PLAIN]),
    m("check-measures-empty", "        measures = {k: _plain(v) for k, v in result.measures.items()}",
      "        measures = {}", [REC_MEASURES]),
    m("check-key-path-always-read", "        key_path = self.store.status().key_path if result.passed else \"\"",
      "        key_path = self.store.status().key_path", [RF_NO_KEY]),
    m("check-key-path-never-read", "        key_path = self.store.status().key_path if result.passed else \"\"",
      "        key_path = \"\"", [K_KEY_PATH, DI_STATUS_ERROR]),
    m("check-key-path-read-inside-the-lock",
      "        key_path = self.store.status().key_path if result.passed else \"\"\n\n        # crewcut: the lock is held across one small file write (the\n        # candidate WAV and its measures file) so that a discard cannot slip\n        # between the job check and the write. Write to a temp name and\n        # rename after the check if this ever needs to be shorter.\n        with self._lock:\n",
      "        # crewcut: the lock is held across one small file write (the\n        # candidate WAV and its measures file) so that a discard cannot slip\n        # between the job check and the write. Write to a temp name and\n        # rename after the check if this ever needs to be shorter.\n        with self._lock:\n            key_path = self.store.status().key_path if result.passed else \"\"\n",
      [NL_KEY]),
    m("check-stale-job-writes", "            if self._job is not job:\n                return  # discarded during the check: no file, no message",
      "            if False:\n                return  # discarded during the check: no file, no message", [DI_CHECK]),
    m("check-stale-job-always", "            if self._job is not job:\n                return  # discarded during the check: no file, no message",
      "            if True:\n                return  # discarded during the check: no file, no message", [REC_ORDER]),
    m("check-passed-not-saved", "            if result.passed:\n                self.store.save_candidate(\n                    result.cut, measures, self._get_microphone())",
      "            if result.passed:\n                pass", [REC_CANDIDATE]),
    m("check-saved-without-microphone", "                    result.cut, measures, self._get_microphone())",
      "                    result.cut, measures, '')", [REC_CANDIDATE]),
    m("check-saved-whole-capture", "                    result.cut, measures, self._get_microphone())",
      "                    recording.samples, measures, self._get_microphone())", [REC_CANDIDATE]),
    m("check-refused-not-deleted", "            else:\n                self.store.delete_refused()", "            else:\n                pass", [RF_APPEARED]),
    m("check-job-not-ended", "                self.store.delete_refused()\n            self._job = None",
      "                self.store.delete_refused()", [RF_AFTER]),
    m("check-candidate-write-after-the-lock",
      "        with self._lock:\n            if self._job is not job:\n                return  # discarded during the check: no file, no message\n            if result.passed:\n                self.store.save_candidate(\n                    result.cut, measures, self._get_microphone())\n            else:\n                self.store.delete_refused()\n            self._job = None\n",
      "        with self._lock:\n            if self._job is not job:\n                return  # discarded during the check: no file, no message\n            self._job = None\n        if result.passed:\n            self.store.save_candidate(\n                result.cut, measures, self._get_microphone())\n        else:\n            self.store.delete_refused()\n",
      [NL_DISCARD]),
    m("check-passed-wrong-stage", "                job.request_id, \"passed\", measures=measures,\n                key_path=key_path,",
      "                job.request_id, \"refused\", measures=measures,\n                key_path=key_path,", [REC_MEASURES]),
    m("check-passed-no-key-path", "                job.request_id, \"passed\", measures=measures,\n                key_path=key_path,",
      "                job.request_id, \"passed\", measures=measures,\n               ", [K_KEY_PATH]),
    m("check-passed-no-measures", "                job.request_id, \"passed\", measures=measures,\n                key_path=key_path,",
      "                job.request_id, \"passed\",\n                key_path=key_path,", [REC_MEASURES]),
    # ---- the noise advice (wh-training-word-noisy-reject) --------------------
    m("check-passed-no-advice",
      "                key_path=key_path,\n                noise_advice=lead_word_recorder.noise_advice(\n                    measures, self.noise_advice_db))",
      "                key_path=key_path)", [ADV_NOISY, ADV_QUIET]),
    m("check-passed-advice-always",
      "                noise_advice=lead_word_recorder.noise_advice(\n                    measures, self.noise_advice_db))",
      "                noise_advice=True)", [ADV_QUIET, ADV_NO_SNR]),
    m("check-passed-advice-never",
      "                noise_advice=lead_word_recorder.noise_advice(\n                    measures, self.noise_advice_db))",
      "                noise_advice=False)", [ADV_NOISY, ADV_EDGE]),
    m("check-passed-advice-default-limit", "                    measures, self.noise_advice_db))",
      "                    measures, lead_word_recorder.NOISE_ADVICE_DB))", [ADV_CONFIG]),
    m("check-refused-carries-advice",
      "                job.request_id, \"refused\", reason=result.reason,\n                measures=measures)",
      "                job.request_id, \"refused\", reason=result.reason,\n                measures=measures, noise_advice=True)", [ADV_REFUSED]),
    m("advice-limit-not-stored", "        self.noise_advice_db = _advice_limit(noise_advice_db)",
      "        self.noise_advice_db = lead_word_recorder.NOISE_ADVICE_DB", [ADV_CONFIG]),
    m("advice-limit-unchecked", "        self.noise_advice_db = _advice_limit(noise_advice_db)",
      "        self.noise_advice_db = noise_advice_db", [ADV_BAD]),
    m("advice-limit-default-argument", "        noise_advice_db=lead_word_recorder.NOISE_ADVICE_DB,\n    ):",
      "        noise_advice_db=20.0,\n    ):", [ADV_DEFAULT]),
    m("advice-limit-takes-a-bool",
      "    if (isinstance(value, (int, float)) and not isinstance(value, bool)\n",
      "    if (isinstance(value, (int, float))\n", [ADV_BAD]),
    m("advice-limit-takes-nan", "            and math.isfinite(value)):", "            and True):", [ADV_BAD]),
    m("advice-limit-refuses-an-integer",
      "    if (isinstance(value, (int, float)) and not isinstance(value, bool)\n",
      "    if (isinstance(value, float) and not isinstance(value, bool)\n", [ADV_INT]),
    m("advice-limit-wrong-fallback",
      "    return lead_word_recorder.NOISE_ADVICE_DB\n\n\nclass LeadWordController",
      "    return 0.0\n\n\nclass LeadWordController", [ADV_BAD]),
    m("advice-limit-no-warning",
      "    logger.warning(\n        f\"lead_word_noise_advice_db = {value!r} is not a number; using \"\n        f\"{lead_word_recorder.NOISE_ADVICE_DB}\")",
      "    pass", [ADV_BAD]),
    m("check-passed-wrong-id", "            self._send(\n                job.request_id, \"passed\"", "            self._send(\n                '', \"passed\"", [REC_ORDER]),
    m("check-refused-wrong-stage", "                job.request_id, \"refused\", reason=result.reason,\n                measures=measures)",
      "                job.request_id, \"passed\", reason=result.reason,\n                measures=measures)", [RF_ROW]),
    m("check-refused-no-reason", "                job.request_id, \"refused\", reason=result.reason,\n                measures=measures)",
      "                job.request_id, \"refused\", measures=measures)", [RF_ROW]),
    m("check-refused-no-measures", "                job.request_id, \"refused\", reason=result.reason,\n                measures=measures)",
      "                job.request_id, \"refused\", reason=result.reason)", [RF_NO_SPEECH, RF_NOT_HELLO]),
    m("check-refused-wrong-id", "            self._send(\n                job.request_id, \"refused\"", "            self._send(\n                '', \"refused\"", [RF_NO_SPEECH]),
    m("check-passed-test-inverted", "        if result.passed:\n            self._send(\n                job.request_id, \"passed\"",
      "        if not result.passed:\n            self._send(\n                job.request_id, \"passed\"", [REC_ORDER]),
    m("plain-numpy-kept", "    if isinstance(value, np.generic):\n        return value.item()", "    if False:\n        return value.item()", [REC_PLAIN]),
    m("plain-everything-converted", "    if isinstance(value, np.generic):\n        return value.item()", "    if True:\n        return value.item()", [REC_MEASURES]),

    # ---- main.py ------------------------------------------------------------
    mm("main-builds-no-controller-on-error", "        except Exception as e:\n            logger.warning(f\"Training word is not available: {e!r}\")\n            return None",
      "        except OSError as e:\n            logger.warning(f\"Training word is not available: {e!r}\")\n            return None", [W_OTHER_RATE]),
    mm("main-builds-the-controller-anyway", "            logger.warning(f\"Training word is not available: {e!r}\")\n            return None",
      "            logger.warning(f\"Training word is not available: {e!r}\")\n            return self.lead_word_controller", [W_OTHER_RATE]),
    mm("main-store-config-path", "                lead_word_folder(), Path(__file__).parent / \"config.toml\")",
      "                lead_word_folder(), Path(__file__).parent / \"other.toml\")", [W_STORE]),
    mm("main-store-folder", "                lead_word_folder(), Path(__file__).parent / \"config.toml\")",
      "                Path(__file__).parent, Path(__file__).parent / \"config.toml\")", [W_STORE]),
    mm("main-controller-engine", "                engine=self.engine,\n                forwarder=self.forwarder,\n                store=store,",
      "                engine=None,\n                forwarder=self.forwarder,\n                store=store,", [W_ENGINE]),
    mm("main-controller-threshold", "                silero_threshold=silero_threshold,", "                silero_threshold=0.5,", [W_CLIENT]),
    mm("main-controller-microphone", "                get_microphone=lambda: self.forwarder.microphone_device,",
      "                get_microphone=lambda: '',", [W_MICROPHONE]),
    mm("main-controller-microphone-cached", "                get_microphone=lambda: self.forwarder.microphone_device,",
      "                get_microphone=(lambda name=self.forwarder.microphone_device: name),", [W_MICROPHONE]),
    mm("main-controller-rate", "                sample_rate=sample_rate,\n                vad_lead_in_ms=vad_lead_in_ms,",
      "                sample_rate=16000,\n                vad_lead_in_ms=vad_lead_in_ms,", [W_OTHER_RATE]),
    mm("main-controller-lead-in", "                vad_lead_in_ms=vad_lead_in_ms,\n                noise_advice_db=",
      "                vad_lead_in_ms=300,\n                noise_advice_db=", [W_CLIENT]),
    mm("main-advice-key-ignored",
      "            noise_advice_db=engine_config.get(\n                \"lead_word_noise_advice_db\", lead_word_recorder.NOISE_ADVICE_DB))",
      "            noise_advice_db=lead_word_recorder.NOISE_ADVICE_DB)", [W_ADV_KEY]),
    mm("main-advice-key-misspelled", "                \"lead_word_noise_advice_db\", lead_word_recorder.NOISE_ADVICE_DB))",
      "                \"noise_advice_db\", lead_word_recorder.NOISE_ADVICE_DB))", [W_ADV_KEY]),
    mm("main-advice-key-wrong-default", "                \"lead_word_noise_advice_db\", lead_word_recorder.NOISE_ADVICE_DB))",
      "                \"lead_word_noise_advice_db\", 20.0))", [W_ADV_DEFAULT]),
    mm("main-advice-not-passed-on", "                noise_advice_db=noise_advice_db,", "", [W_ADV_KEY]),
    mm("main-callback-not-set", "        self.forwarder.lead_word_callback = controller.handle_command",
      "        pass", [W_CALLBACKS]),
    mm("main-capabilities-not-set", "        self.forwarder.lead_word_capabilities = controller.capabilities",
      "        pass", [W_CALLBACKS]),
    mm("main-callback-wrong-method", "        self.forwarder.lead_word_callback = controller.handle_command",
      "        self.forwarder.lead_word_callback = controller.capabilities", [W_CALLBACKS]),
    mm("main-recorder-not-given", "            chunk_recorder=(\n                self.lead_word_controller.recorder\n                if self.lead_word_controller is not None else None),",
      "            chunk_recorder=None,", [W_RECORDER]),
    mm("main-recorder-given-without-controller", "                if self.lead_word_controller is not None else None),",
      "                if True else None),", [W_NO_APPDATA]),
    mm("main-release-does-not-end-the-take", "                        self.audio_processor.end_recording_now()",
      "                        pass", [W_OFF_ENDS]),
    mm("main-release-error-escapes", "                    except Exception as e:\n                        logger.error(f\"Cannot end the training-word take: {e}\")",
      "                    except KeyError as e:\n                        logger.error(f\"Cannot end the training-word take: {e}\")", [W_FAILING_END]),
    mm("main-on-ends-the-take", "                self.audio_processor.process_chunk(chunk)\n                self._load_work_s += time.perf_counter() - _load_t0",
      "                self.audio_processor.end_recording_now()\n                self.audio_processor.process_chunk(chunk)\n                self._load_work_s += time.perf_counter() - _load_t0",
      [W_ON_NOT]),
    ml("main-logger-names-without-store", "    lead_word_store.__name__,", "    lead_word_controller.__name__,", [L_LINE], ),
    ml("main-logger-names-without-recorder", "    lead_word_recorder.__name__,", "    lead_word_controller.__name__,", [L_LINE]),
    ml("main-logger-names-without-controller", "    lead_word_controller.__name__,\n    lead_word_recorder.__name__,",
      "    lead_word_recorder.__name__,\n    lead_word_recorder.__name__,", [L_LINE]),
    ml("main-logger-handler-not-attached", "        for name in LEAD_WORD_LOGGER_NAMES:\n            logging.getLogger(name).addHandler(ws_log_handler)",
      "        for name in ():\n            logging.getLogger(name).addHandler(ws_log_handler)", [L_LINE]),
]

# Mutants that no test can fail for. `--equivalent` runs them; each must be
# reported SURVIVED, and one that is caught is no longer equivalent.
EQUIVALENT_MUTANTS = [
    # The constructor raises for any rate other than 16000, so at this line the
    # rate is always 16000 and the literal changes nothing.
    m("recorder-rate-fixed", "            sample_rate=sample_rate,\n            lead_in_ms=",
      "            sample_rate=16000,\n            lead_in_ms=", [SR_RECORDER]),
    # Only the forwarder thread sets _job to a job, one command at a time, and
    # the audio thread clears it only for its own job. Between the line that
    # sets the job and this except block nothing else can have replaced it, so
    # `self._job is job` is always true here.
    m("record-failure-ends-any-job", "                if self._job is job:\n                    self._job = None\n            logger.exception",
      "                if True:\n                    self._job = None\n            logger.exception", [DI_STALE]),
    # Once _job is None under the lock, a check that is writing its candidate
    # finds a job that is not its own and writes nothing; a check that wrote
    # before holds the lock, so the discard waited for it. Deleting after the
    # lock is released finds the same files either way, and a new check cannot
    # start before the next record command, which runs after this one returns.
    m("discard-delete-outside-the-lock",
      "        with self._lock:\n            self._job = None\n            self.store.discard_candidate()\n        self.recorder.cancel()",
      "        with self._lock:\n            self._job = None\n        self.store.discard_candidate()\n        self.recorder.cancel()",
      [NL_DISCARD]),
    # Every on_finish is preceded by an on_start that sets _capture_job again,
    # so the value left behind by the previous take is never read.
    # The int limit is compared with a float snr_db; 25 and 25.0 give the
    # same answer for every snr_db, so float() changes nothing a caller sees.
    m("advice-limit-kept-as-an-integer", "        return float(value)\n    logger.warning(",
      "        return value\n    logger.warning(", [ADV_INT]),
    # main.py always passes noise_advice_db by keyword, and no other caller
    # builds the controller, so the builder's default is never used.
    mm("main-builder-advice-default", "        noise_advice_db=lead_word_recorder.NOISE_ADVICE_DB,\n    ) -> LeadWordController | None:",
      "        noise_advice_db=20.0,\n    ) -> LeadWordController | None:", [W_ADV_DEFAULT]),
    m("finish-capture-job-kept", "            job = self._capture_job\n            self._capture_job = None",
      "            job = self._capture_job", [DI_STALE]),
]


if __name__ == "__main__":
    main(MUTATIONS, EQUIVALENT_MUTANTS)
