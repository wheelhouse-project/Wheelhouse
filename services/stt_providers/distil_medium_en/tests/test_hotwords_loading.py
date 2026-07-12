"""wh-apmg: hints.txt must actually reach the Whisper engine.

Before this change the add-hint flow appended to shared/hints.txt and
hard-restarted the service, but nothing on the distil-medium startup
path ever read the file -- the feature was a silent no-op. These tests
pin the loader (budget cap, newest-kept ordering, error degradation)
and the pass-through into WhisperStreamingEngine.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import main as distil_main
from main import _HOTWORDS_CHAR_BUDGET, DistilMediumServer, build_hotwords_string


@pytest.fixture
def hints_stub(monkeypatch):
    stub = types.ModuleType("hints_updater")
    stub.get_hints = MagicMock(return_value=["Zwicky", "WheelHouse"])
    monkeypatch.setitem(sys.modules, "hints_updater", stub)
    return stub


class TestBuildHotwordsString:
    def test_joins_hints_newest_first(self, hints_stub):
        # wh-apmg.1.1: faster-whisper truncates hotwords at 223 TOKENS
        # keeping the HEAD (get_prompt, venv-verified). Emitting newest
        # first means any token-level truncation eats the OLDEST hints,
        # keeping the newest-survive guarantee density-independent.
        assert build_hotwords_string() == "WheelHouse, Zwicky"

    def test_no_hints_returns_none(self, hints_stub):
        hints_stub.get_hints.return_value = []
        assert build_hotwords_string() is None

    def test_error_degrades_to_none(self, hints_stub):
        # get_hints returning [] on error is the hints_updater contract;
        # a broken import must also degrade instead of killing startup.
        hints_stub.get_hints.side_effect = RuntimeError("boom")
        assert build_hotwords_string() is None

    def test_budget_keeps_newest_hints(self, hints_stub):
        # hints.txt appends, so the newest hints are at the END of the
        # file. When the joined string would exceed the prompt budget,
        # the oldest hints are dropped, never the newest -- and the
        # newest hint leads the string so faster-whisper's own
        # head-keeping token truncation cannot remove it (wh-apmg.1.1).
        old = [f"oldword{i:03d}" for i in range(200)]
        hints_stub.get_hints.return_value = old + ["newestword"]
        result = build_hotwords_string()
        assert result is not None
        assert len(result) <= _HOTWORDS_CHAR_BUDGET
        assert result.startswith("newestword")
        assert "oldword000" not in result

    def test_under_budget_emits_newest_first(self, hints_stub):
        hints_stub.get_hints.return_value = ["alpha", "beta", "gamma"]
        assert build_hotwords_string() == "gamma, beta, alpha"


class TestServerPassesHotwords:
    def test_engine_receives_hotwords(self):
        with (
            patch("main.get_audio_provider"),
            patch("main.WhisperStreamingEngine") as engine_cls,
            patch("main.WSForwarder"),
            patch("main.AudioProcessor"),
        ):
            DistilMediumServer(
                model_config={},
                engine_config={},
                hotwords="Zwicky, WheelHouse",
            )
        assert engine_cls.call_args.kwargs["hotwords"] == "Zwicky, WheelHouse"

    def test_engine_default_no_hotwords(self):
        with (
            patch("main.get_audio_provider"),
            patch("main.WhisperStreamingEngine") as engine_cls,
            patch("main.WSForwarder"),
            patch("main.AudioProcessor"),
        ):
            DistilMediumServer(model_config={}, engine_config={})
        assert engine_cls.call_args.kwargs["hotwords"] is None

    def test_startup_wires_loader_into_server(self):
        # The __main__ block must call build_hotwords_string() and hand
        # the result to DistilMediumServer. Source-level pin: cheap, and
        # it fails loudly if the wiring line is ever dropped.
        import inspect

        source = inspect.getsource(distil_main)
        main_block = source[source.index('if __name__ == "__main__"'):]
        assert "build_hotwords_string()" in main_block
        assert "hotwords=hotwords" in main_block

    def test_startup_has_config_escape_hatch(self):
        # wh-apmg.1.2: without a [hotwords] enabled gate, the only way
        # to turn off a misbehaving bias is to empty shared/hints.txt --
        # which also destroys Google STT phrase adaptation and parakeet
        # hotwords. The gate must be read in the __main__ block.
        import inspect

        source = inspect.getsource(distil_main)
        main_block = source[source.index('if __name__ == "__main__"'):]
        assert 'get("hotwords", {}).get("enabled", True)' in main_block
