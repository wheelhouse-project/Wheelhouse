"""Mutation gate for the check of lead_word_recorder.py (wh-first-sound-lost.23, item 31).

The second half of the recorder gate: check_capture (the measures it takes and
the cut it returns), row (i) of the decode, cut_to_float, decode_cut, run_check,
write_wav and format_log_line. The cut, the regions, the constants and the
rows of failed_row are in mutation_gate_lead_word_recorder.py. The gate is
split in two because one file would pass 180 mutations. This file takes the
builder and the test names from the first one.

Run it as lead_word_gate_common.py says.
"""
from __future__ import annotations

from lead_word_gate_common import main
from mutation_gate_lead_word_recorder import *  # noqa: F401,F403
from mutation_gate_lead_word_recorder import m  # noqa: F401

TWO_REGIONS = "test_with_two_regions_the_cut_is_made_around_the_first"
NOISE_EVERY = "test_check_capture_leaves_out_the_window_of_every_region"
ONE_FRAME = "test_a_lead_in_of_one_frame_still_gives_the_noise_and_snr_measures"
CUT_SCALE = "test_the_cut_is_scaled_by_32768_as_float32"
CUT_WRAP = "test_a_sample_above_127_is_not_wrapped"
CUT_UNCHANGED = "test_the_cut_holds_the_capture_samples_unchanged"

MUTATIONS = [
    # ---- check_capture ------------------------------------------------------
    m("check-capture-ms-of-region", '        "capture_ms": _ms(len(capture)),',
      '        "capture_ms": _ms(len(capture) - 1),', [GOOD]),
    m("check-regions-count-wrong", '        "regions": len(regions),',
      '        "regions": len(regions) + 1,', [GOOD]),
    m("check-no-regions-test-inverted", "    if not regions:\n        return CheckResult(",
      "    if regions:\n        return CheckResult(", [ROW_A_SILENCE]),
    m("check-no-speech-row-wrong", "            ROW_NO_SPEECH, REASONS[ROW_NO_SPEECH],\n            _measures_through(ROW_NO_SPEECH, measures), None)",
      "            ROW_TOO_QUIET, REASONS[ROW_NO_SPEECH],\n            _measures_through(ROW_NO_SPEECH, measures), None)",
      [ROW_A_SILENCE]),
    m("check-cut-around-last-region", "    word = cut_word(capture, regions[0], noise)",
      "    word = cut_word(capture, regions[-1], noise)", [TWO_REGIONS]),
    m("check-cut-without-the-noise", "    word = cut_word(capture, regions[0], noise)",
      "    word = cut_word(capture, regions[0])", [LOW_SNR_CHECK]),
    m("check-noise-of-the-first-region-only", "    noise = noise_level_dbfs(capture, regions)",
      "    noise = noise_level_dbfs(capture, regions[:1])", [NOISE_EVERY]),
    m("check-cut-from-voice-bounds", "    cut = capture[word.start:word.end].copy()",
      "    cut = capture[word.voice_start:word.voice_end].copy()", [CUT_UNCHANGED, GOOD]),
    m("check-cut-not-copied", "    cut = capture[word.start:word.end].copy()",
      "    cut = capture[word.start:word.end]", [CUT_COPY]),
    m("check-level-is-minimum", "    level = round(float(frame_levels_dbfs(cut).max()), 2)",
      "    level = round(float(frame_levels_dbfs(cut).min()), 2)", [LEVEL_LOUDEST]),
    m("check-level-is-mean", "    level = round(float(frame_levels_dbfs(cut).max()), 2)",
      "    level = round(float(frame_levels_dbfs(cut).mean()), 2)", [LEVEL_LOUDEST]),
    m("check-level-over-the-whole-capture",
      "    level = round(float(frame_levels_dbfs(cut).max()), 2)",
      "    level = round(float(frame_levels_dbfs(capture).max()), 2)", [LEVEL_CLICK]),
    m("check-level-rounded-to-one-place",
      "    level = round(float(frame_levels_dbfs(cut).max()), 2)",
      "    level = round(float(frame_levels_dbfs(cut).max()), 0)", [LEVEL_LOUDEST]),
    m("check-lead-in-from-cut-start", "        lead_in_ms=_ms(word.voice_start),",
      "        lead_in_ms=_ms(word.start),", [GOOD]),
    m("check-voiced-from-cut", "        voiced_ms=_ms(word.voice_end - word.voice_start),",
      "        voiced_ms=_ms(word.end - word.start),", [GOOD]),
    m("check-voiced-from-end-only", "        voiced_ms=_ms(word.voice_end - word.voice_start),",
      "        voiced_ms=_ms(word.voice_end),", [GOOD]),
    m("check-cut-ms-of-capture", "        cut_ms=_ms(len(cut)),",
      "        cut_ms=_ms(len(capture)),", [GOOD]),
    m("check-noise-measures-dropped", "    if noise is not None:\n        noise = round(noise, 2)",
      "    if False:\n        noise = round(noise, 2)", [ONE_FRAME, NOISE_MEDIAN]),
    m("check-noise-none-not-guarded", "    if noise is not None:\n        noise = round(noise, 2)",
      "    if True:\n        noise = round(noise or 0.0, 2)", [NO_NOISE_AT_START]),
    m("check-noise-not-rounded", "        noise = round(noise, 2)",
      "        noise = noise", [NOISE_MEDIAN]),
    m("check-noise-measure-dropped", '        measures["noise_dbfs"] = noise',
      '        pass', [NOISE_MEDIAN]),
    m("check-snr-reversed", '        measures["snr_db"] = round(level - noise, 2)',
      '        measures["snr_db"] = round(noise - level, 2)', [SNR_READ]),
    m("check-snr-not-rounded", '        measures["snr_db"] = round(level - noise, 2)',
      '        measures["snr_db"] = level - noise', [SNR_READ]),
    m("check-snr-is-level", '        measures["snr_db"] = round(level - noise, 2)',
      '        measures["snr_db"] = round(level, 2)', [SNR_READ]),
    m("check-clipped-strict", "        np.count_nonzero(np.abs(cut.astype(np.int32)) >= CLIP_ABS))",
      "        np.count_nonzero(np.abs(cut.astype(np.int32)) > CLIP_ABS))", [ROW_D_POS]),
    m("check-clipped-int16-abs", "        np.count_nonzero(np.abs(cut.astype(np.int32)) >= CLIP_ABS))",
      "        np.count_nonzero(np.abs(cut) >= CLIP_ABS))", [ROW_D_NEG]),
    m("check-clipped-over-capture", "        np.count_nonzero(np.abs(cut.astype(np.int32)) >= CLIP_ABS))",
      "        np.count_nonzero(np.abs(capture.astype(np.int32)) >= CLIP_ABS))", [ROW_D_CUT_ONLY]),
    m("check-clipped-counts-nothing-negative",
      "        np.count_nonzero(np.abs(cut.astype(np.int32)) >= CLIP_ABS))",
      "        np.count_nonzero(cut.astype(np.int32) >= CLIP_ABS))", [ROW_D_NEG]),
    m("check-pass-has-no-cut", "        return CheckResult(None, \"\", measures, cut)",
      "        return CheckResult(None, \"\", measures, None)", [GOOD]),
    m("check-refusal-reports-all-measures",
      "        refusal.row, refusal.reason,\n        _measures_through(refusal.row, measures), cut)",
      "        refusal.row, refusal.reason,\n        measures, cut)", [ROW_B_QUIET]),
    m("check-capture-dtype-changed", "    capture = np.asarray(samples, dtype=np.int16)",
      "    capture = np.asarray(samples, dtype=np.int8)", [GOOD]),

    # ---- row (i): the decode ------------------------------------------------
    m("decode-none-not-guarded", '    runs = re.findall(_LEAD_WORD_RUN, text or "")',
      "    runs = re.findall(_LEAD_WORD_RUN, text)", [DEC_NONE]),
    m("decode-no-runs-accepted", "    if not runs:\n        return REASON_NO_WORD_DECODED",
      "    if False:\n        return REASON_NO_WORD_DECODED", [DEC_REFUSED]),
    m("decode-no-runs-reason-wrong", "        return REASON_NO_WORD_DECODED",
      "        return REASON_NOT_HELLO", [DEC_REFUSED]),
    m("decode-two-runs-accepted", "    if len(runs) > 1:\n        return REASONS[ROW_MORE_THAN_ONE_WORD]",
      "    if len(runs) > 2:\n        return REASONS[ROW_MORE_THAN_ONE_WORD]", [DEC_REFUSED]),
    m("decode-one-run-refused", "    if len(runs) > 1:\n        return REASONS[ROW_MORE_THAN_ONE_WORD]",
      "    if len(runs) > 0:\n        return REASONS[ROW_MORE_THAN_ONE_WORD]", [DEC_HELLO]),
    m("decode-case-sensitive", "    if runs[0].lower() != LEAD_WORD:",
      "    if runs[0] != LEAD_WORD:", [DEC_HELLO]),
    m("decode-not-hello-accepted", "    if runs[0].lower() != LEAD_WORD:",
      "    if False:", [DEC_REFUSED]),
    m("decode-not-hello-reason-wrong", "        return REASON_NOT_HELLO",
      "        return REASON_NO_WORD_DECODED", [DEC_REFUSED]),
    m("decode-leftover-accepted", '    if left != "":', "    if False:", [DEC_REFUSED]),
    m("decode-leftover-always-refused", '    if left != "":', "    if True:", [DEC_HELLO]),
    m("decode-leftover-reason-wrong",
      '    if left != "":\n        return REASONS[ROW_MORE_THAN_ONE_WORD]',
      '    if left != "":\n        return REASON_NOT_HELLO', [DEC_REFUSED]),
    m("decode-removal-given-other-word", "    left, _found = remove_lead_word(text, LEAD_WORD)",
      '    left, _found = remove_lead_word("", LEAD_WORD)', [DEC_REFUSED]),
    m("decode-passes-nothing",
      "        return REASONS[ROW_MORE_THAN_ONE_WORD]\n    return None",
      "        return REASONS[ROW_MORE_THAN_ONE_WORD]\n    return REASON_NOT_HELLO", [DEC_HELLO]),

    # ---- cut_to_float, decode_cut, run_check --------------------------------
    m("float-full-scale-32767",
      "    return np.asarray(cut, dtype=np.int16).astype(np.float32) / np.float32(32768.0)",
      "    return np.asarray(cut, dtype=np.int16).astype(np.float32) / np.float32(32767.0)",
      [CUT_SCALE]),
    m("float-int16-overflow-wraps",
      "    return np.asarray(cut, dtype=np.int16).astype(np.float32) / np.float32(32768.0)",
      "    return np.asarray(cut, dtype=np.int8).astype(np.float32) / np.float32(32768.0)",
      [CUT_WRAP]),
    m("float-float64", "    return np.asarray(cut, dtype=np.int16).astype(np.float32) / np.float32(32768.0)",
      "    return np.asarray(cut, dtype=np.int16).astype(np.float64) / 32768.0", [DC_ALONE]),
    m("decode-cut-uses-recognize", "    return engine._decode_plain(cut_to_float(cut))",
      "    return engine._recognize(cut_to_float(cut))", [DC_NO_REMOVAL]),
    m("decode-cut-gives-raw-int16", "    return engine._decode_plain(cut_to_float(cut))",
      "    return engine._decode_plain(cut)", [DC_ALONE]),
    m("run-check-decodes-the-capture", "    text = decode(result.cut)",
      "    text = decode(samples)", [RC_CUT_ONLY]),
    m("run-check-decodes-after-refusal", "    if not result.passed:\n        return result",
      "    if False:\n        return result", [RC_NOT_RUN]),
    m("run-check-decode-measure-dropped", '    measures["decode"] = text',
      '    measures["decode"] = ""', [RC_PASS]),
    m("run-check-decode-always-passes", "    if reason is None:\n        return CheckResult(None, \"\", measures, result.cut)",
      "    if True:\n        return CheckResult(None, \"\", measures, result.cut)", [RC_OTHER]),
    m("run-check-decode-never-passes", "    if reason is None:\n        return CheckResult(None, \"\", measures, result.cut)",
      "    if False:\n        return CheckResult(None, \"\", measures, result.cut)", [RC_PASS]),
    m("run-check-decode-row-wrong", "    return CheckResult(ROW_DECODE, reason, measures, result.cut)",
      "    return CheckResult(ROW_DECODE, reason, measures, None)", [RC_OTHER]),
    m("run-check-uses-the-given-regions-only",
      "    result = check_capture(samples, speech_regions(samples, vad))",
      "    result = check_capture(samples, [])", [RC_PASS]),

    # ---- write_wav ----------------------------------------------------------
    m("wav-two-channels", "        wav.setnchannels(1)", "        wav.setnchannels(2)", [WAV_FILE]),
    m("wav-8-bit", "        wav.setsampwidth(2)", "        wav.setsampwidth(1)", [WAV_FILE]),
    m("wav-wrong-rate", "        wav.setframerate(SAMPLE_RATE)", "        wav.setframerate(8000)", [WAV_FILE]),
    m("wav-big-endian", '        wav.writeframes(np.asarray(cut, dtype="<i2").tobytes())',
      '        wav.writeframes(np.asarray(cut, dtype=">i2").tobytes())', [WAV_FILE]),
    m("wav-drops-the-last-sample", '        wav.writeframes(np.asarray(cut, dtype="<i2").tobytes())',
      '        wav.writeframes(np.asarray(cut[:-1], dtype="<i2").tobytes())', [WAV_FILE]),

    # ---- format_log_line ----------------------------------------------------
    m("log-passed-test-inverted", "    if result.passed:\n        verdict = \"passed\"",
      "    if not result.passed:\n        verdict = \"passed\"", [LOG_PASSED]),
    m("log-refusal-without-reason", '        verdict = f"refused ({result.row}): {result.reason}"',
      '        verdict = f"refused ({result.row})"', [LOG_REFUSED]),
    m("log-refusal-without-row", '        verdict = f"refused ({result.row}): {result.reason}"',
      '        verdict = f"refused: {result.reason}"', [LOG_REFUSED]),
    m("log-passed-decode-chars-only", "            if result.passed:\n                fields.append(f\"decode={value!r}\")",
      "            if False:\n                fields.append(f\"decode={value!r}\")", [LOG_PASSED]),
    m("log-refused-decode-text-logged", "            if result.passed:\n                fields.append(f\"decode={value!r}\")",
      "            if True:\n                fields.append(f\"decode={value!r}\")", [LOG_REFUSED]),
    m("log-refused-decode-length-wrong", '                fields.append(f"decode_chars={len(value)}")',
      '                fields.append(f"decode_chars={len(value) + 1}")', [LOG_REFUSED]),
    m("log-measure-skipped", '            fields.append(f"{key}={value}")',
      "            pass", [LOG_PASSED]),
    m("log-prefix-changed", '    return "Training-word recording: " + " ".join([verdict] + fields)',
      '    return "Training word recording: " + " ".join([verdict] + fields)', [LOG_PASSED]),
    m("log-fields-joined-with-comma", '    return "Training-word recording: " + " ".join([verdict] + fields)',
      '    return "Training-word recording: " + ", ".join([verdict] + fields)', [LOG_PASSED]),
]

EQUIVALENT_MUTANTS = [
    # The controller reads result.cut only when the check passed (to save the
    # candidate), and run_check reads it only after a pass; no code reads the
    # cut of a refused result, so dropping it changes no behaviour.
    m("check-refusal-drops-cut",
      "        refusal.row, refusal.reason,\n        _measures_through(refusal.row, measures), cut)",
      "        refusal.row, refusal.reason,\n        _measures_through(refusal.row, measures), None)", [RC_OTHER]),
    # check_capture's result is local to run_check, which returns a new
    # CheckResult holding the same dict; nothing reads the first result after
    # the "decode" key is written, so the copy is not observable.
    m("run-check-measures-mutated-in-place",
      "    measures = dict(result.measures)\n    measures[\"decode\"] = text",
      "    measures = result.measures\n    measures[\"decode\"] = text", [RC_NO_DECODE]),
]


if __name__ == "__main__":
    main(MUTATIONS, EQUIVALENT_MUTANTS)
