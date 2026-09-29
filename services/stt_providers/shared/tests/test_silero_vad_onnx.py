"""Tests for the onnxruntime Silero VAD (wh-vad-releases-gil).

pysilero-vad 3.2.0 ran the Silero model through ggml without releasing the
GIL and with a fixed pool of 4 threads, so under Parakeet decode load one
30 ms chunk could take seconds and stall the capture thread. SileroVAD now
runs the upstream Silero v6.2 ONNX file through onnxruntime on the calling
thread. These tests pin the four things that change makes true:

1. The session runs on one thread with no worker pool.
2. Each call carries the previous call's last 64 samples and the model state,
   as upstream utils_vad.OnnxWrapper feeds the model, and reset() clears both.
3. The probabilities match pysilero-vad 3.2.0 on a recorded fixture. Those
   tests read a recording, so they live in test_silero_vad_fixture.py, which
   the release export prunes with the recording.
4. A missing or unreadable model file fails with an error that names the path.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_audio import silero_vad  # noqa: E402


class TestSessionOptions:
    def test_session_runs_on_one_thread_with_cpu_provider(self):
        session = silero_vad._get_session()
        options = session.get_session_options()
        import onnxruntime as ort
        assert options.intra_op_num_threads == 1
        assert options.inter_op_num_threads == 1
        assert options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL
        assert session.get_providers() == ["CPUExecutionProvider"]

    def test_one_session_per_process(self):
        assert silero_vad._get_session() is silero_vad._get_session()


class TestInputFeeding:
    """The model input is 64 context samples plus the 512-sample chunk."""

    def test_each_call_prepends_previous_chunk_tail_and_carries_state(self):
        detector = silero_vad._get_silero_detector()
        calls = []
        real_run = detector._session.run

        def spy(names, feeds):
            calls.append({k: np.array(v, copy=True) for k, v in feeds.items()})
            return real_run(names, feeds)

        with patch.object(detector._session, "run", side_effect=spy, create=True):
            first = (np.arange(512, dtype="<i2") * 7).tobytes()
            second = (np.arange(512, dtype="<i2") * -3).tobytes()
            detector(first)
            state_after_first = detector._state.copy()
            detector(second)

        assert calls[0]["input"].shape == (1, 576)
        assert np.all(calls[0]["input"][0, :64] == 0.0)
        assert np.all(calls[0]["state"] == 0.0)
        expected_tail = np.frombuffer(first, dtype="<i2")[-64:].astype(np.float32) / 32768.0
        assert np.array_equal(calls[1]["input"][0, :64], expected_tail)
        expected_chunk = np.frombuffer(second, dtype="<i2").astype(np.float32) / 32768.0
        assert np.array_equal(calls[1]["input"][0, 64:], expected_chunk)
        assert np.array_equal(calls[1]["state"], state_after_first)
        assert not np.all(state_after_first == 0.0)
        assert int(calls[0]["sr"]) == 16000

    def test_reset_clears_context_and_state(self):
        detector = silero_vad._get_silero_detector()
        detector((np.arange(512, dtype="<i2") * 11).tobytes())
        detector.reset()
        assert np.all(detector._context == 0.0)
        assert np.all(detector._state == 0.0)


class TestModelFile:
    def test_model_file_ships_beside_the_module(self):
        assert silero_vad.MODEL_PATH.is_file()
        assert silero_vad.MODEL_PATH.parent == Path(silero_vad.__file__).parent / "vad_model"

    def test_missing_model_file_names_the_path(self, tmp_path):
        missing = tmp_path / "absent.onnx"
        with pytest.raises(silero_vad.SileroModelError) as info:
            silero_vad._create_session(missing)
        assert str(missing) in str(info.value)
        # The file check runs before onnxruntime is asked to load anything,
        # so a missing file reads "not found", not "could not be loaded".
        assert "not found" in str(info.value)

    def test_unreadable_model_file_names_the_path(self, tmp_path):
        broken = tmp_path / "broken.onnx"
        broken.write_bytes(b"not an onnx model")
        with pytest.raises(silero_vad.SileroModelError) as info:
            silero_vad._create_session(broken)
        assert str(broken) in str(info.value)
