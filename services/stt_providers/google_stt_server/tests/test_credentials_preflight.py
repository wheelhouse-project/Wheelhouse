"""Startup credential preflight and streamer-failure recovery (review
findings wh-google-creds-file-picker.1.4, .1.5, .1.6, .1.7).

The provider used to send "Transcription service ready" three seconds
after startup no matter what, and built the Google client only when the
first confirmed speech arrived. A bad, missing, or stale key file then
failed inside a per-utterance except block that logged at info level
and dropped every utterance -- the engine looked ready and produced
nothing, with no user-visible failure (.1.4). On top of that, the
client was constructed again for every utterance, re-reading the key
file from disk on the latency-critical speech-start path (.1.7); a
construction failure left the utterance state machine wedged in ACTIVE
with the VAD gate open, so no later utterance could ever start a new
stream (.1.6); and the failure notification carried no machine-readable
kind, so WheelHouse's is_starting suppression could swallow it (.1.5).

The preflight now builds the client once at startup and hands the same
client to every streamer; the startup notification carries a kind
("ready" or "startup_failed"); a construction failure resets the state
machine and VAD gate and is retried only after a cooldown; and the
runtime failure notice carries kind="error".
"""
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as server_main


def _cfg(credentials_file=""):
    return SimpleNamespace(credentials_file=credentials_file)


def _streamer_cfg(credentials_file=""):
    """A config with every attribute build_streamer reads."""
    return SimpleNamespace(
        language="en-US",
        model="latest_long",
        rate=16000,
        auto_punct=True,
        debug=SimpleNamespace(log_lifecycle=False),
        single_utterance=True,
        phrase_hints=[],
        hints_boost=10.0,
        class_tokens=[],
        credentials_file=credentials_file,
    )


class TestBuildSpeechClient:
    def test_configured_file_builds_from_it(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            client = server_main.build_speech_client(_cfg("C:/keys/sa.json"))
            assert client is mock_cls.from_service_account_file.return_value
            mock_cls.from_service_account_file.assert_called_once_with(
                "C:/keys/sa.json"
            )

    def test_bare_client_used_when_not_configured(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            client = server_main.build_speech_client(_cfg(""))
            assert client is mock_cls.return_value
            mock_cls.assert_called_once_with()
            mock_cls.from_service_account_file.assert_not_called()

    def test_construction_failure_raises(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            mock_cls.from_service_account_file.side_effect = ValueError(
                "bad key"
            )
            with pytest.raises(ValueError):
                server_main.build_speech_client(_cfg("C:/keys/sa.json"))


class TestCredentialsPreflight:
    def test_returns_the_client_when_it_builds(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            client, error = server_main.credentials_preflight(
                _cfg("C:/keys/sa.json")
            )
        assert error is None
        assert client is mock_cls.from_service_account_file.return_value

    def test_returns_the_error_when_it_does_not(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            mock_cls.from_service_account_file.side_effect = ValueError(
                "bad key"
            )
            client, error = server_main.credentials_preflight(
                _cfg("C:/keys/sa.json")
            )
        assert client is None
        assert error is not None
        assert "ValueError" in error
        assert "bad key" in error

    def test_a_hung_client_build_times_out_instead_of_blocking(
        self, monkeypatch
    ):
        """A credentials file on a disconnected network drive can make
        from_service_account_file block indefinitely on I/O. The
        preflight must give up after a bound and report a startup
        failure instead of hanging the provider's main loop forever
        (wh-google-creds-file-picker.1.17)."""
        release = threading.Event()

        def hung_build(_unused_cfg):
            release.wait(10.0)
            return object()

        monkeypatch.setattr(server_main, "build_speech_client", hung_build)
        try:
            started = time.monotonic()
            client, error = server_main.credentials_preflight(
                _cfg("Z:/keys/sa.json"), timeout_s=0.2
            )
            elapsed = time.monotonic() - started
        finally:
            release.set()

        assert client is None
        assert error is not None
        assert "did not finish" in error
        assert elapsed < 5.0


class TestRebuildSpeechClient:
    """The recovery path used to call build_speech_client directly on
    the provider's audio loop, bypassing the preflight bound and
    recreating the indefinite hang (wh-google-creds-file-picker.1.18).
    rebuild_speech_client is the bounded replacement: it returns the
    client or raises, so the existing streamer-failure handling
    (cooldown, VAD-gate close, kind="error" notice) applies."""

    def test_returns_the_client_when_it_builds(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(
            server_main, "build_speech_client", lambda _cfg_arg: sentinel
        )
        assert server_main.rebuild_speech_client(_cfg("C:/keys/sa.json")) is sentinel

    def test_a_build_failure_raises_with_the_error(self, monkeypatch):
        def failing_build(_cfg_arg):
            raise ValueError("bad key")

        monkeypatch.setattr(server_main, "build_speech_client", failing_build)
        with pytest.raises(RuntimeError) as excinfo:
            server_main.rebuild_speech_client(_cfg("C:/keys/sa.json"))
        assert "ValueError" in str(excinfo.value)
        assert "bad key" in str(excinfo.value)

    def test_a_hung_build_raises_within_the_bound(self, monkeypatch):
        """A hung key-file read on the recovery path must raise into the
        streamer-failure handling within the bound, not block the audio
        loop forever (wh-google-creds-file-picker.1.18)."""
        release = threading.Event()

        def hung_build(_unused_cfg):
            release.wait(10.0)
            return object()

        monkeypatch.setattr(server_main, "build_speech_client", hung_build)
        try:
            started = time.monotonic()
            with pytest.raises(RuntimeError) as excinfo:
                server_main.rebuild_speech_client(
                    _cfg("Z:/keys/sa.json"), timeout_s=0.2
                )
            elapsed = time.monotonic() - started
        finally:
            release.set()

        assert "did not finish" in str(excinfo.value)
        assert elapsed < 5.0


class TestStartupNotification:
    def test_ready_when_preflight_passed(self):
        title, message, kind = server_main.startup_notification(None)
        assert title == "Google STT"
        assert "ready" in message.lower()
        assert kind == "ready"

    def test_failure_kind_when_preflight_failed(self):
        title, message, kind = server_main.startup_notification(
            "ValueError: bad key"
        )
        assert title == "Google STT"
        assert "ready" not in message.lower()
        assert "bad key" in message
        assert kind == "startup_failed"


class TestStreamerFailureNotice:
    def test_notifies_exactly_once_per_run(self):
        forwarder = MagicMock()
        notified = server_main.report_streamer_start_failure(
            forwarder, ValueError("boom"), False
        )
        assert notified is True
        forwarder.send_notification.assert_called_once()
        title, message = forwarder.send_notification.call_args.args
        assert title == "Google STT"
        assert "boom" in message

        notified = server_main.report_streamer_start_failure(
            forwarder, ValueError("boom"), notified
        )
        assert notified is True
        forwarder.send_notification.assert_called_once()

    def test_notice_carries_the_error_kind(self):
        """kind="error" lets WheelHouse show the notice even while its
        is_starting suppression is active (wh-google-creds-file-picker.1.5)."""
        forwarder = MagicMock()
        server_main.report_streamer_start_failure(
            forwarder, ValueError("boom"), False
        )
        kwargs = forwarder.send_notification.call_args.kwargs
        assert kwargs.get("kind") == "error"

    def test_missing_forwarder_is_tolerated(self):
        notified = server_main.report_streamer_start_failure(
            None, ValueError("boom"), False
        )
        assert notified is False


class TestStreamerFailureRecovery:
    def test_recovery_resets_the_state_machine_and_vad(self):
        """A construction failure used to leave the state machine ACTIVE
        with the VAD gate open: silence finalization needs a Google
        response timestamp that never comes, the no-text timeout is
        gated on a live stream, and close_stream returns early with no
        stream -- so no later utterance could ever start
        (wh-google-creds-file-picker.1.6)."""
        utterance_mgr = MagicMock()
        vad = MagicMock()
        lead_in_buffer = deque([b"frame1", b"frame2"])

        gate_open = server_main.recover_from_streamer_failure(
            utterance_mgr, vad, lead_in_buffer
        )

        assert gate_open is False
        assert utterance_mgr.state is server_main.UtteranceState.IDLE
        assert len(lead_in_buffer) == 0
        vad.reset.assert_called_once()

    def test_retry_waits_for_the_cooldown(self):
        """Without a cooldown the loop would rebuild and fail the
        streamer on every audio frame, tens of times per second."""
        last_failure = 1000.0
        assert server_main.streamer_retry_allowed(1001.0, last_failure) is False
        assert (
            server_main.streamer_retry_allowed(
                last_failure + server_main._STREAMER_RETRY_COOLDOWN_S,
                last_failure,
            )
            is True
        )

    def test_retry_allowed_when_nothing_has_failed(self):
        assert server_main.streamer_retry_allowed(1000.0, 0.0) is True


class TestBuildStreamerClientPassthrough:
    def test_prebuilt_client_reaches_the_streamer(self):
        """The client built once at startup must be the one every
        streamer uses; rebuilding per utterance re-reads the key file
        on the speech-start path (wh-google-creds-file-picker.1.7)."""
        sentinel = object()
        event = threading.Event()
        with patch.object(server_main, "GoogleDirectStreamer") as mock_streamer:
            server_main.build_streamer(
                _streamer_cfg("C:/keys/sa.json"), event, client=sentinel
            )
        kwargs = mock_streamer.call_args.kwargs
        assert kwargs["client"] is sentinel
        assert kwargs["credentials_file"] == "C:/keys/sa.json"

    def test_client_defaults_to_none(self):
        """Without a prebuilt client the streamer builds its own from
        the config, the pre-feature behavior (bounded to abnormal
        recovery paths)."""
        event = threading.Event()
        with patch.object(server_main, "GoogleDirectStreamer") as mock_streamer:
            server_main.build_streamer(_streamer_cfg(""), event)
        assert mock_streamer.call_args.kwargs["client"] is None


class TestVadGateDuringCooldown:
    """While the streamer retry cooldown is active there is nothing to
    send audio to. If the VAD gate opened anyway, the lead-in buffer
    would be flushed into chunks that are then thrown away, and the
    eventual retry would start mid-utterance -- losing the first seconds
    of the command (review finding wh-google-creds-file-picker.1.11).
    Keeping the gate closed keeps the rolling lead-in buffer alive, so
    the retry starts from a bounded lead-in instead."""

    def test_gate_stays_closed_while_retry_is_disallowed(self):
        assert server_main.vad_gate_may_open(None, 1001.0, 1000.0) is False

    def test_gate_opens_when_a_stream_already_exists(self):
        assert server_main.vad_gate_may_open(object(), 1001.0, 1000.0) is True

    def test_gate_opens_once_the_cooldown_expires(self):
        assert (
            server_main.vad_gate_may_open(
                None,
                1000.0 + server_main._STREAMER_RETRY_COOLDOWN_S,
                1000.0,
            )
            is True
        )

    def test_gate_opens_when_nothing_has_failed(self):
        assert server_main.vad_gate_may_open(None, 1000.0, 0.0) is True


class TestCooldownContaminatedSpeech:
    """A spoken command that overlaps the streamer retry cooldown must
    not be sent to Google as a suffix (review finding
    wh-google-creds-file-picker.1.23).

    The lead-in buffer holds only vad_lead_in_ms of audio, but frames
    keep rolling through it for the whole 5-second cooldown. If the
    gate opened at cooldown expiry purely because the timer ran out,
    the utterance would start from the last 300 ms of a command begun
    seconds earlier -- and the surviving tail can parse as a DIFFERENT
    command. Speech observed while the gate cannot open marks the
    current speech run as contaminated; the gate reopens only after
    silence and a fresh speech onset.
    """

    def _cooldown(self):
        return server_main._STREAMER_RETRY_COOLDOWN_S

    def test_speech_spanning_the_cooldown_never_emits_a_partial(self):
        import collections

        failure_time = 1000.0
        buffer = collections.deque(maxlen=3)
        gate = False
        contaminated = False
        emitted = []

        # Speech starts 1s after the failure and continues past expiry.
        speech_times = [1001.0, 1002.0, 1003.0, 1004.0, 1004.5]
        # This frame arrives AFTER the cooldown expired, speech still
        # continuing -- the frame the unfixed code turns into a
        # partial utterance.
        speech_times.append(failure_time + self._cooldown() + 0.5)

        for i, now in enumerate(speech_times):
            chunks, gate, contaminated = server_main.gate_frame(
                raw_is_speech=True,
                agc_audio=f"agc-cooldown-{i}",
                audio_frame=f"raw-cooldown-{i}",
                streaming=None,
                now=now,
                last_failure_time=failure_time,
                vad_gate_open=gate,
                lead_in_buffer=buffer,
                contaminated_silence_left=contaminated,
            )
            emitted.extend(chunks)

        assert emitted == []
        assert gate is False
        assert contaminated

    def test_silence_then_fresh_speech_reopens_with_clean_lead_in(self):
        import collections

        failure_time = 1000.0
        expiry = failure_time + self._cooldown()
        buffer = collections.deque(maxlen=3)
        gate = False
        contaminated = False

        # Contaminated speech through the cooldown and past expiry.
        for i, now in enumerate([1001.0, 1003.0, expiry + 0.5]):
            chunks, gate, contaminated = server_main.gate_frame(
                raw_is_speech=True,
                agc_audio=f"agc-cooldown-{i}",
                audio_frame=f"raw-cooldown-{i}",
                streaming=None,
                now=now,
                last_failure_time=failure_time,
                vad_gate_open=gate,
                lead_in_buffer=buffer,
                contaminated_silence_left=contaminated,
            )
            assert chunks == []

        # Enough silence to roll every contaminated frame out of the
        # bounded lead-in buffer and satisfy the fixed silence floor.
        for i in range(server_main._CONTAMINATED_SILENCE_MIN_FRAMES):
            chunks, gate, contaminated = server_main.gate_frame(
                raw_is_speech=False,
                agc_audio=f"agc-silence-{i}",
                audio_frame=f"raw-silence-{i}",
                streaming=None,
                now=expiry + 1.0 + i * 0.2,
                last_failure_time=failure_time,
                vad_gate_open=gate,
                lead_in_buffer=buffer,
                contaminated_silence_left=contaminated,
            )
            assert chunks == []
            assert gate is False

        # A fresh command opens the gate with only post-silence lead-in.
        chunks, gate, contaminated = server_main.gate_frame(
            raw_is_speech=True,
            agc_audio="agc-fresh",
            audio_frame="raw-fresh",
            streaming=None,
            now=expiry + 2.0,
            last_failure_time=failure_time,
            vad_gate_open=gate,
            lead_in_buffer=buffer,
            contaminated_silence_left=contaminated,
        )
        assert gate is True
        assert not contaminated
        assert chunks == [
            "raw-silence-7", "raw-silence-8", "raw-silence-9", "agc-fresh"
        ]
        assert not any("cooldown" in c for c in chunks)

    def test_one_vad_flicker_does_not_reopen_the_gate(self):
        """A single silence frame mid-command (a breath, a plosive gap,
        one Silero miss) must not end the contaminated run: the lead-in
        buffer still holds the command's earlier frames, and reopening
        would send them plus the tail (review finding
        wh-google-creds-file-picker.1.24).
        """
        import collections

        failure_time = 1000.0
        expiry = failure_time + self._cooldown()
        buffer = collections.deque(maxlen=3)
        gate = False
        contaminated = False

        # Contaminated speech through the cooldown and past expiry.
        for i, now in enumerate([1001.0, 1003.0, expiry + 0.5]):
            chunks, gate, contaminated = server_main.gate_frame(
                True, f"agc-cooldown-{i}", f"raw-cooldown-{i}",
                None, now, failure_time, gate, buffer, contaminated,
            )
            assert chunks == []

        # One false VAD frame -- a breath, not the end of the command.
        chunks, gate, contaminated = server_main.gate_frame(
            False, "agc-flicker", "raw-flicker",
            None, expiry + 0.6, failure_time, gate, buffer, contaminated,
        )
        assert chunks == []

        # The command's speech resumes 30ms later. The gate must stay
        # closed: the buffer still holds contaminated frames.
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-tail", "raw-tail",
            None, expiry + 0.7, failure_time, gate, buffer, contaminated,
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_silence_shorter_than_the_buffer_keeps_the_gate_closed(self):
        """Fewer consecutive silence frames than the lead-in buffer
        holds cannot have evicted every contaminated frame, so speech
        after such a pause must not reopen the gate (review finding
        wh-google-creds-file-picker.1.24).
        """
        import collections

        failure_time = 1000.0
        expiry = failure_time + self._cooldown()
        buffer = collections.deque(maxlen=3)
        gate = False
        contaminated = False

        for i, now in enumerate([1001.0, 1003.0, expiry + 0.5]):
            chunks, gate, contaminated = server_main.gate_frame(
                True, f"agc-cooldown-{i}", f"raw-cooldown-{i}",
                None, now, failure_time, gate, buffer, contaminated,
            )
            assert chunks == []

        # Two silence frames: one contaminated frame is still buffered.
        for i in range(2):
            chunks, gate, contaminated = server_main.gate_frame(
                False, f"agc-pause-{i}", f"raw-pause-{i}",
                None, expiry + 0.6 + i * 0.03, failure_time,
                gate, buffer, contaminated,
            )
            assert chunks == []

        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-after-pause", "raw-after-pause",
            None, expiry + 0.7, failure_time, gate, buffer, contaminated,
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def _contaminated_run_then_pause(self, buffer, pause_frames):
        """Drive a contaminated speech run, then pause_frames of
        silence, then one speech frame; return its gate_frame result.
        """
        failure_time = 1000.0
        expiry = failure_time + self._cooldown()
        gate = False
        contaminated = 0

        for i, now in enumerate([1001.0, 1003.0, expiry + 0.5]):
            chunks, gate, contaminated = server_main.gate_frame(
                True, f"agc-cooldown-{i}", f"raw-cooldown-{i}",
                None, now, failure_time, gate, buffer, contaminated,
            )
            assert chunks == []

        for i in range(pause_frames):
            chunks, gate, contaminated = server_main.gate_frame(
                False, f"agc-pause-{i}", f"raw-pause-{i}",
                None, expiry + 0.6 + i * 0.03, failure_time,
                gate, buffer, contaminated,
            )
            assert chunks == []

        return server_main.gate_frame(
            True, "agc-tail", "raw-tail",
            None, expiry + 1.0, failure_time, gate, buffer, contaminated,
        )

    def test_one_frame_buffer_still_needs_sustained_silence(self):
        """A configuration with vad_lead_in_ms <= chunk_ms gives the
        lead-in buffer a capacity of 1. The contamination boundary must
        not shrink with it: one 30ms VAD miss is still not an utterance
        boundary, so the tail must stay unsent (review finding
        wh-google-creds-file-picker.1.25).
        """
        import collections

        buffer = collections.deque(maxlen=1)
        chunks, gate, contaminated = self._contaminated_run_then_pause(
            buffer, pause_frames=1
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_zero_frame_buffer_still_needs_sustained_silence(self):
        """A capacity-0 lead-in buffer discards every frame, but speech
        during the cooldown still contaminates the run and one VAD miss
        must not release the tail (review finding
        wh-google-creds-file-picker.1.25).
        """
        import collections

        buffer = collections.deque(maxlen=0)
        chunks, gate, contaminated = self._contaminated_run_then_pause(
            buffer, pause_frames=1
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_undersized_buffer_reopens_after_the_minimum_silence(self):
        """The safety floor must not close the gate forever: after the
        fixed minimum of consecutive silence frames, fresh speech
        reopens normally even with a capacity-1 buffer.
        """
        import collections

        buffer = collections.deque(maxlen=1)
        chunks, gate, contaminated = self._contaminated_run_then_pause(
            buffer,
            pause_frames=server_main._CONTAMINATED_SILENCE_MIN_FRAMES,
        )
        assert gate is True
        assert not contaminated
        assert chunks == [
            f"raw-pause-{server_main._CONTAMINATED_SILENCE_MIN_FRAMES - 1}",
            "agc-tail",
        ]

    def test_speech_spanning_a_discontinuity_never_emits_a_tail(self):
        """The soft-restart block and the transcription-disabled branch
        both discard audio while the user may still be speaking. The
        frames after the gap are only the tail of the command, and
        treating the first of them as a fresh onset sends a suffix that
        can parse as a different command (review finding
        wh-google-creds-file-picker.1.26). audio_discontinuity models
        the gap; speech resuming right after it must stay unsent.
        """
        import collections

        buffer = collections.deque(maxlen=3)

        # The command begins normally and the gate opens.
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-before", "raw-before",
            object(), 2000.0, 0.0, False, buffer, 0,
        )
        assert gate is True

        # The audio gap: soft restart or transcription suppression.
        gate, contaminated = server_main.audio_discontinuity(buffer)
        assert gate is False
        assert len(buffer) == 0

        # The user is still speaking when capture resumes. The gate
        # must not reopen onto the tail.
        emitted = []
        for i in range(5):
            chunks, gate, contaminated = server_main.gate_frame(
                True, f"agc-tail-{i}", f"raw-tail-{i}",
                object(), 2001.0 + i * 0.03, 0.0,
                gate, buffer, contaminated,
            )
            emitted.extend(chunks)
        assert emitted == []
        assert gate is False
        assert contaminated

        # Sustained silence, then a fresh command, reopens normally.
        for i in range(server_main._CONTAMINATED_SILENCE_MIN_FRAMES):
            chunks, gate, contaminated = server_main.gate_frame(
                False, f"agc-quiet-{i}", f"raw-quiet-{i}",
                object(), 2002.0 + i * 0.03, 0.0,
                gate, buffer, contaminated,
            )
            assert chunks == []
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-fresh", "raw-fresh",
            object(), 2003.0, 0.0, gate, buffer, contaminated,
        )
        assert gate is True
        assert not contaminated
        assert chunks == ["raw-quiet-7", "raw-quiet-8", "raw-quiet-9",
                          "agc-fresh"]
        assert not any("tail" in c or "before" in c for c in chunks)

    def test_discontinuity_holds_the_floor_for_a_tiny_buffer(self):
        """The discontinuity requirement must use the same fixed floor
        as the contamination boundary: a capacity-1 lead-in buffer must
        not let one silence frame reopen onto the tail (review findings
        wh-google-creds-file-picker.1.25 and .1.26).
        """
        import collections

        buffer = collections.deque(maxlen=1)
        gate, contaminated = server_main.audio_discontinuity(buffer)

        # One silence frame, then speech: still closed.
        chunks, gate, contaminated = server_main.gate_frame(
            False, "agc-pause", "raw-pause",
            object(), 2001.0, 0.0, gate, buffer, contaminated,
        )
        assert chunks == []
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-tail", "raw-tail",
            object(), 2001.1, 0.0, gate, buffer, contaminated,
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_capture_gap_decision_table(self):
        """capture_lost_audio decides when the ordinary capture path
        lost audio (review finding wh-google-creds-file-picker.1.27):
        a run of consecutive None reads means capture stopped, and an
        advanced drops counter means the queue discarded frames. A
        single None read is a late frame, not a gap -- the audio still
        arrives complete, and interrupting an open utterance for it
        would clip ordinary commands.
        """
        # Continuous capture: no gap.
        assert server_main.capture_lost_audio(0, 5, 5) is False
        # One late frame: no gap.
        assert server_main.capture_lost_audio(1, 5, 5) is False
        # Capture stopped for two or more read timeouts: gap.
        assert server_main.capture_lost_audio(
            server_main._CAPTURE_GAP_NONE_READS, 5, 5
        ) is True
        assert server_main.capture_lost_audio(40, 5, 5) is True
        # Queue overflow dropped frames: gap, even with no None reads.
        assert server_main.capture_lost_audio(0, 6, 5) is True

    def test_speech_resuming_after_a_capture_stall_stays_unsent(self):
        """A command begun during a capture stall must not have its
        tail sent as fresh speech when frames resume (review finding
        wh-google-creds-file-picker.1.27). Models the loop: the stall
        is observed, audio_discontinuity runs before the resumed frame
        is processed.
        """
        import collections

        buffer = collections.deque(maxlen=3)

        # Speech opened the gate before the stall.
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-before", "raw-before",
            object(), 2000.0, 0.0, False, buffer, 0,
        )
        assert gate is True

        # Three consecutive None reads, then a frame arrives.
        assert server_main.capture_lost_audio(3, 0, 0) is True
        gate, contaminated = server_main.audio_discontinuity(buffer)

        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-tail", "raw-tail",
            object(), 2001.0, 0.0, gate, buffer, contaminated,
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_speech_resuming_after_a_queue_drop_stays_unsent(self):
        """Queue overflow discards frames without any None read; the
        advanced drops counter must trigger the same transition
        (review finding wh-google-creds-file-picker.1.27).
        """
        import collections

        buffer = collections.deque(maxlen=3)
        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-before", "raw-before",
            object(), 2000.0, 0.0, False, buffer, 0,
        )
        assert gate is True

        assert server_main.capture_lost_audio(0, 3, 2) is True
        gate, contaminated = server_main.audio_discontinuity(buffer)

        chunks, gate, contaminated = server_main.gate_frame(
            True, "agc-tail", "raw-tail",
            object(), 2000.1, 0.0, gate, buffer, contaminated,
        )
        assert chunks == []
        assert gate is False
        assert contaminated

    def test_drop_count_reads_through_the_public_provider_interface(self):
        """The loop's mic object is the AudioProvider adapter from
        get_audio_provider(), whose public method is get_stats() --
        get_stats_snapshot() exists only on the private stream inside
        the sounddevice adapter. Reading drops through anything but the
        public interface crashes the provider at startup on every
        backend (review finding wh-google-creds-file-picker.1.28).
        Both real adapter classes construct without hardware access, so
        this drives the exact objects main() uses.
        """
        from shared_audio.capture.sounddevice_capture import (
            SounddeviceAudioCapture,
        )
        from shared_audio.capture.winrt_capture import WinRTAudioCapture

        assert server_main.read_drop_count(SounddeviceAudioCapture()) == 0
        assert server_main.read_drop_count(WinRTAudioCapture()) == 0

    def test_drop_count_defaults_to_zero_on_a_statless_provider(self):
        """A provider whose stats lack the 'drops' field must degrade
        to no-drop-detection, not kill the audio loop (review finding
        wh-google-creds-file-picker.1.28).
        """
        class _BareProvider:
            def get_stats(self):
                return {}

        assert server_main.read_drop_count(_BareProvider()) == 0

    def test_fresh_speech_after_expiry_opens_immediately(self):
        import collections

        buffer = collections.deque(["raw-lead"], maxlen=3)
        chunks, gate, contaminated = server_main.gate_frame(
            raw_is_speech=True,
            agc_audio="agc-now",
            audio_frame="raw-now",
            streaming=None,
            now=2000.0,
            last_failure_time=0.0,
            vad_gate_open=False,
            lead_in_buffer=buffer,
            contaminated_silence_left=0,
        )
        assert gate is True
        assert chunks == ["raw-lead", "agc-now"]
        assert len(buffer) == 0

    def test_open_gate_passes_audio_straight_through(self):
        import collections

        buffer = collections.deque(maxlen=3)
        chunks, gate, contaminated = server_main.gate_frame(
            raw_is_speech=True,
            agc_audio="agc-now",
            audio_frame="raw-now",
            streaming=object(),
            now=2000.0,
            last_failure_time=0.0,
            vad_gate_open=True,
            lead_in_buffer=buffer,
            contaminated_silence_left=0,
        )
        assert gate is True
        assert chunks == ["agc-now"]


class TestRestartCompletionNotification:
    """The soft restart reloads the config, which may name a different
    (or now-missing) credentials file. The completion notice must say
    "ready" only when the reloaded credentials actually build a client;
    announcing ready and then dropping the first utterance hides the
    failure (review finding wh-google-creds-file-picker.1.12)."""

    def test_ready_only_when_the_preflight_passed(self):
        title, message, kind = server_main.restart_completion_notification(None)
        assert title == "STT Service"
        assert "ready" in message.lower()
        assert kind == "ready"

    def test_failure_reports_the_credentials_problem(self):
        title, message, kind = server_main.restart_completion_notification(
            "ValueError: bad key"
        )
        assert "ready" not in message.lower()
        assert "bad key" in message
        assert kind == "startup_failed"
