"""The cut and the check of the training-word recording (wh-first-sound-lost.23).

The audio thread of the Parakeet provider collects the raw chunks of one
spoken "hello". lead_word_recorder cuts the word out of that capture and
runs the check rows (a) to (i) over it. These tests drive the functions with
synthetic int16 signals and a fake Silero VAD, so no model is loaded.

The synthetic word is a square wave of constant amplitude `amp`: every sample
is +amp or -amp, so every 5 ms frame it fills has an RMS of exactly `amp`,
and a frame that holds even one sample of it is within 40 dB of the peak
when the rest of the signal is digital silence.
"""
from __future__ import annotations

import wave
from types import SimpleNamespace

import numpy as np
import pytest

import lead_word_recorder as rec
from sherpa_engine import SherpaOfflineEngine, load_lead_word_audio

SR = 16000
CHUNK = 512
FRAME = 80  # 5 ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class EnergyVad:
    """A fake Silero: a 512-sample chunk is speech when a sample passes the level."""

    def __init__(self, level: int = 300):
        self.level = level
        self.calls = 0

    def is_speech(self, pcm_bytes: bytes) -> bool:
        self.calls += 1
        assert len(pcm_bytes) == CHUNK * 2
        return bool(np.abs(np.frombuffer(pcm_bytes, dtype="<i2")).max() > self.level)


class FlagVad:
    """A fake Silero that answers from a list, one answer per chunk."""

    def __init__(self, flags):
        self.flags = list(flags)

    def is_speech(self, pcm_bytes: bytes) -> bool:
        return self.flags.pop(0)


def burst(start: int, length: int, amp: int = 8000):
    return start, length, amp


def make_capture(
    total: int = 3 * SR,
    bursts=(burst(20000, 9600),),
    noise_amp: int = 0,
    seed: int = 5,
) -> np.ndarray:
    """Silence (or low noise) with square-wave bursts."""
    if noise_amp:
        rng = np.random.default_rng(seed)
        x = rng.integers(-noise_amp, noise_amp + 1, size=total).astype(np.int16)
    else:
        x = np.zeros(total, dtype=np.int16)
    for start, length, amp in bursts:
        wave_ = np.where(np.arange(length) % 2 == 0, amp, -amp).astype(np.int16)
        x[start:start + length] = wave_
    return x


def check(samples, level: int = 300):
    vad = EnergyVad(level)
    regions = rec.speech_regions(samples, vad)
    return rec.check_capture(samples, regions)


def dbfs(rms: float) -> float:
    return 20 * np.log10(rms / 32768.0)


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

class TestSpeechRegions:
    def test_a_run_of_speech_chunks_is_one_region_in_samples(self):
        flags = [False] * 3 + [True] * 10 + [False] * 4
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [(3 * CHUNK, 13 * CHUNK)]

    def test_the_vad_gets_each_512_sample_chunk_once_in_order(self):
        samples = np.arange(5 * CHUNK, dtype=np.int16)
        seen = []

        class Recording:
            def is_speech(self, pcm_bytes):
                seen.append(np.frombuffer(pcm_bytes, dtype="<i2").copy())
                return False

        rec.speech_regions(samples, Recording())
        assert len(seen) == 5
        for i, chunk in enumerate(seen):
            assert np.array_equal(chunk, samples[i * CHUNK:(i + 1) * CHUNK])

    def test_a_partial_last_chunk_is_not_given_to_the_vad(self):
        vad = EnergyVad()
        rec.speech_regions(np.zeros(3 * CHUNK + 511, dtype=np.int16), vad)
        assert vad.calls == 3

    def test_regions_9_chunks_apart_merge(self):
        # 9 chunks = 4608 samples = 288 ms, under the 300 ms merge gap.
        flags = [True] * 8 + [False] * 9 + [True] * 8
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [(0, 25 * CHUNK)]

    def test_regions_10_chunks_apart_stay_apart(self):
        # 10 chunks = 5120 samples = 320 ms, over the 300 ms merge gap.
        flags = [True] * 8 + [False] * 10 + [True] * 8
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [
            (0, 8 * CHUNK), (18 * CHUNK, 26 * CHUNK)]

    def test_a_region_of_7_chunks_is_kept(self):
        # 7 chunks = 3584 samples = 224 ms, over the 200 ms minimum.
        flags = [False] * 2 + [True] * 7 + [False] * 2
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [(2 * CHUNK, 9 * CHUNK)]

    def test_a_region_of_6_chunks_is_dropped(self):
        # 6 chunks = 3072 samples = 192 ms, under the 200 ms minimum.
        flags = [False] * 2 + [True] * 6 + [False] * 2
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == []

    def test_two_short_regions_that_merge_into_a_long_one_are_kept(self):
        # Each part is 4 chunks (128 ms); merged they are 4 + 3 + 4 = 11 chunks.
        flags = [True] * 4 + [False] * 3 + [True] * 4
        samples = np.zeros(len(flags) * CHUNK, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [(0, 11 * CHUNK)]

    def test_a_region_that_runs_to_the_end_of_the_capture_ends_at_the_last_full_chunk(self):
        flags = [False] * 2 + [True] * 8
        samples = np.zeros(len(flags) * CHUNK + 100, dtype=np.int16)
        assert rec.speech_regions(samples, FlagVad(flags)) == [(2 * CHUNK, 10 * CHUNK)]

    def test_no_speech_gives_no_region(self):
        assert rec.speech_regions(np.zeros(4 * SR, dtype=np.int16), EnergyVad()) == []

    def test_the_silero_vad_uses_a_new_instance_with_the_given_threshold(self):
        vad = rec.new_silero_vad(0.37)
        assert vad.threshold == 0.37
        assert vad.sample_rate == SR
        # A silent chunk is not speech; the call proves the model runs.
        assert vad.is_speech(bytes(CHUNK * 2)) is False

    def test_the_new_vad_is_not_the_instance_of_the_live_processor(self):
        a = rec.new_silero_vad(0.5)
        b = rec.new_silero_vad(0.5)
        assert a is not b
        assert a._detector is not b._detector


# ---------------------------------------------------------------------------
# The cut, to the sample
# ---------------------------------------------------------------------------

class TestCut:
    def test_the_cut_boundaries_to_the_sample(self):
        # Word from 20000 to 29600. Chunks 39..57 are speech, so the region is
        # 19968..29696 and the window 16768..32896 (200 ms = 3200 each side).
        # Frames run from 16768 in steps of 80: the first frame that holds
        # the word starts at 16768 + 40 * 80 = 19968, the last ends at
        # 16768 + 161 * 80 = 29648. The cut adds 800 samples (50 ms) each side.
        samples = make_capture()
        regions = rec.speech_regions(samples, EnergyVad())
        assert regions == [(19968, 29696)]
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 19968
        assert word.voice_end == 29648
        assert word.start == 19168
        assert word.end == 30448
        cut = samples[word.start:word.end]
        assert len(cut) == 11280

    def test_the_window_is_limited_to_the_capture_at_the_start(self):
        # Word from 1000: region 512.., window starts at 0, frames from 0.
        samples = make_capture(bursts=(burst(1000, 9600),))
        regions = rec.speech_regions(samples, EnergyVad())
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 960          # 12 * 80
        assert word.start == 160                # 960 - 800
        assert word.voice_end == 10640          # frame grid from 0

    def test_the_cut_is_limited_to_the_capture_at_the_start(self):
        # Voice starts at 640: 800 samples before it would be negative.
        samples = make_capture(bursts=(burst(700, 9600),))
        regions = rec.speech_regions(samples, EnergyVad())
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 640
        assert word.start == 0

    def test_the_cut_is_limited_to_the_capture_at_the_end(self):
        total = 30000
        samples = make_capture(total=total, bursts=(burst(20000, 9800),))
        regions = rec.speech_regions(samples, EnergyVad())
        word = rec.cut_word(samples, regions[0])
        assert word.end == total
        assert word.voice_end <= total

    def test_a_frame_20_db_below_the_peak_is_part_of_the_voice(self):
        # A quiet lead-in at amp 800 (20 dB under 8000) is inside the 40 dB band.
        samples = make_capture(bursts=(burst(18000, 2000, 800), burst(20000, 9600)))
        word = rec.cut_word(samples, rec.speech_regions(samples, EnergyVad(300))[0])
        assert word.voice_start == 18000

    def test_a_frame_more_than_40_db_below_the_peak_is_not_voice(self):
        # Amp 70 against 8000 is 41.1 dB below the peak, so the frames of the
        # quiet part are outside the band. The fake VAD is set low to see them.
        samples = make_capture(bursts=(burst(18000, 2000, 70), burst(20000, 9600)))
        regions = rec.speech_regions(samples, EnergyVad(300))
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 19968

    def test_a_frame_just_inside_the_40_db_band_is_voice(self):
        # Amp 81 is 39.9 dB below 8000. The frame 17968..18048 holds only 48
        # samples of it (RMS 62.7, outside the band), so the voice starts at
        # the next frame, 18048, the first one that is full of it.
        samples = make_capture(bursts=(burst(18000, 2000, 81), burst(20000, 9600)))
        regions = rec.speech_regions(samples, EnergyVad(300))
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 18048

    def test_a_frame_40_1_db_below_the_peak_is_not_voice(self):
        # Amp 79 is 40.1 dB below 8000. The band of 40 dB leaves it out; a
        # wider band (41 dB) lets it in and moves the voice start to 18048.
        samples = make_capture(bursts=(burst(18000, 2000, 79), burst(20000, 9600)))
        regions = rec.speech_regions(samples, EnergyVad(300))
        word = rec.cut_word(samples, regions[0])
        assert word.voice_start == 19968

    def test_a_frame_exactly_40_db_below_the_peak_is_voice(self, monkeypatch):
        # Stubbed levels, so that the difference is exactly 40.0 in floating
        # point: -60.0 against a peak of -20.0. The band is inclusive.
        levels = np.full(130, -120.0)
        levels[10] = -60.0
        levels[20] = -20.0
        monkeypatch.setattr(rec, "frame_levels_dbfs", lambda samples: levels)
        word = rec.cut_word(np.zeros(20000, dtype=np.int16), (4000, 8000))
        window_start = 4000 - 3200
        assert word.voice_start == window_start + 10 * FRAME
        assert word.voice_end == window_start + 21 * FRAME

    def test_the_window_reaches_200_ms_after_the_region_and_no_further(self):
        # Region 19968..29696, window end 32896, frames from 16768, so the
        # last full frame of the window is 32768..32848. A soft sound (20 dB
        # under the word, inside the band) in that frame is voice; the same
        # sound at 32900, past the window end, is not. The region is given
        # to cut_word directly, so the fake VAD cannot merge the soft sound
        # into it.
        region = (19968, 29696)
        inside = make_capture(bursts=(burst(20000, 9600), burst(32768, 80, 800)))
        assert rec.cut_word(inside, region).voice_end == 32848
        outside = make_capture(bursts=(burst(20000, 9600), burst(32900, 80, 800)))
        assert rec.cut_word(outside, region).voice_end == 29648

    def test_the_window_reaches_200_ms_before_the_region_and_no_further(self):
        # The window starts at 16768. A soft sound in the first frame
        # (16768..16848) is voice; the same sound at 16640..16720, before the
        # window, is not.
        region = (19968, 29696)
        inside = make_capture(bursts=(burst(16768, 80, 800), burst(20000, 9600)))
        assert rec.cut_word(inside, region).voice_start == 16768
        outside = make_capture(bursts=(burst(16640, 80, 800), burst(20000, 9600)))
        assert rec.cut_word(outside, region).voice_start == 19968

    def test_the_cut_holds_the_capture_samples_unchanged(self):
        samples = make_capture(noise_amp=6)
        regions = rec.speech_regions(samples, EnergyVad())
        result = rec.check_capture(samples, regions)
        word = rec.cut_word(samples, regions[0])
        assert np.array_equal(result.cut, samples[word.start:word.end])
        assert result.cut.dtype == np.int16

    def test_the_cut_is_a_copy(self):
        samples = make_capture()
        result = check(samples)
        result.cut[0] = 123
        assert samples[19168] != 123


# ---------------------------------------------------------------------------
# The voice band follows the measured noise (wh-training-word-noisy-reject)
# ---------------------------------------------------------------------------

class TestVoiceBand:
    """The band is min(VOICE_BAND_DB, peak - noise - NOISE_MARGIN_DB), never
    below MIN_VOICE_BAND_DB. The Yoga laptop microphone measured 20 to 31 dB
    between the word and its own noise; the Ikon microphone 72.6 dB.
    """

    def test_no_noise_measure_keeps_the_40_db_band(self):
        assert rec.voice_band_db(-10.0, None) == 40

    def test_a_50_db_distance_keeps_the_40_db_band_and_49_db_narrows_it(self):
        assert rec.voice_band_db(-10.0, -60.0) == 40
        assert rec.voice_band_db(-10.0, -59.0) == 39.0

    def test_the_ikon_distance_keeps_the_40_db_band(self):
        assert rec.voice_band_db(-13.9, -86.5) == 40

    def test_the_band_never_goes_below_its_floor(self):
        assert rec.voice_band_db(-10.0, -27.0) == 7.0
        assert rec.voice_band_db(-10.0, -26.0) == 6.0
        assert rec.voice_band_db(-10.0, -25.0) == 6.0
        assert rec.voice_band_db(-10.0, 0.0) == 6.0

    def test_on_a_low_snr_capture_the_cut_leaves_out_the_noise_on_each_side(self):
        # Word of amp 8000 (-12.2 dBFS) from 20480 to 30080 in noise of amp
        # 600 (RMS 346, about -39.5 dBFS): 27 dB apart, like the Yoga takes.
        # Chunks 40..58 are speech, so the region is 20480..30208 and the
        # window 17280..33408. With the 40 dB band every noise frame of the
        # window is voice and the voice runs from 17280 to 33360 (1005 ms).
        # The band of about 17 dB keeps only the frames of the word.
        samples = make_capture(bursts=(burst(20480, 9600),), noise_amp=600)
        regions = rec.speech_regions(samples, EnergyVad(3000))
        assert regions == [(20480, 30208)]
        noise = rec.noise_level_dbfs(samples, regions)
        word = rec.cut_word(samples, regions[0], noise)
        assert word.voice_start == 20480
        assert word.voice_end == 30080
        assert word.start == 20480 - 800
        assert word.end == 30080 + 800

    def test_check_capture_cuts_a_low_snr_word_without_the_noise(self):
        samples = make_capture(bursts=(burst(20480, 9600),), noise_amp=600)
        result = check(samples, level=3000)
        assert result.measures["lead_in_ms"] == 1280     # 20480 samples
        assert result.measures["voiced_ms"] == 600       # 9600 samples
        assert result.measures["cut_ms"] == 700

    def test_a_high_snr_capture_keeps_the_40_db_band_and_the_same_cut(self):
        # Ikon-like: a word of amp 6600 (-13.9 dBFS) in noise of amp 2 (RMS
        # 1.41, -87.3 dBFS). In front of the word: a soft sound of amp 37
        # (45 dB under the word, outside the 40 dB band) and an onset of
        # amp 117 (35 dB under it, inside the band). The cut with the noise
        # measure is the cut of the 40 dB band without one, to the sample:
        # the voice starts at the onset (18048), not at the soft sound.
        samples = make_capture(
            bursts=(burst(17200, 848, 37), burst(18048, 1952, 117),
                    burst(20000, 9600, 6600)),
            noise_amp=2)
        regions = rec.speech_regions(samples, EnergyVad())
        assert regions == [(19968, 29696)]
        noise = rec.noise_level_dbfs(samples, regions)
        window = rec.frame_levels_dbfs(samples[16768:32896])
        assert rec.voice_band_db(float(window.max()), noise) == 40
        with_noise = rec.cut_word(samples, regions[0], noise)
        without = rec.cut_word(samples, regions[0])
        assert with_noise == without
        assert with_noise.voice_start == 18048
        result = rec.check_capture(samples, regions)
        assert np.array_equal(result.cut, samples[without.start:without.end])


    # A quiet start or tail inside the speech region is part of the word. Word
    # of amp 8000 (-12.2 dBFS) from 20480 to 30080, in noise of amp 300 (RMS
    # 173, -45.5 dBFS): 33.3 dB apart, so the band is 23.3 dB. A quiet part of
    # amp 400 (-38.3 dBFS, 26 dB under the peak, outside the band) lasts 160 ms
    # (2560 samples), which is more than CUT_MARGIN_MS (50 ms).

    def test_a_quiet_start_inside_the_region_is_not_cut_on_a_low_snr_capture(self):
        # Quiet start 17920..20480: chunks 35..57 are speech, so the region is
        # 17920..30208. The voice still starts at the loud part (20480).
        samples = make_capture(
            bursts=(burst(17920, 2560, 400), burst(20480, 9600)), noise_amp=300)
        regions = rec.speech_regions(samples, EnergyVad(350))
        assert regions == [(17920, 30208)]
        noise = rec.noise_level_dbfs(samples, regions)
        window = rec.frame_levels_dbfs(samples[14720:33408])
        assert rec.voice_band_db(float(window.max()), noise) < 40
        word = rec.cut_word(samples, regions[0], noise)
        assert word.voice_start == 20480
        assert word.voice_end == 30080
        assert word.start == 17920              # the start of the region
        assert word.end == 30080 + 800          # the margin; the region ends earlier

    def test_a_quiet_tail_inside_the_region_is_not_cut_on_a_low_snr_capture(self):
        # Quiet tail 30080..32640: chunks 40..63 are speech, so the region is
        # 20480..32768. The voice still ends with the loud part (30080).
        samples = make_capture(
            bursts=(burst(20480, 9600), burst(30080, 2560, 400)), noise_amp=300)
        regions = rec.speech_regions(samples, EnergyVad(350))
        assert regions == [(20480, 32768)]
        noise = rec.noise_level_dbfs(samples, regions)
        word = rec.cut_word(samples, regions[0], noise)
        assert word.voice_start == 20480
        assert word.voice_end == 30080
        assert word.start == 20480 - 800        # the margin; the region starts later
        assert word.end == 32768                # the end of the region

    def test_a_low_snr_cut_leaves_out_the_noise_outside_the_region(self):
        samples = make_capture(
            bursts=(burst(17920, 2560, 400), burst(20480, 9600)), noise_amp=300)
        regions = rec.speech_regions(samples, EnergyVad(350))
        noise = rec.noise_level_dbfs(samples, regions)
        word = rec.cut_word(samples, regions[0], noise)
        assert word.start >= regions[0][0]
        assert word.end <= word.voice_end + 800

    def test_a_quiet_start_does_not_change_the_voice_start_or_the_lead_in(self):
        samples = make_capture(
            bursts=(burst(17920, 2560, 400), burst(20480, 9600)), noise_amp=300)
        result = check(samples, level=350)
        assert result.measures["lead_in_ms"] == 1280      # 20480 samples
        assert result.measures["voiced_ms"] == 600        # 9600 samples
        assert result.measures["cut_ms"] == (30880 - 17920) * 1000 // SR

    def test_with_the_40_db_band_the_cut_stops_at_the_margin_not_at_the_region(self):
        # Ikon-like noise (amp 2): the band stays 40 dB, so the quiet start
        # (amp 45, 45 dB under the peak) is outside the band and outside the
        # margin, although it lies inside the region. The cut is the cut of
        # a capture without a noise measure (CUT_EXACT's rule), to the sample.
        samples = make_capture(
            bursts=(burst(17920, 2560, 45), burst(20480, 9600)), noise_amp=2)
        regions = rec.speech_regions(samples, EnergyVad(30))
        assert regions == [(17920, 30208)]
        noise = rec.noise_level_dbfs(samples, regions)
        window = rec.frame_levels_dbfs(samples[14720:33408])
        assert rec.voice_band_db(float(window.max()), noise) == 40
        with_noise = rec.cut_word(samples, regions[0], noise)
        assert with_noise == rec.cut_word(samples, regions[0])
        assert with_noise.start == 20480 - 800
        assert with_noise.end == 30080 + 800


class TestNoiseLevel:
    """The noise is measured outside every window, so it does not depend on
    the cut that it sets."""

    def test_the_noise_is_the_median_of_the_frames_outside_every_window(self):
        # Region 19968..29696, window 16768..32896. Outside it: 120 frames of
        # amp 6 (0..9600), 89 full frames of amp 70 (9600..16720) and 188
        # frames of amp 70 after the window (32960..48000), so the median is
        # amp 70. The window frames before the voice are digital silence:
        # the old measure (the capture before the voice start) had 40 such
        # frames and 120 of amp 6 against 89 of amp 70, a median of amp 6.
        samples = make_capture(
            bursts=(burst(0, 9600, 6), burst(9600, 7168, 70),
                    burst(20000, 9600), burst(32896, 15104, 70)))
        regions = rec.speech_regions(samples, EnergyVad())
        assert regions == [(19968, 29696)]
        assert rec.noise_level_dbfs(samples, regions) == pytest.approx(dbfs(70))

    def test_without_frames_after_the_window_the_frames_before_it_decide(self):
        # The same head as above, and the capture ends 80 samples after the
        # window end, so no full frame lies after the window. The median of
        # the 209 frames before the window is amp 6 (120 of amp 6 against 89
        # of amp 70): the tail of the test above is what made it amp 70.
        head_only = make_capture(
            bursts=(burst(0, 9600, 6), burst(9600, 7168, 70), burst(20000, 9600)),
            total=32896 + 80)
        assert rec.noise_level_dbfs(head_only, [(19968, 29696)]) == pytest.approx(dbfs(6))

    def test_the_window_of_every_region_is_left_out(self):
        # Two regions: (7680, 17920) and (29696, 54272). Noise of amp 70
        # everywhere outside the words: 204 frames outside both windows. The
        # second window holds 300 frames of a loud word (amp 8000); a measure
        # that left out only the first window would count them, and its
        # median would be the level of the word, not of the noise.
        samples = make_capture(
            total=4 * SR,
            bursts=(burst(0, 4 * SR, 70), burst(8000, 9600), burst(30000, 24000)))
        regions = rec.speech_regions(samples, EnergyVad())
        assert len(regions) == 2
        assert rec.noise_level_dbfs(samples, regions) == pytest.approx(dbfs(70))

    def test_no_frame_outside_the_window_gives_no_noise(self):
        samples = make_capture(total=16000, bursts=(burst(3200, 9600),))
        regions = rec.speech_regions(samples, EnergyVad())
        assert rec.noise_level_dbfs(samples, regions) is None

    def test_an_empty_capture_gives_no_noise(self):
        assert rec.noise_level_dbfs(np.zeros(0, dtype=np.int16), []) is None

    def test_a_frame_across_the_window_start_is_left_out(self):
        # Region (3480, 3600): window 280..4040, the capture end. Frame 3
        # (240..320) crosses the window start. Frames 0, 1 and 2 (amp 100,
        # 200 and 400) lie wholly before it, so the median is amp 200.
        # Counting frame 3 (amp 3000) moves the median between amp 200 and
        # amp 400; leaving out frame 2 moves it between amp 100 and amp 200.
        # The 40 samples after the last full frame keep a frame start moved
        # one frame late (4000) inside the window.
        samples = np.full(4040, 3000, dtype=np.int16)
        samples[0:80] = 100
        samples[80:160] = 200
        samples[160:240] = 400
        assert rec.noise_level_dbfs(samples, [(3480, 3600)]) == pytest.approx(dbfs(200))

    def test_a_frame_across_the_window_end_is_left_out(self):
        # Region (0, 120): window 0..3320. Frame 41 (3280..3360) crosses the
        # window end. Frames 42, 43 and 44 (amp 100, 200 and 400) lie wholly
        # after it, so the median is amp 200. Counting frame 41 (amp 3000)
        # moves the median between amp 200 and amp 400.
        samples = np.full(3600, 3000, dtype=np.int16)
        samples[3360:3440] = 100
        samples[3440:3520] = 200
        samples[3520:3600] = 400
        assert rec.noise_level_dbfs(samples, [(0, 120)]) == pytest.approx(dbfs(200))


# ---------------------------------------------------------------------------
# The WAV file
# ---------------------------------------------------------------------------

class TestWriteWav:
    def test_the_file_is_16k_mono_16_bit_with_the_cut_samples(self, tmp_path):
        samples = make_capture(noise_amp=6)
        result = check(samples)
        path = tmp_path / "w.wav"
        rec.write_wav(path, result.cut)
        with wave.open(str(path), "rb") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
            assert w.getnframes() == len(result.cut)
            assert np.array_equal(
                np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), result.cut)

    def test_load_lead_word_audio_accepts_the_written_file(self, tmp_path):
        result = check(make_capture(noise_amp=6))
        path = tmp_path / "w.wav"
        rec.write_wav(path, result.cut)
        audio = load_lead_word_audio(str(path), SR)
        assert audio.dtype == np.float32
        assert audio.flags.writeable is False
        assert np.array_equal(audio, rec.cut_to_float(result.cut))
        assert np.array_equal(audio, result.cut.astype(np.float32) / np.float32(32768.0))


# ---------------------------------------------------------------------------
# The check rows (a) to (h) over a capture: a refusal and a pass at the limit
# ---------------------------------------------------------------------------

class TestCheckCapture:
    def test_a_good_word_passes_with_every_measure(self):
        result = check(make_capture(noise_amp=6))
        assert result.row is None
        assert result.reason == ""
        assert result.passed
        m = result.measures
        assert set(m) == {
            "capture_ms", "lead_in_ms", "voiced_ms", "cut_ms", "level_dbfs",
            "noise_dbfs", "snr_db", "clipped", "regions"}
        assert m["capture_ms"] == 3000
        assert m["regions"] == 1
        assert m["lead_in_ms"] == 1248          # 19968 samples
        assert m["voiced_ms"] == 605            # 9680 samples
        assert m["cut_ms"] == 705               # 11280 samples
        assert m["clipped"] == 0
        assert result.cut is not None and len(result.cut) == 11280

    def test_measures_are_json_values(self):
        import json
        result = check(make_capture(noise_amp=6))
        json.dumps(result.measures)

    # (a) no speech
    def test_row_a_refuses_silence(self):
        result = check(np.zeros(3 * SR, dtype=np.int16))
        assert result.row == "no_speech"
        assert result.reason == "No speech heard"
        assert result.cut is None
        assert result.measures == {"capture_ms": 3000, "regions": 0}

    def test_row_a_refuses_an_empty_capture(self):
        result = rec.check_capture(np.zeros(0, dtype=np.int16), [])
        assert result.row == "no_speech"
        assert result.measures == {"capture_ms": 0, "regions": 0}

    def test_row_a_refuses_a_region_under_200_ms(self):
        # 3072 samples of word: 6 chunks, 192 ms.
        result = check(make_capture(bursts=(burst(20480, 3072),)))
        assert result.row == "no_speech"

    def test_row_a_passes_a_region_of_7_chunks(self):
        result = check(make_capture(bursts=(burst(20480, 3584),)))
        assert result.row != "no_speech"

    # (b) too quiet
    def test_row_b_refuses_a_quiet_word(self):
        # Amp 500 is -36.3 dBFS.
        result = check(make_capture(bursts=(burst(20480, 9600, 500),), noise_amp=1))
        assert result.row == "too_quiet"
        assert result.reason == "Too quiet"
        assert set(result.measures) == {
            "capture_ms", "regions", "lead_in_ms", "voiced_ms", "cut_ms",
            "level_dbfs", "noise_dbfs", "snr_db"}

    def test_row_b_passes_a_word_at_minus_29_dbfs(self):
        # Amp 1170: 20 * log10(1170 / 32768) = -28.9 dBFS.
        result = check(make_capture(bursts=(burst(20480, 9600, 1170),)))
        assert result.row is None

    # The noise is measured and logged, but it refuses nothing
    # (wh-training-word-noisy-reject).
    def test_a_low_snr_word_that_passes_every_other_row_is_saved(self):
        # Word at 8000 (-12.2 dBFS), noise of amp 600 (about -39.5 dBFS):
        # 27 dB apart, inside the range of the Yoga takes (20 to 31 dB).
        result = check(make_capture(bursts=(burst(20480, 9600),), noise_amp=600),
                       level=3000)
        assert result.row is None
        assert result.passed
        assert set(result.measures) == {
            "capture_ms", "regions", "lead_in_ms", "voiced_ms", "cut_ms",
            "level_dbfs", "noise_dbfs", "snr_db", "clipped"}
        assert 20 < result.measures["snr_db"] < 30

    def test_no_row_names_the_noise(self):
        assert "noisy_room" not in rec.REASONS
        assert "The room is too noisy" not in rec.REASONS.values()
        assert not hasattr(rec, "MIN_SNR_DB")

    # (d) clipping
    def test_row_d_refuses_one_sample_at_32767(self):
        samples = make_capture(noise_amp=6)
        samples[24000] = 32767
        result = check(samples)
        assert result.row == "clipping"
        assert result.reason == "The recording is too loud and clips"
        assert result.measures["clipped"] == 1

    def test_row_d_refuses_one_sample_at_minus_32768(self):
        samples = make_capture(noise_amp=6)
        samples[24000] = -32768
        result = check(samples)
        assert result.row == "clipping"
        assert result.measures["clipped"] == 1

    def test_row_d_passes_samples_at_32766(self):
        samples = make_capture(bursts=(burst(20480, 9600, 32766),), noise_amp=6)
        result = check(samples)
        assert result.row is None
        assert result.measures["clipped"] == 0

    def test_row_d_counts_only_samples_in_the_cut(self):
        samples = make_capture(noise_amp=6)
        samples[100] = 32767        # in the lead-in, outside the cut
        result = check(samples)
        assert result.row is None

    # (e) too long
    def test_row_e_refuses_1005_ms(self):
        result = check(make_capture(bursts=(burst(20480, 16080),)))
        assert result.row == "too_long"
        assert result.reason == "The word is too long"
        assert result.measures["voiced_ms"] == 1005

    def test_row_e_passes_1000_ms(self):
        result = check(make_capture(bursts=(burst(20480, 16000),)))
        assert result.row is None
        assert result.measures["voiced_ms"] == 1000

    # (f) too short
    def test_row_f_refuses_245_ms(self):
        result = check(make_capture(bursts=(burst(20480, 3920),)))
        assert result.row == "too_short"
        assert result.reason == "The word is too short"
        assert result.measures["voiced_ms"] == 245

    def test_row_f_passes_250_ms(self):
        result = check(make_capture(bursts=(burst(20480, 4000),)))
        assert result.row is None
        assert result.measures["voiced_ms"] == 250

    # (g) started too soon
    def test_row_g_refuses_a_voice_start_at_720_samples(self):
        # 45 ms of audio before the voice start.
        result = check(make_capture(bursts=(burst(720, 9600),)))
        assert result.row == "started_too_soon"
        assert result.reason == "You started too soon after the last sound"
        assert result.measures["lead_in_ms"] == 45

    def test_row_g_passes_a_voice_start_at_800_samples(self):
        # Exactly 50 ms before the voice start.
        result = check(make_capture(bursts=(burst(800, 9600),)))
        assert result.row is None
        assert result.measures["lead_in_ms"] == 50

    # (h) more than one word
    def test_row_h_refuses_two_regions(self):
        # Gap of 512 * 20 samples between the two words.
        result = check(make_capture(
            bursts=(burst(15360, 6144), burst(15360 + 6144 + 10240, 6144))))
        assert result.row == "more_than_one_word"
        assert result.reason == "More than one word heard"
        assert result.measures["regions"] == 2

    def test_with_two_regions_the_cut_is_made_around_the_first(self):
        # Loud word then quiet word: the cut is around the loud one, so no row
        # before (h) refuses. Quiet word then loud word: the cut is around the
        # quiet one, and row (b) refuses before row (h) is reached.
        loud, quiet = burst(15360, 6144, 8000), burst(15360 + 6144 + 10240, 6144, 400)
        first_loud = check(make_capture(bursts=(loud, quiet)))
        assert first_loud.row == "more_than_one_word"
        assert first_loud.measures["level_dbfs"] == round(float(dbfs(8000)), 2)
        quiet_first = burst(15360, 6144, 400)
        loud_second = burst(15360 + 6144 + 10240, 6144, 8000)
        first_quiet = check(make_capture(bursts=(quiet_first, loud_second)))
        assert first_quiet.row == "too_quiet"
        assert first_quiet.measures["level_dbfs"] == round(float(dbfs(400)), 2)

    def test_row_h_passes_two_parts_under_300_ms_apart(self):
        # Gap of 4608 samples (288 ms): one merged region.
        result = check(make_capture(
            bursts=(burst(15360, 4096), burst(15360 + 4096 + 4608, 4096))))
        assert result.row is None
        assert result.measures["regions"] == 1

    # the order of the rows
    def test_the_first_failing_row_is_the_one_reported(self):
        # Quiet (b) and too short (f) at once: b comes first.
        result = check(make_capture(bursts=(burst(20480, 3920, 500),), noise_amp=1))
        assert result.row == "too_quiet"


class TestMeasuredLevels:
    """level_dbfs, noise_dbfs and snr_db to the value (contract section 4).

    Every burst is a square wave of constant amplitude, so every full 5 ms
    frame in it has an RMS of exactly its amplitude and the expected levels
    follow from the amplitudes.
    """

    @staticmethod
    def level(amp: int) -> float:
        return round(float(dbfs(amp)), 2)

    def test_level_is_the_loudest_frame_of_the_cut(self):
        result = check(make_capture())
        assert result.measures["level_dbfs"] == self.level(8000)

    def test_a_loud_sound_outside_the_word_does_not_set_the_level(self):
        # A 10 ms click of amp 20000 at 44000, far outside the window of the
        # word. It is a region of 64 ms, so it is dropped. The level is the
        # level of the cut, not of the capture.
        samples = make_capture(bursts=(burst(20000, 9600), burst(44000, 160, 20000)))
        result = check(samples)
        assert result.measures["regions"] == 1
        assert result.measures["level_dbfs"] == self.level(8000)

    def test_noise_is_the_median_frame_level_outside_the_windows(self):
        # Region 19968..29696, window 16768..32896. The 397 full frames
        # outside the window: 120 of amp 6 (0..9600), 50 of amp 200
        # (9600..13600), 39 of amp 70 (13600..16720) and 188 of amp 70 after
        # the window. The median is the 199th sorted level, an amp-70 frame;
        # the maximum is amp 200, the minimum amp 6, and the mean is neither.
        # The 40 frames of the window before the voice are digital silence.
        # The old measure (the 249 frames before the voice start, with those
        # 40 and without the tail) had a median of amp 6.
        samples = make_capture(
            bursts=(burst(0, 9600, 6), burst(9600, 4000, 200),
                    burst(13600, 3168, 70), burst(20000, 9600),
                    burst(32896, 15104, 70)))
        result = check(samples)
        assert result.row is None
        m = result.measures
        assert m["noise_dbfs"] == self.level(70)
        assert m["level_dbfs"] == self.level(8000)
        assert m["snr_db"] == round(m["level_dbfs"] - m["noise_dbfs"], 2)
        assert m["snr_db"] == round(self.level(8000) - self.level(70), 2)

    def test_check_capture_leaves_out_the_window_of_every_region(self):
        # Two words in noise of amp 70; the second (300 frames of amp 8000)
        # is longer than the noise outside both windows (204 frames). Row (h)
        # refuses, and its measures hold the noise of row (b). A measure that
        # left out only the first window would give the level of the second
        # word.
        samples = make_capture(
            total=4 * SR,
            bursts=(burst(0, 4 * SR, 70), burst(8000, 9600), burst(30000, 24000)))
        result = check(samples)
        assert result.row == "more_than_one_word"
        assert result.measures["regions"] == 2
        assert result.measures["noise_dbfs"] == self.level(70)

    def test_a_lead_in_of_one_frame_still_gives_the_noise_and_snr_measures(self):
        # The voice starts at sample 80, so the lead-in is one 5 ms frame of
        # digital silence. The noise comes from the frames after the window.
        # Row (g) refuses it, and the measures hold both noise keys.
        result = check(make_capture(bursts=(burst(80, 9600),)))
        assert result.row == "started_too_soon"
        assert result.measures["lead_in_ms"] == 5
        assert result.measures["noise_dbfs"] == -120.0
        assert result.measures["snr_db"] == round(self.level(8000) + 120.0, 2)

    def test_the_snr_measure_is_the_level_of_the_cut_minus_the_noise(self):
        # Noise of amp 700 (-33.4 dBFS) before and after the window, against
        # a word of 8000 (-12.2 dBFS): 21 dB. The recording passes.
        samples = make_capture(
            bursts=(burst(0, 16768, 700), burst(20000, 9600),
                    burst(32896, 15104, 700)))
        result = check(samples, level=3000)
        assert result.row is None
        assert result.measures["snr_db"] == round(
            self.level(8000) - self.level(700), 2)

    def test_the_level_is_measured_against_a_full_scale_of_32768(self):
        # Amp 2010: 20 * log10(2010 / 32768) rounds to -24.25; against a full
        # scale of 32767 the same frame would round to -24.24.
        result = check(make_capture(bursts=(burst(20000, 9600, 2010),)))
        assert result.measures["level_dbfs"] == -24.25

    def test_digital_silence_has_the_floor_level_of_minus_120_dbfs(self):
        levels = rec.frame_levels_dbfs(np.zeros(2 * FRAME, dtype=np.int16))
        assert list(levels) == [-120.0, -120.0]
        # One least-significant bit is -90.3 dBFS, above the floor.
        quiet = rec.frame_levels_dbfs(np.ones(FRAME, dtype=np.int16))
        assert round(float(quiet[0]), 2) == round(dbfs(1.0), 2)

    def test_a_silent_lead_in_gives_the_floor_as_the_noise_level(self):
        result = check(make_capture())
        m = result.measures
        assert m["noise_dbfs"] == -120.0
        assert m["snr_db"] == round(self.level(8000) + 120.0, 2)

    def test_a_capture_inside_the_window_has_no_noise_and_no_snr_measure(self):
        # The window (0..16000) covers the whole capture, so no frame is
        # left to measure the noise. The cut uses the 40 dB band, the
        # recording passes, and its measures hold neither key.
        result = check(make_capture(total=16000, bursts=(burst(3200, 9600),)))
        assert result.row is None
        assert result.measures["lead_in_ms"] == 200
        assert "noise_dbfs" not in result.measures
        assert "snr_db" not in result.measures


class TestFailedRow:
    GOOD = {
        "capture_ms": 3000, "lead_in_ms": 1860, "voiced_ms": 600, "cut_ms": 700,
        "level_dbfs": -13.0, "noise_dbfs": -80.0, "snr_db": 67.0, "clipped": 0,
        "regions": 1,
    }

    def with_(self, **changes):
        measures = dict(self.GOOD)
        measures.update(changes)
        return rec.failed_row(measures)

    def test_a_good_set_of_measures_fails_no_row(self):
        assert rec.failed_row(dict(self.GOOD)) is None

    def test_level_minus_30_2_refuses_and_minus_30_0_passes(self):
        assert self.with_(level_dbfs=-30.2, snr_db=40.0).row == "too_quiet"
        assert self.with_(level_dbfs=-30.0, snr_db=40.0) is None

    def test_a_low_snr_fails_no_row(self):
        assert self.with_(snr_db=34.9) is None
        assert self.with_(snr_db=0.0, noise_dbfs=-13.0) is None

    def test_clipped_1_refuses_and_0_passes(self):
        assert self.with_(clipped=1).row == "clipping"
        assert self.with_(clipped=0) is None

    def test_voiced_1001_refuses_and_1000_passes(self):
        assert self.with_(voiced_ms=1001).row == "too_long"
        assert self.with_(voiced_ms=1000) is None

    def test_voiced_249_refuses_and_250_passes(self):
        assert self.with_(voiced_ms=249).row == "too_short"
        assert self.with_(voiced_ms=250) is None

    def test_lead_in_49_refuses_and_50_passes(self):
        assert self.with_(lead_in_ms=49).row == "started_too_soon"
        assert self.with_(lead_in_ms=50) is None

    def test_regions_2_refuses_and_1_passes(self):
        assert self.with_(regions=2).row == "more_than_one_word"
        assert self.with_(regions=1) is None

    def test_regions_0_refuses_as_no_speech(self):
        assert self.with_(regions=0).row == "no_speech"

    def test_a_missing_noise_measure_does_not_stop_the_other_rows(self):
        measures = dict(self.GOOD)
        del measures["noise_dbfs"], measures["snr_db"]
        assert rec.failed_row(measures) is None
        measures["lead_in_ms"] = 10
        assert rec.failed_row(measures).row == "started_too_soon"

    @pytest.mark.parametrize("changes, row", [
        pytest.param({"regions": 0, "level_dbfs": -31.0}, "no_speech", id="a_before_b"),
        pytest.param({"level_dbfs": -31.0, "snr_db": 10.0}, "too_quiet", id="b_low_snr"),
        pytest.param({"level_dbfs": -31.0, "clipped": 1}, "too_quiet", id="b_before_d"),
        pytest.param({"snr_db": 10.0, "clipped": 1}, "clipping", id="d_low_snr"),
        pytest.param({"clipped": 1, "voiced_ms": 1500}, "clipping", id="d_before_e"),
        pytest.param({"voiced_ms": 100, "lead_in_ms": 10}, "too_short", id="f_before_g"),
        pytest.param({"lead_in_ms": 10, "regions": 2}, "started_too_soon", id="g_before_h"),
        pytest.param({"regions": 2, "level_dbfs": -31.0}, "too_quiet", id="h_after_b"),
        pytest.param({"regions": 2, "voiced_ms": 1500}, "too_long", id="h_after_e"),
    ])
    def test_when_several_rows_fail_the_first_is_reported(self, changes, row):
        assert self.with_(**changes).row == row

    def test_every_limit_is_one_named_constant(self):
        assert rec.MIN_LEVEL_DBFS == -30.0
        assert rec.VOICE_BAND_DB == 40
        assert rec.NOISE_MARGIN_DB == 10.0
        assert rec.MIN_VOICE_BAND_DB == 6.0
        assert rec.NOISE_ADVICE_DB == 30.0
        assert rec.MAX_VOICED_MS == 1000
        assert rec.MIN_VOICED_MS == 250
        assert rec.MIN_LEAD_IN_MS == 50
        assert rec.MIN_REGION_MS == 200
        assert rec.MERGE_GAP_MS == 300
        assert rec.NO_SPEECH_TIMEOUT_S == 10.0
        assert rec.CLIP_ABS == 32767


# ---------------------------------------------------------------------------
# Row (i): the decode text
# ---------------------------------------------------------------------------

class TestDecodeRow:
    @pytest.mark.parametrize(
        "text",
        ["hello", "Hello", "HELLO", "Hello.", " hello ", "hello!", "Hello,"],
        ids=["lower", "capital", "upper", "full-stop", "spaces", "bang", "comma"],
    )
    def test_one_run_that_is_hello_passes(self, text):
        assert rec.decode_row_refusal(text) is None

    @pytest.mark.parametrize(
        "text, reason",
        [
            ("", "No word was recognized"),
            ("   ", "No word was recognized"),
            ("..", "No word was recognized"),
            ("Hallo", "The word was not hello"),
            ("hello's", "The word was not hello"),
            ("Hello, hello", "More than one word heard"),
            ("hello world", "More than one word heard"),
            ("Hello 5", "More than one word heard"),
            ("hello5", "More than one word heard"),
            ("he llo", "More than one word heard"),
        ],
        ids=["empty", "spaces", "dots", "hallo", "apostrophe", "two-hellos",
             "hello-world", "hello-5", "hello5", "he-llo"],
    )
    def test_anything_else_is_refused_with_a_plain_reason(self, text, reason):
        assert rec.decode_row_refusal(text) == reason

    def test_a_none_text_is_no_word(self):
        assert rec.decode_row_refusal(None) == "No word was recognized"

    def test_the_rule_agrees_with_remove_lead_word_on_what_is_left(self):
        from sherpa_engine import remove_lead_word
        for text in ["hello", "Hello.", "Hello 5", "hello!"]:
            left, found = remove_lead_word(text, "hello")
            assert (rec.decode_row_refusal(text) is None) == (found and left == "")


class TestNoiseAdvice:
    """The provider asks the window for the noise advice when the measured
    signal-to-noise ratio is under the limit (lead_word_noise_advice_db)."""

    def test_an_snr_under_the_limit_gives_the_advice_and_the_limit_does_not(self):
        assert rec.noise_advice({"snr_db": 29.99}, 30.0) is True
        assert rec.noise_advice({"snr_db": 30.0}, 30.0) is False
        assert rec.noise_advice({"snr_db": 30.01}, 30.0) is False

    def test_the_limit_is_the_one_given(self):
        assert rec.noise_advice({"snr_db": 25.0}, 20.0) is False
        assert rec.noise_advice({"snr_db": 25.0}, 26.0) is True

    def test_no_snr_measure_gives_no_advice(self):
        assert rec.noise_advice({}, 30.0) is False
        assert rec.noise_advice({"snr_db": None}, 30.0) is False


class TestRunCheck:
    def test_a_low_snr_word_with_a_decode_of_hello_is_saved(self):
        result = rec.run_check(
            make_capture(bursts=(burst(20480, 9600),), noise_amp=600),
            EnergyVad(3000), lambda cut: "Hello.")
        assert result.passed
        assert result.row is None
        assert result.measures["decode"] == "Hello."
        assert result.measures["snr_db"] < 30
        assert len(result.cut) == 11200

    def test_a_decode_of_hello_passes_and_the_measures_hold_the_decode(self):
        result = rec.run_check(
            make_capture(noise_amp=6), EnergyVad(), lambda cut: "Hello.")
        assert result.passed
        assert result.measures["decode"] == "Hello."

    def test_another_word_is_refused_by_row_i(self):
        result = rec.run_check(
            make_capture(noise_amp=6), EnergyVad(), lambda cut: "Goodbye")
        assert result.row == "decode"
        assert result.reason == "The word was not hello"
        assert result.measures["decode"] == "Goodbye"
        assert result.cut is not None

    def test_the_decode_gets_the_cut_not_the_whole_capture(self):
        seen = []

        def decode(cut):
            seen.append(cut)
            return "hello"

        samples = make_capture(noise_amp=6)
        result = rec.run_check(samples, EnergyVad(), decode)
        assert len(seen) == 1
        assert np.array_equal(seen[0], result.cut)
        assert len(seen[0]) < len(samples)

    def test_the_decode_does_not_run_when_an_earlier_row_refused(self):
        calls = []
        result = rec.run_check(
            np.zeros(3 * SR, dtype=np.int16), EnergyVad(),
            lambda cut: calls.append(cut) or "hello")
        assert result.row == "no_speech"
        assert calls == []
        assert "decode" not in result.measures

    def test_a_refusal_by_an_earlier_row_has_no_decode_measure(self):
        result = rec.run_check(
            make_capture(bursts=(burst(20480, 16080),)), EnergyVad(),
            lambda cut: "hello")
        assert result.row == "too_long"


# ---------------------------------------------------------------------------
# The check decode gets the cut alone while the engine holds a lead word
# ---------------------------------------------------------------------------

class _Stream:
    def __init__(self):
        self.audio = None
        self.result = SimpleNamespace(tokens=[], text="")

    def accept_waveform(self, sample_rate, audio):
        self.audio = np.asarray(audio)


class _Recognizer:
    def __init__(self):
        self.inputs = []

    def create_stream(self):
        return _Stream()

    def decode_stream(self, stream):
        self.inputs.append(stream.audio)
        stream.result.tokens = [" Hello", "."]


@pytest.fixture
def engine_with_a_lead_word(monkeypatch):
    monkeypatch.setattr(SherpaOfflineEngine, "_load_model", lambda *a, **kw: None)
    engine = SherpaOfflineEngine(model_path="/nonexistent")
    engine._recognizer = _Recognizer()
    lead = np.full(800, 0.5, dtype=np.float32)
    lead.flags.writeable = False
    engine.set_lead_word(lead)
    return engine, lead


class TestCutToFloat:
    def test_the_cut_is_scaled_by_32768_as_float32(self):
        out = rec.cut_to_float(np.array([-32768, 0, 16384, 32767], dtype=np.int16))
        assert out.dtype == np.float32
        assert list(out) == [-1.0, 0.0, 0.5, np.float32(32767) / np.float32(32768)]

    def test_a_sample_above_127_is_not_wrapped(self):
        out = rec.cut_to_float(np.array([200, -200], dtype=np.int16))
        assert list(out) == [200 / 32768, -200 / 32768]


class TestDecodeCut:
    def test_the_check_decode_gets_the_cut_alone_while_the_engine_holds_a_word(
        self, engine_with_a_lead_word
    ):
        engine, lead = engine_with_a_lead_word
        cut = check(make_capture(noise_amp=6)).cut
        text = rec.decode_cut(engine, cut)
        assert text == "Hello."
        inputs = engine._recognizer.inputs
        assert len(inputs) == 1
        assert inputs[0].dtype == np.float32
        assert np.array_equal(inputs[0], rec.cut_to_float(cut))
        assert len(inputs[0]) == len(cut)       # the lead word is not in front

    def test_the_decode_leaves_the_lead_word_of_the_engine_alone(
        self, engine_with_a_lead_word
    ):
        engine, lead = engine_with_a_lead_word
        rec.decode_cut(engine, check(make_capture(noise_amp=6)).cut)
        assert engine._lead_audio is lead

    def test_the_decode_removes_no_word_from_the_text(self, engine_with_a_lead_word):
        engine, _ = engine_with_a_lead_word
        # The raw text is "Hello."; a removal would leave "".
        assert rec.decode_cut(engine, check(make_capture(noise_amp=6)).cut) != ""


# ---------------------------------------------------------------------------
# The log line
# ---------------------------------------------------------------------------

class TestLogLine:
    def test_a_passed_line_holds_the_measures_and_the_decode(self):
        result = rec.run_check(make_capture(noise_amp=6), EnergyVad(), lambda c: "Hello.")
        line = rec.format_log_line(result)
        assert line.startswith(
            "Training-word recording: passed capture_ms=3000 regions=1 "
            "lead_in_ms=1248 voiced_ms=605 cut_ms=705 level_dbfs=")
        assert line.endswith("clipped=0 decode='Hello.'")
        assert "," not in line.replace("decode='Hello.'", "")
        assert "\n" not in line

    def test_a_refused_line_names_the_row_and_holds_no_decode_text(self):
        result = rec.run_check(make_capture(noise_amp=6), EnergyVad(), lambda c: "Goodbye now")
        line = rec.format_log_line(result)
        assert line.startswith(
            "Training-word recording: refused (decode): More than one word heard"
            " capture_ms=3000 ")
        assert "Goodbye" not in line
        assert "decode_chars=11" in line

    def test_a_refusal_before_the_decode_has_no_decode_field(self):
        result = rec.run_check(np.zeros(SR, dtype=np.int16), EnergyVad(), lambda c: "x")
        line = rec.format_log_line(result)
        assert "refused (no_speech)" in line
        assert "decode" not in line.replace("(no_speech)", "")
