"""Tests for Parakeet hotwords: startup file generation, engine wiring, and
the live add_hint handler (wh-q33mj Phase 2: wh-5w04r, wh-afhfj, wh-kcu8f,
wh-pirep).

The sherpa recognizer is never really constructed here; from_transducer is
mocked and its kwargs inspected. The spike (wh-q3nrw) fixed the contract:
plain-text hotwords + modeling_unit='bpe' + bpe_vocab=tokens.txt, and
omitting bpe_vocab segfaults sherpa natively, so the wiring asserts it is
always present whenever hotwords are passed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main as parakeet_main
from main import ParakeetServer, prepare_hotwords_file
from sherpa_engine import SherpaOfflineEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_model_dir(tmp_path: Path) -> Path:
    """A model directory that passes _load_model's existence checks."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (model_dir / name).write_text("", encoding="utf-8")
    return model_dir


def _build_engine(model_dir: Path, **kwargs):
    """Construct SherpaOfflineEngine with sherpa_onnx mocked; return the
    kwargs from_transducer received."""
    with patch("sherpa_engine.sherpa_onnx") as sherpa_mock:
        SherpaOfflineEngine(model_path=str(model_dir), **kwargs)
        call = sherpa_mock.OfflineRecognizer.from_transducer.call_args
    assert call is not None, "from_transducer was not called"
    return call.kwargs


# ---------------------------------------------------------------------------
# wh-5w04r: hotwords file generation at startup
# ---------------------------------------------------------------------------

class TestPrepareHotwordsFile:
    @pytest.fixture
    def fake_hints(self, monkeypatch, tmp_path):
        """Install a stub hints_updater module whose hints we control."""
        stub = types.ModuleType("hints_updater")
        stub.hints = ["Zwicky", "WheelHouse", "sherpa"]
        stub.get_hints = lambda: list(stub.hints)
        monkeypatch.setitem(sys.modules, "hints_updater", stub)
        monkeypatch.setattr(
            parakeet_main, "_HOTWORDS_RUNTIME_PATH", tmp_path / "runtime" / "parakeet-hotwords.txt"
        )
        return stub

    def test_writes_one_phrase_per_line(self, fake_hints, tmp_path):
        result = prepare_hotwords_file()
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert result == str(dest)
        assert dest.read_text(encoding="utf-8") == "Zwicky\nWheelHouse\nsherpa\n"

    def test_regenerated_each_start(self, fake_hints, tmp_path):
        prepare_hotwords_file()
        fake_hints.hints = ["Zwicky"]
        prepare_hotwords_file()
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert dest.read_text(encoding="utf-8") == "Zwicky\n"

    def test_no_hints_returns_none_and_removes_stale_file(self, fake_hints, tmp_path):
        prepare_hotwords_file()
        fake_hints.hints = []
        result = prepare_hotwords_file()
        assert result is None
        assert not (tmp_path / "runtime" / "parakeet-hotwords.txt").exists()

    def test_filesystem_error_degrades_to_none(self, fake_hints, monkeypatch, tmp_path):
        # wh-q33mj.1.4: an I/O error preparing the file must degrade to
        # no-hotwords, never crash startup into the launcher's
        # fast-crash restart loop. Parent path is a FILE, so mkdir
        # raises.
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            parakeet_main,
            "_HOTWORDS_RUNTIME_PATH",
            blocker / "runtime" / "parakeet-hotwords.txt",
        )
        assert prepare_hotwords_file() is None

    def test_unlink_race_does_not_raise(self, fake_hints, tmp_path):
        # wh-q33mj.1.4: stale-file removal must use missing_ok
        # semantics; an exists()/unlink() race must not crash startup.
        fake_hints.hints = []
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert not dest.exists()
        assert prepare_hotwords_file() is None


class TestHotwordsVocabCoverage:
    """wh-q33mj.1.6: hints with characters outside the model vocab are
    silently ignored by sherpa while the user still pays the beam-search
    latency. prepare_hotwords_file warns about and drops uncovered hints
    when given the model tokens.txt."""

    @pytest.fixture
    def fake_hints(self, monkeypatch, tmp_path):
        stub = types.ModuleType("hints_updater")
        stub.hints = ["Zwicky", "sherpa"]
        stub.get_hints = lambda: list(stub.hints)
        monkeypatch.setitem(sys.modules, "hints_updater", stub)
        monkeypatch.setattr(
            parakeet_main,
            "_HOTWORDS_RUNTIME_PATH",
            tmp_path / "runtime" / "parakeet-hotwords.txt",
        )
        return stub

    @pytest.fixture
    def tokens_file(self, tmp_path):
        tokens = tmp_path / "tokens.txt"
        # Real format: "<piece> <id>" per line; pieces carry the BPE
        # space marker.
        pieces = ["▁Zwi", "cky", "▁sher", "pa", "▁the", "s"]
        tokens.write_text(
            "\n".join(f"{p} {i}" for i, p in enumerate(pieces)) + "\n",
            encoding="utf-8",
        )
        return tokens

    def test_covered_hints_written(self, fake_hints, tokens_file, tmp_path):
        result = prepare_hotwords_file(tokens_path=tokens_file)
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert result == str(dest)
        assert dest.read_text(encoding="utf-8") == "Zwicky\nsherpa\n"

    def test_uncovered_hint_dropped_with_warning(
        self, fake_hints, tokens_file, tmp_path, caplog
    ):
        import logging

        fake_hints.hints = ["Zwicky", "日本語"]
        with caplog.at_level(logging.WARNING):
            prepare_hotwords_file(tokens_path=tokens_file)
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert dest.read_text(encoding="utf-8") == "Zwicky\n"
        # The hint itself is redacted (wh-797.19.4); the warning carries
        # the missing-character metadata instead.
        assert "outside the model vocab" in caplog.text
        assert "日" in caplog.text

    def test_all_hints_uncovered_behaves_like_no_hints(
        self, fake_hints, tokens_file, tmp_path
    ):
        fake_hints.hints = ["日本語"]
        assert prepare_hotwords_file(tokens_path=tokens_file) is None
        assert not (tmp_path / "runtime" / "parakeet-hotwords.txt").exists()

    def test_missing_tokens_file_skips_validation(self, fake_hints, tmp_path):
        fake_hints.hints = ["日本語"]
        result = prepare_hotwords_file(tokens_path=tmp_path / "absent.txt")
        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert result == str(dest)
        assert dest.read_text(encoding="utf-8") == "日本語\n"

    def test_no_tokens_path_skips_validation(self, fake_hints, tmp_path):
        fake_hints.hints = ["日本語"]
        result = prepare_hotwords_file()
        assert result == str(tmp_path / "runtime" / "parakeet-hotwords.txt")


# ---------------------------------------------------------------------------
# wh-afhfj: engine wiring of hotwords_file + hotwords_score
# ---------------------------------------------------------------------------

class TestEngineHotwordsWiring:
    def test_hotwords_passed_with_bpe_vocab_and_beam_search(self, tmp_path):
        model_dir = _fake_model_dir(tmp_path)
        hotwords = tmp_path / "hotwords.txt"
        hotwords.write_text("Zwicky\n", encoding="utf-8")

        kwargs = _build_engine(
            model_dir, hotwords_file=str(hotwords), hotwords_score=3.5
        )

        assert kwargs["hotwords_file"] == str(hotwords)
        assert kwargs["hotwords_score"] == 3.5
        assert kwargs["modeling_unit"] == "bpe"
        # Omitting bpe_vocab with hotwords segfaults sherpa natively
        # (wh-q3nrw case B) -- it must always ride along.
        assert kwargs["bpe_vocab"] == str(model_dir / "tokens.txt")
        # greedy_search silently ignores hotwords_file.
        assert kwargs["decoding_method"] == "modified_beam_search"

    def test_no_hotwords_file_keeps_greedy_default(self, tmp_path):
        kwargs = _build_engine(_fake_model_dir(tmp_path))
        for key in ("hotwords_file", "hotwords_score", "modeling_unit", "bpe_vocab", "decoding_method"):
            assert key not in kwargs

    def test_missing_hotwords_file_degrades_to_no_hotwords(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            kwargs = _build_engine(
                _fake_model_dir(tmp_path),
                hotwords_file=str(tmp_path / "does-not-exist.txt"),
            )
        assert "hotwords" in caplog.text.lower()
        for key in ("hotwords_file", "hotwords_score", "modeling_unit", "bpe_vocab", "decoding_method"):
            assert key not in kwargs


# ---------------------------------------------------------------------------
# wh-kcu8f: live add_hint handler
# ---------------------------------------------------------------------------

class TestHandleAddHint:
    @pytest.fixture
    def flag_path(self, tmp_path, monkeypatch):
        """Point the launcher restart flag at a temp file so the real
        flag-write path runs without touching AppData."""
        import shared_stt.launcher as launcher_mod

        flag = tmp_path / "restart.flag"
        monkeypatch.setattr(
            launcher_mod, "get_restart_flag_path", lambda name: flag
        )
        return flag

    @pytest.fixture
    def server(self, flag_path, monkeypatch):
        """A ParakeetServer shell: no audio, no engine, just the handler's
        collaborators. The add-hint gate re-reads config.toml
        (wh-q33mj.4.1), so the fixture patches load_config to report
        hotwords enabled; tests override it to flip the gate."""
        monkeypatch.setattr(
            parakeet_main,
            "load_config",
            lambda: {"hotwords": {"enabled": True}},
        )
        s = ParakeetServer.__new__(ParakeetServer)
        s.forwarder = MagicMock()
        s.display_name = "Parakeet v3 (CPU)"
        s.hotwords_enabled = True
        s.stop = MagicMock()
        return s

    @pytest.fixture
    def hints_stub(self, monkeypatch):
        # The REAL hints_updater contract (wh-q33mj.1.2): add_hint
        # returns False for BOTH duplicate and I/O error (it catches
        # Exception internally), and get_hints returns [] on error.
        # The stub mirrors that; no test may stub add_hint raising.
        stub = types.ModuleType("hints_updater")
        stub.add_hint = MagicMock(return_value=True)
        stub.get_hints = MagicMock(return_value=["Zwicky"])
        monkeypatch.setitem(sys.modules, "hints_updater", stub)
        return stub

    def test_new_hint_added_and_restarts(self, server, hints_stub, flag_path):
        server._handle_add_hint("Zwicky")
        hints_stub.add_hint.assert_called_once_with("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()
        notification = server.forwarder.send_notification.call_args[0]
        assert "restarting" in notification[1].lower()

    def test_flag_failure_reports_deferred_not_restarting(
        self, server, hints_stub, monkeypatch
    ):
        # wh-q33mj.3.1: the "restarting to apply" announcement must come
        # AFTER the restart flag is durably written. Announcing first and
        # then failing the flag write sent two contradictory voice
        # notifications back to back.
        import shared_stt.launcher as launcher_mod

        def boom(name):
            raise OSError("AppData unwritable")

        monkeypatch.setattr(launcher_mod, "get_restart_flag_path", boom)
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        messages = [
            c[0][1].lower()
            for c in server.forwarder.send_notification.call_args_list
        ]
        assert not any("restarting" in m for m in messages)
        assert "restart failed" in messages[-1]

    def test_hotwords_disabled_saves_without_restart(
        self, server, hints_stub, flag_path, monkeypatch
    ):
        # wh-q33mj.1.1: with the shipped enabled=false default, a
        # restart reloads the 0.6B model for zero effect. Save the hint,
        # skip the restart, and say so honestly. The cached flag stays
        # True to prove the gate reads the config file, not the cache
        # (wh-q33mj.4.1).
        monkeypatch.setattr(
            parakeet_main,
            "load_config",
            lambda: {"hotwords": {"enabled": False}},
        )
        server.hotwords_enabled = True
        server._handle_add_hint("Zwicky")
        hints_stub.add_hint.assert_called_once_with("Zwicky")
        server.stop.assert_not_called()
        assert not flag_path.exists()
        notification = server.forwarder.send_notification.call_args[0]
        assert "saved" in notification[1].lower()
        assert "restarting" not in notification[1].lower()

    def test_stale_disabled_cache_updates_from_config(
        self, server, hints_stub, flag_path
    ):
        # wh-q33mj.4.1: the user enables [hotwords] in config.toml AFTER
        # the service started (the soft-restart handler applies nothing).
        # The gate must read the file fresh, or the hint is saved with a
        # misleading "enable [hotwords] in config.toml" message even
        # though it already IS enabled. Fixture load_config says enabled.
        server.hotwords_enabled = False  # stale construction-time cache
        server._handle_add_hint("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()
        notification = server.forwarder.send_notification.call_args[0]
        assert "restarting" in notification[1].lower()

    def test_config_reread_failure_falls_back_to_cached(
        self, server, hints_stub, flag_path, monkeypatch
    ):
        # A malformed config.toml mid-edit must not turn add-hint into an
        # error; the gate falls back to the construction-time value.
        def boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(parakeet_main, "load_config", boom)
        server.hotwords_enabled = True
        server._handle_add_hint("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()

    def test_duplicate_hint_no_restart(self, server, hints_stub):
        # add_hint False + hint present in get_hints = duplicate.
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = ["Zwicky"]
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_duplicate_check_is_case_insensitive(self, server, hints_stub):
        # hints_updater dedupes case-insensitively; the duplicate-vs-
        # error probe must match that or a duplicate with different
        # casing would be misreported as a write error.
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = ["zwicky"]
        server._handle_add_hint("Zwicky")
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_long_duplicate_matches_truncated_stored_form(self, server, hints_stub):
        # wh-q33mj.2.1: add_hint truncates hints over 100 chars before
        # storing and before its own duplicate check. The probe must
        # apply the same normalization, or a long duplicate is
        # misreported as a write failure.
        long_hint = "x" * 120
        stored_form = long_hint[:100]
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = [stored_form]
        server._handle_add_hint(long_hint)
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_write_error_reported_as_error_not_duplicate(self, server, hints_stub):
        # wh-q33mj.1.2: add_hint False + hint ABSENT from get_hints =
        # the write failed (disk full, permissions). Telling the user
        # "already exists" loses the hint silently.
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = []
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" not in notification[1].lower()
        assert "could not save" in notification[1].lower()


class TestHardRestartService:
    """wh-q33mj.1.3: a failed restart-flag write must NOT stop the
    service -- exit 0 with no flag means the launcher never brings it
    back, permanently killing STT from a voice command."""

    @pytest.fixture
    def server(self):
        s = ParakeetServer.__new__(ParakeetServer)
        s.forwarder = MagicMock()
        s.display_name = "Parakeet v3 (CPU)"
        s.stop = MagicMock()
        return s

    def test_flag_written_then_stops(self, server, tmp_path, monkeypatch):
        import shared_stt.launcher as launcher_mod

        flag = tmp_path / "restart.flag"
        monkeypatch.setattr(
            launcher_mod, "get_restart_flag_path", lambda name: flag
        )
        server._handle_hard_restart_service()
        assert flag.read_text() == "restart"
        server.stop.assert_called_once()

    def test_flag_write_failure_keeps_service_running(
        self, server, tmp_path, monkeypatch
    ):
        import shared_stt.launcher as launcher_mod

        def boom(name):
            raise OSError("AppData unwritable")

        monkeypatch.setattr(launcher_mod, "get_restart_flag_path", boom)
        server._handle_hard_restart_service()
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "restart failed" in notification[1].lower()


class TestSoftRestartService:
    """wh-parakeet-soft-restart-noop: the soft-restart handler loads
    config.toml into a local and applies none of it. Announcing
    'reloaded successfully' was misleading -- the only thing it proves
    is that the file parses. The honest message: validated, restart to
    apply."""

    @pytest.fixture
    def server(self, monkeypatch):
        monkeypatch.setattr(parakeet_main, "load_config", lambda: {})
        s = ParakeetServer.__new__(ParakeetServer)
        s.forwarder = MagicMock()
        s.display_name = "Parakeet v3 (CPU)"
        return s

    def test_success_says_validated_not_applied(self, server):
        server._handle_restart_service()
        messages = [
            c[0][1].lower()
            for c in server.forwarder.send_notification.call_args_list
        ]
        assert not any("reloaded successfully" in m for m in messages)
        assert any("restart to apply" in m for m in messages)

    def test_parse_failure_reports_error(self, server, monkeypatch):
        def boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(parakeet_main, "load_config", boom)
        server._handle_restart_service()
        messages = [
            c[0][1].lower()
            for c in server.forwarder.send_notification.call_args_list
        ]
        assert any("failed" in m for m in messages)


class TestHotwordsVocabWarningRedaction:
    """wh-797.19.4: the OOV-hotword warning logged the hint verbatim at
    provider startup. The hint is redacted; the missing-character set
    stays verbatim as diagnostic metadata (it names WHICH characters the
    model vocab lacks, not the hint itself)."""

    @pytest.fixture
    def fake_hints(self, monkeypatch, tmp_path):
        stub = types.ModuleType("hints_updater")
        stub.hints = ["Zwicky"]
        stub.get_hints = lambda: list(stub.hints)
        monkeypatch.setitem(sys.modules, "hints_updater", stub)
        monkeypatch.setattr(
            parakeet_main,
            "_HOTWORDS_RUNTIME_PATH",
            tmp_path / "runtime" / "parakeet-hotwords.txt",
        )
        return stub

    @pytest.fixture
    def tokens_file(self, tmp_path):
        tokens = tmp_path / "tokens.txt"
        pieces = ["▁Zwi", "cky", "▁sher", "pa", "▁the", "s"]
        tokens.write_text(
            "\n".join(f"{p} {i}" for i, p in enumerate(pieces)) + "\n",
            encoding="utf-8",
        )
        return tokens

    def test_oov_warning_redacts_hint_by_default(
        self, fake_hints, tokens_file, monkeypatch, caplog
    ):
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        # All chars of "secret" are in the vocab fixture; the kanji is not,
        # so only it appears in the missing-character metadata.
        fake_hints.hints = ["Zwicky", "secret日"]
        with caplog.at_level(logging.WARNING):
            prepare_hotwords_file(tokens_path=tokens_file)

        assert "secret日" not in caplog.text
        assert "<redacted:" in caplog.text
        assert "日" in caplog.text  # missing-char diagnostic survives
