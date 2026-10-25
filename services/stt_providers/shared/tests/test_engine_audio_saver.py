"""Tests for shared_stt.engine_audio_saver (wh-first-sound-lost.1).

The feature: with WHEELHOUSE_SAVE_ENGINE_AUDIO=1 a provider writes one WAV
file for every utterance, holding the samples its speech engine received for
the decode that produced the final transcript, and deletes each file it wrote
after 2 days. The evidence is for wh-first-sound-lost (the first sound of an
utterance is sometimes missing from the transcript).

These tests use tmp_path and monkeypatch only. They never touch the real
%LOCALAPPDATA%, and every wait is bounded.

Acceptance items covered here (bead wh-first-sound-lost.1):
  item 3  the WAV holds the samples the engine received
  item 4  file name and the wheelhouse.log line text
  item 6  deletion: only this feature's files, only older than 2 days
  item 7  off writes nothing; the write is on another thread; a write
          failure warns once per provider start and never raises
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from shared_stt import engine_audio_saver as eas
from shared_stt.engine_audio_saver import (
    CLEANUP_INTERVAL_SECONDS,
    ENV_VAR,
    LOGGER_NAME,
    MAX_AGE_SECONDS,
    EngineAudioSaver,
    build_file_name,
    create_from_environment,
    default_folder,
    delete_old_files,
    is_saver_file,
    samples_to_pcm16,
)

WHEN = datetime(2026, 10, 19, 20, 5, 7, 45000)
GOOD_NAME = "engine-audio_20261019-200507-045_parakeet_utt12.wav"
WAIT = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for(predicate, timeout: float = WAIT) -> bool:
    """Poll *predicate* until it is true or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def read_wav(path: Path):
    """Return (channels, sample_width, rate, int16 samples) of a WAV file."""
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        return (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            np.frombuffer(frames, dtype="<i2"),
        )


def expected_pcm16(audio: np.ndarray) -> np.ndarray:
    return np.clip(
        np.rint(audio.astype(np.float64) * 32768.0), -32768, 32767
    ).astype(np.int64)


def saver_thread_idents() -> set:
    """Idents of the live saver threads only, so a thread that another test
    or a library starts or ends cannot change the answer."""
    return {t.ident for t in threading.enumerate()
            if t.name.startswith("engine-audio-saver-")}


def make_old(path: Path, age_seconds: float, now: float) -> None:
    os.utime(path, (now - age_seconds, now - age_seconds))


@pytest.fixture
def records():
    """Every record on the saver's logger, whatever the parent loggers do.

    A handler on LOGGER_NAME itself sees each record even when an earlier
    test left propagate = False on shared_stt, which would blind caplog.
    """
    out: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            out.append(record)

    handler = _Collect(level=logging.DEBUG)
    logger = logging.getLogger(LOGGER_NAME)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield out
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture
def savers():
    """Collect savers so that every one is stopped, whatever the test did."""
    made: list[EngineAudioSaver] = []
    yield made
    for saver in made:
        try:
            saver.stop(timeout=2.0)
        except Exception:
            pass


@pytest.fixture
def make_saver(tmp_path, savers):
    def _make(folder=None, provider="parakeet_tdt", **kwargs):
        kwargs.setdefault("local_now", lambda: WHEN)
        kwargs.setdefault("cleanup_interval", 3600.0)
        saver = EngineAudioSaver(
            provider, folder if folder is not None else tmp_path / "audio",
            **kwargs,
        )
        savers.append(saver)
        return saver

    return _make


@pytest.fixture
def gated_wav_open(monkeypatch):
    """Make every write-mode wave.open wait until the test releases it.

    Read-mode opens pass straight through, so the test can still read files.
    Records the thread that opened the file for writing.
    """
    real_open = wave.open
    entered = threading.Event()
    release = threading.Event()
    writer_threads: list[threading.Thread] = []

    def gated(*args, **kwargs):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "rb")
        if "w" in str(mode):
            writer_threads.append(threading.current_thread())
            entered.set()
            release.wait(WAIT)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(wave, "open", gated)

    class _Gate:
        pass

    gate = _Gate()
    gate.entered = entered
    gate.release = release
    gate.writer_threads = writer_threads
    yield gate
    release.set()


def saved_files(folder: Path):
    return sorted(p for p in folder.glob("*.wav")) if folder.exists() else []


def warnings_in(records):
    return [r for r in records if r.levelno == logging.WARNING]


def info_in(records):
    return [r for r in records if r.levelno == logging.INFO]


def ramp(n: int = 600) -> np.ndarray:
    """Float32 samples that are all different and start/end near +-1."""
    base = np.linspace(-1.0, 1.0, n, dtype=np.float32)
    base[1] = np.float32(0.3333333)
    base[2] = np.float32(-0.7071067)
    base[3] = 0.0
    base[4] = np.float32(1.0 / 32768.0)
    return base.astype(np.float32)


# ---------------------------------------------------------------------------
# Constants and pure functions
# ---------------------------------------------------------------------------

class TestConstants:
    def test_env_var_name(self):
        assert ENV_VAR == "WHEELHOUSE_SAVE_ENGINE_AUDIO"

    def test_logger_is_a_child_of_the_audio_processor_logger(self):
        assert LOGGER_NAME == "shared_stt.audio_processor.engine_audio"

    def test_two_days_and_one_hour(self):
        assert MAX_AGE_SECONDS == 2 * 24 * 3600
        assert CLEANUP_INTERVAL_SECONDS == 3600


class TestDefaultFolder:
    def test_it_is_under_local_app_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_folder() == tmp_path / "WheelHouse" / "engine-audio"

    def test_it_creates_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        default_folder()
        assert list(tmp_path.iterdir()) == []


class TestBuildFileName:
    def test_the_documented_example(self):
        assert build_file_name(WHEN, "parakeet", 12) == GOOD_NAME

    def test_milliseconds_are_zero_padded_to_three_digits(self):
        name = build_file_name(
            datetime(2026, 1, 2, 3, 4, 5, 5000), "google_stt", 3)
        assert name == "engine-audio_20260102-030405-005_google_stt_utt3.wav"

    def test_milliseconds_truncate_and_never_reach_1000(self):
        name = build_file_name(
            datetime(2026, 1, 2, 3, 4, 5, 999999), "parakeet_tdt", 1)
        assert name == "engine-audio_20260102-030405-999_parakeet_tdt_utt1.wav"

    def test_zero_microseconds(self):
        name = build_file_name(
            datetime(2026, 12, 31, 23, 59, 59, 0), "p", 100)
        assert name == "engine-audio_20261231-235959-000_p_utt100.wav"

    def test_the_name_it_builds_passes_the_deletion_check(self):
        assert is_saver_file(build_file_name(WHEN, "parakeet_tdt", 7))


class TestIsSaverFile:
    @pytest.mark.parametrize("name", [
        GOOD_NAME,
        "engine-audio_20261019-200507-045_parakeet_tdt_utt0.wav",
        "engine-audio_20261019-200507-045_google_stt_utt123456.wav",
        "engine-audio_20261019-200507-045_p2_utt1.wav",
    ])
    def test_a_saver_name_matches(self, name):
        assert is_saver_file(name) is True

    @pytest.mark.parametrize("name", [
        "notes.wav",
        "",
        GOOD_NAME + ".bak",                       # suffix after the pattern
        "x" + GOOD_NAME,                          # prefix before the pattern
        GOOD_NAME.replace(".wav", ".WAV"),        # wrong extension case
        GOOD_NAME.replace(".wav", ".mp3"),
        GOOD_NAME[:-4],                           # no extension
        "engine-audio_20261019-200507-045_parakeet_utt.wav",    # no number
        "engine-audio_20261019-200507-045_parakeet_utt1x.wav",  # junk after N
        "engine-audio_20261019-200507-045__utt1.wav",           # empty provider
        "engine-audio_20261019-200507-045_Parakeet_utt1.wav",   # uppercase
        "engine-audio_20261019-200507-045_para-keet_utt1.wav",  # hyphen
        "engine-audio_20261019-200507_parakeet_utt1.wav",       # no ms
        "engine-audio_2026101-200507-045_parakeet_utt1.wav",    # short date
        "engine-audio_20261019-200507-045_parakeet_utt1.wav\n", # newline end
        "dir/" + GOOD_NAME,                       # a path, not a name
    ])
    def test_anything_else_does_not(self, name):
        assert is_saver_file(name) is False


class TestSamplesToPcm16:
    def test_rounds_to_nearest_and_clips(self):
        audio = np.array(
            [0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0,
             0.6 / 32768, -0.6 / 32768, 0.4 / 32768, 1.0 / 32768],
            dtype=np.float32)
        pcm = np.frombuffer(samples_to_pcm16(audio), dtype="<i2")
        assert pcm.tolist() == [
            0, 16384, -16384, 32767, -32768, 32767, -32768, 1, -1, 0, 1]

    def test_two_bytes_per_sample_little_endian(self):
        data = samples_to_pcm16(np.array([0.5], dtype=np.float32))
        assert isinstance(data, bytes)
        assert data == (16384).to_bytes(2, "little", signed=True)

    def test_length_is_preserved(self):
        audio = ramp(321)
        assert len(samples_to_pcm16(audio)) == 2 * 321

    def test_matches_the_reference_conversion(self):
        audio = ramp(1000)
        pcm = np.frombuffer(samples_to_pcm16(audio), dtype="<i2")
        assert np.array_equal(pcm.astype(np.int64), expected_pcm16(audio))


# ---------------------------------------------------------------------------
# delete_old_files (item 6)
# ---------------------------------------------------------------------------

NOW = 1_800_000_000.0


def _saver_name(n: int) -> str:
    return f"engine-audio_20261019-2005{n:02d}-045_parakeet_utt{n}.wav"


class TestDeleteOldFiles:
    def test_a_matching_file_older_than_two_days_is_deleted(self, tmp_path):
        f = tmp_path / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, MAX_AGE_SECONDS + 1, NOW)
        assert delete_old_files(tmp_path, NOW) == 1
        assert not f.exists()

    def test_a_matching_file_younger_than_two_days_stays(self, tmp_path):
        f = tmp_path / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, MAX_AGE_SECONDS - 1, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert f.exists()

    def test_a_file_exactly_two_days_old_stays(self, tmp_path):
        """The rule is 'more than' two days, so the boundary itself stays."""
        f = tmp_path / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, MAX_AGE_SECONDS, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert f.exists()

    def test_a_fresh_file_stays(self, tmp_path):
        f = tmp_path / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, 0, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert f.exists()

    @pytest.mark.parametrize("name", [
        "notes.wav",
        GOOD_NAME + ".bak",
        "x" + GOOD_NAME,
        "engine-audio_20261019-200507-045_Parakeet_utt12.wav",
        "readme.txt",
    ])
    def test_a_file_that_is_not_ours_is_never_deleted(self, tmp_path, name):
        f = tmp_path / name
        f.write_bytes(b"x")
        make_old(f, 3 * 24 * 3600, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert f.exists()

    def test_a_folder_with_a_matching_name_is_never_deleted(self, tmp_path):
        sub = tmp_path / GOOD_NAME
        sub.mkdir()
        inner = sub / _saver_name(5)
        inner.write_bytes(b"x")
        make_old(inner, 3 * 24 * 3600, NOW)
        make_old(sub, 3 * 24 * 3600, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert sub.is_dir()

    def test_it_never_recurses_into_a_subfolder(self, tmp_path):
        sub = tmp_path / "older"
        sub.mkdir()
        inner = sub / GOOD_NAME
        inner.write_bytes(b"x")
        make_old(inner, 3 * 24 * 3600, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert inner.exists()

    def test_the_count_and_the_survivors_with_a_mixed_folder(self, tmp_path):
        old = [tmp_path / _saver_name(n) for n in (1, 2, 3)]
        young = tmp_path / _saver_name(4)
        other = tmp_path / "notes.wav"
        for f in old:
            f.write_bytes(b"x")
            make_old(f, MAX_AGE_SECONDS + 60, NOW)
        young.write_bytes(b"x")
        make_old(young, 60, NOW)
        other.write_bytes(b"x")
        make_old(other, 10 * 24 * 3600, NOW)
        assert delete_old_files(tmp_path, NOW) == 3
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            [young.name, other.name])

    def test_the_age_is_judged_against_the_now_argument(self, tmp_path):
        f = tmp_path / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, 100, NOW)
        assert delete_old_files(tmp_path, NOW) == 0
        assert delete_old_files(tmp_path, NOW + MAX_AGE_SECONDS) == 1
        assert not f.exists()

    def test_a_path_that_is_a_regular_file_returns_zero_and_raises_nothing(
            self, tmp_path):
        """The running saver relies on this: when the folder path is held by
        a file, the start-up deletion pass is silent, so the one warning of
        that provider start belongs to the failed write."""
        occupied = tmp_path / "occupied"
        occupied.write_text("a regular file, not a folder")
        assert delete_old_files(occupied, NOW) == 0
        assert occupied.read_text() == "a regular file, not a folder"

    def test_a_missing_folder_returns_zero_and_is_not_created(self, tmp_path):
        missing = tmp_path / "nope"
        assert delete_old_files(missing, NOW) == 0
        assert not missing.exists()


# ---------------------------------------------------------------------------
# EngineAudioSaver: writing (items 3, 4)
# ---------------------------------------------------------------------------

class TestWritesTheWav:
    def test_the_wav_holds_the_samples_given_to_submit(
            self, make_saver, tmp_path):
        audio = ramp(800)
        saver = make_saver()
        saver.start()
        saver.submit(audio, 12, True)
        saver.stop()
        files = saved_files(tmp_path / "audio")
        assert len(files) == 1
        channels, width, rate, pcm = read_wav(files[0])
        assert (channels, width, rate) == (1, 2, 16000)
        assert len(pcm) == len(audio)
        expected = expected_pcm16(audio)
        assert np.max(np.abs(pcm.astype(np.int64) - expected)) <= 1

    def test_samples_stay_in_order_and_include_the_extremes(
            self, make_saver, tmp_path):
        audio = np.array(
            [1.0, -1.0, 0.0, 0.25, 0.9999, -0.9999, 0.3333333, -0.7071067],
            dtype=np.float32)
        saver = make_saver()
        saver.start()
        saver.submit(audio, 1, True)
        saver.stop()
        (path,) = saved_files(tmp_path / "audio")
        _, _, _, pcm = read_wav(path)
        expected = expected_pcm16(audio)
        assert np.all(np.abs(pcm.astype(np.int64) - expected) <= 1)
        # Order: the first sample is the loudest positive one, the second
        # the loudest negative one. A reversed or shuffled file fails here.
        assert pcm[0] > 32000 and pcm[1] < -32000

    def test_the_sample_rate_argument_is_the_wav_rate(
            self, make_saver, tmp_path):
        saver = make_saver(sample_rate=22050)
        saver.start()
        saver.submit(ramp(100), 1, True)
        saver.stop()
        (path,) = saved_files(tmp_path / "audio")
        assert read_wav(path)[2] == 22050

    def test_a_change_to_the_callers_array_after_submit_does_not_reach_the_file(
            self, make_saver, tmp_path, gated_wav_open):
        """Boss note: submit COPIES the array. The audio thread reuses and
        the engine resets its buffers right after submit returns.

        The writer is held on item A, so item B waits in the queue and the
        writer has not touched B's samples. B's array changes in place after
        submit returns. Only a copy made inside submit keeps B's original
        values, whatever order the writer converts and opens in."""
        audio_a = ramp(300)
        audio_b = ramp(400)
        original_b = audio_b.copy()
        saver = make_saver()
        saver.start()
        saver.submit(audio_a, 5, True)
        assert gated_wav_open.entered.wait(WAIT), "the writer never started"
        saver.submit(audio_b, 6, True)       # waits in the queue behind A
        audio_b[:] = 0.0                     # the caller changes B in place
        gated_wav_open.release.set()
        saver.stop()
        path_b = tmp_path / "audio" / build_file_name(WHEN, "parakeet_tdt", 6)
        pcm = read_wav(path_b)[3].astype(np.int64)
        assert len(pcm) == len(original_b)
        assert np.max(np.abs(pcm - expected_pcm16(original_b))) <= 1
        assert np.any(pcm != 0)

    def test_the_folder_and_its_parents_are_created_on_the_first_write(
            self, make_saver, tmp_path):
        folder = tmp_path / "deep" / "er" / "engine-audio"
        saver = make_saver(folder=folder)
        saver.start()
        saver.submit(ramp(50), 1, True)
        saver.stop()
        assert len(saved_files(folder)) == 1

    def test_no_submit_means_no_folder(self, make_saver, tmp_path):
        folder = tmp_path / "never"
        saver = make_saver(folder=folder)
        saver.start()
        saver.stop()
        assert not folder.exists()

    def test_the_file_is_named_from_the_local_time_and_the_provider(
            self, make_saver, tmp_path):
        saver = make_saver(provider="parakeet_tdt")
        saver.start()
        saver.submit(ramp(50), 12, True)
        saver.stop()
        names = [p.name for p in saved_files(tmp_path / "audio")]
        assert names == [build_file_name(WHEN, "parakeet_tdt", 12)]

    def test_the_default_clock_names_the_file_with_the_local_time(
            self, tmp_path, savers):
        """The default local_now must be the LOCAL clock, not UTC. The other
        tests inject a fixed time, so only this one reads the default."""
        offset = datetime.now().astimezone().utcoffset()
        if offset == timedelta(0):
            pytest.skip("the local UTC offset is 0, so this test cannot tell "
                        "local time from UTC")
        saver = EngineAudioSaver("parakeet_tdt", tmp_path / "audio")
        savers.append(saver)
        saver.start()
        saver.submit(ramp(50), 9, True)
        saver.stop()
        (path,) = saved_files(tmp_path / "audio")
        match = re.fullmatch(
            r"engine-audio_(\d{8}-\d{6})-(\d{3})_parakeet_tdt_utt9\.wav",
            path.name)
        assert match, path.name
        stamped = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
        assert abs((datetime.now() - stamped).total_seconds()) < 10

    def test_one_file_for_each_utterance_even_when_the_final_was_not_sent(
            self, make_saver, tmp_path):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        saver.submit(ramp(60), 2, False)
        saver.submit(ramp(70), 3, True)
        saver.stop()
        names = sorted(p.name for p in saved_files(tmp_path / "audio"))
        assert names == sorted(
            build_file_name(WHEN, "parakeet_tdt", n) for n in (1, 2, 3))

    def test_stop_writes_what_is_queued(
            self, make_saver, tmp_path, gated_wav_open):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        assert gated_wav_open.entered.wait(WAIT)
        saver.submit(ramp(50), 2, True)        # waits in the queue
        gated_wav_open.release.set()
        saver.stop()
        assert len(saved_files(tmp_path / "audio")) == 2

    def test_stop_ends_the_thread(self, make_saver):
        before = saver_thread_idents()
        saver = make_saver()
        saver.start()
        assert saver_thread_idents() - before
        saver.stop()
        assert wait_for(lambda: not (saver_thread_idents() - before))


class TestWritingIsOffTheCallersThread:
    def test_the_write_happens_on_a_daemon_thread_that_is_not_the_caller(
            self, make_saver, gated_wav_open):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        assert gated_wav_open.entered.wait(WAIT)
        (writer,) = set(gated_wav_open.writer_threads)
        assert writer is not threading.current_thread()
        assert writer.daemon is True

    def test_submit_returns_while_the_writer_is_blocked(
            self, make_saver, gated_wav_open):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        assert gated_wav_open.entered.wait(WAIT)

        done = threading.Event()

        def second_submit():
            saver.submit(ramp(50), 2, True)
            done.set()

        t = threading.Thread(target=second_submit, daemon=True)
        t.start()
        assert done.wait(2.0), "submit blocked on the writer"
        t.join(WAIT)

    def test_submit_writes_nothing_on_the_callers_thread(
            self, make_saver, tmp_path, gated_wav_open):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        # The writer is held at wave.open, so no file can exist yet.
        assert gated_wav_open.entered.wait(WAIT)
        assert saved_files(tmp_path / "audio") == []


# ---------------------------------------------------------------------------
# The wheelhouse.log lines (item 4)
# ---------------------------------------------------------------------------

class TestLogLines:
    def test_a_final_that_was_sent(self, make_saver, tmp_path, records):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 12, True)
        saver.stop()
        path = tmp_path / "audio" / build_file_name(WHEN, "parakeet_tdt", 12)
        infos = info_in(records)
        assert [r.getMessage() for r in infos] == [
            f"Saved engine audio for UTT-12: {path}"]
        assert infos[0].name == LOGGER_NAME

    def test_an_utterance_with_no_final_transcript(
            self, make_saver, tmp_path, records):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 7, False)
        saver.stop()
        path = tmp_path / "audio" / build_file_name(WHEN, "parakeet_tdt", 7)
        assert [r.getMessage() for r in info_in(records)] == [
            "Saved engine audio for UTT-7 "
            f"(no final transcript was sent): {path}"]

    def test_the_path_in_the_line_is_the_full_path_of_a_real_file(
            self, make_saver, records, tmp_path):
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 3, True)
        saver.stop()
        message = info_in(records)[0].getMessage()
        named = Path(message.split(": ", 1)[1])
        assert named.is_absolute()
        assert named.exists()
        assert named.parent == tmp_path / "audio"

    def test_one_line_for_each_saved_file(self, make_saver, records):
        saver = make_saver()
        saver.start()
        for n in (1, 2, 3):
            saver.submit(ramp(50), n, n != 2)
        saver.stop()
        messages = [r.getMessage() for r in info_in(records)]
        assert len(messages) == 3
        assert sum("no final transcript was sent" in m for m in messages) == 1
        assert [re.search(r"UTT-(\d+)", m).group(1) for m in messages] == [
            "1", "2", "3"]

    def test_the_line_comes_after_the_file_is_complete(
            self, make_saver, records, tmp_path):
        """A line that names a file must not appear before the whole file
        (here: all 50 samples) can be read."""
        saver = make_saver()
        existed_at_log_time: list[bool] = []

        class _Probe(logging.Handler):
            def emit(self, record):
                path = Path(record.getMessage().rsplit(": ", 1)[1])
                try:
                    existed_at_log_time.append(len(read_wav(path)[3]) == 50)
                except Exception:
                    existed_at_log_time.append(False)

        probe = _Probe(level=logging.INFO)
        logging.getLogger(LOGGER_NAME).addHandler(probe)
        try:
            saver.start()
            saver.submit(ramp(50), 1, True)
            saver.stop()
        finally:
            logging.getLogger(LOGGER_NAME).removeHandler(probe)
        assert existed_at_log_time == [True]

    def test_no_line_without_a_file(self, make_saver, records):
        saver = make_saver()
        saver.start()
        saver.stop()
        assert info_in(records) == []


# ---------------------------------------------------------------------------
# Deletion by the running saver (item 6)
# ---------------------------------------------------------------------------

class TestRunningDeletion:
    def test_the_default_cleanup_interval_is_one_hour(self, tmp_path):
        # crewcut: reads a private attribute. The class has no public name for
        # the interval. Add one if a second reader needs it.
        assert EngineAudioSaver("p", tmp_path)._cleanup_interval == 3600

    def test_start_deletes_old_files_at_once(self, make_saver, tmp_path):
        folder = tmp_path / "audio"
        folder.mkdir()
        old = folder / GOOD_NAME
        old.write_bytes(b"x")
        keep = folder / "notes.wav"
        keep.write_bytes(b"x")
        now = time.time()
        make_old(old, MAX_AGE_SECONDS + 3600, now)
        make_old(keep, MAX_AGE_SECONDS + 3600, now)
        saver = make_saver(folder=folder, cleanup_interval=3600.0)
        saver.start()
        assert wait_for(lambda: not old.exists()), "no deletion at start"
        assert keep.exists()

    def test_the_first_pass_does_not_wait_for_the_interval(
            self, make_saver, tmp_path, monkeypatch):
        calls: list[tuple] = []
        real = eas.delete_old_files

        def counting(folder, now):
            calls.append((folder, now))
            return real(folder, now)

        monkeypatch.setattr(eas, "delete_old_files", counting)
        folder = tmp_path / "audio"
        saver = make_saver(
            folder=folder, clock=lambda: 12345.0, cleanup_interval=3600.0)
        saver.start()
        assert wait_for(lambda: len(calls) >= 1)
        assert calls[0] == (folder, 12345.0)

    def test_it_deletes_again_while_it_runs(
            self, make_saver, tmp_path, monkeypatch):
        """With a small interval and a fake clock the deletion runs again
        and then removes a file that had not yet aged out on the first pass."""
        folder = tmp_path / "audio"
        folder.mkdir()
        f = folder / GOOD_NAME
        f.write_bytes(b"x")
        make_old(f, 0, 1_000_000.0)             # mtime = 1,000,000
        clock_now = {"t": 1_000_000.0 + MAX_AGE_SECONDS - 100}
        calls: list[float] = []
        real = eas.delete_old_files

        def counting(folder_arg, now):
            calls.append(now)
            return real(folder_arg, now)

        monkeypatch.setattr(eas, "delete_old_files", counting)
        saver = make_saver(
            folder=folder, clock=lambda: clock_now["t"], cleanup_interval=0.05)
        saver.start()
        assert wait_for(lambda: len(calls) >= 1)
        assert f.exists()                        # not old enough yet
        clock_now["t"] = 1_000_000.0 + MAX_AGE_SECONDS + 100
        assert wait_for(lambda: not f.exists()), "no second deletion pass"
        assert len(calls) >= 2

    def test_a_failing_deletion_does_not_stop_the_writer(
            self, make_saver, tmp_path, monkeypatch, records):
        calls: list[float] = []

        def boom(folder, now):
            calls.append(now)
            raise OSError("scan failed")

        monkeypatch.setattr(eas, "delete_old_files", boom)
        saver = make_saver(cleanup_interval=0.05)
        saver.start()
        saver.submit(ramp(50), 1, True)
        assert wait_for(lambda: len(saved_files(tmp_path / "audio")) == 1)
        # Count the warnings only after three failed passes, so a second
        # warning would have had its chance to appear.
        assert wait_for(lambda: len(calls) >= 3), "too few deletion passes"
        assert len(warnings_in(records)) == 1


# ---------------------------------------------------------------------------
# Failure handling (item 7)
# ---------------------------------------------------------------------------

class TestFailures:
    def test_a_folder_path_held_by_a_file_warns_once_and_never_raises(
            self, make_saver, tmp_path, records):
        folder = tmp_path / "occupied"
        folder.write_text("a regular file, not a folder")
        saver = make_saver(folder=folder)
        saver.start()
        saver.submit(ramp(50), 1, True)       # must not raise
        saver.submit(ramp(50), 2, True)       # must not raise
        saver.stop()
        warnings = warnings_in(records)
        assert len(warnings) == 1
        assert warnings[0].name == LOGGER_NAME
        assert info_in(records) == []         # nothing was saved

    def test_an_oserror_from_the_write_warns_once_and_the_thread_goes_on(
            self, make_saver, tmp_path, monkeypatch, records):
        real_open = wave.open
        attempts: list[int] = []

        def failing_open(*args, **kwargs):
            mode = kwargs.get("mode", args[1] if len(args) > 1 else "rb")
            if "w" in str(mode) and len(attempts) < 2:
                attempts.append(1)
                raise OSError(28, "No space left on device")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(wave, "open", failing_open)
        saver = make_saver()
        saver.start()
        saver.submit(ramp(50), 1, True)
        saver.submit(ramp(50), 2, True)
        saver.submit(ramp(50), 3, True)       # the thread still runs: this works
        assert wait_for(lambda: len(saved_files(tmp_path / "audio")) == 1)
        saver.stop()
        assert [p.name for p in saved_files(tmp_path / "audio")] == [
            build_file_name(WHEN, "parakeet_tdt", 3)]
        assert len(warnings_in(records)) == 1       # not one per failure
        assert len(info_in(records)) == 1

    def test_a_permission_error_on_mkdir_warns_once(
            self, make_saver, tmp_path, monkeypatch, records):
        def refuse(*args, **kwargs):
            raise PermissionError(13, "Access is denied")

        # Path.mkdir, and the os calls an implementation could use instead.
        monkeypatch.setattr(Path, "mkdir", refuse)
        monkeypatch.setattr(os, "makedirs", refuse)
        monkeypatch.setattr(os, "mkdir", refuse)
        saver = make_saver(folder=tmp_path / "blocked")
        saver.start()
        saver.submit(ramp(50), 1, True)
        saver.submit(ramp(50), 2, True)
        saver.stop()
        assert len(warnings_in(records)) == 1

    def test_the_warning_budget_is_per_saver_object(
            self, make_saver, tmp_path, records):
        """One warning per provider start: a new saver warns again."""
        folder = tmp_path / "occupied"
        folder.write_text("a regular file")
        for _ in range(2):
            saver = make_saver(folder=folder)
            saver.start()
            saver.submit(ramp(50), 1, True)
            saver.submit(ramp(50), 2, True)
            saver.stop()
        assert len(warnings_in(records)) == 2

    def test_a_full_queue_warns_once_and_never_raises(
            self, make_saver, tmp_path, gated_wav_open, records):
        saver = make_saver(queue_size=1)
        saver.start()
        saver.submit(ramp(50), 1, True)
        assert gated_wav_open.entered.wait(WAIT)    # #1 is in the writer
        saver.submit(ramp(50), 2, True)             # fills the queue
        saver.submit(ramp(50), 3, True)             # full: dropped, warns
        saver.submit(ramp(50), 4, True)             # full: dropped, silent
        gated_wav_open.release.set()
        saver.stop()
        assert len(warnings_in(records)) == 1
        names = sorted(p.name for p in saved_files(tmp_path / "audio"))
        assert names == sorted(
            build_file_name(WHEN, "parakeet_tdt", n) for n in (1, 2))

    @pytest.mark.parametrize("bad", [None, "not audio", object(), 5])
    def test_submit_never_raises_on_unusable_input(self, make_saver, bad):
        saver = make_saver()
        saver.start()
        saver.submit(bad, 1, True)
        saver.stop()

    def test_submit_before_start_does_not_raise(self, make_saver):
        saver = make_saver()
        saver.submit(ramp(50), 1, True)

    def test_a_bad_item_does_not_stop_the_thread(
            self, make_saver, tmp_path, records):
        saver = make_saver()
        saver.start()
        saver.submit("not audio", 1, True)
        saver.submit(ramp(50), 2, True)
        assert wait_for(lambda: len(saved_files(tmp_path / "audio")) == 1)


# ---------------------------------------------------------------------------
# create_from_environment (item 7: off by default, nothing created)
# ---------------------------------------------------------------------------

@pytest.fixture
def local_app_data(monkeypatch, tmp_path):
    folder = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder


class TestCreateFromEnvironment:
    @pytest.mark.parametrize("value", [None, "0", "", "true", "yes", "1 ", " 1", "11"])
    def test_anything_but_exactly_one_means_off(
            self, monkeypatch, local_app_data, value):
        if value is None:
            monkeypatch.delenv(ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(ENV_VAR, value)
        before = saver_thread_idents()
        assert create_from_environment("parakeet_tdt") is None
        assert not local_app_data.exists()
        assert not (local_app_data / "WheelHouse").exists()
        assert saver_thread_idents() - before == set()

    def test_off_creates_no_folder_in_the_working_directory_either(
            self, monkeypatch, local_app_data, tmp_path):
        monkeypatch.delenv(ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        before = sorted(os.listdir(tmp_path))
        create_from_environment("parakeet_tdt")
        assert sorted(os.listdir(tmp_path)) == before

    def test_the_saver_it_builds_deletes_once_an_hour(
            self, monkeypatch, local_app_data, savers):
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("parakeet_tdt")
        savers.append(saver)
        # crewcut: reads a private attribute. The class has no public name for
        # the interval. Add one if a second reader needs it.
        assert saver._cleanup_interval == 3600

    def test_one_returns_a_started_saver_on_the_default_folder(
            self, monkeypatch, local_app_data, savers):
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("parakeet_tdt")
        assert isinstance(saver, EngineAudioSaver)
        savers.append(saver)
        saver.submit(ramp(50), 4, True)
        saver.stop()
        folder = local_app_data / "WheelHouse" / "engine-audio"
        files = saved_files(folder)
        assert len(files) == 1
        assert is_saver_file(files[0].name)
        assert files[0].name.endswith("_parakeet_tdt_utt4.wav")

    def test_the_sample_rate_reaches_the_wav(
            self, monkeypatch, local_app_data, savers):
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("google_stt", sample_rate=8000)
        savers.append(saver)
        saver.submit(ramp(50), 1, True)
        saver.stop()
        (path,) = saved_files(local_app_data / "WheelHouse" / "engine-audio")
        assert read_wav(path)[2] == 8000

    def test_the_default_sample_rate_is_16000(
            self, monkeypatch, local_app_data, savers):
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("google_stt")
        savers.append(saver)
        saver.submit(ramp(50), 1, True)
        saver.stop()
        (path,) = saved_files(local_app_data / "WheelHouse" / "engine-audio")
        assert read_wav(path)[2] == 16000

    def test_one_with_no_local_app_data_warns_once_and_returns_none(
            self, monkeypatch, tmp_path, records):
        monkeypatch.setenv(ENV_VAR, "1")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.chdir(tmp_path)
        before = saver_thread_idents()
        assert create_from_environment("parakeet_tdt") is None
        assert len(warnings_in(records)) == 1
        assert warnings_in(records)[0].name == LOGGER_NAME
        assert os.listdir(tmp_path) == []
        assert saver_thread_idents() - before == set()

    def test_one_removes_old_files_in_the_default_folder_at_start(
            self, monkeypatch, local_app_data, savers):
        folder = local_app_data / "WheelHouse" / "engine-audio"
        folder.mkdir(parents=True)
        old = folder / GOOD_NAME
        old.write_bytes(b"x")
        make_old(old, MAX_AGE_SECONDS + 3600, time.time())
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("parakeet_tdt")
        savers.append(saver)
        assert wait_for(lambda: not old.exists())


# ---------------------------------------------------------------------------
# Deletion while the feature is off (item 6 has no condition on the key)
# ---------------------------------------------------------------------------

def cleanup_threads() -> list:
    """The live off-path cleanup threads only."""
    return [t for t in threading.enumerate()
            if t.name.startswith("engine-audio-cleanup-")]


@pytest.fixture
def cleanup_guard(monkeypatch):
    """Turn the key off, and join the cleanup threads that a test started.

    Yields the idents of the threads that existed before the test. Each test
    that starts a thread also makes it end (the folder holds no saver file
    that stays), so the join returns at once.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    before = {t.ident for t in cleanup_threads()}
    yield before
    for t in cleanup_threads():
        if t.ident not in before:
            t.join(WAIT)


class TestDeletionWhileOff:
    def folder(self, local_app_data):
        return local_app_data / "WheelHouse" / "engine-audio"

    def test_off_deletes_an_old_saver_file_and_only_that(
            self, local_app_data, cleanup_guard):
        folder = self.folder(local_app_data)
        folder.mkdir(parents=True)
        old_saver = folder / GOOD_NAME
        old_other = folder / "notes.wav"
        old_saver.write_bytes(b"x")
        old_other.write_bytes(b"x")
        make_old(old_saver, MAX_AGE_SECONDS + 3600, time.time())
        make_old(old_other, MAX_AGE_SECONDS + 3600, time.time())
        assert create_from_environment("parakeet_tdt") is None
        assert not old_saver.exists()
        assert old_other.exists()
        assert {t.ident for t in cleanup_threads()} - cleanup_guard == set()

    def test_off_with_no_folder_creates_none_and_starts_no_thread(
            self, local_app_data, cleanup_guard):
        assert create_from_environment("parakeet_tdt") is None
        assert not local_app_data.exists()
        assert {t.ident for t in cleanup_threads()} - cleanup_guard == set()

    def test_off_passes_the_hourly_interval_by_default(
            self, local_app_data, cleanup_guard, monkeypatch):
        seen: list[float] = []

        class Recorder:
            def __init__(self, provider, folder, interval, clock):
                seen.append(interval)

            def run_once(self):
                pass

        monkeypatch.setattr(eas, "_OffCleanup", Recorder)
        assert create_from_environment("parakeet_tdt") is None
        assert seen == [3600]

    def test_off_keeps_deleting_while_a_young_saver_file_remains(
            self, local_app_data, cleanup_guard):
        folder = self.folder(local_app_data)
        folder.mkdir(parents=True)
        young = folder / GOOD_NAME
        young.write_bytes(b"x")
        now = time.time()
        make_old(young, 100, now)
        clock_now = {"t": now}
        assert create_from_environment(
            "parakeet_tdt", cleanup_interval=0.05,
            clock=lambda: clock_now["t"]) is None
        assert young.exists()
        threads = [t for t in cleanup_threads() if t.ident not in cleanup_guard]
        assert len(threads) == 1
        assert threads[0].daemon
        clock_now["t"] = now + MAX_AGE_SECONDS + 100
        assert wait_for(lambda: not young.exists()), "the file stayed"
        threads[0].join(WAIT)
        assert not threads[0].is_alive(), "the thread did not end"

    def test_off_when_deletion_raises_warns_once_and_never_raises(
            self, local_app_data, cleanup_guard, monkeypatch, records):
        folder = self.folder(local_app_data)
        folder.mkdir(parents=True)
        young = folder / GOOD_NAME
        young.write_bytes(b"x")
        calls: list[float] = []

        def boom(folder_arg, now):
            calls.append(now)
            raise OSError("scan failed")

        monkeypatch.setattr(eas, "delete_old_files", boom)
        try:
            assert create_from_environment(
                "parakeet_tdt", cleanup_interval=0.05) is None
            assert wait_for(lambda: len(calls) >= 3), "too few passes"
            assert len(warnings_in(records)) == 1
            assert warnings_in(records)[0].name == LOGGER_NAME
        finally:
            young.unlink()
            for t in cleanup_threads():
                if t.ident not in cleanup_guard:
                    t.join(WAIT)

    def test_on_starts_no_cleanup_thread(
            self, local_app_data, cleanup_guard, monkeypatch, savers):
        monkeypatch.setenv(ENV_VAR, "1")
        saver = create_from_environment("parakeet_tdt")
        savers.append(saver)
        assert {t.ident for t in cleanup_threads()} - cleanup_guard == set()
