"""The cut and the check of the training-word recording (wh-first-sound-lost.23).

The audio thread of the Parakeet provider keeps the raw int16 chunks of one
spoken "hello". This module is pure signal work over that capture: it finds
the speech regions with Silero, cuts the word out of the capture, runs the
check rows (a) to (i), and writes the WAV file. It starts no thread and
opens no device. Row (c), the noise row, refuses nothing since
wh-training-word-noisy-reject: the noise sets the voice band of the cut and
the noise advice of a passed recording instead. The other letters keep their
places. Where the files live and which one the engine uses is the
job of lead_word_store.

Every limit is one named constant below. All sample counts assume 16 kHz.
"""
from __future__ import annotations

import logging
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sherpa_engine import _LEAD_WORD_RUN, LEAD_WORD, remove_lead_word

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# --- Capture (the audio thread reads these) ----------------------------------
# The raw lead-in the audio thread keeps in front of the gate opening.
LEAD_IN_MS = 300
# The capture ends this long after the last speech chunk, or this long after
# the gate opened, whichever is first.
CAPTURE_END_SILENCE_S = 1.0
CAPTURE_MAX_S = 4.0
# The gate must open within this time after "Record"; otherwise the result is
# the row (a) refusal.
NO_SPEECH_TIMEOUT_S = 10.0

# --- The cut -----------------------------------------------------------------
SILERO_CHUNK_SAMPLES = 512
MERGE_GAP_MS = 300          # regions closer than this merge
MIN_REGION_MS = 200         # shorter regions are dropped
WINDOW_PAD_MS = 200         # window = region plus this on each side
FRAME_MS = 5
VOICE_BAND_DB = 40          # voice = frames within this of the loudest frame
# The band narrows when the noise is near the word (wh-training-word-noisy-
# reject): band = min(VOICE_BAND_DB, peak - noise - NOISE_MARGIN_DB), never
# below MIN_VOICE_BAND_DB. See voice_band_db.
NOISE_MARGIN_DB = 10.0      # the band ends this far above the measured noise
MIN_VOICE_BAND_DB = 6.0     # the band is never narrower than this
CUT_MARGIN_MS = 50          # the cut starts and ends this far outside the voice

# --- The check rows ----------------------------------------------------------
MIN_LEVEL_DBFS = -30.0      # (b) refuse below this
# The noise refuses nothing. Under this signal-to-noise ratio the window
# shows the noise advice; the provider reads the limit from the [engine] key
# lead_word_noise_advice_db and uses this value when the key is absent.
NOISE_ADVICE_DB = 30.0
CLIP_ABS = 32767            # (d) refuse a cut with a sample at or above this
MAX_VOICED_MS = 1000        # (e) refuse above this
MIN_VOICED_MS = 250         # (f) refuse below this
MIN_LEAD_IN_MS = 50         # (g) refuse below this
MAX_REGIONS = 1             # (h) refuse above this
# A frame with no signal has no level; it counts as this, so every level is a
# finite number that a JSON file can hold.
FLOOR_DBFS = -120.0

# Row names, in the order the check runs them. Row (i) is the decode row.
# There is no row (c): the noise refuses nothing.
ROW_NO_SPEECH = "no_speech"
ROW_TOO_QUIET = "too_quiet"
ROW_CLIPPING = "clipping"
ROW_TOO_LONG = "too_long"
ROW_TOO_SHORT = "too_short"
ROW_STARTED_TOO_SOON = "started_too_soon"
ROW_MORE_THAN_ONE_WORD = "more_than_one_word"
ROW_DECODE = "decode"

REASONS = {
    ROW_NO_SPEECH: "No speech heard",
    ROW_TOO_QUIET: "Too quiet",
    ROW_CLIPPING: "The recording is too loud and clips",
    ROW_TOO_LONG: "The word is too long",
    ROW_TOO_SHORT: "The word is too short",
    ROW_STARTED_TOO_SOON: "You started too soon after the last sound",
    ROW_MORE_THAN_ONE_WORD: "More than one word heard",
}
REASON_NO_WORD_DECODED = "No word was recognized"
REASON_NOT_HELLO = "The word was not hello"

# The measures each row adds. A refusal carries the measures of the rows up to
# and including the row that refused, and no others. The noise is measured
# with the cut (it sets the voice band), so its keys come with row (b).
_ROW_MEASURES = (
    (ROW_NO_SPEECH, ("capture_ms", "regions")),
    (ROW_TOO_QUIET, ("lead_in_ms", "voiced_ms", "cut_ms", "level_dbfs",
                     "noise_dbfs", "snr_db")),
    (ROW_CLIPPING, ("clipped",)),
    (ROW_TOO_LONG, ()),
    (ROW_TOO_SHORT, ()),
    (ROW_STARTED_TOO_SOON, ()),
    (ROW_MORE_THAN_ONE_WORD, ()),
    (ROW_DECODE, ("decode",)),
)


def _samples(ms: int) -> int:
    return ms * SAMPLE_RATE // 1000


def _ms(samples: int) -> int:
    return samples * 1000 // SAMPLE_RATE


# ---------------------------------------------------------------------------
# Silero regions
# ---------------------------------------------------------------------------

def new_silero_vad(threshold: float):
    """A new Silero VAD with its own state, for the cut.

    The live VAD object of AudioProcessor must keep its state while the
    provider records (wh-first-sound-lost.23 item 3), so the cut never uses
    it. SileroVAD shares the one onnx session and keeps its own detector
    state, so this costs no second model load.
    """
    from shared_audio.silero_vad import SileroVAD

    return SileroVAD(threshold=threshold, sample_rate=SAMPLE_RATE)


def speech_regions(samples: np.ndarray, vad) -> list[tuple[int, int]]:
    """The speech regions of the capture as (start, end) sample pairs.

    The VAD gets each full 512-sample chunk once, in order, so it decides
    once per chunk. Runs of speech chunks are regions. Regions less than
    MERGE_GAP_MS apart merge, then regions shorter than MIN_REGION_MS are
    dropped. A last partial chunk is not given to the VAD: it is under 32 ms
    of the quiet tail.
    """
    pcm = np.ascontiguousarray(samples, dtype="<i2")
    flags = [
        bool(vad.is_speech(
            pcm[i * SILERO_CHUNK_SAMPLES:(i + 1) * SILERO_CHUNK_SAMPLES].tobytes()))
        for i in range(len(pcm) // SILERO_CHUNK_SAMPLES)
    ]
    regions: list[list[int]] = []
    for i, flag in enumerate(flags):
        if not flag:
            continue
        start = i * SILERO_CHUNK_SAMPLES
        end = start + SILERO_CHUNK_SAMPLES
        if regions and regions[-1][1] == start:
            regions[-1][1] = end
        else:
            regions.append([start, end])
    merged: list[list[int]] = []
    for region in regions:
        if merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):
            merged[-1][1] = region[1]
        else:
            merged.append(region)
    return [
        (start, end) for start, end in merged
        if end - start >= _samples(MIN_REGION_MS)
    ]


# ---------------------------------------------------------------------------
# Levels and the cut
# ---------------------------------------------------------------------------

def frame_levels_dbfs(samples: np.ndarray) -> np.ndarray:
    """The RMS level of each full 5 ms frame, in dBFS, from the first sample.

    A frame with no signal gets FLOOR_DBFS, and so does any level below it.
    """
    size = _samples(FRAME_MS)
    count = len(samples) // size
    if count == 0:
        return np.zeros(0, dtype=np.float64)
    frames = np.asarray(samples[:count * size], dtype=np.float64).reshape(count, size)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    with np.errstate(divide="ignore"):
        levels = 20.0 * np.log10(rms / 32768.0)
    return np.maximum(levels, FLOOR_DBFS)


@dataclass(frozen=True)
class WordCut:
    """Where the word sits in the capture, in samples."""

    voice_start: int
    voice_end: int
    start: int
    end: int


def _window(samples: np.ndarray, region: tuple[int, int]) -> tuple[int, int]:
    """The region plus WINDOW_PAD_MS on each side, limited to the capture."""
    pad = _samples(WINDOW_PAD_MS)
    return max(0, region[0] - pad), min(len(samples), region[1] + pad)


def noise_level_dbfs(
    samples: np.ndarray, regions: list[tuple[int, int]]
) -> float | None:
    """The noise of the capture: the median level of the 5 ms frames that lie
    wholly outside the window of every region, or None when there is none.

    The frames run from the first sample. The measure leaves out the windows,
    not the cut, so it does not depend on the cut whose voice band it sets.
    The capture keeps its raw lead-in before the word and about one second
    after it (CAPTURE_END_SILENCE_S), so frames outside the windows exist in
    an ordinary take.
    """
    levels = frame_levels_dbfs(samples)
    size = _samples(FRAME_MS)
    starts = np.arange(len(levels)) * size
    outside = np.ones(len(levels), dtype=bool)
    for region in regions:
        window_start, window_end = _window(samples, region)
        outside &= (starts + size <= window_start) | (starts >= window_end)
    if not outside.any():
        return None
    return float(np.median(levels[outside]))


def voice_band_db(peak_dbfs: float, noise_dbfs: float | None) -> float:
    """How far under the loudest frame a frame still counts as voice.

    band = min(VOICE_BAND_DB, peak - noise - NOISE_MARGIN_DB), and never
    below MIN_VOICE_BAND_DB. Without a noise measure the band is
    VOICE_BAND_DB. With noise more than VOICE_BAND_DB + NOISE_MARGIN_DB
    (50 dB) under the peak the band is VOICE_BAND_DB, so a quiet microphone
    (Ikon: 72.6 dB) gets the cut it always had. With noise nearer the peak
    (the Yoga laptop: 20 to 31 dB) the band ends NOISE_MARGIN_DB above the
    median noise level, so the noise frames on each side of the word are not
    voice. NOISE_MARGIN_DB is a margin over the frame-to-frame spread of a
    steady noise around its median.
    crewcut: the margin and the floor are rough values from one synthetic
    noise; measure the frame spread of real laptop microphones to set them.
    """
    if noise_dbfs is None:
        return VOICE_BAND_DB
    return max(MIN_VOICE_BAND_DB,
               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))


def cut_word(
    samples: np.ndarray, region: tuple[int, int],
    noise_dbfs: float | None = None,
) -> WordCut:
    """The cut of the word that sits in `region` of the capture.

    The window is the region plus WINDOW_PAD_MS on each side, limited to the
    capture. The voice start is the start of the first 5 ms frame of the
    window within the voice band of its loudest frame (voice_band_db, from
    `noise_dbfs`); the voice end is the end of the last such frame. The cut
    runs from CUT_MARGIN_MS before the voice start to CUT_MARGIN_MS after
    the voice end, limited to the capture. When the band is narrower than
    VOICE_BAND_DB, the cut also holds the whole region: Silero found speech
    there, and a narrow band can leave out a quiet start or tail of the word
    (the /h/ of "hello"). The voice start and the voice end do not change.
    """
    window_start, window_end = _window(samples, region)
    levels = frame_levels_dbfs(samples[window_start:window_end])
    peak = levels.max()
    band = voice_band_db(float(peak), noise_dbfs)
    voiced = np.flatnonzero(levels >= peak - band)
    frame = _samples(FRAME_MS)
    voice_start = window_start + int(voiced[0]) * frame
    voice_end = window_start + (int(voiced[-1]) + 1) * frame
    margin = _samples(CUT_MARGIN_MS)
    start = voice_start - margin
    end = voice_end + margin
    if band < VOICE_BAND_DB:
        start = min(start, region[0])
        end = max(end, region[1])
    return WordCut(
        voice_start=voice_start,
        voice_end=voice_end,
        start=max(0, start),
        end=min(len(samples), end),
    )


# ---------------------------------------------------------------------------
# The check rows (a) to (h)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Refusal:
    row: str
    reason: str


@dataclass(frozen=True)
class CheckResult:
    """The outcome of the check. `row` is None when the recording passed."""

    row: str | None
    reason: str
    measures: dict
    cut: np.ndarray | None

    @property
    def passed(self) -> bool:
        return self.row is None


def failed_row(measures: dict) -> Refusal | None:
    """The first of the rows (a) to (h) that the measures fail, or None.

    The rows compare the numbers in `measures` as they are. The noise
    measures (noise_dbfs, snr_db) refuse nothing: noise_advice reads them.
    """
    def refuse(row):
        return Refusal(row, REASONS[row])

    if measures["regions"] < 1:
        return refuse(ROW_NO_SPEECH)
    if measures["level_dbfs"] < MIN_LEVEL_DBFS:
        return refuse(ROW_TOO_QUIET)
    if measures["clipped"] >= 1:
        return refuse(ROW_CLIPPING)
    if measures["voiced_ms"] > MAX_VOICED_MS:
        return refuse(ROW_TOO_LONG)
    if measures["voiced_ms"] < MIN_VOICED_MS:
        return refuse(ROW_TOO_SHORT)
    if measures["lead_in_ms"] < MIN_LEAD_IN_MS:
        return refuse(ROW_STARTED_TOO_SOON)
    if measures["regions"] > MAX_REGIONS:
        return refuse(ROW_MORE_THAN_ONE_WORD)
    return None


def noise_advice(measures: dict, limit_db: float) -> bool:
    """True when the measured signal-to-noise ratio is under `limit_db`.

    A recording without the snr_db measure gets no advice.
    """
    snr = measures.get("snr_db")
    return snr is not None and snr < limit_db


def _measures_through(row: str, measures: dict) -> dict:
    """The measures of the rows up to and including `row`."""
    keep: list[str] = []
    for name, keys in _ROW_MEASURES:
        keep.extend(keys)
        if name == row:
            break
    return {key: value for key, value in measures.items() if key in keep}


def check_capture(
    samples: np.ndarray, regions: list[tuple[int, int]]
) -> CheckResult:
    """Cut the word out of the capture and run the rows (a) to (h).

    With several regions the cut is made around the first one; row (h)
    refuses the recording anyway unless an earlier row refuses it. The
    numbers in the measures are rounded to two decimals, and the rows
    compare the rounded numbers, so the measures are the record of what was
    decided.
    """
    capture = np.asarray(samples, dtype=np.int16)
    measures: dict = {
        "capture_ms": _ms(len(capture)),
        "regions": len(regions),
    }
    if not regions:
        return CheckResult(
            ROW_NO_SPEECH, REASONS[ROW_NO_SPEECH],
            _measures_through(ROW_NO_SPEECH, measures), None)

    noise = noise_level_dbfs(capture, regions)
    word = cut_word(capture, regions[0], noise)
    cut = capture[word.start:word.end].copy()
    level = round(float(frame_levels_dbfs(cut).max()), 2)
    measures.update(
        lead_in_ms=_ms(word.voice_start),
        voiced_ms=_ms(word.voice_end - word.voice_start),
        cut_ms=_ms(len(cut)),
        level_dbfs=level,
    )
    if noise is not None:
        noise = round(noise, 2)
        measures["noise_dbfs"] = noise
        measures["snr_db"] = round(level - noise, 2)
    measures["clipped"] = int(
        np.count_nonzero(np.abs(cut.astype(np.int32)) >= CLIP_ABS))

    refusal = failed_row(measures)
    if refusal is None:
        return CheckResult(None, "", measures, cut)
    return CheckResult(
        refusal.row, refusal.reason,
        _measures_through(refusal.row, measures), cut)


# ---------------------------------------------------------------------------
# Row (i): the decode
# ---------------------------------------------------------------------------

def decode_row_refusal(text: str | None) -> str | None:
    """The reason row (i) refuses the raw decode text, or None.

    The text must be exactly one run of letters, that run must be "hello"
    with the case ignored, and the removal rule of the join must leave no
    text.
    """
    runs = re.findall(_LEAD_WORD_RUN, text or "")
    if not runs:
        return REASON_NO_WORD_DECODED
    if len(runs) > 1:
        return REASONS[ROW_MORE_THAN_ONE_WORD]
    if runs[0].lower() != LEAD_WORD:
        return REASON_NOT_HELLO
    left, _found = remove_lead_word(text, LEAD_WORD)
    if left != "":
        return REASONS[ROW_MORE_THAN_ONE_WORD]
    return None


def cut_to_float(cut: np.ndarray) -> np.ndarray:
    """The cut as float32 samples, the way load_lead_word_audio reads a file."""
    return np.asarray(cut, dtype=np.int16).astype(np.float32) / np.float32(32768.0)


def decode_cut(engine, cut: np.ndarray) -> str:
    """Decode the cut alone with the engine, with no join and no removal.

    The engine may hold a lead word. _decode_plain never joins it, and this
    function reads the raw text before normalize_transcript. The audio
    thread calls it between utterances, so it does not use the recognizer at
    the same time as an utterance decode.
    """
    return engine._decode_plain(cut_to_float(cut))


def run_check(samples: np.ndarray, vad, decode) -> CheckResult:
    """The whole check: regions, rows (a) to (h), then row (i).

    `vad` is a Silero VAD object for the cut (see new_silero_vad). `decode`
    takes the int16 cut and returns the raw decode text; it runs only when
    rows (a) to (h) pass.
    """
    result = check_capture(samples, speech_regions(samples, vad))
    if not result.passed:
        return result
    text = decode(result.cut)
    measures = dict(result.measures)
    measures["decode"] = text
    reason = decode_row_refusal(text)
    if reason is None:
        return CheckResult(None, "", measures, result.cut)
    return CheckResult(ROW_DECODE, reason, measures, result.cut)


# ---------------------------------------------------------------------------
# The file and the log line
# ---------------------------------------------------------------------------

def write_wav(path, cut: np.ndarray) -> None:
    """Write the cut as WAV: 16 kHz, 1 channel, 16-bit PCM, no change to a sample.

    Same writer calls as shared_stt.engine_audio_saver.
    """
    with wave.open(str(Path(path)), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(np.asarray(cut, dtype="<i2").tobytes())


def format_log_line(result: CheckResult) -> str:
    """The one log line of a recording: the verdict and every measure.

    It holds no transcript text, except the decode of a passed recording,
    which is the word. A refused recording logs only the length of its decode.
    """
    if result.passed:
        verdict = "passed"
    else:
        verdict = f"refused ({result.row}): {result.reason}"
    fields = []
    for key, value in result.measures.items():
        if key == "decode":
            if result.passed:
                fields.append(f"decode={value!r}")
            else:
                fields.append(f"decode_chars={len(value)}")
        else:
            fields.append(f"{key}={value}")
    return "Training-word recording: " + " ".join([verdict] + fields)
