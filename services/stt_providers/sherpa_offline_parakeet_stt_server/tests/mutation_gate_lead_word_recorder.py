"""Mutation gate for lead_word_recorder.py (wh-first-sound-lost.23, acceptance item 31).

The code is the cut and the check of the training-word recording: the Silero
regions, the frame levels, the noise measure, the voice band, the cut, the
rows (a) to (h), the measures each row reports, the noise advice, the decode
row (i), the WAV writer and the log line. Row (c), the noise row, refuses
nothing since wh-training-word-noisy-reject, so it has no mutants. The tests are
tests/test_lead_word_recorder.py. The three timers and the lead-in of the
capture (LEAD_IN_MS, CAPTURE_END_SILENCE_S, CAPTURE_MAX_S, NO_SPEECH_TIMEOUT_S)
are constants of this file that the audio thread reads; the controller tests
pin them, so their mutations are in mutation_gate_lead_word_controller.py.

Run it as lead_word_gate_common.py says. EQUIVALENT_MUTANTS holds the mutants
that no test can fail for; `--equivalent` runs them and each must be reported
SURVIVED.
"""
from __future__ import annotations

from lead_word_gate_common import RECORDER, main, make

T = "tests/test_lead_word_recorder.py"
m = make(T, RECORDER)

# --- Test names ----------------------------------------------------------
ONE_REGION = "test_a_run_of_speech_chunks_is_one_region_in_samples"
VAD_ONCE = "test_the_vad_gets_each_512_sample_chunk_once_in_order"
PARTIAL = "test_a_partial_last_chunk_is_not_given_to_the_vad"
MERGE_9 = "test_regions_9_chunks_apart_merge"
APART_10 = "test_regions_10_chunks_apart_stay_apart"
KEEP_7 = "test_a_region_of_7_chunks_is_kept"
DROP_6 = "test_a_region_of_6_chunks_is_dropped"
MERGE_THEN_KEEP = "test_two_short_regions_that_merge_into_a_long_one_are_kept"
TO_END = "test_a_region_that_runs_to_the_end_of_the_capture_ends_at_the_last_full_chunk"
NO_SPEECH = "test_no_speech_gives_no_region"
NEW_VAD = "test_the_silero_vad_uses_a_new_instance_with_the_given_threshold"
CUT_EXACT = "test_the_cut_boundaries_to_the_sample"
WINDOW_START = "test_the_window_is_limited_to_the_capture_at_the_start"
CUT_START = "test_the_cut_is_limited_to_the_capture_at_the_start"
CUT_END = "test_the_cut_is_limited_to_the_capture_at_the_end"
MINUS_20 = "test_a_frame_20_db_below_the_peak_is_part_of_the_voice"
MINUS_40_PLUS = "test_a_frame_more_than_40_db_below_the_peak_is_not_voice"
INSIDE_40 = "test_a_frame_just_inside_the_40_db_band_is_voice"
OUTSIDE_40_1 = "test_a_frame_40_1_db_below_the_peak_is_not_voice"
CUT_UNCHANGED = "test_the_cut_holds_the_capture_samples_unchanged"
CUT_COPY = "test_the_cut_is_a_copy"
WAV_FILE = "test_the_file_is_16k_mono_16_bit_with_the_cut_samples"
WAV_LOADS = "test_load_lead_word_audio_accepts_the_written_file"
GOOD = "test_a_good_word_passes_with_every_measure"
JSON_VALUES = "test_measures_are_json_values"
ROW_A_SILENCE = "test_row_a_refuses_silence"
ROW_A_EMPTY = "test_row_a_refuses_an_empty_capture"
ROW_A_SHORT = "test_row_a_refuses_a_region_under_200_ms"
ROW_A_SEVEN = "test_row_a_passes_a_region_of_7_chunks"
ROW_B_QUIET = "test_row_b_refuses_a_quiet_word"
ROW_B_29 = "test_row_b_passes_a_word_at_minus_29_dbfs"
LOW_SNR_SAVED = "test_a_low_snr_word_that_passes_every_other_row_is_saved"
NO_NOISE_ROW = "test_no_row_names_the_noise"
ROW_D_POS = "test_row_d_refuses_one_sample_at_32767"
ROW_D_NEG = "test_row_d_refuses_one_sample_at_minus_32768"
ROW_D_32766 = "test_row_d_passes_samples_at_32766"
ROW_D_CUT_ONLY = "test_row_d_counts_only_samples_in_the_cut"
ROW_E_1005 = "test_row_e_refuses_1005_ms"
ROW_E_1000 = "test_row_e_passes_1000_ms"
ROW_F_245 = "test_row_f_refuses_245_ms"
ROW_F_250 = "test_row_f_passes_250_ms"
ROW_G_720 = "test_row_g_refuses_a_voice_start_at_720_samples"
ROW_G_800 = "test_row_g_passes_a_voice_start_at_800_samples"
ROW_H_TWO = "test_row_h_refuses_two_regions"
ROW_H_CLOSE = "test_row_h_passes_two_parts_under_300_ms_apart"
FIRST_ROW = "test_the_first_failing_row_is_the_one_reported"
LEVEL_LOUDEST = "test_level_is_the_loudest_frame_of_the_cut"
LEVEL_CLICK = "test_a_loud_sound_outside_the_word_does_not_set_the_level"
NOISE_MEDIAN = "test_noise_is_the_median_frame_level_outside_the_windows"
SNR_READ = "test_the_snr_measure_is_the_level_of_the_cut_minus_the_noise"
FR_GOOD = "test_a_good_set_of_measures_fails_no_row"
FR_LEVEL = "test_level_minus_30_2_refuses_and_minus_30_0_passes"
FR_LOW_SNR = "test_a_low_snr_fails_no_row"
FR_CLIP = "test_clipped_1_refuses_and_0_passes"
FR_LONG = "test_voiced_1001_refuses_and_1000_passes"
FR_SHORT = "test_voiced_249_refuses_and_250_passes"
FR_LEAD = "test_lead_in_49_refuses_and_50_passes"
FR_REGIONS_2 = "test_regions_2_refuses_and_1_passes"
FR_REGIONS_0 = "test_regions_0_refuses_as_no_speech"
FR_NO_NOISE = "test_a_missing_noise_measure_does_not_stop_the_other_rows"
FR_CONSTANTS = "test_every_limit_is_one_named_constant"
DEC_HELLO = "test_one_run_that_is_hello_passes"
DEC_REFUSED = "test_anything_else_is_refused_with_a_plain_reason"
DEC_NONE = "test_a_none_text_is_no_word"
DEC_AGREES = "test_the_rule_agrees_with_remove_lead_word_on_what_is_left"
RC_PASS = "test_a_decode_of_hello_passes_and_the_measures_hold_the_decode"
RC_OTHER = "test_another_word_is_refused_by_row_i"
RC_CUT_ONLY = "test_the_decode_gets_the_cut_not_the_whole_capture"
RC_NOT_RUN = "test_the_decode_does_not_run_when_an_earlier_row_refused"
RC_NO_DECODE = "test_a_refusal_by_an_earlier_row_has_no_decode_measure"
DC_ALONE = "test_the_check_decode_gets_the_cut_alone_while_the_engine_holds_a_word"
DC_LEAVES = "test_the_decode_leaves_the_lead_word_of_the_engine_alone"
DC_NO_REMOVAL = "test_the_decode_removes_no_word_from_the_text"
LOG_PASSED = "test_a_passed_line_holds_the_measures_and_the_decode"
LOG_REFUSED = "test_a_refused_line_names_the_row_and_holds_no_decode_text"
LOG_NO_DECODE = "test_a_refusal_before_the_decode_has_no_decode_field"
EXACT_40 = "test_a_frame_exactly_40_db_below_the_peak_is_voice"
WIN_AFTER = "test_the_window_reaches_200_ms_after_the_region_and_no_further"
WIN_BEFORE = "test_the_window_reaches_200_ms_before_the_region_and_no_further"
FULL_SCALE = "test_the_level_is_measured_against_a_full_scale_of_32768"
FLOOR_LEVELS = "test_digital_silence_has_the_floor_level_of_minus_120_dbfs"
FLOOR_NOISE = "test_a_silent_lead_in_gives_the_floor_as_the_noise_level"
NO_NOISE_AT_START = "test_a_capture_inside_the_window_has_no_noise_and_no_snr_measure"
SEVERAL = "test_when_several_rows_fail_the_first_is_reported"
# wh-training-word-noisy-reject: the voice band, the noise measure, the advice.
BAND_NONE = "test_no_noise_measure_keeps_the_40_db_band"
BAND_50 = "test_a_50_db_distance_keeps_the_40_db_band_and_49_db_narrows_it"
BAND_IKON = "test_the_ikon_distance_keeps_the_40_db_band"
BAND_FLOOR = "test_the_band_never_goes_below_its_floor"
LOW_SNR_CUT = "test_on_a_low_snr_capture_the_cut_leaves_out_the_noise_on_each_side"
LOW_SNR_CHECK = "test_check_capture_cuts_a_low_snr_word_without_the_noise"
HIGH_SNR_SAME = "test_a_high_snr_capture_keeps_the_40_db_band_and_the_same_cut"
NL_MEDIAN = "test_the_noise_is_the_median_of_the_frames_outside_every_window"
NL_HEAD = "test_without_frames_after_the_window_the_frames_before_it_decide"
NL_EVERY = "test_the_window_of_every_region_is_left_out"
NL_INSIDE = "test_no_frame_outside_the_window_gives_no_noise"
NL_EMPTY = "test_an_empty_capture_gives_no_noise"
NL_EDGE_START = "test_a_frame_across_the_window_start_is_left_out"
NL_EDGE_END = "test_a_frame_across_the_window_end_is_left_out"
ADV_LIMIT = "test_an_snr_under_the_limit_gives_the_advice_and_the_limit_does_not"
ADV_GIVEN = "test_the_limit_is_the_one_given"
ADV_NONE = "test_no_snr_measure_gives_no_advice"
RC_LOW_SNR = "test_a_low_snr_word_with_a_decode_of_hello_is_saved"
QUIET_START = "test_a_quiet_start_inside_the_region_is_not_cut_on_a_low_snr_capture"
QUIET_TAIL = "test_a_quiet_tail_inside_the_region_is_not_cut_on_a_low_snr_capture"
QUIET_NOISE_OUT = "test_a_low_snr_cut_leaves_out_the_noise_outside_the_region"
QUIET_LEAD = "test_a_quiet_start_does_not_change_the_voice_start_or_the_lead_in"
QUIET_40 = "test_with_the_40_db_band_the_cut_stops_at_the_margin_not_at_the_region"

MUTATIONS = [
    # ---- constants of the cut (both directions) -----------------------------
    m("merge-gap-shorter", "MERGE_GAP_MS = 300          # regions closer than this merge",
      "MERGE_GAP_MS = 280          # regions closer than this merge", [MERGE_9]),
    m("merge-gap-longer", "MERGE_GAP_MS = 300          # regions closer than this merge",
      "MERGE_GAP_MS = 330          # regions closer than this merge", [APART_10]),
    m("min-region-shorter", "MIN_REGION_MS = 200         # shorter regions are dropped",
      "MIN_REGION_MS = 170         # shorter regions are dropped", [DROP_6]),
    m("min-region-longer", "MIN_REGION_MS = 200         # shorter regions are dropped",
      "MIN_REGION_MS = 230         # shorter regions are dropped", [KEEP_7]),
    m("window-pad-shorter", "WINDOW_PAD_MS = 200         # window = region plus this on each side",
      "WINDOW_PAD_MS = 100         # window = region plus this on each side", [WIN_AFTER, WIN_BEFORE]),
    m("window-pad-longer", "WINDOW_PAD_MS = 200         # window = region plus this on each side",
      "WINDOW_PAD_MS = 300         # window = region plus this on each side", [WIN_AFTER, WIN_BEFORE]),
    m("frame-ms-longer", "FRAME_MS = 5", "FRAME_MS = 10", [CUT_EXACT]),
    m("frame-ms-shorter", "FRAME_MS = 5", "FRAME_MS = 4", [CUT_EXACT]),
    m("voice-band-narrower", "VOICE_BAND_DB = 40          # voice = frames within this of the loudest frame",
      "VOICE_BAND_DB = 39          # voice = frames within this of the loudest frame", [INSIDE_40]),
    m("voice-band-wider", "VOICE_BAND_DB = 40          # voice = frames within this of the loudest frame",
      "VOICE_BAND_DB = 41          # voice = frames within this of the loudest frame", [OUTSIDE_40_1]),
    m("noise-margin-smaller", "NOISE_MARGIN_DB = 10.0      # the band ends this far above the measured noise",
      "NOISE_MARGIN_DB = 9.0       # the band ends this far above the measured noise", [BAND_50]),
    m("noise-margin-larger", "NOISE_MARGIN_DB = 10.0      # the band ends this far above the measured noise",
      "NOISE_MARGIN_DB = 11.0      # the band ends this far above the measured noise", [BAND_50]),
    m("min-band-smaller", "MIN_VOICE_BAND_DB = 6.0     # the band is never narrower than this",
      "MIN_VOICE_BAND_DB = 5.0     # the band is never narrower than this", [BAND_FLOOR]),
    m("min-band-larger", "MIN_VOICE_BAND_DB = 6.0     # the band is never narrower than this",
      "MIN_VOICE_BAND_DB = 7.0     # the band is never narrower than this", [BAND_FLOOR]),
    m("cut-margin-shorter", "CUT_MARGIN_MS = 50          # the cut starts and ends this far outside the voice",
      "CUT_MARGIN_MS = 40          # the cut starts and ends this far outside the voice", [CUT_EXACT]),
    m("cut-margin-longer", "CUT_MARGIN_MS = 50          # the cut starts and ends this far outside the voice",
      "CUT_MARGIN_MS = 60          # the cut starts and ends this far outside the voice", [CUT_EXACT]),
    m("silero-chunk-size-changed", "SILERO_CHUNK_SAMPLES = 512", "SILERO_CHUNK_SAMPLES = 256",
      [VAD_ONCE]),
    m("sample-rate-changed", "SAMPLE_RATE = 16000", "SAMPLE_RATE = 8000", [CUT_EXACT]),

    # ---- constants of the rows (both directions) ----------------------------
    m("min-level-lower", "MIN_LEVEL_DBFS = -30.0      # (b) refuse below this",
      "MIN_LEVEL_DBFS = -31.0      # (b) refuse below this", [FR_LEVEL]),
    m("min-level-higher", "MIN_LEVEL_DBFS = -30.0      # (b) refuse below this",
      "MIN_LEVEL_DBFS = -29.0      # (b) refuse below this", [FR_LEVEL]),
    m("advice-limit-lower", "NOISE_ADVICE_DB = 30.0", "NOISE_ADVICE_DB = 29.0", [FR_CONSTANTS]),
    m("advice-limit-higher", "NOISE_ADVICE_DB = 30.0", "NOISE_ADVICE_DB = 31.0", [FR_CONSTANTS]),
    m("clip-abs-lower", "CLIP_ABS = 32767            # (d) refuse a cut with a sample at or above this",
      "CLIP_ABS = 32766            # (d) refuse a cut with a sample at or above this", [ROW_D_32766]),
    m("clip-abs-higher", "CLIP_ABS = 32767            # (d) refuse a cut with a sample at or above this",
      "CLIP_ABS = 32768            # (d) refuse a cut with a sample at or above this", [ROW_D_POS]),
    m("max-voiced-lower", "MAX_VOICED_MS = 1000        # (e) refuse above this",
      "MAX_VOICED_MS = 999         # (e) refuse above this", [ROW_E_1000]),
    m("max-voiced-higher", "MAX_VOICED_MS = 1000        # (e) refuse above this",
      "MAX_VOICED_MS = 1001        # (e) refuse above this", [FR_LONG]),
    m("min-voiced-lower", "MIN_VOICED_MS = 250         # (f) refuse below this",
      "MIN_VOICED_MS = 249         # (f) refuse below this", [FR_SHORT]),
    m("min-voiced-higher", "MIN_VOICED_MS = 250         # (f) refuse below this",
      "MIN_VOICED_MS = 251         # (f) refuse below this", [FR_SHORT]),
    m("min-lead-in-lower", "MIN_LEAD_IN_MS = 50         # (g) refuse below this",
      "MIN_LEAD_IN_MS = 49         # (g) refuse below this", [FR_LEAD]),
    m("min-lead-in-higher", "MIN_LEAD_IN_MS = 50         # (g) refuse below this",
      "MIN_LEAD_IN_MS = 51         # (g) refuse below this", [FR_LEAD]),
    m("max-regions-lower", "MAX_REGIONS = 1             # (h) refuse above this",
      "MAX_REGIONS = 0             # (h) refuse above this", [FR_REGIONS_2]),
    m("max-regions-higher", "MAX_REGIONS = 1             # (h) refuse above this",
      "MAX_REGIONS = 2             # (h) refuse above this", [FR_REGIONS_2]),
    m("floor-dbfs-higher", "FLOOR_DBFS = -120.0", "FLOOR_DBFS = -100.0", [FLOOR_LEVELS, FLOOR_NOISE]),
    m("floor-dbfs-lower", "FLOOR_DBFS = -120.0", "FLOOR_DBFS = -140.0", [FLOOR_LEVELS, FLOOR_NOISE]),

    # ---- speech_regions -----------------------------------------------------
    m("regions-include-partial-chunk",
      "        for i in range(len(pcm) // SILERO_CHUNK_SAMPLES)",
      "        for i in range(len(pcm) // SILERO_CHUNK_SAMPLES + 1)", [PARTIAL]),
    m("regions-skip-last-full-chunk",
      "        for i in range(len(pcm) // SILERO_CHUNK_SAMPLES)",
      "        for i in range(len(pcm) // SILERO_CHUNK_SAMPLES - 1)", [TO_END]),
    m("regions-chunk-offset-wrong",
      "            pcm[i * SILERO_CHUNK_SAMPLES:(i + 1) * SILERO_CHUNK_SAMPLES].tobytes()))",
      "            pcm[i * SILERO_CHUNK_SAMPLES:(i + 1) * SILERO_CHUNK_SAMPLES + 1].tobytes()))",
      [VAD_ONCE]),
    m("regions-flag-inverted", "        if not flag:\n            continue",
      "        if flag:\n            continue", [ONE_REGION]),
    m("regions-adjacent-test-inverted", "        if regions and regions[-1][1] == start:",
      "        if regions and regions[-1][1] != start:", [APART_10]),
    m("regions-end-is-start", "        end = start + SILERO_CHUNK_SAMPLES",
      "        end = start", [ONE_REGION]),
    m("regions-merge-never", "        if merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):",
      "        if False and merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):",
      [MERGE_9]),
    m("regions-merge-always", "        if merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):",
      "        if merged:", [APART_10]),
    m("regions-merge-gap-from-start",
      "        if merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):",
      "        if merged and region[0] - merged[-1][0] < _samples(MERGE_GAP_MS):", [MERGE_9]),
    m("regions-merge-keeps-first-end", "            merged[-1][1] = region[1]",
      "            merged[-1][1] = merged[-1][1]", [MERGE_THEN_KEEP]),
    m("regions-min-length-from-end", "        if end - start >= _samples(MIN_REGION_MS)",
      "        if end >= _samples(MIN_REGION_MS)", [DROP_6]),
    m("regions-min-length-inverted", "        if end - start >= _samples(MIN_REGION_MS)",
      "        if end - start < _samples(MIN_REGION_MS)", [KEEP_7]),
    m("regions-dtype-changed", '    pcm = np.ascontiguousarray(samples, dtype="<i2")',
      '    pcm = np.ascontiguousarray(samples, dtype="<i4")', [VAD_ONCE]),
    m("regions-return-tuples-dropped", "        (start, end) for start, end in merged",
      "        (end, start) for start, end in merged", [ONE_REGION]),

    # ---- frame_levels_dbfs --------------------------------------------------
    m("levels-empty-returns-none", "        return np.zeros(0, dtype=np.float64)",
      "        return np.zeros(1, dtype=np.float64)", [NL_EMPTY]),
    m("levels-partial-frame-counted", "    count = len(samples) // size",
      "    count = -(-len(samples) // size)", [CUT_EXACT]),
    m("levels-no-sqrt", "    rms = np.sqrt(np.mean(frames * frames, axis=1))",
      "    rms = np.mean(frames * frames, axis=1)", [LEVEL_LOUDEST]),
    m("levels-sum-for-mean", "    rms = np.sqrt(np.mean(frames * frames, axis=1))",
      "    rms = np.sqrt(np.sum(frames * frames, axis=1))", [LEVEL_LOUDEST]),
    m("levels-ten-log", "        levels = 20.0 * np.log10(rms / 32768.0)",
      "        levels = 10.0 * np.log10(rms / 32768.0)", [LEVEL_LOUDEST]),
    m("levels-full-scale-32767", "        levels = 20.0 * np.log10(rms / 32768.0)",
      "        levels = 20.0 * np.log10(rms / 32767.0)", [FULL_SCALE]),
    m("levels-floor-as-ceiling", "    return np.maximum(levels, FLOOR_DBFS)",
      "    return np.minimum(levels, FLOOR_DBFS)", [LEVEL_LOUDEST]),
    m("levels-no-floor", "    return np.maximum(levels, FLOOR_DBFS)",
      "    return levels", [FLOOR_LEVELS, FLOOR_NOISE]),
    m("levels-int16-overflow",
      "    frames = np.asarray(samples[:count * size], dtype=np.float64).reshape(count, size)",
      "    frames = np.asarray(samples[:count * size], dtype=np.int16).reshape(count, size)",
      [LEVEL_LOUDEST]),

    # ---- cut_word -----------------------------------------------------------
    m("window-start-unlimited",
      "    return max(0, region[0] - pad), min(len(samples), region[1] + pad)",
      "    return region[0] - pad, min(len(samples), region[1] + pad)", [WINDOW_START]),
    m("window-no-pad-at-start",
      "    return max(0, region[0] - pad), min(len(samples), region[1] + pad)",
      "    return max(0, region[0]), min(len(samples), region[1] + pad)", [WIN_BEFORE]),
    m("window-no-pad-at-end",
      "    return max(0, region[0] - pad), min(len(samples), region[1] + pad)",
      "    return max(0, region[0] - pad), min(len(samples), region[1])", [WIN_AFTER]),
    m("voice-threshold-from-minimum", "    peak = levels.max()",
      "    peak = levels.min()", [MINUS_40_PLUS]),
    m("voice-threshold-above",
      "    voiced = np.flatnonzero(levels >= peak - band)",
      "    voiced = np.flatnonzero(levels >= peak + band)", [CUT_EXACT]),
    m("voice-threshold-strict",
      "    voiced = np.flatnonzero(levels >= peak - band)",
      "    voiced = np.flatnonzero(levels > peak - band)", [EXACT_40]),
    m("cut-ignores-the-noise",
      "    band = voice_band_db(float(peak), noise_dbfs)",
      "    band = voice_band_db(float(peak), None)",
      [LOW_SNR_CUT, LOW_SNR_CHECK]),

    # ---- voice_band_db ------------------------------------------------------
    m("band-without-noise-is-the-floor", "        return VOICE_BAND_DB",
      "        return MIN_VOICE_BAND_DB", [BAND_NONE]),
    m("band-without-floor",
      "    return max(MIN_VOICE_BAND_DB,\n               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      "    return (\n               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      [BAND_FLOOR]),
    m("band-without-ceiling",
      "               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      "               (peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      [BAND_IKON, HIGH_SNR_SAME]),
    m("band-margin-added",
      "               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      "               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs + NOISE_MARGIN_DB))", [BAND_50]),
    m("band-ignores-the-noise",
      "               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      "               min(VOICE_BAND_DB, peak_dbfs - NOISE_MARGIN_DB))", [BAND_50]),
    m("band-noise-added",
      "               min(VOICE_BAND_DB, peak_dbfs - noise_dbfs - NOISE_MARGIN_DB))",
      "               min(VOICE_BAND_DB, peak_dbfs + noise_dbfs - NOISE_MARGIN_DB))", [BAND_50]),

    # ---- noise_level_dbfs ---------------------------------------------------
    m("noise-only-the-first-window",
      "    for region in regions:\n        window_start, window_end = _window(samples, region)",
      "    for region in regions[:1]:\n        window_start, window_end = _window(samples, region)",
      [NL_EVERY]),
    m("noise-inside-the-windows",
      "        outside &= (starts + size <= window_start) | (starts >= window_end)",
      "        outside &= ~((starts + size <= window_start) | (starts >= window_end))", [NL_MEDIAN]),
    m("noise-only-before-the-window",
      "        outside &= (starts + size <= window_start) | (starts >= window_end)",
      "        outside &= (starts + size <= window_start)", [NL_MEDIAN]),
    m("noise-only-after-the-window",
      "        outside &= (starts + size <= window_start) | (starts >= window_end)",
      "        outside &= (starts >= window_end)", [NL_HEAD]),
    m("noise-counts-the-frame-across-the-window-start",
      "        outside &= (starts + size <= window_start) | (starts >= window_end)",
      "        outside &= (starts < window_start) | (starts >= window_end)", [NL_EDGE_START]),
    m("noise-counts-the-frame-across-the-window-end",
      "        outside &= (starts + size <= window_start) | (starts >= window_end)",
      "        outside &= (starts + size <= window_start) | (starts + size > window_end)", [NL_EDGE_END]),
    m("noise-window-is-the-region",
      "        window_start, window_end = _window(samples, region)\n        outside",
      "        window_start, window_end = region\n        outside", [NL_INSIDE, NL_EDGE_START]),
    m("noise-frames-start-one-frame-late", "    starts = np.arange(len(levels)) * size",
      "    starts = (np.arange(len(levels)) + 1) * size", [NL_EDGE_START, NL_EDGE_END]),
    m("noise-none-check-dropped", "    if not outside.any():\n        return None",
      "    if False:\n        return None", [NL_INSIDE]),
    m("noise-is-mean", "    return float(np.median(levels[outside]))",
      "    return float(np.mean(levels[outside]))", [NL_MEDIAN, NOISE_MEDIAN]),
    m("noise-is-maximum", "    return float(np.median(levels[outside]))",
      "    return float(np.max(levels[outside]))", [NL_HEAD, NOISE_MEDIAN]),
    m("noise-is-minimum", "    return float(np.median(levels[outside]))",
      "    return float(np.min(levels[outside]))", [NL_MEDIAN, NOISE_MEDIAN]),
    m("voice-start-from-last-frame", "    voice_start = window_start + int(voiced[0]) * frame",
      "    voice_start = window_start + int(voiced[-1]) * frame", [CUT_EXACT]),
    m("voice-start-without-window-offset", "    voice_start = window_start + int(voiced[0]) * frame",
      "    voice_start = int(voiced[0]) * frame", [CUT_EXACT]),
    m("voice-end-without-last-frame", "    voice_end = window_start + (int(voiced[-1]) + 1) * frame",
      "    voice_end = window_start + int(voiced[-1]) * frame", [CUT_EXACT]),
    m("voice-end-from-first-frame", "    voice_end = window_start + (int(voiced[-1]) + 1) * frame",
      "    voice_end = window_start + (int(voiced[0]) + 1) * frame", [CUT_EXACT]),
    m("cut-start-unlimited", "        start=max(0, start),",
      "        start=start,", [CUT_START]),
    m("cut-start-no-margin", "    start = voice_start - margin",
      "    start = voice_start", [CUT_EXACT]),
    m("cut-end-unlimited", "        end=min(len(samples), end),",
      "        end=end,", [CUT_END]),
    m("cut-end-no-margin", "    end = voice_end + margin",
      "    end = voice_end", [CUT_EXACT]),
    m("cut-voice-start-field-wrong", "        voice_start=voice_start,\n        voice_end=voice_end,",
      "        voice_start=voice_end,\n        voice_end=voice_end,", [CUT_EXACT]),

    # ---- the cut holds the whole region when the band is narrow -------------
    m("cut-region-clamp-always", "    if band < VOICE_BAND_DB:", "    if True:", [QUIET_40]),
    m("cut-region-clamp-never", "    if band < VOICE_BAND_DB:", "    if False:",
      [QUIET_START, QUIET_TAIL, QUIET_LEAD]),
    m("cut-region-clamp-flipped", "    if band < VOICE_BAND_DB:", "    if band > VOICE_BAND_DB:",
      [QUIET_START, QUIET_TAIL, QUIET_LEAD]),
    m("cut-region-clamp-at-the-full-band", "    if band < VOICE_BAND_DB:",
      "    if band <= VOICE_BAND_DB:", [QUIET_40]),
    m("cut-region-start-takes-the-later", "        start = min(start, region[0])",
      "        start = max(start, region[0])", [QUIET_START, QUIET_LEAD]),
    m("cut-region-start-ignores-the-margin", "        start = min(start, region[0])",
      "        start = region[0]", [QUIET_TAIL]),
    m("cut-region-start-not-held", "        start = min(start, region[0])",
      "        pass", [QUIET_START, QUIET_LEAD]),
    m("cut-region-start-runs-to-the-capture", "        start = min(start, region[0])",
      "        start = 0", [QUIET_START, QUIET_NOISE_OUT]),
    m("cut-region-end-takes-the-earlier", "        end = max(end, region[1])",
      "        end = min(end, region[1])", [QUIET_TAIL, QUIET_START]),
    m("cut-region-end-ignores-the-margin", "        end = max(end, region[1])",
      "        end = region[1]", [QUIET_START]),
    m("cut-region-end-not-held", "        end = max(end, region[1])",
      "        pass", [QUIET_TAIL]),
    m("cut-region-end-runs-to-the-capture", "        end = max(end, region[1])",
      "        end = len(samples)", [QUIET_START, QUIET_NOISE_OUT]),
    m("cut-voice-start-field-is-the-cut-start",
      "        voice_start=voice_start,\n        voice_end=voice_end,",
      "        voice_start=start,\n        voice_end=voice_end,", [QUIET_START, QUIET_LEAD]),
    m("cut-voice-end-field-is-the-cut-end",
      "        voice_start=voice_start,\n        voice_end=voice_end,",
      "        voice_start=voice_start,\n        voice_end=end,", [QUIET_TAIL]),

    # ---- failed_row: the rows, their operators and their order --------------
    m("row-a-never", "    if measures[\"regions\"] < 1:", "    if measures[\"regions\"] < 0:",
      [FR_REGIONS_0]),
    m("row-a-at-one", "    if measures[\"regions\"] < 1:", "    if measures[\"regions\"] < 2:",
      [FR_GOOD]),
    m("row-b-inclusive", "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:",
      "    if measures[\"level_dbfs\"] <= MIN_LEVEL_DBFS:", [FR_LEVEL]),
    m("row-b-inverted", "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:",
      "    if measures[\"level_dbfs\"] > MIN_LEVEL_DBFS:", [FR_LEVEL]),
    m("row-d-strict", "    if measures[\"clipped\"] >= 1:", "    if measures[\"clipped\"] > 1:",
      [FR_CLIP]),
    m("row-d-never", "    if measures[\"clipped\"] >= 1:", "    if measures[\"clipped\"] >= 2:",
      [FR_CLIP]),
    m("row-e-inclusive", "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:",
      "    if measures[\"voiced_ms\"] >= MAX_VOICED_MS:", [FR_LONG]),
    m("row-e-inverted", "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:",
      "    if measures[\"voiced_ms\"] < MAX_VOICED_MS:", [FR_LONG]),
    m("row-f-inclusive", "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:",
      "    if measures[\"voiced_ms\"] <= MIN_VOICED_MS:", [FR_SHORT]),
    m("row-f-inverted", "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:",
      "    if measures[\"voiced_ms\"] > MIN_VOICED_MS:", [FR_SHORT]),
    m("row-g-inclusive", "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:",
      "    if measures[\"lead_in_ms\"] <= MIN_LEAD_IN_MS:", [FR_LEAD]),
    m("row-g-inverted", "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:",
      "    if measures[\"lead_in_ms\"] > MIN_LEAD_IN_MS:", [FR_LEAD]),
    m("row-h-inclusive", "    if measures[\"regions\"] > MAX_REGIONS:",
      "    if measures[\"regions\"] >= MAX_REGIONS:", [FR_REGIONS_2]),
    m("row-h-inverted", "    if measures[\"regions\"] > MAX_REGIONS:",
      "    if measures[\"regions\"] < MAX_REGIONS:", [FR_REGIONS_2]),
    m("row-b-names-row-d", "        return refuse(ROW_TOO_QUIET)", "        return refuse(ROW_CLIPPING)",
      [FR_LEVEL]),
    m("row-d-names-row-e", "        return refuse(ROW_CLIPPING)", "        return refuse(ROW_TOO_LONG)",
      [FR_CLIP]),
    m("row-e-names-row-f", "        return refuse(ROW_TOO_LONG)", "        return refuse(ROW_TOO_SHORT)",
      [FR_LONG]),
    m("row-f-names-row-e", "        return refuse(ROW_TOO_SHORT)", "        return refuse(ROW_TOO_LONG)",
      [FR_SHORT]),
    m("row-g-names-row-h", "        return refuse(ROW_STARTED_TOO_SOON)",
      "        return refuse(ROW_MORE_THAN_ONE_WORD)", [FR_LEAD]),
    m("row-h-names-row-g", "        return refuse(ROW_MORE_THAN_ONE_WORD)",
      "        return refuse(ROW_STARTED_TOO_SOON)", [FR_REGIONS_2]),
    m("row-order-b-after-d",
      "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:\n        return refuse(ROW_TOO_QUIET)\n"
      "    if measures[\"clipped\"] >= 1:\n        return refuse(ROW_CLIPPING)\n",
      "    if measures[\"clipped\"] >= 1:\n        return refuse(ROW_CLIPPING)\n"
      "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:\n        return refuse(ROW_TOO_QUIET)\n",
      [SEVERAL]),
    m("row-order-a-after-b",
      "    if measures[\"regions\"] < 1:\n        return refuse(ROW_NO_SPEECH)\n"
      "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:\n        return refuse(ROW_TOO_QUIET)\n",
      "    if measures[\"level_dbfs\"] < MIN_LEVEL_DBFS:\n        return refuse(ROW_TOO_QUIET)\n"
      "    if measures[\"regions\"] < 1:\n        return refuse(ROW_NO_SPEECH)\n",
      [SEVERAL]),
    m("row-order-d-after-e",
      "    if measures[\"clipped\"] >= 1:\n        return refuse(ROW_CLIPPING)\n"
      "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:\n        return refuse(ROW_TOO_LONG)\n",
      "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:\n        return refuse(ROW_TOO_LONG)\n"
      "    if measures[\"clipped\"] >= 1:\n        return refuse(ROW_CLIPPING)\n",
      [SEVERAL]),
    m("row-order-f-after-g",
      "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:\n        return refuse(ROW_TOO_SHORT)\n"
      "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:\n        return refuse(ROW_STARTED_TOO_SOON)\n",
      "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:\n        return refuse(ROW_STARTED_TOO_SOON)\n"
      "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:\n        return refuse(ROW_TOO_SHORT)\n",
      [SEVERAL]),
    m("row-order-g-after-h",
      "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:\n        return refuse(ROW_STARTED_TOO_SOON)\n"
      "    if measures[\"regions\"] > MAX_REGIONS:\n        return refuse(ROW_MORE_THAN_ONE_WORD)\n",
      "    if measures[\"regions\"] > MAX_REGIONS:\n        return refuse(ROW_MORE_THAN_ONE_WORD)\n"
      "    if measures[\"lead_in_ms\"] < MIN_LEAD_IN_MS:\n        return refuse(ROW_STARTED_TOO_SOON)\n",
      [SEVERAL]),
    m("row-order-h-first",
      "    if measures[\"regions\"] < 1:\n        return refuse(ROW_NO_SPEECH)\n",
      "    if measures[\"regions\"] > MAX_REGIONS:\n        return refuse(ROW_MORE_THAN_ONE_WORD)\n"
      "    if measures[\"regions\"] < 1:\n        return refuse(ROW_NO_SPEECH)\n",
      [SEVERAL]),
    m("refuse-reason-not-looked-up", "        return Refusal(row, REASONS[row])",
      "        return Refusal(row, row)", [ROW_B_QUIET]),

    # ---- _measures_through: the measures each row adds ----------------------
    m("through-keeps-all-rows", "        if name == row:\n            break",
      "        if name == row:\n            pass", [ROW_B_QUIET]),
    m("through-stops-before-row", "        keep.extend(keys)\n        if name == row:",
      "        if name == row:\n            break\n        keep.extend(keys)\n        if name == row:",
      [ROW_B_QUIET]),
    m("through-keeps-nothing", "    return {key: value for key, value in measures.items() if key in keep}",
      "    return {key: value for key, value in measures.items() if key not in keep}",
      [ROW_B_QUIET]),
    m("rows-table-no-speech-drops-regions",
      '    (ROW_NO_SPEECH, ("capture_ms", "regions")),',
      '    (ROW_NO_SPEECH, ("capture_ms",)),', [ROW_A_SILENCE]),
    m("rows-table-quiet-drops-level",
      '    (ROW_TOO_QUIET, ("lead_in_ms", "voiced_ms", "cut_ms", "level_dbfs",\n',
      '    (ROW_TOO_QUIET, ("lead_in_ms", "voiced_ms", "cut_ms",\n', [ROW_B_QUIET]),
    m("rows-table-quiet-drops-noise",
      '                     "noise_dbfs", "snr_db")),',
      '                     "snr_db")),', [ROW_B_QUIET]),
    m("rows-table-quiet-drops-snr",
      '                     "noise_dbfs", "snr_db")),',
      '                     "noise_dbfs")),', [ROW_B_QUIET]),

    # ---- noise_advice -------------------------------------------------------
    m("advice-inclusive", "    return snr is not None and snr < limit_db",
      "    return snr is not None and snr <= limit_db", [ADV_LIMIT]),
    m("advice-inverted", "    return snr is not None and snr < limit_db",
      "    return snr is not None and snr > limit_db", [ADV_LIMIT, ADV_GIVEN]),
    m("advice-fixed-limit", "    return snr is not None and snr < limit_db",
      "    return snr is not None and snr < NOISE_ADVICE_DB", [ADV_GIVEN]),
    m("advice-without-snr", "    return snr is not None and snr < limit_db",
      "    return snr is None or snr < limit_db", [ADV_NONE]),
    m("advice-reads-the-noise", '    snr = measures.get("snr_db")',
      '    snr = measures.get("noise_dbfs")', [ADV_LIMIT]),
    m("rows-table-clipping-drops-clipped", '    (ROW_CLIPPING, ("clipped",)),',
      '    (ROW_CLIPPING, ()),', [ROW_D_POS]),
]

# Mutants that no test can fail for. `--equivalent` runs them; each must be
# reported SURVIVED, and one that is caught is no longer equivalent.
EQUIVALENT_MUTANTS = [
    # Regions are runs of whole 512-sample chunks, so a length is a multiple of
    # 512, and 3200 samples (MIN_REGION_MS) is not: 3200 / 512 = 6.25.
    m("eq-min-region-inclusive", "        if end - start >= _samples(MIN_REGION_MS)",
      "        if end - start > _samples(MIN_REGION_MS)", [KEEP_7]),
    # The same holds for the merge gap: 4800 samples (MERGE_GAP_MS) is not a
    # multiple of 512, so a gap is never exactly 4800.
    m("eq-merge-gap-inclusive",
      "        if merged and region[0] - merged[-1][1] < _samples(MERGE_GAP_MS):",
      "        if merged and region[0] - merged[-1][1] <= _samples(MERGE_GAP_MS):", [APART_10]),
    # A run of adjacent speech chunks that is not extended becomes one region
    # per chunk. The merge step then joins them, because their gap is 0, which
    # is under MERGE_GAP_MS, and gives the same (start, end) pair.
    m("eq-regions-never-extend", "        if regions and regions[-1][1] == start:",
      "        if False and regions and regions[-1][1] == start:", [ONE_REGION]),
    # Slicing a numpy array past its end stops at the end, so a window end past
    # the capture gives the same frames as the limited one. The cut end is
    # limited on its own line (cut-end-unlimited).
    # noise_level_dbfs reads the same window end: frames past the capture do
    # not exist, so `starts >= window_end` keeps the same frames either way.
    m("eq-window-end-unlimited",
      "    return max(0, region[0] - pad), min(len(samples), region[1] + pad)",
      "    return max(0, region[0] - pad), region[1] + pad", [CUT_END]),
    # The measure keys of the first row matter only for a refusal by that row.
    # lead_in_ms exists only after the cut, and every row from (b) on already
    # keeps it, so listing it under row (a) changes no result.
    m("eq-rows-table-no-speech-adds-lead-in",
      '    (ROW_NO_SPEECH, ("capture_ms", "regions")),',
      '    (ROW_NO_SPEECH, ("capture_ms", "regions", "lead_in_ms")),', [ROW_A_SILENCE]),
    # _measures_through stops at the row it is given. It is called with a row of
    # (a) to (h) from check_capture, never with ROW_DECODE: run_check adds the
    # decode measure itself. The ROW_DECODE entry is never read.
    m("eq-rows-table-decode-drops-decode", '    (ROW_DECODE, ("decode",)),',
      '    (ROW_DECODE, ()),', [RC_OTHER]),
    # Rows (e) and (f) cannot both fail: voiced_ms above 1000 and below 250
    # exclude each other, so their order changes no result.
    m("eq-row-order-e-after-f",
      "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:\n        return refuse(ROW_TOO_LONG)\n"
      "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:\n        return refuse(ROW_TOO_SHORT)\n",
      "    if measures[\"voiced_ms\"] < MIN_VOICED_MS:\n        return refuse(ROW_TOO_SHORT)\n"
      "    if measures[\"voiced_ms\"] > MAX_VOICED_MS:\n        return refuse(ROW_TOO_LONG)\n",
      [SEVERAL]),
]


if __name__ == "__main__":
    main(MUTATIONS, EQUIVALENT_MUTANTS)
