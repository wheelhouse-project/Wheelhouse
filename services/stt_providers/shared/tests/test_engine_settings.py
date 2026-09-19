"""Tests for shared_stt.engine_settings (wh-7ou.7.1.3, spec Section 5.3).

The apply_engine_settings command's provider-side core: validate the two
allowed single-word rescue keys, and write them into a provider config.toml
[engine] section while preserving every comment and every other key.
"""
import math
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.engine_settings import (
    ALLOWED_ENGINE_SETTINGS,
    validate_engine_settings,
    write_engine_settings,
)


FIXTURE = """\
# Distil-Whisper Medium.en STT (GPU)
# Top-of-file comment that must survive the write.

[model]
model_size_or_path = "Systran/faster-distil-whisper-medium.en"
device = "cuda"

[engine]
# wh-7ou.2 hallucination filter comment block
# that spans two lines and must survive.
hallucination_logprob_threshold = -0.55
# single-word rescue comments sit between keys
single_word_min_probability = 0.6
single_word_max_no_speech_prob = 0.03  # inline comment survives

[client]
rate = 16000
"""


def write_fixture(tmp_path: Path, text: str = FIXTURE) -> Path:
    path = tmp_path / "config.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def parse(path: Path) -> dict:
    import tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


class TestValidate:
    """Key and value validation (contract A.3: fixed two-key allowed list,
    real floats in [0, 1], booleans and non-finite values rejected)."""

    def test_allowed_list_is_exactly_the_two_rescue_keys(self):
        assert sorted(ALLOWED_ENGINE_SETTINGS) == [
            "single_word_max_no_speech_prob",
            "single_word_min_probability",
        ]

    def test_both_keys_accepted(self):
        clean, error = validate_engine_settings({
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        })
        assert error is None
        assert clean == {
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        }

    def test_single_key_accepted_write_or_keep(self):
        """Either key may be absent (per-setting write-or-keep)."""
        clean, error = validate_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        assert error is None
        assert clean == {"single_word_min_probability": 0.15}

    def test_unknown_key_rejected(self):
        clean, error = validate_engine_settings({
            "single_word_min_probability": 0.15,
            "hallucination_logprob_threshold": -0.9,
        })
        assert clean == {}
        assert error is not None
        assert "hallucination_logprob_threshold" in error

    def test_empty_settings_rejected(self):
        """Nothing to write must not trigger a pointless restart."""
        clean, error = validate_engine_settings({})
        assert clean == {}
        assert error is not None

    def test_non_dict_rejected(self):
        clean, error = validate_engine_settings([0.15])
        assert clean == {}
        assert error is not None

    @pytest.mark.parametrize("bad", [True, False])
    def test_boolean_rejected(self, bad):
        """bool is an int subclass; TOML true/false must not pass."""
        clean, error = validate_engine_settings(
            {"single_word_min_probability": bad}
        )
        assert clean == {}
        assert error is not None
        assert "single_word_min_probability" in error

    @pytest.mark.parametrize("bad", ["0.5", None, [0.5], {"v": 0.5}])
    def test_non_number_rejected(self, bad):
        clean, error = validate_engine_settings(
            {"single_word_max_no_speech_prob": bad}
        )
        assert clean == {}
        assert error is not None

    @pytest.mark.parametrize(
        "bad",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.1,
            1.5,
            2.0,
            10**400,  # float() raises OverflowError; must be an error, not a crash
        ],
    )
    def test_out_of_range_and_non_finite_rejected(self, bad):
        clean, error = validate_engine_settings(
            {"single_word_min_probability": bad}
        )
        assert clean == {}
        assert error is not None

    @pytest.mark.parametrize("edge", [0.0, 1.0])
    def test_range_is_inclusive(self, edge):
        """0.0 and 1.0 are documented off-switch values."""
        clean, error = validate_engine_settings(
            {"single_word_min_probability": edge}
        )
        assert error is None
        assert clean == {"single_word_min_probability": edge}

    def test_int_zero_and_one_accepted_as_floats(self):
        """JSON has no int/float distinction for whole numbers; a non-bool
        0 or 1 is a legal in-range value and normalizes to float."""
        clean, error = validate_engine_settings(
            {"single_word_min_probability": 1, "single_word_max_no_speech_prob": 0}
        )
        assert error is None
        assert clean["single_word_min_probability"] == 1.0
        assert isinstance(clean["single_word_min_probability"], float)
        assert clean["single_word_max_no_speech_prob"] == 0.0
        assert isinstance(clean["single_word_max_no_speech_prob"], float)


class TestWrite:
    """Comment-preserving write into the [engine] section."""

    def test_values_written_and_parse_back(self, tmp_path):
        path = write_fixture(tmp_path)
        write_engine_settings(path, {
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.034,
        })
        engine = parse(path)["engine"]
        assert engine["single_word_min_probability"] == 0.15
        assert engine["single_word_max_no_speech_prob"] == 0.034

    def test_comment_preservation_round_trip(self, tmp_path):
        """Only the two value lines may change; every other line -- comments,
        other keys, other sections -- must survive byte for byte."""
        path = write_fixture(tmp_path)
        original_lines = FIXTURE.splitlines()
        write_engine_settings(path, {
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.034,
        })
        new_lines = path.read_bytes().decode("utf-8").splitlines()
        assert len(new_lines) == len(original_lines)
        changed = [
            (old, new)
            for old, new in zip(original_lines, new_lines)
            if old != new
        ]
        assert changed == [
            (
                "single_word_min_probability = 0.6",
                "single_word_min_probability = 0.15",
            ),
            (
                "single_word_max_no_speech_prob = 0.03  # inline comment survives",
                "single_word_max_no_speech_prob = 0.034  # inline comment survives",
            ),
        ]

    def test_other_keys_and_sections_untouched(self, tmp_path):
        path = write_fixture(tmp_path)
        write_engine_settings(path, {"single_word_min_probability": 0.15})
        config = parse(path)
        assert config["engine"]["hallucination_logprob_threshold"] == -0.55
        assert config["engine"]["single_word_max_no_speech_prob"] == 0.03
        assert config["model"]["device"] == "cuda"
        assert config["client"]["rate"] == 16000

    def test_write_or_keep_single_key(self, tmp_path):
        path = write_fixture(tmp_path)
        write_engine_settings(path, {"single_word_max_no_speech_prob": 0.030})
        engine = parse(path)["engine"]
        assert engine["single_word_max_no_speech_prob"] == 0.030
        assert engine["single_word_min_probability"] == 0.6

    def test_crlf_line_endings_preserved(self, tmp_path):
        """A CRLF config must stay pure CRLF -- no mixed endings."""
        crlf_text = FIXTURE.replace("\n", "\r\n")
        path = write_fixture(tmp_path, crlf_text)
        write_engine_settings(path, {"single_word_min_probability": 0.15})
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        assert parse(path)["engine"]["single_word_min_probability"] == 0.15

    def test_missing_key_inserted_into_engine_section(self, tmp_path):
        """A hand-edited config may lack a rescue key; it is added inside
        [engine], not appended after later sections."""
        text = FIXTURE.replace("single_word_min_probability = 0.6\n", "")
        path = write_fixture(tmp_path, text)
        write_engine_settings(path, {"single_word_min_probability": 0.15})
        content = path.read_bytes().decode("utf-8")
        assert parse(path)["engine"]["single_word_min_probability"] == 0.15
        assert content.index("single_word_min_probability") < content.index("[client]")

    def test_missing_engine_section_appended(self, tmp_path):
        text = "[model]\ndevice = \"cuda\"\n"
        path = write_fixture(tmp_path, text)
        write_engine_settings(path, {"single_word_min_probability": 0.15})
        config = parse(path)
        assert config["engine"]["single_word_min_probability"] == 0.15
        assert config["model"]["device"] == "cuda"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            write_engine_settings(
                tmp_path / "does_not_exist" / "config.toml",
                {"single_word_min_probability": 0.15},
            )

    def test_failed_write_leaves_original_intact(self, tmp_path, monkeypatch):
        """The write is temp-file-plus-replace: a failure mid-write must
        never leave a truncated config behind."""
        import os
        path = write_fixture(tmp_path)
        original = path.read_bytes()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            write_engine_settings(path, {"single_word_min_probability": 0.15})
        assert path.read_bytes() == original
