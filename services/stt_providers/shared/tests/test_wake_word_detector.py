"""Tests for WakeWordDetector wrapper."""
import contextlib
import http.server
import socket
import threading
import time

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestWakeWordDetectorInit:
    """Test detector initialization and model loading."""

    def test_init_with_valid_local_model(self, tmp_path):
        """Detector loads a model file from the local model directory."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake model data")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )
            assert detector.is_loaded
            mock_oww.Model.assert_called_once()

    def test_init_with_missing_model_not_loaded(self, tmp_path):
        """Detector gracefully handles missing model file."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="nonexistent",
            model_dir=str(tmp_path),
            sensitivity=0.5,
        )
        assert not detector.is_loaded

    def test_init_downloads_missing_openwakeword_resources(self, tmp_path):
        """Missing openwakeword ONNX resources trigger lazy download before load."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake model data")

        oww_pkg = tmp_path / "openwakeword_pkg"
        resources_dir = oww_pkg / "resources" / "models"
        resources_dir.mkdir(parents=True)
        fake_init = oww_pkg / "__init__.py"
        fake_init.write_text("# fake openwakeword package marker\n", encoding="utf-8")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_oww.__file__ = str(fake_init)
            mock_oww.utils.download_models = MagicMock()
            mock_oww.Model.return_value = MagicMock()

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            assert detector.is_loaded
            mock_oww.utils.download_models.assert_called_once_with(
                model_names=["__wheelhouse_noop__"],
                target_directory=str(resources_dir),
            )

    def test_init_with_disabled_flag(self, tmp_path):
        """Detector skips loading when enabled=False."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="test_word",
            model_dir=str(tmp_path),
            sensitivity=0.5,
            enabled=False,
        )
        assert not detector.is_loaded


class TestWakeWordDetectorProcess:
    """Test audio frame processing and detection."""

    def test_process_returns_none_when_no_detection(self, tmp_path):
        """Processing audio with no wake word returns None."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.1}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            frame = b"\x00" * 2560
            result = detector.process(frame)
            assert result is None

    def test_process_returns_keyword_on_detection(self, tmp_path):
        """Processing audio with wake word returns the keyword name."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.8}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            frame = b"\x00" * 2560
            result = detector.process(frame)
            assert result == "test_word"

    def test_process_when_not_loaded_returns_none(self, tmp_path):
        """Processing when model failed to load returns None safely."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="missing",
            model_dir=str(tmp_path),
            sensitivity=0.5,
        )
        frame = b"\x00" * 2560
        result = detector.process(frame)
        assert result is None


class TestWakeWordDetectorReset:
    """Test detector reset between activations."""

    def test_reset_clears_internal_state(self, tmp_path):
        """Reset clears openWakeWord's prediction buffer."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.0}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )
            detector.reset()
            mock_model.reset.assert_called_once()


# The first bytes of the shipped computer_v2.onnx: ModelProto field 1
# (ir_version) as a varint, then the producer name.
_ONNX_BODY = b"\x08\x08\x12\x07tf2onnx" + bytes(200_000)


@contextlib.contextmanager
def _http_server(body: bytes, content_type: str, declared_length: int | None = None):
    """Serve ``body`` with status 200 for every GET on 127.0.0.1.

    ``declared_length`` sends a Content-Length other than the body's size, so
    the server closes the connection before the declared body is complete.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(
                len(body) if declared_length is None else declared_length))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def _silent_server(hold_s: float):
    """Accept one connection, send nothing for ``hold_s`` seconds, then close."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    done = threading.Event()

    def hold():
        listener.settimeout(hold_s)
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        done.wait(hold_s)
        conn.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        done.set()
        thread.join(hold_s + 1)
        listener.close()


def _one_local_url(port: int):
    return patch(
        "shared_stt.wake_word_detector._MODEL_URL_TEMPLATES",
        [f"http://127.0.0.1:{port}/{{keyword}}_v2.onnx"],
    )


class TestWakeWordModelDownload:
    """The download path: a timeout, an ONNX header check, no bad file kept
    (wh-wake-word-download-hardening)."""

    def test_a_server_that_sends_nothing_times_out_and_leaves_no_file(self, tmp_path):
        from shared_stt.wake_word_detector import WakeWordDetector

        with _silent_server(hold_s=4.0) as port, _one_local_url(port), \
                patch("shared_stt.wake_word_detector._DOWNLOAD_TIMEOUT_S", 0.3), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            started = time.monotonic()
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))
            elapsed = time.monotonic() - started

        assert elapsed < 2.0
        assert list(tmp_path.iterdir()) == []
        assert not detector.is_loaded
        mock_oww.Model.assert_not_called()

    def test_an_html_page_is_refused_and_leaves_no_file(self, tmp_path):
        from shared_stt.wake_word_detector import WakeWordDetector

        page = b"<!DOCTYPE html><html><body>Sign in to the Wi-Fi</body></html>"
        with _http_server(page, "text/html") as port, _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        assert list(tmp_path.iterdir()) == []
        assert not detector.is_loaded
        mock_oww.Model.assert_not_called()

    def test_an_empty_body_is_refused_and_leaves_no_file(self, tmp_path):
        from shared_stt.wake_word_detector import WakeWordDetector

        with _http_server(b"", "application/octet-stream") as port, _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        assert list(tmp_path.iterdir()) == []
        assert not detector.is_loaded
        mock_oww.Model.assert_not_called()

    def test_an_onnx_body_is_saved_whole_and_loaded(self, tmp_path):
        from shared_stt.wake_word_detector import WakeWordDetector

        with _http_server(_ONNX_BODY, "application/octet-stream") as port, \
                _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        target = tmp_path / "test_word_v2.onnx"
        assert target.exists()
        assert target.read_bytes() == _ONNX_BODY
        assert detector.is_loaded
        mock_oww.Model.assert_called_once_with(
            wakeword_models=[str(target)], inference_framework="onnx")

    def test_a_downloaded_file_that_fails_to_load_is_removed(self, tmp_path):
        from shared_stt.wake_word_detector import WakeWordDetector

        with _http_server(_ONNX_BODY, "application/octet-stream") as port, \
                _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_oww.Model.side_effect = RuntimeError("bad model")
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        mock_oww.Model.assert_called_once()
        assert list(tmp_path.iterdir()) == []
        assert not detector.is_loaded

    def test_a_saved_file_without_the_onnx_header_is_removed_when_it_fails_to_load(
            self, tmp_path):
        """The captive-portal page an earlier start saved as the model."""
        from shared_stt.wake_word_detector import WakeWordDetector

        saved = tmp_path / "test_word_v2.onnx"
        saved.write_bytes(b"<!DOCTYPE html><html><body>Sign in</body></html>")
        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_oww.Model.side_effect = RuntimeError("not a model")
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        mock_oww.Model.assert_called_once()
        assert not saved.exists()
        assert not detector.is_loaded

    def test_a_saved_onnx_file_is_kept_when_the_resources_step_fails(self, tmp_path):
        """An offline start must not delete the shipped computer_v2.onnx."""
        from shared_stt.wake_word_detector import WakeWordDetector

        saved = tmp_path / "test_word_v2.onnx"
        saved.write_bytes(_ONNX_BODY)
        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww, \
                patch.object(WakeWordDetector, "_ensure_openwakeword_resources",
                             side_effect=OSError("offline")) as resources:
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        resources.assert_called_once()
        mock_oww.Model.assert_not_called()
        assert saved.exists()
        assert saved.read_bytes() == _ONNX_BODY
        assert not detector.is_loaded

    def test_a_body_shorter_than_its_content_length_is_refused(self, tmp_path):
        """A connection closed part way through the body ends the read without
        an error, so the byte count is what shows the file is not whole."""
        from shared_stt.wake_word_detector import WakeWordDetector

        short = _ONNX_BODY[:150_000]
        with _http_server(short, "application/octet-stream",
                          declared_length=len(_ONNX_BODY)) as port, \
                _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            detector = WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))

        assert list(tmp_path.iterdir()) == []
        assert not detector.is_loaded
        mock_oww.Model.assert_not_called()

    def test_a_download_stopped_part_way_leaves_no_model_file(self, tmp_path):
        """A kill during a first download must not leave a partial model that
        begins with the ONNX header, because the next start would keep it.
        KeyboardInterrupt stands in for the kill: no except arm catches it."""
        from shared_stt.wake_word_detector import WakeWordDetector

        with _http_server(_ONNX_BODY, "application/octet-stream") as port, \
                _one_local_url(port), \
                patch("shared_stt.wake_word_detector.openwakeword"), \
                patch("shutil.copyfileobj", side_effect=KeyboardInterrupt) as stop:
            try:
                WakeWordDetector(keyword="test_word", model_dir=str(tmp_path))
            except KeyboardInterrupt:
                pass

        assert not (tmp_path / "test_word_v2.onnx").exists()
        # The stop really came part way through the body.
        stop.assert_called_once()


class TestShouldListenForWakeWord:
    """The single rule that decides whether a disable reason arms the detector.

    wh-audio-suppression-control C3. The three provider mains each carried
    their own copy of this rule; they now call this function, so the rule
    has one place to read and one place to change.
    """

    def test_idle_recovery_arms_on_idle(self):
        """The idle pause is the case the recovery mode was built for."""
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("idle_recovery", "idle") is True

    def test_idle_recovery_does_not_arm_on_audio(self):
        """wh-audio-suppression-auto: the sound pause has no voice way out.

        The detector armed on "audio" so the user could say the command
        that switched audio suppression off. That command is gone, and so
        is the recovery window it was spoken into, so arming here would
        open a window onto nothing.
        """
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("idle_recovery", "audio") is False

    def test_idle_recovery_does_not_arm_on_sonos(self):
        """The Sonos pause keeps the behaviour it has today in this mode."""
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("idle_recovery", "sonos") is False

    def test_push_to_talk_arms_on_sonos(self):
        """push_to_talk arms on every disable reason, unchanged."""
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("push_to_talk", "sonos") is True

    def test_push_to_talk_arms_on_audio(self):
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("push_to_talk", "audio") is True

    def test_unknown_mode_never_arms(self):
        """An unrecognised mode value in config.toml must not arm the
        detector: a mode nobody wrote a rule for has no armed reasons."""
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("no_such_mode", "idle") is False

    def test_no_reason_never_arms(self):
        """reason None means transcription was re-enabled, not paused."""
        from shared_stt.wake_word_detector import should_listen_for_wake_word

        assert should_listen_for_wake_word("idle_recovery", None) is False
