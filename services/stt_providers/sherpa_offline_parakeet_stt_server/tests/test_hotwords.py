"""Tests for Parakeet hotwords: startup file generation, engine wiring, and
the live add_hint handler (wh-q33mj Phase 2: wh-5w04r, wh-afhfj, wh-kcu8f,
wh-pirep).

The sherpa recognizer is never really constructed here; from_transducer is
mocked and its kwargs inspected. The contract is plain-text hotwords +
modeling_unit='bpe' + a sentencepiece bpe_vocab, and omitting bpe_vocab
segfaults sherpa natively, so the wiring asserts it is always present
whenever hotwords are passed. That vocabulary is bpe.vocab, never
tokens.txt: the spike (wh-q3nrw) established the shape of the contract but
named the wrong file for it, and tokens.txt's second column is an integer
id rather than a log probability (wh-parakeet-hotword-vocab).
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main as parakeet_main
from main import ParakeetServer, prepare_hotwords_file
from sherpa_engine import SherpaOfflineEngine, read_token_pieces


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A miniature stand-in for the model's own token inventory. tokens.txt
# lists '<piece> <id>' with the id equal to the line number; a
# sentencepiece vocabulary lists '<piece>\t<score>' with a score that is
# never an id. The pair below is what a correctly generated bpe.vocab
# looks like beside its tokens.txt.
_PIECES = ["<blk>", "<unk>", "▁the", "▁zwick", "y", "▁wheel", "house"]


def _tokens_txt(pieces: list[str] | None = None) -> str:
    pieces = _PIECES if pieces is None else pieces
    return "".join(f"{p} {i}\n" for i, p in enumerate(pieces))


def _bpe_vocab(pieces: list[str] | None = None) -> str:
    pieces = _PIECES if pieces is None else pieces
    return "".join(f"{p}\t{-i}\n" for i, p in enumerate(pieces))


def _fake_model_dir(tmp_path: Path, with_vocab: bool = True) -> Path:
    """A model directory that passes _load_model's existence checks.

    tokens.txt carries real-looking lines because the vocabulary check
    reads it to decide whether a bpe.vocab belongs to this model."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx"):
        (model_dir / name).write_text("", encoding="utf-8")
    (model_dir / "tokens.txt").write_text(_tokens_txt(), encoding="utf-8")
    if with_vocab:
        (model_dir / "bpe.vocab").write_text(_bpe_vocab(), encoding="utf-8")
    return model_dir


def _build_engine_full(model_dir: Path, **kwargs):
    """Construct SherpaOfflineEngine with sherpa_onnx mocked; return the
    engine and the kwargs from_transducer received."""
    with patch("sherpa_engine.sherpa_onnx") as sherpa_mock:
        engine = SherpaOfflineEngine(model_path=str(model_dir), **kwargs)
        call = sherpa_mock.OfflineRecognizer.from_transducer.call_args
    assert call is not None, "from_transducer was not called"
    return engine, call.kwargs


def _build_engine(model_dir: Path, **kwargs):
    """The kwargs from_transducer received, for tests that need only those."""
    return _build_engine_full(model_dir, **kwargs)[1]


def _deny_access(monkeypatch, target: Path) -> None:
    """Make one path answer every stat and every read with a denial.

    Path.exists() does not swallow a PermissionError. It calls Path.stat,
    and pathlib re-raises anything that is not a missing-path error, which
    was measured on this provider's Python 3.12.10. A file the account
    cannot stat therefore reaches the caller as an exception rather than
    as a False, and that is the case wh-parakeet-hotword-vocab.2.5 is
    about. Both seams are denied because a caller may reach the file by
    either one."""
    real_stat = Path.stat
    real_read_bytes = Path.read_bytes

    def stat(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Access is denied", str(self), 5)
        return real_stat(self, *args, **kwargs)

    def read_bytes(self):
        if self == target:
            raise PermissionError(13, "Access is denied", str(self), 5)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)


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

    def test_a_character_the_model_keeps_inside_a_token_is_covered(
        self, fake_hints, tmp_path
    ):
        """wh-parakeet-hotword-vocab.2.6. The model character set is read
        from tokens.txt the way sherpa reads it, so a character the
        loader keeps inside a token -- U+2028 here -- is in the set, and
        a hint holding it is written rather than dropped. str.splitlines
        ended the token at that character and left it out of the set."""
        tokens = tmp_path / "tokens.txt"
        pieces = ["▁Zwi", "cky", "p\u2028q"]
        tokens.write_bytes(
            ("\n".join(f"{p} {i}" for i, p in enumerate(pieces)) + "\n")
            .encode("utf-8")
        )
        fake_hints.hints = ["Zwicky", "p\u2028q"]

        result = prepare_hotwords_file(tokens_path=tokens)

        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert result == str(dest)
        assert dest.read_text(encoding="utf-8") == "Zwicky\np\u2028q\n"

    @pytest.mark.skipif(
        os.name != "nt", reason="the C runtime's text mode is Windows-only"
    )
    def test_a_character_only_after_a_text_mode_eof_marker_is_not_covered(
        self, fake_hints, tmp_path
    ):
        """wh-parakeet-hotword-vocab.2.8. A token after the marker is not
        in the model, so a hint made of its characters is dropped as
        uncovered rather than handed to a loader that never read it."""
        tokens = tmp_path / "tokens.txt"
        tokens.write_bytes(b"\xe2\x96\x81Zwi 0\ncky 1\n\x1aphantom 2\n")
        fake_hints.hints = ["Zwicky", "phantom"]

        result = prepare_hotwords_file(tokens_path=tokens)

        dest = tmp_path / "runtime" / "parakeet-hotwords.txt"
        assert result == str(dest)
        assert dest.read_text(encoding="utf-8") == "Zwicky\n"


# ---------------------------------------------------------------------------
# wh-afhfj: engine wiring of hotwords_file + hotwords_score
# ---------------------------------------------------------------------------

_HOTWORD_KWARGS = (
    "hotwords_file",
    "hotwords_score",
    "modeling_unit",
    "bpe_vocab",
    "decoding_method",
)


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
        # wh-parakeet-hotword-vocab: sherpa's own contract, quoted from
        # the installed package (sherpa_onnx/offline_recognizer.py, the
        # bpe_vocab argument): "The vocabulary generated by google's
        # sentencepiece program. It is a file has two columns, one is the
        # token, the other is the log probability". tokens.txt's second
        # column is an integer id, so it is the wrong file and the
        # hotword tokenization it produces is wrong.
        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        # greedy_search silently ignores hotwords_file.
        assert kwargs["decoding_method"] == "modified_beam_search"

    def test_no_hotwords_file_keeps_greedy_default(self, tmp_path):
        kwargs = _build_engine(_fake_model_dir(tmp_path))
        for key in _HOTWORD_KWARGS:
            assert key not in kwargs

    def test_missing_hotwords_file_degrades_to_no_hotwords(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            kwargs = _build_engine(
                _fake_model_dir(tmp_path),
                hotwords_file=str(tmp_path / "does-not-exist.txt"),
            )
        assert "hotwords" in caplog.text.lower()
        for key in _HOTWORD_KWARGS:
            assert key not in kwargs


# ---------------------------------------------------------------------------
# wh-parakeet-hotword-vocab: the bpe.vocab check and the honest status
# ---------------------------------------------------------------------------

class TestBpeVocabValidation:
    """The engine must refuse a vocabulary that is missing, malformed, or
    from another model, and must say which of the two things happened:
    hotwords initialized, or hotwords merely requested."""

    @pytest.fixture
    def hotwords(self, tmp_path) -> str:
        path = tmp_path / "hotwords.txt"
        path.write_text("Zwicky\n", encoding="utf-8")
        return str(path)

    def test_good_vocab_reports_active(self, tmp_path, hotwords, caplog):
        import logging

        model_dir = _fake_model_dir(tmp_path)
        with caplog.at_level(logging.INFO):
            engine, kwargs = _build_engine_full(
                model_dir, hotwords_file=hotwords
            )

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is True
        assert "hotwords initialized" in caplog.text.lower()

    def test_missing_vocab_degrades_and_says_so(self, tmp_path, hotwords, caplog):
        import logging

        model_dir = _fake_model_dir(tmp_path, with_vocab=False)
        with caplog.at_level(logging.WARNING):
            engine, kwargs = _build_engine_full(
                model_dir, hotwords_file=hotwords
            )

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        # The wording is asserted from the start of the message, not as a
        # substring. Every rejection message ends with the path, and this
        # test's own pytest directory is named test_missing_vocab_..., so
        # a bare `"missing" in detail` was satisfied by the path itself:
        # the mutation that reports a missing file as unreadable survived
        # this test (wh-parakeet-hotword-vocab.2.5).
        assert engine.hotwords_status.detail.startswith(
            "the vocabulary file is missing"
        )
        # The startup log must not read as a success.
        assert "hotwords requested" in caplog.text.lower()
        assert "hotwords initialized" not in caplog.text.lower()

    def test_tokens_txt_as_the_vocabulary_is_rejected(self, tmp_path, hotwords):
        """The defect this bead exists for: tokens.txt carries ids, not
        scores, so sherpa tokenizes every hotword wrongly."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(_tokens_txt(), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        assert "score" in engine.hotwords_status.detail.lower()

    def test_tokens_txt_with_ids_that_start_at_one_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.1.1. The id column does not have to
        equal the line number. A release that numbers from one is still
        tokens.txt, and the pieces are the model's own, so the overlap
        floor cannot catch it either."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(
            "".join(f"{p} {i + 1}\n" for i, p in enumerate(_PIECES)),
            encoding="utf-8",
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        assert "score" in engine.hotwords_status.detail.lower()

    def test_tokens_txt_in_another_line_order_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.1.1. Sorting tokens.txt by token
        keeps every id but breaks the line-number relation, and the file
        is no less wrong for it."""
        model_dir = _fake_model_dir(tmp_path)
        numbered = list(enumerate(_PIECES))
        numbered.sort(key=lambda pair: pair[1])
        (model_dir / "bpe.vocab").write_text(
            "".join(f"{p} {i}\n" for i, p in numbered), encoding="utf-8"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        assert "score" in engine.hotwords_status.detail.lower()

    def test_a_score_of_zero_alone_does_not_reject_a_real_vocabulary(
        self, tmp_path, hotwords
    ):
        """The new check reads every line, not one. sentencepiece gives
        its control symbols a score of exactly 0, so a genuine file opens
        with lines that look like ids; the negative scores below them are
        what make it a vocabulary."""
        model_dir = _fake_model_dir(tmp_path)
        lines = [f"{_PIECES[0]}\t0\n", f"{_PIECES[1]}\t0\n"]
        lines += [f"{p}\t-{i}.5\n" for i, p in enumerate(_PIECES[2:], start=1)]
        (model_dir / "bpe.vocab").write_text("".join(lines), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_vocabulary_scored_only_zero_and_negative_zero_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.1. A score written '-0' parses to
        -0.0, which is both whole and not less than zero, so reading the
        column as numbers alone calls a signed score an id. The minus
        sign is in the text, and no token id column carries one."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(
            f"{_PIECES[0]}\t0\n{_PIECES[1]}\t0\n{_PIECES[2]}\t-0\n",
            encoding="utf-8",
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_vocabulary_of_unsigned_fractional_scores_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.1, the other half of the same
        rule. An id is a whole number written without a sign. A value
        that is not whole is a score whichever way it is signed, so a
        column of unsigned fractions is not a token list."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(
            "".join(f"{p}\t{i}.5\n" for i, p in enumerate(_PIECES)),
            encoding="utf-8",
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_blank_row_in_the_vocabulary_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.2. sherpa hands this file to
        simple-sentencepiece, which reads every physical line and then
        requires a token and a score from it. A blank line gives it
        neither, and the native loader ends the whole process rather
        than raising, so no except here can catch it. Refusing the file
        is what keeps the provider running."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        # Not a bare `"blank" in detail`: this test's pytest directory is
        # named test_a_blank_row_..., and a message carrying the path
        # would satisfy that (wh-parakeet-hotword-vocab.2.5).
        assert "is blank" in engine.hotwords_status.detail

    def test_a_piece_holding_a_space_is_rejected(self, tmp_path, hotwords):
        """wh-parakeet-hotword-vocab.2.2. sentencepiece warns about a
        piece that holds a space and writes it anyway. The native loader
        reads the first word as the token, fails to read the second word
        as a score, and ends the process."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "two words\t-2.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        # 'words' sits where the score belongs, which is the item the
        # loader fails to read (wh-parakeet-hotword-vocab.2.3).
        assert "line 4" in engine.hotwords_status.detail
        assert "score" in engine.hotwords_status.detail.lower()

    def test_a_piece_holding_a_tab_is_rejected(self, tmp_path, hotwords):
        """wh-parakeet-hotword-vocab.2.2. Splitting on the last tab hid
        this one: it left the tab inside the token and read the score
        after it, so the line passed every check here while the native
        loader still ended the process on it."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "two\twords\t-2.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        # 'words' sits where the score belongs, which is the item the
        # loader fails to read (wh-parakeet-hotword-vocab.2.3).
        assert "line 4" in engine.hotwords_status.detail
        assert "score" in engine.hotwords_status.detail.lower()

    def test_a_non_breaking_space_as_the_score_delimiter_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.3. str.split() treats U+00A0 as a
        separator; the loader reads bytes and does not, so it sees one
        unreadable item and ends the process. Measured against the real
        model: this file was accepted here and killed the provider."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "piece\u00a0-2.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert "line 4" in engine.hotwords_status.detail


    @pytest.mark.parametrize(
        "score",
        [
            pytest.param("nan", id="nan"),
            pytest.param("NaN", id="NaN"),
            pytest.param("inf", id="inf"),
            pytest.param("-inf", id="neg-inf"),
            pytest.param("-Infinity", id="neg-infinity"),
            pytest.param("-\u0662.5", id="arabic-indic-digit"),
            pytest.param("\u00a0-2.5", id="leading-nbsp"),
            pytest.param("-1e\u0662", id="exponent-with-unicode-digit"),
        ],
    )
    def test_a_score_the_loader_cannot_read_as_a_number_is_rejected(
        self, tmp_path, hotwords, score
    ):
        """wh-parakeet-hotword-vocab.2.7. The loader reads a score with
        `stream >> float`, which collects an ASCII number and nothing
        else; float() also reads nan, inf, Unicode digits and Unicode
        whitespace. Measured against the real model: each of these was
        accepted here and ended the provider."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, f"piece\t{score}\n")
        (model_dir / "bpe.vocab").write_bytes("".join(rows).encode("utf-8"))

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert "line 4" in engine.hotwords_status.detail
        assert "score" in engine.hotwords_status.detail.lower()

    @pytest.mark.parametrize(
        "score",
        [
            pytest.param("-1e40", id="finite-double-above-float32"),
            pytest.param("-3.4028236e38", id="rounds-above-float32-max"),
            pytest.param("-1e999", id="double-overflow"),
            pytest.param("-1e-50", id="float32-underflow-to-zero"),
            pytest.param("-1e-999", id="double-underflow-to-zero"),
        ],
    )
    def test_a_score_outside_float32_range_is_rejected(
        self, tmp_path, hotwords, score
    ):
        """wh-parakeet-hotword-vocab.2.7. The loader converts the score
        with strtof and refuses the item when the conversion overflows
        or underflows to zero; float() holds a double and never refuses
        a magnitude. Measured against the real model: each of these was
        accepted here and ended the provider."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, f"piece\t{score}\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert "line 4" in engine.hotwords_status.detail
        assert "score" in engine.hotwords_status.detail.lower()

    @pytest.mark.parametrize(
        "score",
        [
            pytest.param("-3.4028235e38", id="rounds-to-float32-max"),
            pytest.param("-1e-40", id="float32-denormal"),
            pytest.param("-1.1754944e-38", id="float32-smallest-normal"),
            pytest.param("+1.5", id="leading-plus"),
            pytest.param("-.5", id="leading-point"),
            pytest.param("-5.", id="trailing-point"),
            pytest.param("-2.5E-3", id="upper-case-exponent"),
        ],
    )
    def test_a_score_the_loader_reads_is_accepted(
        self, tmp_path, hotwords, score
    ):
        """wh-parakeet-hotword-vocab.2.7, the other direction: the range
        and grammar checks must not refuse a score the loader reads.
        Measured against the real model: the loader builds the
        recognizer from each of these."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows[3] = f"{_PIECES[3]}\t{score}\n"
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_score_with_a_digit_group_underscore_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.7. float() reads -1_000 as -1000;
        the loader reads -1 and runs on with it. Refusing the file is the
        documented safe direction, the same one '-2.5x' takes: boosting
        is off with a reason instead of on with a score nobody wrote."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "piece\t-1_000\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        assert "line 4" in engine.hotwords_status.detail

    def test_a_piece_holding_a_non_breaking_space_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.3, the other direction. The loader
        reads this row as one token and one score and builds the
        recognizer, so refusing it turns boosting off for a file that
        works. Measured against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "two\u00a0words\t-2.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_piece_holding_a_line_separator_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.3. str.splitlines() ends a line at
        U+2028; std::getline ends one at a newline and at nothing else,
        so this is one good row to the loader and was two broken ones
        here. Measured against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "two\u2028words\t-2.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_row_carrying_a_third_column_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.3. The loader reads a token and a
        score and ignores whatever follows, so a third column does not
        end the provider and must not turn boosting off. Measured
        against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows.insert(3, "word\t-2.5\t-3.5\n")
        (model_dir / "bpe.vocab").write_text("".join(rows), encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_row_delimited_by_a_carriage_return_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.4. Path.read_text turns a lone
        carriage return into a newline before the check sees the file,
        so a row the loader reads as one token and one score arrived
        here as two broken lines. The loader treats the carriage return
        as whitespace inside the line and builds the recognizer from
        this file; measured against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5".encode("utf-8") for i, p in enumerate(_PIECES)]
        rows.insert(3, b"piece\r\t-2.5")
        (model_dir / "bpe.vocab").write_bytes(b"\n".join(rows) + b"\n")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_vocabulary_from_another_model_is_rejected(self, tmp_path, hotwords):
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(
            _bpe_vocab(["▁katze", "▁hund", "▁maus", "▁vogel"]),
            encoding="utf-8",
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        # Not a bare `"model" in detail`: the fake model directory is
        # named model, so a message carrying the path would satisfy that
        # (wh-parakeet-hotword-vocab.2.5).
        assert "built for another model" in engine.hotwords_status.detail

    def test_one_column_vocabulary_is_rejected(self, tmp_path, hotwords):
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text(
            "▁the\n▁zwick\ny\n", encoding="utf-8"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False

    def test_empty_vocabulary_is_rejected(self, tmp_path, hotwords):
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_text("", encoding="utf-8")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False

    def test_no_hotwords_requested_reports_neither(self, tmp_path):
        engine, kwargs = _build_engine_full(_fake_model_dir(tmp_path))

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is False
        assert engine.hotwords_status.active is False

    def test_missing_hotwords_file_reports_requested_not_active(
        self, tmp_path
    ):
        engine, _ = _build_engine_full(
            _fake_model_dir(tmp_path),
            hotwords_file=str(tmp_path / "absent.txt"),
        )

        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert engine.hotwords_status.detail

    def test_a_vocabulary_the_account_cannot_read_degrades_and_says_so(
        self, tmp_path, hotwords, monkeypatch
    ):
        """wh-parakeet-hotword-vocab.2.5. A denied vocabulary must turn
        boosting off with a reason and still build the recognizer. The
        existence check could not do that, because Path.exists raises a
        PermissionError instead of answering False."""
        model_dir = _fake_model_dir(tmp_path)
        _deny_access(monkeypatch, model_dir / "bpe.vocab")

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert "could not be read" in engine.hotwords_status.detail

    def test_a_hotwords_file_the_account_cannot_read_degrades_and_says_so(
        self, tmp_path, hotwords, monkeypatch
    ):
        """wh-parakeet-hotword-vocab.2.5, the same class one file over.
        The engine never reads the hotwords file itself -- sherpa reads
        it -- so its existence check is the only place a denial reaches
        this code."""
        model_dir = _fake_model_dir(tmp_path)
        _deny_access(monkeypatch, Path(hotwords))

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.requested is True
        assert engine.hotwords_status.active is False
        assert "could not be read" in engine.hotwords_status.detail

    def test_a_model_token_holding_a_line_separator_is_read_whole(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.6. sherpa reads tokens.txt with
        std::getline, which ends a line at a newline and at nothing else,
        so a token holding U+2028 is one token to the loader, and the
        same token in bpe.vocab belongs to this model. str.splitlines
        read it as two fragments here, and a vocabulary made of such
        tokens was refused as built for another model. Measured against
        the real model: the loader builds the recognizer from a
        tokens.txt whose token holds U+2028."""
        model_dir = _fake_model_dir(tmp_path)
        pieces = [f"p{i}\u2028q" for i in range(5)]
        (model_dir / "tokens.txt").write_text(
            _tokens_txt(pieces), encoding="utf-8"
        )
        (model_dir / "bpe.vocab").write_text(
            _bpe_vocab(pieces), encoding="utf-8"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    def test_a_model_token_ending_in_a_non_breaking_space_is_read_whole(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.6, the whitespace half. The loader
        separates the token from its id on C whitespace, so a token
        ending in U+00A0 keeps it. str.rsplit(None, 1) separated on the
        U+00A0 as well and dropped it from the token. Measured against
        the real model: the loader builds the recognizer from such a
        tokens.txt."""
        model_dir = _fake_model_dir(tmp_path)
        pieces = [f"p{i}\u00a0" for i in range(5)]
        (model_dir / "tokens.txt").write_text(
            _tokens_txt(pieces), encoding="utf-8"
        )
        (model_dir / "bpe.vocab").write_text(
            _bpe_vocab(pieces), encoding="utf-8"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    @pytest.mark.skipif(
        os.name != "nt", reason="the C runtime's text mode is Windows-only"
    )
    def test_a_vocabulary_ending_in_a_text_mode_eof_marker_is_accepted(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.8. The loader opens the file as a
        text stream, and on Windows the C runtime ends a text-mode file
        at the first 0x1A byte, so a marker after the last row is not a
        row. Read as a row, it was a token with no score, and a file the
        loader accepts lost boosting. Measured against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_bytes(
            _bpe_vocab().encode("utf-8") + b"\x1a"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    @pytest.mark.skipif(
        os.name != "nt", reason="the C runtime's text mode is Windows-only"
    )
    def test_bytes_after_a_text_mode_eof_marker_are_not_read(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.8. Nothing after the marker reaches
        the loader, whether it is a row with no score or bytes that are
        not UTF-8, so neither is a reason to refuse the file. Measured
        against the real model: the loader builds the recognizer from
        this file."""
        model_dir = _fake_model_dir(tmp_path)
        (model_dir / "bpe.vocab").write_bytes(
            _bpe_vocab().encode("utf-8") + b"\x1a\nphantom\n\xff\xfe"
        )

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        assert kwargs["bpe_vocab"] == str(model_dir / "bpe.vocab")
        assert engine.hotwords_status.active is True

    @pytest.mark.skipif(
        os.name != "nt", reason="the C runtime's text mode is Windows-only"
    )
    def test_a_text_mode_eof_marker_inside_a_row_cuts_it_and_is_rejected(
        self, tmp_path, hotwords
    ):
        """wh-parakeet-hotword-vocab.2.8, the unsafe direction. A marker
        inside a piece ends the file there, so the loader reads that row
        as a token with no score and ends the provider. Read whole, the
        row had a token and a score and passed every check here.
        Measured against the real model."""
        model_dir = _fake_model_dir(tmp_path)
        rows = [f"{p}\t{-i}.5\n" for i, p in enumerate(_PIECES)]
        rows[3] = "pie\x1ace\t-2.5\n"
        (model_dir / "bpe.vocab").write_bytes("".join(rows).encode("utf-8"))

        engine, kwargs = _build_engine_full(model_dir, hotwords_file=hotwords)

        for key in _HOTWORD_KWARGS:
            assert key not in kwargs
        assert engine.hotwords_status.active is False
        assert "line 4" in engine.hotwords_status.detail
        assert "score" in engine.hotwords_status.detail.lower()


class TestTokensTxtReader:
    """wh-parakeet-hotword-vocab.2.6: the token column of tokens.txt is
    read the way sherpa's SymbolTable::ReadTokens reads it."""

    def test_a_one_item_line_is_the_space_symbol(self, tmp_path):
        """ReadTokens reads a line holding one item as the space symbol
        whose id is that item, so the token on such a line is ' ' and
        not the item. Measured against the real model: the loader builds
        the recognizer from a tokens.txt whose line 501 is '500'."""
        tokens = tmp_path / "tokens.txt"
        tokens.write_bytes("a 0\n1\nb 2\n".encode("utf-8"))

        assert read_token_pieces(tokens) == ["a", " ", "b"]

    def test_a_trailing_newline_does_not_add_a_token(self, tmp_path):
        """std::getline yields no line after the final newline, so the
        empty remainder of a file that ends in one is not a line and
        must not read as the space symbol."""
        tokens = tmp_path / "tokens.txt"
        tokens.write_bytes("a 0\nb 1\n".encode("utf-8"))

        assert read_token_pieces(tokens) == ["a", "b"]

    @pytest.mark.skipif(
        os.name != "nt", reason="the C runtime's text mode is Windows-only"
    )
    def test_tokens_after_a_text_mode_eof_marker_are_not_read(self, tmp_path):
        """wh-parakeet-hotword-vocab.2.8. SymbolTable::ReadTokens opens
        tokens.txt as a text stream, and on Windows that stream ends at
        the first 0x1A byte, so tokens written after one are not in the
        model. Measured against the real model: the loader builds the
        recognizer from the real tokens.txt with a marker, a phantom
        token and a blank line appended, and reads none of them."""
        tokens = tmp_path / "tokens.txt"
        tokens.write_bytes("a 0\nb 1\n\x1aphantom 2\n\n".encode("utf-8"))

        assert read_token_pieces(tokens) == ["a", "b"]


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
