"""The files and the config key of the training word (wh-first-sound-lost.23).

lead_word_store owns the folder, the [engine] key lead_word_path, and the
store actions (save a candidate, Keep, go back, turn off, turn on, discard).
Every test writes under tmp_path: the folder and the config.toml copy. The
engine is a recording fake: a callable that stores what it was given.
"""
from __future__ import annotations

import json
import logging
import tomllib
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

import lead_word_recorder as rec
import lead_word_store as store_mod
from lead_word_store import (
    FILE_CANDIDATE,
    FILE_CANDIDATE_META,
    FILE_META,
    FILE_PREVIOUS,
    FILE_WAV,
    LeadWordStore,
    LeadWordStoreError,
    lead_word_folder,
    read_lead_word_key,
    write_lead_word_key,
)
from sherpa_engine import load_lead_word_audio

NOW = datetime(2026, 10, 22, 9, 5, 59)
NOW_TEXT = "2026-10-22 09:05"

CONFIG = (
    "# Provider settings -- keep this comment\n"
    "[server]\n"
    "port = 9001\n"
    "\n"
    "[engine]\n"
    "# the training word\n"
    "lead_word_path = ''  # set by the window\n"
    "hotwords_enabled = true\n"
    "\n"
    "[logging]\n"
    "level = 'INFO'\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cut(seed: int, length: int = 8000) -> np.ndarray:
    """A distinct int16 'recording' per seed."""
    rng = np.random.default_rng(seed)
    return rng.integers(-3000, 3000, size=length, dtype=np.int16)


class FakeEngine:
    """Records every word the store gives the engine."""

    def __init__(self):
        self.calls = []

    def __call__(self, audio):
        self.calls.append(audio)


def folder_bytes(folder) -> dict:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.exists() else {}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(CONFIG.encode("utf-8"))
    return path


@pytest.fixture
def store(tmp_path, config):
    return LeadWordStore(tmp_path / "lead-word", config, now=lambda: NOW)


def candidate(store, seed=1, decode="Hello.", microphone="Mic A"):
    measures = {"capture_ms": 1500, "decode": decode}
    store.save_candidate(make_cut(seed), measures, microphone)
    return make_cut(seed)


def keep_one(store, seed=1, **kw):
    cut = candidate(store, seed, **kw)
    engine = FakeEngine()
    store.keep(engine)
    return cut, engine


def put(path, text: str) -> None:
    """Write `text` byte for byte. Path.write_text turns LF into CRLF on
    Windows, and the newline that the key writer picks is what several tests
    check."""
    path.write_bytes(text.encode("utf-8"))


def get(path) -> str:
    return path.read_bytes().decode("utf-8")


def key(config) -> str:
    return tomllib.loads(config.read_text(encoding="utf-8"))["engine"]["lead_word_path"]


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------

class TestFolder:
    def test_folder_is_under_local_app_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert lead_word_folder() == tmp_path / "WheelHouse" / "lead-word"
        assert not (tmp_path / "WheelHouse").exists()  # creates nothing

    def test_missing_local_app_data_raises(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        with pytest.raises(KeyError):
            lead_word_folder()

    def test_the_four_file_names_and_the_measures_file(self):
        assert (FILE_WAV, FILE_PREVIOUS, FILE_CANDIDATE, FILE_META) == (
            "hello.wav", "hello.previous.wav", "hello.candidate.wav", "hello.json")
        assert FILE_CANDIDATE_META == "hello.candidate.json"


# ---------------------------------------------------------------------------
# The key writer and reader
# ---------------------------------------------------------------------------

class TestKeyWriter:
    def test_replaces_the_value_and_keeps_every_other_byte(self, config):
        write_lead_word_key(config, r"C:\Users\x\hello.wav")
        after = config.read_text(encoding="utf-8")
        assert after == CONFIG.replace(
            "lead_word_path = ''", r"lead_word_path = 'C:\Users\x\hello.wav'")
        assert key(config) == r"C:\Users\x\hello.wav"

    def test_the_same_line_comment_stays(self, config):
        write_lead_word_key(config, "a.wav")
        assert "lead_word_path = 'a.wav'  # set by the window\n" in config.read_text(
            encoding="utf-8")

    def test_writing_an_empty_value(self, config):
        write_lead_word_key(config, "a.wav")
        write_lead_word_key(config, "")
        assert key(config) == ""
        assert config.read_text(encoding="utf-8") == CONFIG

    def test_a_double_quoted_old_value_is_replaced(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, '[engine]\nlead_word_path = "old\\\\path" # note\nx = 1\n')
        write_lead_word_key(path, "new.wav")
        assert get(path) == "[engine]\nlead_word_path = 'new.wav' # note\nx = 1\n"

    def test_an_old_value_with_an_escaped_quote_is_replaced_whole(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, '[engine]\nlead_word_path = "a\\"b" # note\nx = 1\n')
        write_lead_word_key(path, "new.wav")
        assert get(path) == "[engine]\nlead_word_path = 'new.wav' # note\nx = 1\n"

    def test_a_bare_old_value_ends_at_the_first_space(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "[engine]\nlead_word_path = 5   # note\nx = 1\n")
        write_lead_word_key(path, "a")
        assert get(path) == "[engine]\nlead_word_path = 'a'   # note\nx = 1\n"

    @pytest.mark.parametrize("line, message", [
        ("lead_word_path = 'abc", "unterminated"),
        ('lead_word_path = "abc', "unterminated"),
        ("lead_word_path = '''a'''", "multi-line"),
        ('lead_word_path = """a"""', "multi-line"),
    ], ids=["literal-unterminated", "basic-unterminated", "triple-single", "triple-double"])
    def test_an_old_value_that_cannot_be_replaced_is_refused_with_its_reason(
        self, tmp_path, line, message
    ):
        path = tmp_path / "c.toml"
        text = f"[engine]\n{line}\nx = 1\n"
        put(path, text)
        with pytest.raises(ValueError, match=message):
            write_lead_word_key(path, "b")
        assert get(path) == text

    def test_a_missing_line_is_added_at_the_end_of_the_engine_body(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "[engine]\nhotwords_enabled = true\n\n[logging]\nlevel = 'INFO'\n")
        write_lead_word_key(path, "a.wav")
        assert get(path) == (
            "[engine]\nhotwords_enabled = true\nlead_word_path = 'a.wav'\n\n"
            "[logging]\nlevel = 'INFO'\n")

    def test_a_missing_line_at_the_end_of_a_file_without_a_final_newline(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_bytes(b"[engine]\nx = 1")
        write_lead_word_key(path, "a.wav")
        assert path.read_bytes() == b"[engine]\nx = 1\nlead_word_path = 'a.wav'\n"

    def test_a_missing_section_is_added(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "[server]\nport = 1\n")
        write_lead_word_key(path, "a.wav")
        assert get(path) == "[server]\nport = 1\n\n[engine]\nlead_word_path = 'a.wav'\n"

    def test_a_missing_section_after_a_last_line_without_a_newline(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "[server]\nport = 1")
        write_lead_word_key(path, "a.wav")
        assert get(path) == "[server]\nport = 1\n\n[engine]\nlead_word_path = 'a.wav'\n"

    def test_a_missing_section_in_an_empty_file(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "")
        write_lead_word_key(path, "a.wav")
        assert get(path) == "[engine]\nlead_word_path = 'a.wav'\n"

    def test_a_key_of_a_later_section_is_not_touched_when_engine_has_none(self, tmp_path):
        path = tmp_path / "c.toml"
        put(path, "[engine]\nx = 1\n\n[logging]\nlead_word_path = 'keep'\n")
        write_lead_word_key(path, "y")
        assert get(path) == (
            "[engine]\nx = 1\nlead_word_path = 'y'\n\n[logging]\nlead_word_path = 'keep'\n")

    def test_a_key_of_another_section_is_not_touched(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[other]\nlead_word_path = 'keep'\n\n[engine]\nlead_word_path = 'x'\n")
        write_lead_word_key(path, "y")
        parsed = tomllib.loads(path.read_text())
        assert parsed["other"]["lead_word_path"] == "keep"
        assert parsed["engine"]["lead_word_path"] == "y"

    def test_a_header_with_a_comment_is_the_engine_section(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[engine]  # the engine\nx = 1\n")
        write_lead_word_key(path, "a")
        assert tomllib.loads(path.read_text())["engine"] == {"x": 1, "lead_word_path": "a"}

    def test_crlf_stays_crlf(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_bytes(b"[engine]\r\nlead_word_path = ''\r\nx = 1\r\n")
        write_lead_word_key(path, "a.wav")
        assert path.read_bytes() == b"[engine]\r\nlead_word_path = 'a.wav'\r\nx = 1\r\n"
        path.write_bytes(b"[engine]\r\nx = 1\r\n")
        write_lead_word_key(path, "a.wav")
        assert path.read_bytes() == b"[engine]\r\nx = 1\r\nlead_word_path = 'a.wav'\r\n"

    @pytest.mark.parametrize("value", [
        "it's here.wav",            # single quote: needs a basic string
        "caf\u00e9\\h\u00e9llo.wav",  # non-ASCII letters and a backslash
        'say "hi".wav',
        r"C:\Users\d\AppData\Local\WheelHouse\lead-word\hello.wav",
        "tab\there",
        "bell\x07.wav",             # control character: needs a basic string
        "del\x7f.wav",              # DEL: needs a basic string
        "it's \"x\" \\y",           # all three escapes in one basic string
    ], ids=["apostrophe", "non-ascii", "double-quote", "windows-path", "tab",
            "control-char", "del-char", "all-escapes"])
    def test_values_round_trip(self, config, value):
        write_lead_word_key(config, value)
        assert key(config) == value
        assert read_lead_word_key(config) == value

    @pytest.mark.parametrize("value, text", [
        ("a.wav", "'a.wav'"),
        ("tab\there", "'tab\there'"),
        ("it's", "\"it's\""),
        ("it's \"x\" \\y", "\"it's \\\"x\\\" \\\\y\""),
        ("a\x01b'", "\"a\\u0001b'\""),
        ("a\x7fb", "\"a\\u007fb\""),
    ], ids=["plain", "tab-stays-literal", "apostrophe", "escapes", "control-escaped",
            "del-escaped"])
    def test_values_are_written_in_the_right_string_form(self, tmp_path, value, text):
        path = tmp_path / "c.toml"
        put(path, "[engine]\nlead_word_path = ''\n")
        write_lead_word_key(path, value)
        assert get(path) == f"[engine]\nlead_word_path = {text}\n"

    def test_a_multi_line_old_value_is_refused_and_the_file_is_unchanged(self, tmp_path):
        path = tmp_path / "c.toml"
        text = "[engine]\nlead_word_path = '''\na\n'''\n"
        path.write_text(text)
        with pytest.raises(ValueError):
            write_lead_word_key(path, "b")
        assert path.read_text() == text

    def test_a_failed_replace_leaves_the_file_and_no_temp_file(self, config, monkeypatch):
        before = config.read_bytes()

        def boom(src, dst):
            raise OSError("disk")

        monkeypatch.setattr(store_mod.os, "replace", boom)
        with pytest.raises(OSError):
            write_lead_word_key(config, "a.wav")
        assert config.read_bytes() == before
        assert [p.name for p in config.parent.iterdir()] == ["config.toml"]

    def test_the_read_back_check_refuses_a_wrong_edit(self, config, monkeypatch):
        before = config.read_bytes()
        monkeypatch.setattr(store_mod, "_toml_string", lambda v: "'other'")
        with pytest.raises(ValueError, match="verification"):
            write_lead_word_key(config, "a.wav")
        assert config.read_bytes() == before


class TestKeyReader:
    def test_reads_the_file_now_not_a_cached_value(self, config):
        assert read_lead_word_key(config) == ""
        config.write_text(CONFIG.replace("''", "'b.wav'"), encoding="utf-8")
        assert read_lead_word_key(config) == "b.wav"

    def test_a_missing_file_gives_the_fallback(self, tmp_path):
        assert read_lead_word_key(tmp_path / "none.toml", "kept") == "kept"

    def test_a_broken_file_gives_the_fallback(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[engine\nlead_word_path = ")
        assert read_lead_word_key(path, "kept") == "kept"

    def test_a_value_that_is_not_text_counts_as_empty(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[engine]\nlead_word_path = 5\n")
        assert read_lead_word_key(path, "kept") == ""

    def test_no_engine_section_gives_empty(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[server]\nx = 1\n")
        assert read_lead_word_key(path, "kept") == ""


# ---------------------------------------------------------------------------
# The candidate
# ---------------------------------------------------------------------------

class TestCandidate:
    def test_save_candidate_writes_the_wav_and_the_facts(self, store):
        cut = candidate(store, 1, decode="Hello.", microphone="Mic A")
        assert np.array_equal(
            (load_lead_word_audio(str(store.candidate_path), 16000) * 32768).round().astype(np.int16),
            cut)
        facts = json.loads(store.candidate_meta_path.read_text(encoding="utf-8"))
        assert facts == {
            "measures": {"capture_ms": 1500, "decode": "Hello."},
            "recorded": NOW_TEXT,
            "microphone": "Mic A",
            "decode": "Hello.",
        }
        assert sorted(p.name for p in store.folder.iterdir()) == sorted(
            [FILE_CANDIDATE, FILE_CANDIDATE_META])

    def test_save_candidate_never_touches_hello_wav(self, store):
        keep_one(store, 1)
        before = store.wav_path.read_bytes()
        candidate(store, 2)
        assert store.wav_path.read_bytes() == before

    def test_a_second_candidate_replaces_the_first(self, store):
        candidate(store, 1)
        candidate(store, 2, microphone="Mic B")
        assert json.loads(store.candidate_meta_path.read_text())["microphone"] == "Mic B"

    def test_a_failed_write_leaves_no_candidate(self, store, monkeypatch):
        candidate(store, 1)

        def boom(path, cut):
            raise OSError("disk full")

        monkeypatch.setattr(rec, "write_wav", boom)
        with pytest.raises(LeadWordStoreError, match="disk full"):
            store.save_candidate(make_cut(2), {"decode": "x"}, "m")
        assert not store.candidate_path.exists()
        assert not store.candidate_meta_path.exists()
        assert [p.name for p in store.folder.iterdir()] == []

    def test_save_candidate_returns_the_candidate_path(self, store):
        path = store.save_candidate(make_cut(1), {"decode": "Hello."}, "Mic A")
        assert path == store.candidate_path
        assert path.exists()

    def test_a_failed_move_leaves_no_temp_file(self, store, monkeypatch):
        """The wav is written to a temp name; when the move fails, the temp
        file must not stay in the folder."""
        def boom(src, dst):
            raise OSError("cannot move")

        monkeypatch.setattr(store_mod.os, "replace", boom)
        with pytest.raises(LeadWordStoreError, match="cannot move"):
            store.save_candidate(make_cut(2), {"decode": "x"}, "m")
        assert list(store.folder.iterdir()) == []

    def test_discarding_nothing_logs_no_warning(self, store, caplog):
        store.folder.mkdir(parents=True)
        with caplog.at_level(logging.WARNING, logger="lead_word_store"):
            assert store.discard_candidate() is False
        assert caplog.records == []

    def test_discard_candidate(self, store):
        assert store.discard_candidate() is False
        candidate(store, 1)
        assert store.discard_candidate() is True
        assert list(store.folder.iterdir()) == []

    def test_discard_leaves_the_kept_files(self, store):
        keep_one(store, 1)
        before = folder_bytes(store.folder)
        candidate(store, 2)
        store.discard_candidate()
        assert folder_bytes(store.folder) == before

    def test_delete_refused_removes_a_candidate(self, store):
        candidate(store, 1)
        store.delete_refused()
        assert list(store.folder.iterdir()) == []


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------

class TestKeepFirst:
    def test_first_keep_creates_hello_wav_json_and_key_and_loads_the_engine(self, store, config):
        cut, engine = keep_one(store, 1)
        assert sorted(p.name for p in store.folder.iterdir()) == [FILE_META, FILE_WAV]
        loaded = load_lead_word_audio(str(store.wav_path), 16000)
        assert np.array_equal((loaded * 32768).round().astype(np.int16), cut)
        assert key(config) == str(store.wav_path)
        assert len(engine.calls) == 1
        assert np.array_equal(engine.calls[0], loaded)
        facts = json.loads(store.meta_path.read_text(encoding="utf-8"))
        assert facts["recorded"] == NOW_TEXT
        assert facts["microphone"] == "Mic A"
        assert facts["decode"] == "Hello."
        assert "previous" not in facts

    def test_the_next_provider_start_loads_the_same_file_as_the_running_engine(
        self, store, config, monkeypatch
    ):
        """Acceptance item 14, last sentence: the engine of the next start is
        built from the key in config.toml, and its word is the one that Keep
        gave the running engine."""
        from sherpa_engine import SherpaOfflineEngine

        monkeypatch.setattr(SherpaOfflineEngine, "_load_model", lambda *a, **kw: None)
        _, running = keep_one(store, 1)
        restarted = SherpaOfflineEngine(
            model_path="/nonexistent", lead_word_path=read_lead_word_key(config))
        assert restarted.has_lead_word()
        assert np.array_equal(restarted._lead_audio, running.calls[0])
        # Also after a replacement, which changes the files but not the key.
        _, running = keep_one(store, 2)
        restarted = SherpaOfflineEngine(
            model_path="/nonexistent", lead_word_path=read_lead_word_key(config))
        assert np.array_equal(restarted._lead_audio, running.calls[0])

    def test_the_other_config_bytes_stay(self, store, config):
        keep_one(store, 1)
        assert config.read_text(encoding="utf-8") == CONFIG.replace(
            "lead_word_path = ''", f"lead_word_path = '{store.wav_path}'")

    def test_keep_returns_the_new_status(self, store):
        candidate(store, 1)
        status = store.keep(FakeEngine())
        assert status.current_exists
        assert not status.previous_exists
        assert not status.candidate_exists
        assert status.recorded == NOW_TEXT
        assert status.microphone == "Mic A"
        assert status.key_path == str(store.wav_path)
        assert not status.key_names_other_file

    def test_keep_without_a_candidate_is_refused(self, store):
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError, match="No new recording to keep"):
            store.keep(engine)
        assert engine.calls == []

    def test_keep_with_a_candidate_whose_facts_are_gone_is_refused(self, store, config):
        candidate(store, 1)
        store.candidate_meta_path.unlink()
        before = (folder_bytes(store.folder), config.read_bytes())
        with pytest.raises(LeadWordStoreError):
            store.keep(FakeEngine())
        assert (folder_bytes(store.folder), config.read_bytes()) == before

    def test_a_file_that_cannot_be_read_stops_keep_with_a_store_error(self, store, config):
        """A directory where hello.previous.wav should be cannot be read as a
        file; the snapshot reports it as a LeadWordStoreError and Keep
        changes nothing."""
        candidate(store, 1)
        store.previous_path.mkdir()
        before = config.read_bytes()
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError, match="Cannot read the training word files"):
            store.keep(engine)
        assert config.read_bytes() == before
        assert store.candidate_path.exists() and not store.wav_path.exists()
        assert engine.calls == []

    def test_keep_with_an_unusable_candidate_changes_nothing(self, store, config):
        candidate(store, 1)
        store.candidate_path.write_bytes(b"not a wav")
        before = (folder_bytes(store.folder), config.read_bytes())
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError, match="cannot be used"):
            store.keep(engine)
        assert (folder_bytes(store.folder), config.read_bytes()) == before
        assert engine.calls == []


class TestKeepReplacement:
    def test_the_old_recording_becomes_previous_and_its_facts_follow(self, store, config):
        first, _ = keep_one(store, 1, microphone="Mic A")
        first_bytes = store.wav_path.read_bytes()
        first_facts = json.loads(store.meta_path.read_text())
        candidate(store, 2, decode="Hello!", microphone="Mic B")
        store.keep(FakeEngine())
        assert store.previous_path.read_bytes() == first_bytes
        assert sorted(p.name for p in store.folder.iterdir()) == [
            FILE_META, FILE_PREVIOUS, FILE_WAV]
        facts = json.loads(store.meta_path.read_text())
        assert facts["microphone"] == "Mic B"
        assert facts["previous"] == first_facts
        assert key(config) == str(store.wav_path)

    def test_a_third_keep_drops_the_oldest_copy(self, store):
        keep_one(store, 1)
        keep_one(store, 2)
        second_bytes = store.wav_path.read_bytes()
        keep_one(store, 3)
        assert store.previous_path.read_bytes() == second_bytes
        facts = json.loads(store.meta_path.read_text())
        assert "previous" in facts and "previous" not in facts["previous"]

    def test_the_engine_gets_the_new_word(self, store):
        keep_one(store, 1)
        cut, engine = keep_one(store, 2)
        assert len(engine.calls) == 1
        assert np.array_equal((engine.calls[0] * 32768).round().astype(np.int16), cut)

    def test_a_key_that_is_already_the_wav_is_not_rewritten(self, store, config, monkeypatch):
        keep_one(store, 1)
        candidate(store, 2)

        def boom(*a):
            raise AssertionError("the key was written")

        monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        store.keep(FakeEngine())
        # The key already named hello.wav, so no old key value is recorded.
        assert "old_key_value" not in json.loads(store.meta_path.read_text())

    def test_a_key_that_is_empty_is_set(self, store, config):
        keep_one(store, 1)
        store.turn_off(FakeEngine())
        assert key(config) == ""
        keep_one(store, 2)
        assert key(config) == str(store.wav_path)

    def test_a_key_in_another_slash_style_counts_as_the_wav(self, store, config, monkeypatch):
        keep_one(store, 1)
        write_lead_word_key(config, str(store.wav_path).replace("\\", "/").upper())
        before = config.read_bytes()
        candidate(store, 2)
        store.keep(FakeEngine())
        assert config.read_bytes() == before


class TestKeepKeyNamesAnotherFile:
    def other(self, tmp_path, config):
        other = tmp_path / "mine" / "word.wav"
        other.parent.mkdir()
        rec.write_wav(other, make_cut(9))
        write_lead_word_key(config, str(other))
        return other

    def test_first_keep_copies_the_other_file_to_previous_and_never_changes_it(
        self, store, config, tmp_path, caplog
    ):
        other = self.other(tmp_path, config)
        other_bytes = other.read_bytes()
        candidate(store, 1)
        with caplog.at_level(logging.INFO, logger="lead_word_store"):
            store.keep(FakeEngine())
        assert other.read_bytes() == other_bytes
        assert store.previous_path.read_bytes() == other_bytes
        assert key(config) == str(store.wav_path)
        facts = json.loads(store.meta_path.read_text())
        assert facts["old_key_value"] == str(other)
        assert facts["previous"] == {
            "measures": {}, "recorded": "", "microphone": "", "decode": "",
            "source": str(other)}
        assert str(other) in caplog.text

    @pytest.mark.parametrize("key_names", ["empty", "the-wav"])
    def test_a_first_keep_copies_no_file_when_the_key_names_none(
        self, store, config, monkeypatch, key_names
    ):
        """An empty key, or a key that names hello.wav (which does not exist
        yet), has no other file to copy: no copy is tried at all."""
        if key_names == "the-wav":
            write_lead_word_key(config, str(store.wav_path))

        def boom(src, dst):
            raise AssertionError(f"a copy was tried: {src}")

        monkeypatch.setattr(store_mod, "_copy_file", boom)
        candidate(store, 1)
        store.keep(FakeEngine())
        assert not store.previous_path.exists()
        assert "previous" not in json.loads(store.meta_path.read_text())

    def test_the_button_data_before_keep(self, store, config, tmp_path):
        other = self.other(tmp_path, config)
        status = store.status()
        assert status.key_path == str(other)
        assert status.key_names_other_file is True
        assert status.current_exists is False

    def test_with_a_kept_wav_the_old_wav_is_previous_and_the_other_file_is_not_copied(
        self, store, config, tmp_path
    ):
        keep_one(store, 1)
        old_wav = store.wav_path.read_bytes()
        other = self.other(tmp_path, config)
        other_bytes = other.read_bytes()
        status = store.status()
        assert status.key_names_other_file is True and status.current_exists is True
        candidate(store, 2)
        store.keep(FakeEngine())
        assert store.previous_path.read_bytes() == old_wav
        assert other.read_bytes() == other_bytes
        assert key(config) == str(store.wav_path)

    def test_a_missing_other_file_is_not_an_error(self, store, config, tmp_path):
        write_lead_word_key(config, str(tmp_path / "gone.wav"))
        candidate(store, 1)
        store.keep(FakeEngine())
        assert not store.previous_path.exists()
        assert key(config) == str(store.wav_path)
        # There is nothing older to go back to, so the facts name none.
        assert "previous" not in json.loads(store.meta_path.read_text())

    def test_an_existing_previous_is_kept_when_there_is_no_wav(self, store, config, tmp_path):
        keep_one(store, 1)
        keep_one(store, 2)
        previous = store.previous_path.read_bytes()
        previous_facts = json.loads(store.meta_path.read_text())["previous"]
        store.wav_path.unlink()
        self.other(tmp_path, config)
        candidate(store, 3)
        store.keep(FakeEngine())
        assert store.previous_path.read_bytes() == previous
        assert json.loads(store.meta_path.read_text())["previous"] == previous_facts


KEEP_FAULTS = ["replace-1", "replace-2", "json", "key", "engine"]


class TestKeepFailures:
    """Each Keep step fails in turn: files and key as before, engine unchanged."""

    def arm(self, fault, monkeypatch, engine):
        if fault.startswith("replace-"):
            real = store_mod._replace
            wanted = int(fault.split("-")[1])
            count = {"n": 0}

            def failing(src, dst):
                count["n"] += 1
                if count["n"] == wanted:
                    raise OSError(f"cannot rename #{wanted}")
                real(src, dst)

            monkeypatch.setattr(store_mod, "_replace", failing)
        elif fault == "json":
            def boom(self, path, data):
                raise OSError("cannot write the facts")

            monkeypatch.setattr(LeadWordStore, "_write_json", boom)
        elif fault == "key":
            def boom(path, value):
                raise OSError("cannot write the key")

            monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        elif fault == "engine":
            def boom(audio):
                engine.calls.append("raised")
                raise RuntimeError("engine said no")

            return boom
        return engine

    @pytest.mark.parametrize("fault", KEEP_FAULTS)
    def test_replacement_keep_puts_everything_back(self, store, config, monkeypatch, fault):
        keep_one(store, 1)
        write_lead_word_key(config, "elsewhere.wav")  # so the key step runs
        candidate(store, 2)
        before = (folder_bytes(store.folder), config.read_bytes())
        engine = FakeEngine()
        setter = self.arm(fault, monkeypatch, engine)
        with pytest.raises(LeadWordStoreError, match="Keep failed"):
            store.keep(setter)
        assert (folder_bytes(store.folder), config.read_bytes()) == before
        assert [c for c in engine.calls if not isinstance(c, str)] == []
        assert key(config) == "elsewhere.wav"

    @pytest.mark.parametrize("fault", ["replace-1", "replace-2", "json", "key", "engine"])
    def test_first_keep_from_another_file_puts_everything_back(
        self, store, config, tmp_path, monkeypatch, fault
    ):
        other = tmp_path / "word.wav"
        rec.write_wav(other, make_cut(9))
        write_lead_word_key(config, str(other))
        candidate(store, 1)
        before = (folder_bytes(store.folder), config.read_bytes(), other.read_bytes())
        engine = FakeEngine()
        setter = self.arm(fault, monkeypatch, engine)
        with pytest.raises(LeadWordStoreError):
            store.keep(setter)
        assert (folder_bytes(store.folder), config.read_bytes(), other.read_bytes()) == before
        assert key(config) == str(other)
        assert [c for c in engine.calls if not isinstance(c, str)] == []

    def test_a_failed_copy_of_the_other_file_puts_everything_back(
        self, store, config, tmp_path, monkeypatch
    ):
        other = tmp_path / "word.wav"
        rec.write_wav(other, make_cut(9))
        write_lead_word_key(config, str(other))
        candidate(store, 1)
        before = (folder_bytes(store.folder), config.read_bytes())

        targets = []

        def boom(src, dst):
            targets.append(dst)
            Path(dst).write_bytes(b"half a copy")  # a copy that stops part way
            raise OSError("copy refused")

        monkeypatch.setattr(store_mod, "_copy_file", boom)
        with pytest.raises(LeadWordStoreError, match="copy refused"):
            store.keep(FakeEngine())
        assert (folder_bytes(store.folder), config.read_bytes()) == before
        # The copy goes to a temp name, never straight to hello.previous.wav.
        assert [Path(t).name for t in targets] == [FILE_PREVIOUS + ".tmp"]

    def test_a_key_edit_that_wrote_before_failing_is_undone(self, store, config, monkeypatch):
        """The engine step fails after the key was written: the key goes back."""
        candidate(store, 1)
        before = config.read_bytes()

        def boom(audio):
            raise RuntimeError("engine said no")

        with pytest.raises(LeadWordStoreError):
            store.keep(boom)
        assert config.read_bytes() == before
        assert not store.wav_path.exists()
        assert store.candidate_path.exists()  # the candidate is back for a retry

    def test_a_step_that_cannot_be_undone_is_named_in_the_error(
        self, store, config, monkeypatch, caplog
    ):
        keep_one(store, 1)
        write_lead_word_key(config, "elsewhere.wav")  # so the key step runs
        candidate(store, 2)
        real = store_mod._atomic_write
        armed = {"on": False}

        def atomic(path, data):
            if armed["on"]:
                raise OSError("disk gone")
            real(path, data)

        def engine(audio):
            armed["on"] = True  # from here on, no file can be put back
            raise RuntimeError("engine said no")

        monkeypatch.setattr(store_mod, "_atomic_write", atomic)
        with caplog.at_level(logging.WARNING, logger="lead_word_store"):
            with pytest.raises(LeadWordStoreError) as caught:
                store.keep(engine)
        text = str(caught.value)
        assert text.startswith("Keep failed: engine said no")
        assert "could not be fully put back" in text
        assert "hello.wav: disk gone" in text
        assert "hello.json: disk gone" in text
        assert "config.toml: disk gone" in text
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    def test_a_failed_keep_can_be_tried_again(self, store, config, monkeypatch):
        candidate(store, 1)

        def boom(audio):
            raise RuntimeError("engine said no")

        with pytest.raises(LeadWordStoreError):
            store.keep(boom)
        engine = FakeEngine()
        store.keep(engine)
        assert len(engine.calls) == 1
        assert store.wav_path.exists()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_an_empty_store(self, store):
        status = store.status()
        assert (status.key_path, status.key_names_other_file) == ("", False)
        assert (status.current_exists, status.previous_exists, status.candidate_exists) == (
            False, False, False)
        assert (status.recorded, status.microphone) == ("", "")

    def test_the_candidate_shows(self, store):
        candidate(store, 1)
        assert store.status().candidate_exists is True

    def test_a_damaged_facts_file_gives_empty_facts(self, store):
        keep_one(store, 1)
        store.meta_path.write_text("{not json")
        status = store.status()
        assert status.current_exists is True
        assert (status.recorded, status.microphone) == ("", "")

    def test_a_facts_file_that_is_not_an_object_gives_empty_facts(self, store):
        keep_one(store, 1)
        store.meta_path.write_text('["recorded", "microphone"]')
        status = store.status()
        assert status.current_exists is True
        assert (status.recorded, status.microphone) == ("", "")

    def test_the_facts_of_a_missing_recording_are_not_shown(self, store):
        keep_one(store, 1)
        store.wav_path.unlink()
        status = store.status()
        assert status.current_exists is False
        assert (status.recorded, status.microphone) == ("", "")

    def test_the_status_reads_the_key_fresh(self, store, config):
        keep_one(store, 1)
        assert store.status().key_path == str(store.wav_path)
        write_lead_word_key(config, "")
        assert store.status().key_path == ""
        assert store.status().key_names_other_file is False


# ---------------------------------------------------------------------------
# Go back to the previous recording
# ---------------------------------------------------------------------------

class TestRestore:
    def two(self, store):
        keep_one(store, 1, microphone="Mic A")
        keep_one(store, 2, microphone="Mic B")
        return store.wav_path.read_bytes(), store.previous_path.read_bytes()

    def test_swap_and_swap_back(self, store, config):
        current, previous = self.two(store)
        engine = FakeEngine()
        status = store.restore(engine)
        assert store.wav_path.read_bytes() == previous
        assert store.previous_path.read_bytes() == current
        assert json.loads(store.meta_path.read_text())["microphone"] == "Mic A"
        assert status.microphone == "Mic A"
        assert len(engine.calls) == 1
        assert np.array_equal(
            engine.calls[0], load_lead_word_audio(str(store.wav_path), 16000))
        facts = json.loads(store.meta_path.read_text())
        # The replaced recording's facts sit one level down, without a level
        # of their own below them.
        assert facts["previous"]["microphone"] == "Mic B"
        assert "previous" not in facts["previous"]
        store.restore(FakeEngine())
        assert store.wav_path.read_bytes() == current
        assert store.previous_path.read_bytes() == previous
        assert json.loads(store.meta_path.read_text())["microphone"] == "Mic B"
        assert key(config) == str(store.wav_path)

    def test_restore_does_not_rewrite_a_key_that_names_the_wav(self, store, monkeypatch):
        self.two(store)

        def boom(*a):
            raise AssertionError("the key was written")

        monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        store.restore(FakeEngine())

    def test_no_swap_file_is_left(self, store):
        self.two(store)
        store.restore(FakeEngine())
        assert sorted(p.name for p in store.folder.iterdir()) == [
            FILE_META, FILE_PREVIOUS, FILE_WAV]

    def test_no_previous_is_refused(self, store):
        keep_one(store, 1)
        before = folder_bytes(store.folder)
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError, match="no previous"):
            store.restore(engine)
        assert folder_bytes(store.folder) == before and engine.calls == []

    def test_restore_sets_the_key_when_it_was_off(self, store, config):
        self.two(store)
        store.turn_off(FakeEngine())
        store.restore(FakeEngine())
        assert key(config) == str(store.wav_path)

    def test_restore_when_only_a_previous_exists(self, store):
        self.two(store)
        previous = store.previous_path.read_bytes()
        store.wav_path.unlink()
        store.restore(FakeEngine())
        assert store.wav_path.read_bytes() == previous
        assert not store.previous_path.exists()

    @pytest.mark.parametrize("fault", ["replace-1", "replace-2", "replace-3", "json", "key", "engine"])
    def test_a_failing_step_puts_everything_back(self, store, config, monkeypatch, fault):
        self.two(store)
        write_lead_word_key(config, "elsewhere.wav")
        before = (folder_bytes(store.folder), config.read_bytes())
        engine = FakeEngine()
        setter = TestKeepFailures().arm(fault, monkeypatch, engine)
        with pytest.raises(LeadWordStoreError, match="Going back failed"):
            store.restore(setter)
        assert (folder_bytes(store.folder), config.read_bytes()) == before
        assert [c for c in engine.calls if not isinstance(c, str)] == []

    def test_an_unusable_previous_changes_nothing(self, store, config):
        self.two(store)
        store.previous_path.write_bytes(b"junk")
        before = (folder_bytes(store.folder), config.read_bytes())
        with pytest.raises(LeadWordStoreError, match="cannot be used"):
            store.restore(FakeEngine())
        assert (folder_bytes(store.folder), config.read_bytes()) == before


# ---------------------------------------------------------------------------
# Turn off and turn on
# ---------------------------------------------------------------------------

class TestTurnOffOn:
    def test_turn_off_empties_the_key_keeps_the_files_and_clears_the_engine(self, store, config):
        keep_one(store, 1)
        keep_one(store, 2)
        before = folder_bytes(store.folder)
        engine = FakeEngine()
        status = store.turn_off(engine)
        assert key(config) == ""
        assert folder_bytes(store.folder) == before
        assert engine.calls == [None]
        assert status.key_path == "" and status.current_exists

    def test_turn_off_changes_only_the_key_line(self, store, config):
        keep_one(store, 1)
        store.turn_off(FakeEngine())
        assert config.read_text(encoding="utf-8") == CONFIG

    def test_turn_off_with_an_empty_key_does_not_rewrite_the_config(self, store, config, monkeypatch):
        def boom(*a):
            raise AssertionError("the key was written")

        monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        engine = FakeEngine()
        store.turn_off(engine)
        assert engine.calls == [None]

    def test_turn_on_sets_the_key_to_hello_wav_and_loads_it(self, store, config):
        keep_one(store, 1)
        store.turn_off(FakeEngine())
        engine = FakeEngine()
        status = store.turn_on(engine)
        assert key(config) == str(store.wav_path)
        assert len(engine.calls) == 1
        assert np.array_equal(
            engine.calls[0], load_lead_word_audio(str(store.wav_path), 16000))
        assert status.key_path == str(store.wav_path)
        assert status.current_exists and not status.key_names_other_file

    def test_turn_on_loads_the_current_recording_not_the_previous_one(self, store):
        keep_one(store, 1)
        cut, _ = keep_one(store, 2)
        assert store.previous_path.exists()
        store.turn_off(FakeEngine())
        engine = FakeEngine()
        store.turn_on(engine)
        assert np.array_equal(
            (engine.calls[0] * 32768).round().astype(np.int16), cut)

    def test_turn_on_when_the_key_already_names_it_does_not_rewrite(self, store, monkeypatch):
        keep_one(store, 1)

        def boom(*a):
            raise AssertionError("the key was written")

        monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        engine = FakeEngine()
        store.turn_on(engine)
        assert len(engine.calls) == 1

    def test_turn_on_without_a_recording_is_refused(self, store, config):
        before = config.read_bytes()
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError, match="no recording"):
            store.turn_on(engine)
        assert config.read_bytes() == before and engine.calls == []

    def test_turn_on_with_an_unusable_file_changes_nothing(self, store, config):
        keep_one(store, 1)
        store.turn_off(FakeEngine())
        store.wav_path.write_bytes(b"junk")
        before = (folder_bytes(store.folder), config.read_bytes())
        with pytest.raises(LeadWordStoreError, match="cannot be used"):
            store.turn_on(FakeEngine())
        assert (folder_bytes(store.folder), config.read_bytes()) == before

    def test_turn_off_failure_puts_the_key_back(self, store, config):
        keep_one(store, 1)
        before = config.read_bytes()

        def boom(audio):
            raise RuntimeError("engine said no")

        with pytest.raises(LeadWordStoreError, match="Turning off failed"):
            store.turn_off(boom)
        assert config.read_bytes() == before

    def test_a_failure_does_not_write_a_key_that_it_did_not_change(self, store, config):
        """The key was empty and Turn off did not write it. Another program
        changes config.toml while the engine step runs; the undo must not
        overwrite that change with the old copy."""
        engine_calls = []

        def boom(audio):
            engine_calls.append(audio)
            config.write_bytes(CONFIG.replace("INFO", "DEBUG").encode("utf-8"))
            raise RuntimeError("engine said no")

        with pytest.raises(LeadWordStoreError, match="Turning off failed"):
            store.turn_off(boom)
        assert engine_calls == [None]
        assert "level = 'DEBUG'" in config.read_text(encoding="utf-8")

    def test_turn_on_failure_puts_the_key_back(self, store, config):
        keep_one(store, 1)
        store.turn_off(FakeEngine())
        before = config.read_bytes()

        def boom(audio):
            raise RuntimeError("engine said no")

        with pytest.raises(LeadWordStoreError, match="Turning on failed"):
            store.turn_on(boom)
        assert config.read_bytes() == before

    def test_turn_off_key_write_failure_changes_nothing_and_leaves_the_engine(
        self, store, config, monkeypatch
    ):
        keep_one(store, 1)
        before = (folder_bytes(store.folder), config.read_bytes())

        def boom(path, value):
            raise OSError("cannot write the key")

        monkeypatch.setattr(store_mod, "write_lead_word_key", boom)
        engine = FakeEngine()
        with pytest.raises(LeadWordStoreError):
            store.turn_off(engine)
        assert (folder_bytes(store.folder), config.read_bytes()) == before
        assert engine.calls == []
