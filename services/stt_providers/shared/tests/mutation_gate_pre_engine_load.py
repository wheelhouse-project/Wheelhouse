"""Focused mutation proof for wh-load-unmeasured-work.

Uses the existing shared gate runner's byte restoration, compilation,
collection, baseline, and timeout checks. No microphone/model is launched.
"""
from pathlib import Path

from mutation_gate_runner import run
from mutation_gate_load_metrics import MUTATIONS as EXISTING

SHARED = Path(__file__).resolve().parents[1]
PROCESSOR = SHARED / 'shared_stt/audio_processor.py'
WAKE = SHARED / 'shared_stt/wake_word_detector.py'
COUNTERS = SHARED / 'shared_stt/pre_engine_metrics.py'
GOOGLE = SHARED.parent / 'google_stt_server/main.py'
PROCEDURE = SHARED.parents[2] / 'docs/testing/stt-cpu-load-test-procedure.md'
TESTS = 'tests/test_pre_engine_load.py'
SHERPA = SHARED.parent / 'sherpa_offline_parakeet_stt_server'
DISTIL = SHARED.parent / 'distil_medium_en'
WIRING_TESTS = 'tests/test_load_diagnostics_wiring.py'
GOOGLE_SERVICE = GOOGLE.parent
CONFIG_KEY_TESTS = 'tests/test_load_diagnostics_config_key.py'
DOC_TESTS = 'tests/test_capture_load_metrics.py'


def mutation(name, path, old, new, *expected):
    return dict(name=name, file=path, old=old, new=new, service=SHARED,
                test_file=TESTS, expect=list(expected))


def provider_mutation(name, service, old, new, *expected):
    """A mutation of a provider's main.py, proved by that provider's own suite."""
    return dict(name=name, file=service / 'main.py', old=old, new=new,
                service=service, test_file=WIRING_TESTS, expect=list(expected))


MUTATIONS = [
    mutation('vad-time-omitted', PROCESSOR,
             '        work = PreEngineWork(len(pcm_bytes), vad_ended - work_started,',
             '        work = PreEngineWork(len(pcm_bytes), 0.0,',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    mutation('agc-time-omitted', PROCESSOR,
             '                             agc_ended - vad_ended)',
             '                             0.0)',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    mutation('keep-warm-time-omitted', PROCESSOR,
             '                    work.keep_warm_s = time.monotonic() - warm_started',
             '                    work.keep_warm_s = 0.0',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    mutation('wake-word-time-omitted', WAKE,
             '                    len(pcm_bytes), wake_word_s=time.monotonic() - started))',
             '                    len(pcm_bytes), wake_word_s=0.0))',
             'test_slow_wake_word_is_reported_at_detection_without_engine_ratio'),
    mutation('lead-in-work-omitted', PROCESSOR,
             '                for previous_work in self._pre_engine_history:',
             '                for previous_work in []:',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    mutation('evicted-silence-still-charged', PROCESSOR,
             '               and self._pre_engine_history_bytes > self._lead_in_capacity_bytes):',
             '               and self._pre_engine_history_bytes > self._lead_in_capacity_bytes * 100):',
             'test_evicted_silence_is_not_charged_to_next_utterance'),
    mutation('nonpositive-capacity-pops-empty-history', PROCESSOR,
             '        while (self._pre_engine_history\n',
             '        while (True\n',
             'test_nonpositive_lead_in_keeps_no_work_and_does_not_break_capture'),
    mutation('open-gate-work-omitted', PROCESSOR,
             '        if self._vad_gate_open:\n            self._pre_engine_work.add(work)',
             '        if self._vad_gate_open:\n            pass',
             'test_forced_endpoint_includes_last_vad_and_agc_but_not_finalize'),
    mutation('utterance-work-does-not-reset', PROCESSOR,
             '        self._load_engine_audio_samples = 0\n        self._pre_engine_work = PreEngineWork()',
             '        self._load_engine_audio_samples = 0',
             'test_new_utterance_and_manual_reset_do_not_inherit_work'),
    mutation('total-omits-keep-warm', COUNTERS,
             '        total_s = self.vad_s + self.agc_s + self.keep_warm_s + self.wake_word_s',
             '        total_s = self.vad_s + self.agc_s + self.wake_word_s',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    mutation('silence-window-never-due', PROCESSOR,
             '        if time.monotonic() - self._pre_engine_idle_started >= 10.0:',
             '        if time.monotonic() - self._pre_engine_idle_started >= 100.0:',
             'test_silence_work_is_reported_without_any_recognizer_utterance'),
    mutation('wake-window-never-due', WAKE,
             '                  and time.monotonic() - self._work_started >= 10.0):',
             '                  and time.monotonic() - self._work_started >= 100.0):',
             'test_wake_word_without_detection_still_reports_bounded_windows'),
    mutation('detection-does-not-report', WAKE,
             "            if detected:\n                self._flush_work('detected')",
             '            if detected:\n                pass',
             'test_slow_wake_word_is_reported_at_detection_without_engine_ratio'),
    mutation('control-reset-not-observed', WAKE,
             '        self._work_reset_requested.set()',
             '        pass',
             'test_reset_during_wake_inference_does_not_lose_its_time'),
    mutation('google-wake-logger-unwired', GOOGLE,
             '            diagnostic_logger=logger,',
             '',
             'test_google_wake_cost_uses_the_logger_that_reaches_its_forwarder'),
    mutation('work-audio-assumes-fixed-chunks', COUNTERS,
             '        self.audio_bytes += other.audio_bytes',
             '        self.audio_bytes += 480',
             'test_slow_stage_is_attributed_beside_unchanged_engine_ratio'),
    # wh-audit13-preengine-load-review.1: the ten-second interval lines follow
    # [debug] log_load_diagnostics, off by default. The per-event onset, reset
    # and detection lines stay, and still cover at most ten seconds of work.
    mutation('silence-interval-line-ignores-the-setting', PROCESSOR,
             "            self._flush_pre_engine_idle('interval', log=self._log_load_diagnostics)",
             "            self._flush_pre_engine_idle('interval', log=True)",
             'test_silence_idle_line_is_off_by_default'),
    mutation('silence-window-kept-when-off', PROCESSOR,
             "        if log:\n"
             "            self._pre_engine_idle.log(load_metrics_logger, 'silence', end, self.sample_rate)\n"
             '        self._pre_engine_idle = PreEngineWork()\n'
             '        self._pre_engine_idle_started = None',
             "        if log:\n"
             "            self._pre_engine_idle.log(load_metrics_logger, 'silence', end, self.sample_rate)\n"
             '            self._pre_engine_idle = PreEngineWork()\n'
             '            self._pre_engine_idle_started = None',
             'test_speech_onset_line_still_reports_the_current_window_when_off'),
    mutation('processor-setting-defaults-on', PROCESSOR,
             '        log_load_diagnostics: bool = False,\n    ):',
             '        log_load_diagnostics: bool = True,\n    ):',
             'test_silence_idle_line_is_off_by_default'),
    mutation('wake-interval-line-ignores-the-setting', WAKE,
             "                self._flush_work('interval', log=self._log_load_diagnostics)",
             "                self._flush_work('interval', log=True)",
             'test_wake_word_idle_line_is_off_by_default'),
    mutation('wake-window-kept-when-off', WAKE,
             "        if log:\n"
             "            self._work.log(self._diagnostic_logger, 'wake_word', end, 16000)\n"
             '        self._work = PreEngineWork()\n'
             '        self._work_started = None',
             "        if log:\n"
             "            self._work.log(self._diagnostic_logger, 'wake_word', end, 16000)\n"
             '            self._work = PreEngineWork()\n'
             '            self._work_started = None',
             'test_wake_word_detection_line_still_reports_the_current_window_when_off'),
    mutation('wake-setting-defaults-on', WAKE,
             '        log_load_diagnostics: bool = False,\n    ):',
             '        log_load_diagnostics: bool = True,\n    ):',
             'test_wake_word_idle_line_is_off_by_default'),
    mutation('google-wake-setting-unwired', GOOGLE,
             '            log_load_diagnostics=cfg.debug.log_load_diagnostics,\n',
             '',
             'test_google_wake_detector_follows_the_load_diagnostics_setting'),
    provider_mutation('parakeet-processor-setting-unwired', SHERPA,
                      '            capture_stats=self.audio_capture.get_stats,\n'
                      '            # wh-audit13-preengine-load-review.1: the ten-second silence\n'
                      '            # line follows the same [debug] flag as the window line below.\n'
                      '            log_load_diagnostics=log_load_diagnostics,\n',
                      '            capture_stats=self.audio_capture.get_stats,\n',
                      'test_the_flag_reaches_the_processor'),
    provider_mutation('parakeet-wake-setting-unwired', SHERPA,
                      '                sensitivity=wake_word_sensitivity,\n'
                      '                log_load_diagnostics=log_load_diagnostics,\n',
                      '                sensitivity=wake_word_sensitivity,\n',
                      'test_the_flag_reaches_the_wake_word_detector'),
    provider_mutation('distil-processor-setting-unwired', DISTIL,
                      '            # line is off unless [debug] log_load_diagnostics turns it on.\n'
                      '            log_load_diagnostics=log_load_diagnostics,\n',
                      '            # line is off unless [debug] log_load_diagnostics turns it on.\n',
                      'test_the_flag_reaches_the_processor'),
    provider_mutation('distil-wake-setting-unwired', DISTIL,
                      '                sensitivity=wake_word_sensitivity,\n'
                      '                log_load_diagnostics=log_load_diagnostics,\n',
                      '                sensitivity=wake_word_sensitivity,\n',
                      'test_the_flag_reaches_the_wake_word_detector'),
    provider_mutation('distil-setting-read-from-the-wrong-section', DISTIL,
                      '    return bool(config.get("debug", {}).get("log_load_diagnostics", False))',
                      '    return bool(config.get("dbg", {}).get("log_load_diagnostics", False))',
                      'test_the_reader_follows_the_debug_section'),
    # wh-codex-merge-audit.13.1.1: the Google provider must declare the key
    # its own main.py reads. Without the line in the tracked config.toml the
    # operator has nothing to edit and the setting stays at the dataclass
    # default for the life of every run.
    dict(name='google-config-omits-the-load-diagnostics-key',
         file=GOOGLE_SERVICE / 'config.toml',
         old='\n'
             '# log_load_diagnostics: the periodic [load-diag] window= line, and the\n'
             '# ten-second [load-diag] work_scope=wake_word line the wake-word detector\n'
             '# writes while it is listening. This provider builds no AudioProcessor, so\n'
             '# it writes no work_scope=silence line. Turn it on for a load test, off\n'
             '# afterwards. Same key as the Parakeet and distil providers\n'
             '# (wh-codex-merge-audit.13.1.1).\n'
             'log_load_diagnostics = false\n',
         new='', service=GOOGLE_SERVICE, test_file=CONFIG_KEY_TESTS,
         expect=['test_the_tracked_config_declares_the_key']),
    # wh-audit13-preengine-load-review.2: the guide says which breakdown
    # fields a shipped provider can fill, so 0.0 reads as expected, not broken.
    dict(name='procedure-document-omits-which-breakdown-fields-fill', file=PROCEDURE,
         old='  `keep_warm_ms` is 0.0 in every shipped provider, because no engine\n  defines `keep_warm`; it fills only when an engine adds that call.\n  `wake_word_ms` is non-zero only on `work_scope=wake_word` lines and is\n  0.0 on `utt=` and `work_scope=silence` lines. `vad_ms`, `agc_ms` and\n  `keep_warm_ms` are 0.0 on `work_scope=wake_word` lines, because the\n  detector times only its own inference.\n',
         new='', service=SHARED, test_file=DOC_TESTS,
         expect=['test_the_guide_says_which_breakdown_fields_shipped_providers_fill']),
]

# The document changed this round; preserve and re-prove its older guards.
DOCUMENT_GUARDS = {
    'procedure-document-sends-other-work-to-engine-ratio',
    'procedure-document-omits-what-engine-ratio-measures',
    'procedure-document-still-counts-three-load-diag-lines',
}
MUTATIONS += [m for m in EXISTING if m['name'] in DOCUMENT_GUARDS]
assert DOCUMENT_GUARDS <= {m['name'] for m in MUTATIONS}, 'a reused document guard disappeared'


if __name__ == '__main__':
    raise SystemExit(run(MUTATIONS))
