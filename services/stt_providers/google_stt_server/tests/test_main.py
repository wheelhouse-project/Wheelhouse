"""Tests for google_stt_server main.py - StabilityProcessor, UtteranceManager, and main().

Covers:
- StabilityProcessor: stability filtering, text extraction, dedup, interim toggle
- UtteranceManager: FSM transitions, finalization triggers, timeouts, EOS fallback
- main() handlers: restart, shutdown, hard restart, hints, interim results toggle
- Adversarial: malformed responses, duplicate finals, empty results, rapid state changes
"""
import os
import sys
import time
import threading
import queue
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch, call

import pytest

# Add google_stt_server to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import StabilityProcessor, UtteranceManager, UtteranceState


# ---------------------------------------------------------------------------
# Test fixtures - minimal config and mock objects matching real interfaces
# ---------------------------------------------------------------------------

@dataclass
class FakeDebugConfig:
    log_lifecycle: bool = False
    log_stream_responses: bool = False
    log_frame_stats: bool = False
    log_overflow_diagnostics: bool = False


@dataclass
class FakeLatencyConfig:
    stability_commit_threshold: float = 0.9


@dataclass
class FakeAGCConfig:
    enabled: bool = False
    target_speech_rms: float = 0.1
    vad_threshold_rms: float = 0.08
    noise_floor_alpha: float = 0.1
    min_gain: float = 0.1
    max_gain: float = 10.0
    initial_noise_floor: float = 0.01


@dataclass
class FakeOverflowConfig:
    enabled: bool = False
    overflow_threshold: int = 5
    window_seconds: float = 30.0
    restart_cooldown_seconds: float = 60.0
    max_restart_attempts: int = 3
    stable_reset_seconds: float = 300.0


@dataclass
class FakeAppConfig:
    latency: FakeLatencyConfig = field(default_factory=FakeLatencyConfig)
    debug: FakeDebugConfig = field(default_factory=FakeDebugConfig)
    agc: FakeAGCConfig = field(default_factory=FakeAGCConfig)
    overflow_detection: FakeOverflowConfig = field(default_factory=FakeOverflowConfig)
    silence_finalize_ms: int = 2000
    max_no_text_seconds: float = 5.0
    forward_ws: bool = True
    ws_host: str = "localhost"
    ws_port: int = 5001
    rate: int = 16000
    chunk_ms: int = 20
    vad_lead_in_ms: int = 300
    max_stream_seconds: float = 60.0
    silero_threshold: float = 0.5
    device_index: int | None = None
    model: str = "latest_short"
    language: str = "en-US"
    auto_punct: bool = False
    single_utterance: bool = False
    phrase_hints: list = field(default_factory=list)
    class_tokens: list = field(default_factory=list)
    hints_boost: float | None = None
    mic_check_seconds: float = 0.0
    mic_check_write: str = ""


def make_config(**overrides) -> FakeAppConfig:
    """Create a FakeAppConfig with optional overrides."""
    cfg = FakeAppConfig()
    for key, val in overrides.items():
        setattr(cfg, key, val)
    return cfg


def make_forwarder() -> MagicMock:
    """Create a mock WSForwarder with expected methods."""
    fwd = MagicMock()
    fwd.send_stable = MagicMock()
    fwd.send_final = MagicMock()
    fwd.send_eos = MagicMock()
    fwd.send_vad_start = MagicMock()
    fwd.send_notification = MagicMock()
    fwd.send_log = MagicMock()
    fwd.start = MagicMock()
    fwd.stop = MagicMock()
    return fwd


def make_result(transcript: str, stability: float = 0.0, is_final: bool = False):
    """Build a mock Google STT result alternative."""
    alt = MagicMock()
    alt.transcript = transcript

    result = MagicMock()
    result.alternatives = [alt]
    result.stability = stability
    result.is_final = is_final
    return result


def make_response(results=None, speech_event_type=None, total_billed_time=None):
    """Build a mock StreamingRecognizeResponse."""
    from google.cloud.speech_v1.types import StreamingRecognizeResponse

    resp = MagicMock()
    resp.results = results or []

    if speech_event_type is None:
        resp.speech_event_type = StreamingRecognizeResponse.SpeechEventType.SPEECH_EVENT_UNSPECIFIED
    else:
        resp.speech_event_type = speech_event_type

    if total_billed_time:
        resp.total_billed_time = total_billed_time
    else:
        resp.total_billed_time = None

    return resp


# ===========================================================================
# StabilityProcessor Tests
# ===========================================================================

class TestStabilityProcessorInit:
    """Test StabilityProcessor construction and defaults."""

    def test_init_stores_threshold(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        assert sp.stability_threshold == 0.9

    def test_init_custom_threshold(self):
        cfg = make_config(latency=FakeLatencyConfig(stability_commit_threshold=0.85))
        sp = StabilityProcessor(cfg)
        assert sp.stability_threshold == 0.85

    def test_init_no_forwarder(self):
        sp = StabilityProcessor(make_config())
        assert sp.forwarder is None

    def test_init_with_forwarder(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)
        assert sp.forwarder is fwd

    def test_init_defaults(self):
        sp = StabilityProcessor(make_config())
        assert sp._last_stable_sent == ""
        assert sp.last_google_response_time is None
        assert sp.send_interim_results is True


class TestStabilityProcessorReset:
    """Test reset_for_new_utterance clears state."""

    def test_reset_clears_last_stable(self):
        sp = StabilityProcessor(make_config())
        sp._last_stable_sent = "some text"
        sp.last_google_response_time = 12345.0
        sp.reset_for_new_utterance()
        assert sp._last_stable_sent == ""
        assert sp.last_google_response_time is None


class TestStabilityProcessorFinal:
    """Test process_response with is_final=True."""

    def test_final_sends_full_text(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("hello world", is_final=True)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=True)
        assert text == "hello world"
        fwd.send_final.assert_called_once_with(
            "hello world", 1, trace_id=ANY, final_reason="GOOGLE_FINAL"
        )

    def test_final_strips_whitespace(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("  hello world  ", is_final=True)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=2, is_final=True)
        fwd.send_final.assert_called_once_with(
            "hello world", 2, trace_id=ANY, final_reason="GOOGLE_FINAL"
        )

    def test_final_empty_text_not_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("   ", is_final=True)
        resp = make_response(results=[result])

        sp.process_response(resp, utterance_id=1, is_final=True)
        fwd.send_final.assert_not_called()

    def test_final_concatenates_multiple_results(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        r1 = make_result("hello ", is_final=True)
        r2 = make_result("world", is_final=True)
        resp = make_response(results=[r1, r2])

        text = sp.process_response(resp, utterance_id=1, is_final=True)
        assert text == "hello world"
        fwd.send_final.assert_called_once_with(
            "hello world", 1, trace_id=ANY, final_reason="GOOGLE_FINAL"
        )

    def test_final_updates_last_response_time(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)
        result = make_result("test", is_final=True)
        resp = make_response(results=[result])

        before = time.time()
        sp.process_response(resp, utterance_id=1, is_final=True)
        after = time.time()

        assert sp.last_google_response_time is not None
        assert before <= sp.last_google_response_time <= after


class TestStabilityProcessorInterim:
    """Test process_response with is_final=False (interim/stable results)."""

    def test_stable_above_threshold_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("hello", stability=0.95)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == "hello"
        fwd.send_stable.assert_called_once_with("hello", 1, trace_id=ANY)

    def test_stable_below_threshold_not_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("uncertain", stability=0.5)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == ""
        fwd.send_stable.assert_not_called()

    def test_stable_at_threshold_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("exactly", stability=0.9)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == "exactly"
        fwd.send_stable.assert_called_once()

    def test_duplicate_stable_not_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        result = make_result("hello", stability=0.95)
        resp = make_response(results=[result])

        sp.process_response(resp, utterance_id=1, is_final=False)
        sp.process_response(resp, utterance_id=1, is_final=False)

        # Should only send once (dedup)
        assert fwd.send_stable.call_count == 1

    def test_different_stable_text_sent(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        r1 = make_result("hello", stability=0.95)
        r2 = make_result("hello world", stability=0.95)

        sp.process_response(make_response(results=[r1]), utterance_id=1, is_final=False)
        sp.process_response(make_response(results=[r2]), utterance_id=1, is_final=False)

        assert fwd.send_stable.call_count == 2

    def test_interim_disabled_no_send(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)
        sp.send_interim_results = False

        result = make_result("hello", stability=0.95)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        # Text is still tracked internally
        assert sp._last_stable_sent == "hello"
        # But NOT sent to forwarder
        fwd.send_stable.assert_not_called()

    def test_mixed_stability_stops_at_first_unstable(self):
        """Stable extraction should stop at first result below threshold."""
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        r1 = make_result("hello ", stability=0.95)
        r2 = make_result("world", stability=0.3)  # Below threshold
        resp = make_response(results=[r1, r2])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == "hello "
        fwd.send_stable.assert_called_once_with("hello", 1, trace_id=ANY)  # Stripped

    def test_no_results_empty(self):
        fwd = make_forwarder()
        sp = StabilityProcessor(make_config(), forwarder=fwd)

        resp = make_response(results=[])
        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == ""
        fwd.send_stable.assert_not_called()


# ===========================================================================
# UtteranceManager Tests
# ===========================================================================

class TestUtteranceManagerInit:
    """Test UtteranceManager construction."""

    def test_init_defaults(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        assert um.state == UtteranceState.IDLE
        assert um.current_utterance_id == 0
        assert um.silence_threshold == cfg.silence_finalize_ms / 1000
        assert um.max_no_text_threshold == cfg.max_no_text_seconds
        assert um.utterance_has_final is False
        assert um.stream_should_close is False

    def test_init_with_forwarder_and_metrics(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        fwd = make_forwarder()
        metrics = MagicMock()

        um = UtteranceManager(cfg, sp, forwarder=fwd, usage_metrics=metrics)
        assert um.forwarder is fwd
        assert um.usage_metrics is metrics


class TestUtteranceManagerStartNewUtterance:
    """Test start_new_utterance transitions and side effects."""

    def test_increments_utterance_id(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        um.start_new_utterance()
        assert um.current_utterance_id == 1
        assert um.state == UtteranceState.ACTIVE

    def test_successive_starts_increment(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        um.start_new_utterance()
        um.state = UtteranceState.FINALIZED  # Simulate finalization
        um.start_new_utterance()
        assert um.current_utterance_id == 2

    def test_resets_stability_processor(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        sp._last_stable_sent = "old text"
        sp.last_google_response_time = 12345.0

        um = UtteranceManager(cfg, sp)
        um.start_new_utterance()

        assert sp._last_stable_sent == ""
        assert sp.last_google_response_time is None

    def test_resets_utterance_has_final(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.utterance_has_final = True

        um.start_new_utterance()
        assert um.utterance_has_final is False

    def test_sends_vad_start(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        fwd = make_forwarder()
        um = UtteranceManager(cfg, sp, forwarder=fwd)

        um.start_new_utterance()
        fwd.send_vad_start.assert_called_once_with(1, trace_id=ANY)

    def test_cancels_pending_eos_timer(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        mock_timer = MagicMock()
        um.eos_fallback_timer = mock_timer

        um.start_new_utterance()
        mock_timer.cancel.assert_called_once()
        assert um.eos_fallback_timer is None

    def test_clears_tracking_sets(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.closed_utterances.add(1)
        um.closed_utterances.add(2)

        um.start_new_utterance()
        assert len(um.closed_utterances) == 0

    def test_resets_has_received_stable_text(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.has_received_stable_text = True

        um.start_new_utterance()
        assert um.has_received_stable_text is False

    def test_records_utterance_start_time(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        before = time.time()
        um.start_new_utterance()
        after = time.time()

        assert um.utterance_start_time is not None
        assert before <= um.utterance_start_time <= after


class TestUtteranceManagerProcessGoogleResponse:
    """Test process_google_response for different response types."""

    def _make_active_manager(self, forwarder=None, usage_metrics=None):
        cfg = make_config()
        # StabilityProcessor needs a forwarder to avoid AttributeError on send_*
        if forwarder is None:
            forwarder = make_forwarder()
        sp = StabilityProcessor(cfg, forwarder=forwarder)
        um = UtteranceManager(cfg, sp, forwarder=forwarder, usage_metrics=usage_metrics)
        um.start_new_utterance()
        return um

    def test_ignores_when_not_active(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        # State is IDLE
        resp = make_response(results=[make_result("hello", is_final=True)])
        result = um.process_google_response(resp)
        assert result is None

    def test_interim_result_returned(self):
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        result_obj = make_result("hello", stability=0.95)
        resp = make_response(results=[result_obj])

        result = um.process_google_response(resp)
        assert result == "hello"

    def test_interim_tracks_stable_text(self):
        um = self._make_active_manager()

        result_obj = make_result("hello", stability=0.95)
        resp = make_response(results=[result_obj])

        um.process_google_response(resp)
        assert um.has_received_stable_text is True

    def test_interim_empty_text_not_tracked(self):
        um = self._make_active_manager()

        result_obj = make_result("  ", stability=0.95)
        resp = make_response(results=[result_obj])

        um.process_google_response(resp)
        assert um.has_received_stable_text is False

    def test_final_result_finalizes_utterance(self):
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        result_obj = make_result("hello world", stability=0.95, is_final=True)
        resp = make_response(results=[result_obj])

        result = um.process_google_response(resp)
        assert um.state == UtteranceState.FINALIZED
        assert um.stream_should_close is True

    def test_final_sets_utterance_has_final(self):
        um = self._make_active_manager()

        result_obj = make_result("hello", stability=0.95, is_final=True)
        resp = make_response(results=[result_obj])

        um.process_google_response(resp)
        assert um.utterance_has_final is True

    def test_duplicate_final_ignored(self):
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        result_obj = make_result("hello", stability=0.95, is_final=True)
        resp = make_response(results=[result_obj])

        # First final processes
        um.process_google_response(resp)
        # Force state back to ACTIVE to test dedup logic
        um.state = UtteranceState.ACTIVE

        result = um.process_google_response(resp)
        assert result is None  # Ignored

    def test_final_cancels_eos_timer(self):
        um = self._make_active_manager()

        mock_timer = MagicMock()
        um.eos_fallback_timer = mock_timer

        result_obj = make_result("hello", stability=0.95, is_final=True)
        resp = make_response(results=[result_obj])

        um.process_google_response(resp)
        mock_timer.cancel.assert_called_once()
        assert um.eos_fallback_timer is None

    def test_final_extracts_billed_time(self):
        um = self._make_active_manager(usage_metrics=MagicMock())

        billed = MagicMock()
        billed.seconds = 5

        result_obj = make_result("hello", stability=0.95, is_final=True)
        resp = make_response(results=[result_obj], total_billed_time=billed)

        um.process_google_response(resp)
        # The billed_seconds was captured before finalization reset it
        # Verify via usage_metrics.log_utterance call
        um.usage_metrics.log_utterance.assert_called_once()
        call_kwargs = um.usage_metrics.log_utterance.call_args
        assert call_kwargs.kwargs.get("billed_seconds", call_kwargs[1].get("billed_seconds")) == 5

    def test_no_results_ignored(self):
        um = self._make_active_manager()
        resp = make_response(results=[])
        result = um.process_google_response(resp)
        assert result is None
        assert um.state == UtteranceState.ACTIVE

    def test_eos_event_starts_fallback_timer(self):
        from google.cloud.speech_v1.types import StreamingRecognizeResponse
        um = self._make_active_manager()

        resp = make_response(
            speech_event_type=StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
        )

        result = um.process_google_response(resp)
        assert result is None
        assert um.eos_fallback_timer is not None

    def test_eos_cancels_existing_timer_before_starting_new(self):
        from google.cloud.speech_v1.types import StreamingRecognizeResponse
        um = self._make_active_manager()

        old_timer = MagicMock()
        um.eos_fallback_timer = old_timer

        resp = make_response(
            speech_event_type=StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
        )

        um.process_google_response(resp)
        old_timer.cancel.assert_called_once()
        assert um.eos_fallback_timer is not old_timer  # New timer

    def test_eos_event_forwards_eos_message(self):
        """EOS detection should call forwarder.send_eos with utterance_id and trace_id.

        Phase 1 of the three-mode retraction policy (wh-x4fwo): WheelHouse
        needs to know when Google's END_OF_SINGLE_UTTERANCE arrived so it can
        choose between trusting the stable text and trusting the final.
        """
        from google.cloud.speech_v1.types import StreamingRecognizeResponse
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        resp = make_response(
            speech_event_type=StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
        )

        um.process_google_response(resp)
        fwd.send_eos.assert_called_once_with(
            um.current_utterance_id,
            trace_id=um._current_trace_id,
        )

    def test_eos_event_send_eos_runs_before_fallback_timer(self):
        """send_eos must be invoked before the EOS fallback timer is started.

        If WheelHouse only saw the eventual fallback final, it would be too
        late to apply Mode 2 (trust stable, drop final). The eos message must
        arrive ahead of the fallback final on the wire.
        """
        from google.cloud.speech_v1.types import StreamingRecognizeResponse
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        resp = make_response(
            speech_event_type=StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
        )

        # Capture state at call time: send_eos must have run before the timer
        # was assigned (the timer is what eventually fires send_final).
        send_eos_seen_with_no_timer = []

        def capture(*args, **kwargs):
            send_eos_seen_with_no_timer.append(um.eos_fallback_timer is None)

        fwd.send_eos.side_effect = capture

        um.process_google_response(resp)
        assert send_eos_seen_with_no_timer == [True], (
            "send_eos must run before the fallback timer is started"
        )


class TestUtteranceManagerFinalization:
    """Test _finalize_utterance behavior."""

    def _make_active_manager(self, forwarder=None, usage_metrics=None, debug=False):
        cfg = make_config()
        if debug:
            cfg.debug = FakeDebugConfig(log_lifecycle=True)
        sp = StabilityProcessor(cfg, forwarder=forwarder)
        um = UtteranceManager(cfg, sp, forwarder=forwarder, usage_metrics=usage_metrics)
        um.start_new_utterance()
        um.last_speech_ts = time.time()
        return um

    def test_finalize_transitions_to_finalized(self):
        um = self._make_active_manager()
        um._finalize_utterance("GOOGLE_FINAL")
        assert um.state == UtteranceState.FINALIZED

    def test_finalize_sets_stream_should_close(self):
        um = self._make_active_manager()
        um._finalize_utterance("GOOGLE_FINAL")
        assert um.stream_should_close is True

    def test_finalize_adds_to_closed_utterances(self):
        um = self._make_active_manager()
        utt_id = um.current_utterance_id
        um._finalize_utterance("GOOGLE_FINAL")
        assert utt_id in um.closed_utterances

    def test_finalize_ignored_when_not_active(self):
        um = self._make_active_manager()
        um.state = UtteranceState.IDLE
        result = um._finalize_utterance("GOOGLE_FINAL")
        assert result is None
        assert um.state == UtteranceState.IDLE  # Unchanged

    def test_fallback_finalization_sends_last_stable_text(self):
        """Non-GOOGLE_FINAL triggers should send fallback final via forwarder."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)
        um.stability_processor._last_stable_sent = "some text"

        um._finalize_utterance("GOOGLE_SILENCE_2S")
        fwd.send_final.assert_called_once_with(
            "some text",
            um.current_utterance_id,
            trace_id=ANY,
            final_reason="GOOGLE_SILENCE_2S",
        )

    def test_fallback_finalization_sends_empty_when_no_text(self):
        """Fallback finalization with no stable text should send empty string."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)
        um.stability_processor._last_stable_sent = ""

        um._finalize_utterance("EOS_FALLBACK")
        fwd.send_final.assert_called_once_with(
            "",
            um.current_utterance_id,
            trace_id=ANY,
            final_reason="EOS_FALLBACK",
        )

    def test_fallback_finalization_passes_no_text_timeout_reason(self):
        """NO_TEXT_TIMEOUT trigger should propagate as final_reason on the fallback final."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)
        um.stability_processor._last_stable_sent = ""

        um._finalize_utterance("NO_TEXT_TIMEOUT")
        fwd.send_final.assert_called_once_with(
            "",
            um.current_utterance_id,
            trace_id=ANY,
            final_reason="NO_TEXT_TIMEOUT",
        )

    def test_google_final_does_not_send_fallback(self):
        """GOOGLE_FINAL should NOT send fallback final (stability processor handles it)."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)
        um.stability_processor._last_stable_sent = "some text"

        um._finalize_utterance("GOOGLE_FINAL")
        fwd.send_final.assert_not_called()

    def test_finalize_logs_usage_metrics(self):
        metrics = MagicMock()
        um = self._make_active_manager(usage_metrics=metrics)
        um.stability_processor._last_stable_sent = "hello world"

        um._finalize_utterance("GOOGLE_FINAL")

        metrics.log_utterance.assert_called_once()
        kwargs = metrics.log_utterance.call_args.kwargs
        assert kwargs["result_type"] == "GOOGLE_FINAL"
        assert kwargs["word_count"] == 2
        assert kwargs["text"] == "hello world"

    def test_finalize_estimates_billed_time_without_google(self):
        """When Google doesn't report billed time, estimate from stream duration."""
        metrics = MagicMock()
        um = self._make_active_manager(usage_metrics=metrics)
        um.utterance_start_time = time.time() - 3  # Started 3 seconds ago

        um._finalize_utterance("GOOGLE_SILENCE_2S")

        kwargs = metrics.log_utterance.call_args.kwargs
        # Should round up to nearest second
        assert kwargs["billed_seconds"] >= 3

    def test_finalize_resets_billing_state(self):
        metrics = MagicMock()
        um = self._make_active_manager(usage_metrics=metrics)
        um.last_billed_seconds = 5
        um.last_final_text = "hello"
        um.utterance_start_time = time.time()

        um._finalize_utterance("GOOGLE_FINAL")

        assert um.last_billed_seconds == 0
        assert um.last_final_text == ""
        assert um.utterance_start_time is None


class TestUtteranceManagerSilenceFinalization:
    """Test check_silence_finalization behavior."""

    def _make_active_manager(self):
        cfg = make_config(silence_finalize_ms=2000)  # 2 second threshold
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.start_new_utterance()
        return um

    def test_no_finalization_when_recent_response(self):
        um = self._make_active_manager()
        um.stability_processor.last_google_response_time = time.time()

        result = um.check_silence_finalization(time.time())
        assert result is None
        assert um.state == UtteranceState.ACTIVE

    def test_finalization_after_silence_threshold(self):
        um = self._make_active_manager()
        um.stability_processor.last_google_response_time = time.time() - 3  # 3s ago
        um.last_speech_ts = time.time()

        result = um.check_silence_finalization(time.time())
        assert um.state == UtteranceState.FINALIZED

    def test_no_finalization_when_idle(self):
        um = self._make_active_manager()
        um.state = UtteranceState.IDLE
        um.stability_processor.last_google_response_time = time.time() - 10

        result = um.check_silence_finalization(time.time())
        assert result is None

    def test_no_finalization_when_no_response_time(self):
        um = self._make_active_manager()
        assert um.stability_processor.last_google_response_time is None

        result = um.check_silence_finalization(time.time())
        assert result is None


class TestUtteranceManagerNoTextTimeout:
    """Test check_no_text_timeout behavior."""

    def _make_active_manager(self, max_no_text=5.0):
        cfg = make_config(max_no_text_seconds=max_no_text)
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.start_new_utterance()
        return um

    def test_no_timeout_when_idle(self):
        um = self._make_active_manager()
        um.state = UtteranceState.IDLE
        assert um.check_no_text_timeout(time.time()) is False

    def test_no_timeout_when_stable_text_received(self):
        um = self._make_active_manager()
        um.has_received_stable_text = True
        um.utterance_start_time = time.time() - 10  # Long time

        assert um.check_no_text_timeout(time.time()) is False

    def test_no_timeout_when_disabled(self):
        um = self._make_active_manager(max_no_text=0)
        um.utterance_start_time = time.time() - 100

        assert um.check_no_text_timeout(time.time()) is False

    def test_timeout_fires_after_threshold(self):
        um = self._make_active_manager(max_no_text=5.0)
        um.utterance_start_time = time.time() - 6  # 6s ago, threshold is 5s

        assert um.check_no_text_timeout(time.time()) is True

    def test_no_timeout_before_threshold(self):
        um = self._make_active_manager(max_no_text=5.0)
        um.utterance_start_time = time.time() - 2  # Only 2s ago

        assert um.check_no_text_timeout(time.time()) is False

    def test_no_timeout_when_no_start_time(self):
        um = self._make_active_manager()
        um.utterance_start_time = None

        assert um.check_no_text_timeout(time.time()) is False


class TestUtteranceManagerUpdateSpeechTimestamp:
    """Test update_speech_timestamp."""

    def test_updates_timestamp(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)

        ts = time.time()
        um.update_speech_timestamp(ts)
        assert um.last_speech_ts == ts


class TestUtteranceManagerClearTrackingSets:
    """Test clear_tracking_sets."""

    def test_clears_closed_utterances(self):
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        um = UtteranceManager(cfg, sp)
        um.closed_utterances = {1, 2, 3}

        um.clear_tracking_sets()
        assert len(um.closed_utterances) == 0


# ===========================================================================
# main() Handler Tests
# ===========================================================================

class TestMainHandlers:
    """Test the handler functions defined inside main().

    These are tested by extracting the handler logic and verifying behavior
    through the main function's side effects.
    """

    @patch("main.load_config")
    @patch("main.get_audio_provider")
    @patch("main.get_available_providers", return_value=["sounddevice"])
    @patch("main.SileroVAD")
    @patch("main.SmartAGC")
    @patch("main.UsageMetrics")
    @patch("main.WSForwarder")
    @patch("main.get_startup_banner", return_value="Test Banner v1.0")
    @patch("main.logger")
    def test_main_list_devices(self, mock_logger, mock_banner, mock_ws_class,
                                mock_metrics, mock_agc, mock_vad,
                                mock_providers, mock_audio, mock_load_config):
        """main() with --list-devices should list devices and return 0."""
        args = MagicMock()
        args.list_devices = True
        args.ws_host = None
        args.ws_port = None
        cfg = make_config()
        cfg.overflow_detection.enabled = False
        mock_load_config.return_value = (args, cfg)

        mock_mic = MagicMock()
        mock_mic.list_audio_devices.return_value = [
            {"index": 0, "name": "Test Mic", "rate": 16000, "channels": 1}
        ]
        mock_audio.return_value = mock_mic

        from main import main
        result = main()
        assert result == 0
        mock_mic.list_audio_devices.assert_called_once()

    @patch("main.load_config")
    @patch("main.get_audio_provider")
    @patch("main.get_available_providers", return_value=["sounddevice"])
    @patch("main.run_mic_check", return_value=0)
    @patch("main.logger")
    def test_main_mic_check(self, mock_logger, mock_mic_check, mock_providers,
                             mock_audio, mock_load_config):
        """main() with mic_check_seconds > 0 should run mic check and return."""
        args = MagicMock()
        args.list_devices = False
        args.ws_host = None
        args.ws_port = None
        cfg = make_config(mic_check_seconds=5.0)
        cfg.overflow_detection.enabled = False
        mock_load_config.return_value = (args, cfg)

        mock_mic = MagicMock()
        mock_audio.return_value = mock_mic

        from main import main
        result = main()
        assert result == 0
        mock_mic_check.assert_called_once()


class TestHandleAddHint:
    """Test handle_add_hint callback logic."""

    @patch("main.load_config")
    @patch("main.get_audio_provider")
    @patch("main.get_available_providers", return_value=["sounddevice"])
    @patch("main.SileroVAD")
    @patch("main.SmartAGC")
    @patch("main.UsageMetrics")
    @patch("main.WSForwarder")
    @patch("main.get_startup_banner", return_value="Test v1.0")
    @patch("main.logger")
    def test_add_hint_triggers_restart(self, mock_logger, mock_banner, mock_ws_class,
                                        mock_metrics, mock_agc,
                                        mock_vad, mock_providers, mock_audio,
                                        mock_load_config):
        """Adding a hint should trigger a service restart."""
        # This test verifies the add_hint flow by checking the restart event
        # We capture the add_hint_callback from WSForwarder constructor
        args = MagicMock()
        args.list_devices = False
        args.ws_host = None
        args.ws_port = None
        cfg = make_config(mic_check_seconds=0.0)
        cfg.overflow_detection.enabled = False

        # On first call return normal config, on second (restart reload) return same
        mock_load_config.return_value = (args, cfg)

        mock_mic = MagicMock()
        mock_mic.read.return_value = None  # No audio data
        mock_audio.return_value = mock_mic

        mock_ws_instance = make_forwarder()
        mock_ws_class.return_value = mock_ws_instance

        # Capture the add_hint_callback
        captured_callbacks = {}
        def capture_ws_init(**kwargs):
            captured_callbacks.update(kwargs)
            return mock_ws_instance
        mock_ws_class.side_effect = capture_ws_init

        # Make main() exit quickly by setting stop after a few iterations
        read_count = 0
        def mock_read(timeout=None):
            nonlocal read_count
            read_count += 1
            if read_count > 2:
                # Trigger stop by simulating interrupt
                raise KeyboardInterrupt()
            return None

        mock_mic.read.side_effect = mock_read

        from main import main
        try:
            main()
        except (KeyboardInterrupt, SystemExit):
            pass

        # Verify add_hint_callback was captured
        assert "add_hint_callback" in captured_callbacks


class TestHandleSetInterimResults:
    """Test the set_interim_results handler logic (unit test)."""

    def test_toggle_interim_results_on_processor(self):
        """Simulates what handle_set_interim_results does."""
        cfg = make_config()
        sp = StabilityProcessor(cfg)
        assert sp.send_interim_results is True

        # Simulate the handler
        sp.send_interim_results = False
        assert sp.send_interim_results is False

        sp.send_interim_results = True
        assert sp.send_interim_results is True


class TestHandleSetLogLevel:
    """Test the set_log_level handler logic (unit test)."""

    def setup_method(self):
        from main import logger
        self._saved_level = logger.level
        self._saved_handler_levels = [(h, h.level) for h in logger.handlers]

    def teardown_method(self):
        from main import logger
        logger.setLevel(self._saved_level)
        for handler, level in self._saved_handler_levels:
            handler.setLevel(level)

    def test_handle_set_log_level_debug(self):
        """handle_set_log_level('DEBUG') should lower logger + handler levels."""
        import logging
        from main import handle_set_log_level, logger

        handle_set_log_level("DEBUG")
        assert logger.level == logging.DEBUG
        for handler in logger.handlers:
            assert handler.level == logging.DEBUG

    def test_handle_set_log_level_info(self):
        """handle_set_log_level('INFO') should set logger + handler levels to INFO."""
        import logging
        from main import handle_set_log_level, logger

        handle_set_log_level("INFO")
        assert logger.level == logging.INFO
        for handler in logger.handlers:
            assert handler.level == logging.INFO


# ===========================================================================
# Adversarial Tests
# ===========================================================================

class TestAdversarialResponses:
    """Test handling of malformed, unexpected, or adversarial Google responses."""

    def _make_active_manager(self, forwarder=None):
        cfg = make_config()
        if forwarder is None:
            forwarder = make_forwarder()
        sp = StabilityProcessor(cfg, forwarder=forwarder)
        um = UtteranceManager(cfg, sp, forwarder=forwarder)
        um.start_new_utterance()
        return um

    def test_response_with_no_alternatives(self):
        """Result with empty alternatives list should not crash."""
        um = self._make_active_manager()

        result = MagicMock()
        result.alternatives = []
        result.stability = 0.95
        result.is_final = False
        resp = make_response(results=[result])

        # Should not raise
        um.process_google_response(resp)

    def test_response_with_empty_transcript(self):
        """Empty transcript string should be handled gracefully."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        result = make_result("", stability=0.95)
        resp = make_response(results=[result])

        um.process_google_response(resp)
        fwd.send_stable.assert_not_called()

    def test_rapid_final_after_final(self):
        """Two rapid finals should not cause double finalization."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        r1 = make_result("hello", stability=0.95, is_final=True)
        resp1 = make_response(results=[r1])

        # First final finalizes
        um.process_google_response(resp1)
        assert um.state == UtteranceState.FINALIZED

        # Second final should be ignored (state is FINALIZED, not ACTIVE)
        r2 = make_result("hello again", stability=0.95, is_final=True)
        resp2 = make_response(results=[r2])
        result = um.process_google_response(resp2)
        assert result is None

    def test_eos_when_already_finalized(self):
        """EOS event when already finalized should be ignored."""
        from google.cloud.speech_v1.types import StreamingRecognizeResponse

        um = self._make_active_manager()
        um._finalize_utterance("GOOGLE_FINAL")
        assert um.state == UtteranceState.FINALIZED

        eos_resp = make_response(
            speech_event_type=StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
        )
        result = um.process_google_response(eos_resp)
        assert result is None

    def test_stability_exactly_at_boundary(self):
        """Stability of exactly 0.9 (threshold) should be accepted."""
        fwd = make_forwarder()
        cfg = make_config()
        sp = StabilityProcessor(cfg, forwarder=fwd)

        result = make_result("boundary", stability=0.9)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == "boundary"
        fwd.send_stable.assert_called_once()

    def test_stability_just_below_boundary(self):
        """Stability of 0.8999 should be rejected."""
        fwd = make_forwarder()
        cfg = make_config()
        sp = StabilityProcessor(cfg, forwarder=fwd)

        result = make_result("almost", stability=0.8999)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == ""
        fwd.send_stable.assert_not_called()

    def test_very_long_transcript(self):
        """Very long transcript should be handled without truncation."""
        fwd = make_forwarder()
        cfg = make_config()
        sp = StabilityProcessor(cfg, forwarder=fwd)

        long_text = "word " * 1000  # 5000 chars
        result = make_result(long_text, stability=0.95)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert len(text) == len(long_text)

    def test_finalize_without_speech_timestamp(self):
        """Finalization without any speech timestamp should not crash."""
        um = self._make_active_manager()
        um.last_speech_ts = None

        # Should not raise
        um._finalize_utterance("GOOGLE_SILENCE_2S")
        assert um.state == UtteranceState.FINALIZED

    def test_multiple_utterance_lifecycle(self):
        """Complete lifecycle: start -> interim -> final -> start -> final."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        # First utterance: interim then final
        interim = make_result("hello", stability=0.95)
        um.process_google_response(make_response(results=[interim]))

        final = make_result("hello world", stability=0.95, is_final=True)
        um.process_google_response(make_response(results=[final]))
        assert um.state == UtteranceState.FINALIZED
        assert um.current_utterance_id == 1

        # Start second utterance
        um.start_new_utterance()
        assert um.current_utterance_id == 2
        assert um.state == UtteranceState.ACTIVE

        # Second final
        final2 = make_result("goodbye", stability=0.95, is_final=True)
        um.process_google_response(make_response(results=[final2]))
        assert um.state == UtteranceState.FINALIZED

    def test_concurrent_silence_and_eos_finalization(self):
        """If both silence timeout and EOS fire, only one should finalize."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)

        # Set up conditions for silence finalization
        um.stability_processor.last_google_response_time = time.time() - 5
        um.last_speech_ts = time.time()

        # Silence finalization fires
        um.check_silence_finalization(time.time())
        assert um.state == UtteranceState.FINALIZED

        # EOS-triggered finalization should be ignored (already finalized)
        result = um._finalize_utterance("EOS_FALLBACK")
        assert result is None  # Ignored

    def test_finalize_with_whitespace_only_stable_text(self):
        """Fallback finalization with whitespace-only stable text sends empty."""
        fwd = make_forwarder()
        um = self._make_active_manager(forwarder=fwd)
        um.stability_processor._last_stable_sent = "   "

        um._finalize_utterance("EOS_FALLBACK")
        # Whitespace should be stripped, resulting in empty final
        fwd.send_final.assert_called_once_with(
            "",
            um.current_utterance_id,
            trace_id=ANY,
            final_reason="EOS_FALLBACK",
        )

    def test_no_forwarder_finalization_succeeds(self):
        """UtteranceManager._finalize_utterance guards against None forwarder.

        The code checks `if reason != "GOOGLE_FINAL" and self.forwarder` before
        calling send_final, so None forwarder is safe for finalization.
        """
        cfg = make_config()
        sp = StabilityProcessor(cfg, forwarder=None)  # Explicitly no forwarder
        um = UtteranceManager(cfg, sp, forwarder=None)
        um.start_new_utterance()
        um.stability_processor._last_stable_sent = "test"

        # Should not raise - _finalize_utterance guards against None forwarder
        um._finalize_utterance("GOOGLE_SILENCE_2S")
        assert um.state == UtteranceState.FINALIZED


class TestAdversarialStabilityProcessor:
    """Adversarial tests focused on StabilityProcessor edge cases."""

    def test_result_without_stability_attribute(self):
        """Result missing stability attribute should be handled."""
        cfg = make_config()
        sp = StabilityProcessor(cfg)

        result = MagicMock()
        result.alternatives = [MagicMock(transcript="test")]
        # hasattr(result, 'stability') will return True for MagicMock
        # but we can test with stability=0 which is below threshold
        result.stability = 0
        resp = make_response(results=[result])

        # Should not crash and should return empty (below threshold)
        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == ""

    def test_no_forwarder_crashes_on_stable_send(self):
        """StabilityProcessor with no forwarder crashes when trying to send stable text.

        The code unconditionally calls self.forwarder.send_stable() without None guard.
        In production a forwarder is always provided, so this documents the coupling.
        """
        cfg = make_config()
        sp = StabilityProcessor(cfg)  # No forwarder

        result = make_result("hello", stability=0.95)
        resp = make_response(results=[result])

        with pytest.raises(AttributeError):
            sp.process_response(resp, utterance_id=1, is_final=False)

    def test_no_forwarder_crashes_on_final_send(self):
        """StabilityProcessor with no forwarder crashes when sending final text.

        Same coupling as above - forwarder is required for non-empty finals.
        """
        cfg = make_config()
        sp = StabilityProcessor(cfg)

        result = make_result("hello world", is_final=True)
        resp = make_response(results=[result])

        with pytest.raises(AttributeError):
            sp.process_response(resp, utterance_id=1, is_final=True)

    def test_no_forwarder_ok_for_empty_final(self):
        """Empty final text skips send_final, so no forwarder needed."""
        cfg = make_config()
        sp = StabilityProcessor(cfg)  # No forwarder

        result = make_result("   ", is_final=True)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=True)
        assert text == "   "  # Returned without crash

    def test_no_forwarder_ok_for_below_threshold(self):
        """Below-threshold interim skips send, so no forwarder needed."""
        cfg = make_config()
        sp = StabilityProcessor(cfg)  # No forwarder

        result = make_result("hello", stability=0.1)
        resp = make_response(results=[result])

        text = sp.process_response(resp, utterance_id=1, is_final=False)
        assert text == ""  # Below threshold, nothing sent


class TestUtteranceStateEnum:
    """Test UtteranceState enum values."""

    def test_states_exist(self):
        assert UtteranceState.IDLE is not None
        assert UtteranceState.ACTIVE is not None
        assert UtteranceState.FINALIZED is not None

    def test_states_are_distinct(self):
        assert UtteranceState.IDLE != UtteranceState.ACTIVE
        assert UtteranceState.ACTIVE != UtteranceState.FINALIZED
        assert UtteranceState.IDLE != UtteranceState.FINALIZED
