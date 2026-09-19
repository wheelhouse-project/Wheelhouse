"""Mutation gate for the wh-load-test-script guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared, after `uv sync`
there, with any python (the gate needs only the standard library); it
starts pytest through the service's own .venv interpreter, as a direct
child held in a Windows job object:

    python tests/mutation_gate_stt_load_test.py

What this measurement can get wrong is not being noisy -- it is being
confidently wrong and quiet about it. A verdict computed from a counter
nobody kept, a sentence called correct because two length numbers happened
to agree, a recording reused after the words changed, a config file left
rewritten: every one of those prints something that reads like an answer.
Most mutations below attack exactly that.

Discipline, per the mutation-gate skill: byte IO, patterns translated to each
file's own line endings, exactly-one-match required, compile check, expected
test names validated against real collection before the first mutation,
baseline required green, per-run timeout, suite-abort banner detected,
__pycache__ cleared with PYTHONDONTWRITEBYTECODE set, and the mutant write
itself inside the finally that restores, through a temporary file and
os.replace so no interrupt can leave the tree holding half a file, with a
restore that still refuses to overwrite a concurrent edit, and every pytest
run held in a job object so a timed-out run is ended as a whole tree and
waited for before the source goes back.
Pattern, compile, timeout and abort problems are reported as ERRORS, never as
verdicts; a run whose tree the gate could not hold or could not end stops
the sweep.

crewcut: the runner half of this file repeats
tests/mutation_gate_load_metrics.py, which is the house convention of one
self-contained gate per bead. To remove the duplication: lift the runner into
a tests/mutation_gate_runner.py taking the per-mutation service and test file
both gates already carry, and have both import it.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SHARED = Path(__file__).resolve().parents[1]
TOOL = SHARED / "tools" / "stt_load_test"

TESTS = "tests/test_stt_load_test.py"

SCRIPT = TOOL / "script.py"
LOGPARSE = TOOL / "logparse.py"
JUDGE = TOOL / "judge.py"
REPORT = TOOL / "report.py"
RECORD = TOOL / "record.py"
RUN = TOOL / "run.py"
PROCEDURE_DOC = (SHARED.parents[2] / "docs" / "testing"
                 / "stt-cpu-load-test-procedure.md")
# This file. Its own restore is guard-tested like any other behaviour, so
# the mutations that break it have to mutate this file. The running process
# has already loaded this module, so a mutation here reaches only the pytest
# subprocess, which imports it from source with __pycache__ cleared.
GATE_SELF = Path(__file__).resolve()

MUTATIONS = [
    # -- The fixed script has to stay fixed, and stay readable ----------
    {
        # Two runs are comparable only when they said the same words. A
        # sentence changed in the code and not in the document is the way
        # that stops being true without anyone noticing.
        "name": "sentences-diverge-from-the-procedure",
        "file": SCRIPT,
        "old": '    "November December January February March April",',
        "new": '    "November December January February March August",',
        "expect": ["test_the_six_sentences_are_the_ones_the_procedure_lists"],
    },
    {
        "name": "procedure-lists-a-different-sentence",
        "file": PROCEDURE_DOC,
        "old": "3. November December January February March April\n",
        "new": "3. November December January February March August\n",
        "expect": ["test_the_six_sentences_are_the_ones_the_procedure_lists"],
    },
    {
        # The gap only just clears the endpoint threshold. Under this test's
        # own load the endpoint decision is one of the things that gets
        # slow, so two sentences merge into one utterance and the
        # per-sentence join has nothing to join.
        "name": "gap-only-just-clears-the-endpoint-threshold",
        "file": SCRIPT,
        "old": "_GAP_MULTIPLE = 3.0",
        "new": "_GAP_MULTIPLE = 1.01",
        "expect": ["test_the_gap_leaves_room_for_a_late_endpoint_decision"],
    },
    {
        "name": "gap-shorter-than-the-endpoint-threshold",
        "file": SCRIPT,
        "old": "    return _GAP_MULTIPLE * float(endpoint_silence_ms) / 1000.0",
        "new": "    return 0.5 * float(endpoint_silence_ms) / 1000.0",
        "expect": ["test_the_gap_outlasts_the_provider_endpoint_silence",
                   "test_the_gap_leaves_room_for_a_late_endpoint_decision"],
    },
    {
        # Compare against the spoken words rather than what the provider
        # would write. Sentence four then reads as wrong on every clean run,
        # because the provider writes it as a numeral.
        "name": "expected-text-skips-the-provider-text-rules",
        "file": SCRIPT,
        "old": "    return apply_itn(normalize_transcript(sentence))",
        "new": "    return sentence",
        "expect": ["test_the_expected_text_is_what_the_recognizer_would_write"],
    },
    {
        "name": "expected-text-skips-only-the-itn-stage",
        "file": SCRIPT,
        "old": "def expected_transcript(sentence: str) -> str:",
        "new": "def expected_transcript(sentence: str) -> str:\n"
               "    return normalize_transcript(sentence)",
        "expect": ["test_the_expected_text_is_what_the_recognizer_would_write"],
    },

    # -- n/a is not zero -------------------------------------------------
    {
        # The single most dangerous reading in the whole tool: a counter the
        # provider never kept, reported as a counter that read zero.
        "name": "a-counter-never-kept-is-read-as-zero",
        "file": LOGPARSE,
        "old": "    if raw is None or raw == NOT_REPORTED:\n"
               "        return None\n"
               "    try:\n"
               "        return int(raw)",
        "new": "    if raw is None or raw == NOT_REPORTED:\n"
               "        return 0\n"
               "    try:\n"
               "        return int(raw)",
        "expect": ["test_a_counter_the_provider_does_not_report_is_not_zero",
                   "test_an_unavailable_window_is_marked_rather_than_zeroed"],
    },
    {
        "name": "an-unavailable-window-is-read-as-available",
        "file": LOGPARSE,
        "old": "        capture_available=values.get('capture') != 'unavailable',",
        "new": "        capture_available=True,",
        "expect": ["test_an_unavailable_window_is_marked_rather_than_zeroed"],
    },

    # -- The parser has to read the lines the provider actually writes ---
    {
        # text= holds a repr and can contain spaces. Reading it to the next
        # space truncates every real transcript to its first word, which
        # then fails to match and reports six wrong sentences.
        "name": "transcript-text-read-only-to-the-next-space",
        "file": LOGPARSE,
        "old": "_TEXT_FIELD = re.compile(r'(?:^|\\s)text=(.*)$')",
        "new": "_TEXT_FIELD = re.compile(r'(?:^|\\s)text=(\\S*)')",
        "expect": ["test_a_transcript_containing_spaces_survives_the_parse"],
    },
    {
        # wheelhouse.log carries every subsystem in the application. A
        # loosely matched tag reads another component's quoted text as a
        # measurement.
        "name": "measurement-tags-matched-anywhere-in-the-line",
        "file": LOGPARSE,
        "old": "_WINDOW = re.compile(r'(?:^|\\s)\\[load-diag\\] (window=.*)$')",
        "new": "_WINDOW = re.compile(r'(window=.*)$')",
        "expect": ["test_lines_from_the_rest_of_the_log_are_left_alone"],
    },
    {
        # The transcript tag matched as a prefix rather than whole. The
        # comment in logparse.py names this case by hand:
        # [vad_utt_stats_something_else] is a different line.
        "name": "transcript-tag-matched-as-a-prefix",
        "file": LOGPARSE,
        "old": "_TRANSCRIPT = re.compile(r'(?:^|\\s)\\[vad_utt_stats\\] (.*)$')",
        "new": "_TRANSCRIPT = re.compile(r'\\[vad_utt_stats(.*)$')",
        "expect": ["test_lines_from_the_rest_of_the_log_are_left_alone"],
    },
    {
        "name": "window-length-read-without-stripping-its-unit",
        "file": LOGPARSE,
        "old": "    return _optional_float(raw.removesuffix('s'))",
        "new": "    return _optional_float(raw)",
        "expect": ["test_the_window_line_becomes_numbers",
                   "test_an_unavailable_window_is_marked_rather_than_zeroed",
                   "test_lines_from_the_rest_of_the_log_are_left_alone"],
    },
    {
        "name": "outage-window-count-dropped",
        "file": LOGPARSE,
        "old": "            windows=int(values['windows']),",
        "new": "            windows=0,",
        "expect": ["test_the_outage_event_is_read_as_one_event"],
    },
    {
        "name": "per-utterance-engine-ratio-dropped",
        "file": LOGPARSE,
        "old": "            engine_ratio=_optional_float(values.get('engine_ratio')),",
        "new": "            engine_ratio=0.0,",
        "expect": ["test_the_per_utterance_line_becomes_numbers"],
    },

    # -- A length is not a transcript ------------------------------------
    {
        # The claim the whole privacy design rests on. Equal counts cannot
        # prove equal words, and a report that says "correct" here is
        # telling the reader something nobody measured.
        "name": "a-length-match-is-reported-as-correct",
        "file": JUDGE,
        "old": "                outcome, detail = LENGTH_MATCHES, (",
        "new": "                outcome, detail = CORRECT, (",
        "expect": [
            "test_a_redacted_transcript_of_the_right_size_is_not_called_correct"],
    },
    {
        "name": "redacted-comparison-ignores-the-word-count",
        "file": JUDGE,
        "old": "        return counts == (len(expected), len(expected.split()))",
        "new": "        return counts[0] == len(expected)",
        "expect": [
            "test_a_redacted_transcript_with_the_wrong_word_count_is_wrong"],
    },
    {
        "name": "the-redaction-placeholder-is-never-recognised",
        "file": JUDGE,
        "old": "_REDACTED = re.compile(r'\\A<redacted: (\\d+) chars, (\\d+) words>\\Z')",
        "new": "_REDACTED = re.compile(r'\\Ano such placeholder\\Z')",
        "expect": ["test_the_placeholder_format_is_the_one_redact_writes",
                   "test_a_redacted_transcript_of_the_right_size_is_not_called_correct"],
    },
    {
        # Pair by position. One cough then shifts every later sentence onto
        # the previous sentence's transcript, and one interruption is
        # reported as five failures.
        "name": "sentences-paired-with-utterances-by-position",
        "file": JUDGE,
        "old": "    paired: list[Optional[int]] = [None] * len(expected)\n"
               "    used: set[int] = set()\n"
               "    cursor = 0\n"
               "    for position, text in enumerate(expected):\n"
               "        for index in range(cursor, len(transcripts)):\n"
               "            if index in used:\n"
               "                continue\n"
               "            if _matches(transcripts[index].text, text):\n"
               "                paired[position] = index\n"
               "                used.add(index)\n"
               "                cursor = index + 1\n"
               "                break\n",
        "new": "    paired: list[Optional[int]] = [\n"
               "        i if i < len(transcripts) else None\n"
               "        for i in range(len(expected))]\n"
               "    used: set[int] = {i for i in paired if i is not None}\n",
        "expect": [
            "test_an_extra_utterance_does_not_shift_the_sentences_after_it"],
    },

    # -- The verdict rules the procedure states --------------------------
    {
        # The unavailable windows never found. Nothing then refuses a run
        # that measured nothing, and the gap beside every other verdict
        # counts zero missing windows however many there were.
        "name": "the-unavailable-windows-are-never-found",
        "file": JUDGE,
        "old": "    unavailable = [w for w in parsed.windows if not w.capture_available]",
        "new": "    unavailable = []",
        "expect": ["test_a_run_that_measured_nothing_earns_no_verdict",
                   "test_a_run_with_no_readings_at_all_is_still_refused",
                   "test_the_missing_windows_are_counted_on_the_verdict"],
    },
    {
        "name": "overflow-never-fires-callback-loss",
        "file": JUDGE,
        "old": "    callback_loss = overflow is not None and overflow > 0",
        "new": "    callback_loss = False",
        "expect": ["test_overflow_above_zero_is_callback_loss",
                   "test_overflow_beside_inference_lag_is_more_than_one"],
    },
    {
        # Count drops as a separate failure even when the recognizer is
        # lagging. The reader is then sent after a consumer-loop fix that
        # changes nothing, because the queue stopped draining for the reason
        # already named.
        "name": "drops-counted-as-a-failure-beside-inference-lag",
        "file": JUDGE,
        "old": "    consumer_loss = dropped and not inference_lag",
        "new": "    consumer_loss = dropped",
        "expect": ["test_drops_with_a_high_engine_ratio_are_inference_lag"],
    },
    {
        "name": "only-the-first-of-several-failures-is-named",
        "file": JUDGE,
        "old": "    if len(named) > 1:",
        "new": "    if len(named) > 99:",
        "expect": ["test_overflow_beside_inference_lag_is_more_than_one"],
    },
    {
        "name": "a-partly-counted-overflow-passes-as-clean",
        "file": JUDGE,
        "old": "    if overflow is None or overflow_partial:",
        "new": "    if overflow is None and overflow_partial:",
        "expect": ["test_a_counter_that_stops_partway_cannot_earn_neither"],
    },
    {
        "name": "neither-awarded-with-no-idle-reading-to-compare-against",
        "file": JUDGE,
        "old": "    if baseline_ratio is None:\n"
               "        evidence.append(\n"
               "            'the transcript was bad and no counter moved, but "
               "`neither` also '\n"
               "            'needs engine_ratio near its IDLE value and no "
               "baseline run was '\n"
               "            'given; run with --baseline first')\n"
               "        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)",
        "new": "    if baseline_ratio is None:\n"
               "        evidence.append('no baseline')\n"
               "        return RunVerdict(NEITHER, tuple(evidence), **gap)",
        "expect": [
            "test_neither_is_not_awarded_without_the_baseline_to_compare_against"],
    },
    {
        # A run in which nothing went wrong, reported as an unexplained
        # failure. The reader goes looking for a cause that is not there.
        "name": "a-clean-run-is-reported-as-neither",
        "file": JUDGE,
        "old": "    if not sentences.any_bad:\n"
               "        evidence.append('every sentence came back at the "
               "expected length or '\n"
               "                        'better, and no counter moved')\n"
               "        return RunVerdict(NO_FAILURE, tuple(evidence), **gap)",
        "new": "    if not sentences.any_bad:\n"
               "        evidence.append('clean')\n"
               "        return RunVerdict(NEITHER, tuple(evidence), **gap)",
        "expect": ["test_clean_numbers_and_a_clean_transcript_are_not_a_failure"],
    },

    # -- The template the operator reads ---------------------------------
    {
        "name": "a-template-field-is-dropped",
        "file": REPORT,
        "old": "    lines.append(_field(\n"
               "        'Sentences correct but late:',",
        "new": "    lines.append(_field(\n"
               "        'Sentences correct but slow:',",
        "expect": ["test_every_field_the_procedure_asks_for_is_filled"],
    },
    {
        # Take the fix away and leave the bare column. A label at or past
        # the column receives no padding, so its value starts at the very
        # next character; the only run-time-built label in the report is
        # longer than the column, and the render with no per-utterance
        # evidence read as one word.
        "name": "an-over-long-label-is-fused-to-its-value",
        "file": REPORT,
        "old": "    if len(label) >= _WIDTH:\n"
               "        return f'{label} {value}'\n"
               "    return f'{label:<{_WIDTH}}{value}'",
        "new": "    return f'{label:<{_WIDTH}}{value}'",
        "expect": [
            "test_an_over_long_label_keeps_its_value_a_separate_word",
            "test_a_run_with_no_per_utterance_lines_says_so_as_its_own_words"],
    },
    {
        "name": "the-evidence-behind-the-verdict-is-not-printed",
        "file": REPORT,
        "old": "    lines += [f'  - {line}' for line in verdict.evidence]",
        "new": "    lines += []",
        "expect": ["test_the_verdict_and_its_evidence_are_both_printed"],
    },
    {
        "name": "lateness-needs-a-ratio-no-run-reaches",
        "file": REPORT,
        "old": "_LATE_RATIO = 1.0",
        "new": "_LATE_RATIO = 100.0",
        "expect": [
            "test_a_correct_sentence_whose_utterance_lagged_is_reported_as_late",
            "test_the_late_join_is_on_the_utterance_number_not_the_position"],
    },
    {
        # Join the sentence to its numbers by position rather than by the
        # utterance number. The provider has been running since the
        # application started, so its numbering does not begin at one and
        # one sentence's lag is charged to another.
        "name": "lateness-joined-on-position-not-the-utterance-number",
        "file": REPORT,
        "old": "        ratio = ratios.get(result.utt)",
        "new": "        ratio = ratios.get(result.index)",
        "expect": [
            "test_the_late_join_is_on_the_utterance_number_not_the_position"],
    },
    {
        "name": "playback-underflows-are-not-printed",
        "file": REPORT,
        "old": "        _field('Playback underflows:',",
        "new": "        _field('Playback glitches (not printed):',",
        "expect": [
            "test_playback_underflows_are_printed_beside_the_capture_numbers"],
    },
    {
        # Drop the paragraph that says "length matches" is not "correct". A
        # reader who sees the outcome without it would reasonably assume the
        # words were compared.
        "name": "the-privacy-limit-is-not-stated-in-the-report",
        "file": REPORT,
        "old": "    if any(r.outcome == judge.LENGTH_MATCHES for r in sentences.results):",
        "new": "    if False:",
        "expect": ["test_the_report_says_when_the_words_were_never_in_the_log"],
    },

    # -- The cached recording --------------------------------------------
    {
        # Reuse whatever is in the cache without asking whether it is
        # there. The behaviour this protects has not changed; the
        # statement that produces it has. Reuse used to be decided by
        # comparing a stored description against the wanted one, and
        # this mutation removed the comparison. Since the recording's
        # name became a digest of everything it depends on, serving a
        # file built for other inputs is impossible by construction,
        # and the whole of the reuse decision is whether the file
        # exists. Dropping that check reports a reuse of a recording
        # that is not there, which is the surviving way to reuse
        # something the run cannot play.
        "name": "a-cached-recording-is-always-reused",
        "file": RECORD,
        "old": "    if not force and wav_path.is_file():",
        "new": "    if not force:",
        "expect": [
            "test_a_missing_recording_is_built_and_a_present_one_is_not"],
    },
    {
        # The recording's name is a digest of the whole manifest, and that
        # is what makes reuse a one-file question. Hash a hand-picked part
        # of it instead and two settings collide on one name, which brings
        # back exactly the hole .1.35 closed -- a run asks for one gap and
        # is handed audio built for another. The voice is the clean subset
        # to hash: it is the one field the 800 ms and the 600 ms requests
        # share, so they land on the same file.
        #
        # Reverting the name to a fixed constant would be the obvious
        # mutation and is the weaker one. There is no manifest write left
        # under a fixed name for the interrupt hook to attach to, so the
        # interrupted-publication test would error rather than fail, and a
        # mutation whose failure mode cannot be named proves less than one
        # whose failure mode can. Under this one the three tests below
        # fail on their own assertions: the per-key walk names the field
        # that stopped reaching the name, and the two publication tests
        # never see the rebuild they exist to interrupt.
        "name": "the-recording-name-hashes-only-part-of-the-manifest",
        "file": RECORD,
        "old": "        json.dumps(wanted, sort_keys=True)"
               ".encode('utf-8')).hexdigest()",
        "new": "        json.dumps({'voice': wanted['voice']}, "
               "sort_keys=True)\n"
               "        .encode('utf-8')).hexdigest()",
        "expect": [
            "test_the_name_changes_when_any_part_of_the_manifest_changes",
            "test_an_interrupted_publication_never_serves_the_wrong_recording",
            "test_a_reader_in_the_publication_window_gets_its_own_recording"],
    },
    {
        "name": "the-manifest-forgets-which-sentences-were-spoken",
        "file": RECORD,
        "old": "        'sentences': list(SENTENCES),",
        "new": "        'sentences': 'fixed',",
        "expect": ["test_a_changed_sentence_makes_the_cached_file_stale"],
    },
    {
        "name": "the-manifest-forgets-the-silence-between-sentences",
        "file": RECORD,
        "old": "        'gap_s': inter_sentence_gap_s(endpoint_silence_ms),\n"
               "        'lead_out_s': lead_out_s(endpoint_silence_ms),",
        "new": "        'gap_s': 0.0,\n"
               "        'lead_out_s': 0.0,",
        "expect": [
            "test_a_changed_endpoint_setting_makes_the_cached_file_stale"],
    },

    # -- Touching the config files ---------------------------------------
    {
        # The defect this gate's first red actually caught: \s matches the
        # newline under MULTILINE, so flipping a flag deleted the file's
        # final line ending.
        "name": "the-flag-edit-eats-the-line-ending",
        "file": RUN,
        "old": "        rf'^([^\\S\\n]*{re.escape(key)}[^\\S\\n]*=[^\\S\\n]*)"
               "(true|false)[^\\S\\n]*$',",
        "new": "        rf'^(\\s*{re.escape(key)}\\s*=\\s*)(true|false)\\s*$',",
        "expect": ["test_flipping_a_flag_leaves_every_other_line_alone"],
    },
    {
        # Edit the first of several matches. The wrong setting changes and
        # the run reports success -- the same failure a mutation gate has to
        # refuse when its own anchor is ambiguous.
        "name": "an-ambiguous-flag-edits-the-first-match",
        "file": RUN,
        "old": "    if len(found) != 1:\n"
               "        raise ValueError(\n"
               "            f'{key} appears {len(found)} times as a boolean; "
               "expected once')",
        "new": "    if not found:\n"
               "        return text",
        "expect": ["test_a_flag_that_appears_twice_is_refused",
                   "test_a_flag_that_is_not_there_is_refused"],
    },
    {
        # Put the old content back whatever the file now holds. On this
        # machine several sessions share one checkout, so that deletes
        # someone else's edit to tidy up after a test.
        "name": "the-restore-overwrites-a-concurrent-edit",
        "file": RUN,
        # Refreshed after ea303b9f split this guard into a read and a
        # comparison; the old one-line form matched nothing.
        "old": "        if current != self._written:",
        "new": "        if False:",
        "expect": ["test_a_restore_does_not_overwrite_someone_elses_edit"],
    },
    {
        "name": "the-endpoint-setting-in-the-config-is-ignored",
        "file": RUN,
        "old": "            and math.isfinite(value) and value > 0):\n"
               "        return float(value)",
        "new": "            and math.isfinite(value) and value < 0):\n"
               "        return float(value)",
        "expect": ["test_the_endpoint_setting_is_read_from_the_provider_config"],
    },
    {
        # `endpoint_silence_ms = true` is valid TOML and bool is a subclass
        # of int, so without this rejection the reader returns 1.0 and the
        # run leaves a 3 ms gap between sentences instead of 2.4 s.
        # isinstance(value, str) can never be true here -- the numeric check
        # on the same line already excluded it -- so this disables the bool
        # rejection without changing anything else.
        "name": "a-boolean-endpoint-setting-is-taken-as-a-number",
        "file": RUN,
        "old": "not isinstance(value, bool)",
        "new": "not isinstance(value, str)",
        "expect": ["test_a_setting_that_is_not_a_finite_number_falls_back"],
    },
    {
        # `inf` is valid TOML too, and it reaches time.sleep, which raises
        # OverflowError. nan is rejected by `value > 0` on its own, so this
        # weakening lets infinity through and nothing else.
        "name": "an-infinite-endpoint-setting-is-taken-as-a-number",
        "file": RUN,
        "old": "math.isfinite(value)",
        "new": "not math.isnan(value)",
        "expect": ["test_a_setting_that_is_not_a_finite_number_falls_back"],
    },
    {
        "name": "an-unreadable-config-falls-back-to-the-wrong-value",
        "file": RUN,
        "old": "    except (OSError, ValueError):\n"
               "        return float(DEFAULT_ENDPOINT_SILENCE_MS)",
        "new": "    except (OSError, ValueError):\n"
               "        return 600.0",
        "expect": [
            "test_a_config_that_cannot_be_read_falls_back_to_the_coded_default"],
    },

    {
        # The named checkout is ignored and the tool's own is used instead.
        # From a git worktree that flips a flag in a file the running
        # provider never reads, then waits for a log line in a file the
        # application never writes -- and reports neither as a failure.
        "name": "the-named-checkout-is-ignored",
        "file": RUN,
        "old": "                 else Path(repo_root).resolve())",
        "new": "                 else Path(__file__).resolve().parents[5])",
        "expect": ["test_a_named_checkout_replaces_every_path"],
    },

    # -- Reading the run's own slice of the log ---------------------------
    {
        "name": "a-rotated-log-is-not-noticed",
        "file": RUN,
        # Refreshed after c56ad222 made rotation an identity test as well
        # as a size test.
        "old": "        rotated = replaced or size < mark.size",
        "new": "        rotated = False",
        "expect": [
            "test_a_rotated_log_is_reported_rather_than_read_from_the_mark"],
    },
    {
        "name": "the-log-is-read-from-its-start-every-time",
        "file": RUN,
        # Refreshed after c56ad222 replaced the bare offset with mark.size.
        "old": "        start = 0 if rotated else mark.size",
        "new": "        start = 0",
        "expect": ["test_only_the_lines_written_after_the_mark_are_read"],
    },
    {
        "name": "the-baseline-is-never-written-to-disk",
        "file": RUN,
        # Refreshed after save_baseline started reporting whether it wrote,
        # then again after .1.25 added the finite half to the same test.
        "old": "    if ratio is None or not math.isfinite(ratio):\n"
               "        return False\n",
        "new": "    if True:\n        return False\n",
        # Every test this really fails, read off a run of it: nothing can
        # be written, so every test that saves before it reads goes down
        # with it.
        "expect": ["test_the_baseline_ratio_survives_a_round_trip",
                   "test_a_run_with_a_ratio_writes_one",
                   "test_a_baseline_is_read_back_when_it_matches_this_run",
                   "test_a_baseline_measured_against_another_checkout_is_"
                   "refused",
                   "test_a_baseline_measured_with_another_endpoint_setting_"
                   "is_refused",
                   "test_a_write_that_failed_leaves_no_earlier_calibration_"
                   "behind",
                   "test_an_ordinary_baseline_is_still_written_and_read",
                   "test_a_zero_baseline_is_still_written_and_read"],
    },
    # -- Pacing the person who speaks -------------------------------------
    {
        # Every sentence available at once. A speaker reads them straight
        # through, the provider never sees enough silence to close an
        # utterance, and six sentences arrive under one utt=N with nothing
        # left to join them to their own numbers.
        "name": "prompts-carry-no-silence-between-them",
        "file": SCRIPT,
        "old": "               silence_before_s=LEAD_IN_S if position == 0 else gap)",
        "new": "               silence_before_s=LEAD_IN_S if position == 0 else 0.0)",
        "expect": ["test_every_later_prompt_waits_out_the_endpoint_threshold"],
    },
    {
        # The lead-in is what gives the VAD a noise floor before any speech
        # arrives. Starting on the inter-sentence gap instead is shorter
        # than the lead-in whenever endpoint_silence_ms is 600.
        "name": "every-prompt-uses-the-inter-sentence-gap",
        "file": SCRIPT,
        "old": "        Prompt(index=position + 1, sentence=sentence,\n"
               "               silence_before_s=LEAD_IN_S if position == 0 else gap)",
        "new": "        Prompt(index=position + 1, sentence=sentence,\n"
               "               silence_before_s=gap)",
        "expect": ["test_the_first_prompt_waits_for_the_vad_to_hear_the_room"],
    },
    {
        # The sentence shown first and paced afterwards. The gap then falls
        # inside the time the speaker is reading it aloud, which is exactly
        # the silence it was supposed to create.
        "name": "the-sentence-is-shown-before-its-silence",
        "file": RUN,
        "old": "        out(f'    ... {prompt.silence_before_s:.1f}s of silence, please')\n"
               "        sleep(prompt.silence_before_s)\n"
               "        out(f'[{prompt.index} of {len(prompts)}] SAY THIS, then press Enter:')\n"
               "        out(f'    {prompt.sentence}')\n",
        "new": "        out(f'[{prompt.index} of {len(prompts)}] SAY THIS, then press Enter:')\n"
               "        out(f'    {prompt.sentence}')\n"
               "        out(f'    ... {prompt.silence_before_s:.1f}s of silence, please')\n"
               "        sleep(prompt.silence_before_s)\n",
        "expect": ["test_the_silence_is_slept_before_the_sentence_is_shown"],
    },
    {
        # No wait at all: the run walks the whole script in the time it
        # takes to print it, and the speaker is still on sentence one.
        "name": "the-run-never-waits-for-the-speaker",
        "file": RUN,
        "old": "        wait()\n",
        "new": "        pass\n",
        "expect": ["test_the_silence_is_slept_before_the_sentence_is_shown"],
    },
    {
        # Without the lead-out the sixth utterance can still be open when
        # the run stops reading the log, and sentence six reads as missing.
        "name": "the-last-sentence-gets-no-lead-out",
        "file": RUN,
        "old": "    sleep(lead_out_s)\n",
        "new": "    pass\n",
        "expect": ["test_the_last_sentence_is_given_time_to_reach_its_endpoint"],
    },
    {
        # Playback back on by default. On this machine that measures
        # nothing: ENABLE_AUDIO_SUPPRESSION stops WheelHouse listening
        # while the recording is playing.
        "name": "the-recorded-playback-is-on-by-default",
        "file": RUN,
        "old": "        '--playback', action='store_true',\n",
        "new": "        '--playback', action='store_true', default=True,\n",
        "expect": ["test_the_recorded_playback_is_off_unless_it_is_asked_for"],
    },

    # -- Stopping exactly what the run started ----------------------------
    {
        # The spinners held in a function-local list again. A Popen failure
        # partway through -- process creation under the pressure this test
        # manufactures -- discards every spinner already running, and the
        # caller stops an empty list.
        "name": "spinners-are-held-only-inside-the-function",
        "file": RUN,
        "old": "    for _ in range(count):\n"
               "        _HeldSpinner(\n"
               "            into, SPINNER_ARGV,\n"
               "            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
               "    return into\n",
        "new": "    started: list = []\n"
               "    for _ in range(count):\n"
               "        _HeldSpinner(\n"
               "            started, SPINNER_ARGV,\n"
               "            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
               "    into.extend(started)\n"
               "    return into\n",
        "expect": ["test_a_spinner_that_fails_to_start_does_not_orphan_the_others",
                   "test_an_interrupt_after_the_child_exists_still_holds_it"],
    },
    {
        # The pre-fix line itself, restored exactly: the child is appended
        # only once the whole of Popen.__init__ has returned. A Ctrl+C
        # arriving on the way out of the constructor -- after Windows has
        # made the process -- leaves a busy loop the caller never receives,
        # and neither Popen.__init__'s failure cleanup nor Popen.__del__
        # kills it. The class stays defined and unused, so what runs is
        # exactly the code this fix replaced.
        "name": "the-spinner-is-appended-after-the-constructor-returns",
        "file": RUN,
        "old": "    for _ in range(count):\n"
               "        _HeldSpinner(\n"
               "            into, SPINNER_ARGV,\n"
               "            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
               "    return into\n",
        "new": "    for _ in range(count):\n"
               "        into.append(subprocess.Popen(\n"
               "            SPINNER_ARGV,\n"
               "            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))\n"
               "    return into\n",
        "expect": ["test_an_interrupt_after_the_child_exists_still_holds_it"],
    },
    {
        # The registration moves out of _execute_child and back to the end
        # of __init__, which is the same window the local-variable "fix"
        # leaves open: the constructor has to return before the caller
        # holds anything, and the interrupt lands before it does.
        "name": "the-spinner-registers-only-once-its-constructor-is-done",
        "file": RUN,
        "old": "    def __init__(self, into: list, *args, **kwargs) -> None:\n"
               "        self._into = into\n"
               "        super().__init__(*args, **kwargs)\n"
               "\n"
               "    def _execute_child(self, *args, **kwargs) -> None:\n"
               "        super()._execute_child(*args, **kwargs)\n"
               "        self._into.append(self)\n",
        "new": "    def __init__(self, into: list, *args, **kwargs) -> None:\n"
               "        self._into = into\n"
               "        super().__init__(*args, **kwargs)\n"
               "        self._into.append(self)\n"
               "\n"
               "    def _execute_child(self, *args, **kwargs) -> None:\n"
               "        super()._execute_child(*args, **kwargs)\n",
        "expect": ["test_an_interrupt_after_the_child_exists_still_holds_it"],
    },
    {
        # Registering first and creating afterwards looks like the safer
        # order and is not: the caller's list then holds an object with no
        # child behind it, and stop_spinners has neither a pid nor a handle
        # to work with. Under the process-creation failure this tool
        # manufactures on purpose, that is the common case, not the rare
        # one.
        "name": "the-spinner-registers-before-its-child-exists",
        "file": RUN,
        "old": "        super()._execute_child(*args, **kwargs)\n"
               "        self._into.append(self)\n",
        "new": "        self._into.append(self)\n"
               "        super()._execute_child(*args, **kwargs)\n",
        "expect": ["test_a_spinner_that_fails_to_start_does_not_orphan_the_others"],
    },
    {
        # The registration goes into a copy, so the caller's own list stays
        # empty however the run ends. This is the whole defect wearing the
        # fix's shape: the class is there, the override is there, and the
        # spinners are still held by nobody the teardown can reach.
        "name": "the-spinner-registers-in-a-list-of-its-own",
        "file": RUN,
        "old": "        self._into = into\n",
        "new": "        self._into = list(into)\n",
        "expect": ["test_a_spinner_that_fails_to_start_does_not_orphan_the_others",
                   "test_a_started_spinner_is_held_by_the_caller",
                   "test_an_interrupt_after_the_child_exists_still_holds_it"],
    },
    {
        # A constructor failure is swallowed, so the run walks past the
        # machine refusing to make processes and reports on a load it never
        # applied. The caller never sees the OSError that says so.
        "name": "the-spinner-constructor-swallows-its-own-failure",
        "file": RUN,
        "old": "        self._into = into\n"
               "        super().__init__(*args, **kwargs)\n",
        "new": "        self._into = into\n"
               "        try:\n"
               "            super().__init__(*args, **kwargs)\n"
               "        except OSError:\n"
               "            pass\n",
        "expect": ["test_a_spinner_that_fails_to_start_does_not_orphan_the_others"],
    },
    {
        # The bare restore loop again: the first exception skips every
        # remaining flag and replaces the real failure with a traceback.
        #
        # The whole OSError arm goes, and the KeyboardInterrupt arm added
        # by the .1.21 fix stays. The earlier form replaced the try and
        # its first arm with if/elif, which left the second arm with no
        # try above it -- SyntaxError at run.py line 282. The pattern
        # still matched, so --check reported it clean; only the sweep's
        # compile step saw it. A mutant the interpreter rejects proves
        # nothing, and in a runner without that step it would read as
        # caught.
        "name": "a-failed-restore-stops-the-rest",
        "file": RUN,
        "old": """        try:
            restored = edit.restore()
        except OSError as error:
            interrupt = _say(
                out,
                f'[!] could not put {edit.path} back ({error}). Set '
                f'{edit.key} to {str(not edit.value).lower()} yourself.',
                interrupt)
            continue
        except KeyboardInterrupt as stop:""",
        "new": """        try:
            restored = edit.restore()
        except KeyboardInterrupt as stop:""",
        "expect": ["test_a_restore_that_fails_does_not_skip_the_others"],
    },
    {
        # The failure caught and swallowed. The flag is still flipped and
        # nobody is told which file or which value.
        "name": "a-failed-restore-says-nothing",
        "file": RUN,
        "old": "            interrupt = _say(\n"
               "                out,\n"
               "                f'[!] could not put {edit.path} back ({error}). Set '\n"
               "                f'{edit.key} to {str(not edit.value).lower()} yourself.',\n"
               "                interrupt)\n",
        "new": "            pass\n",
        "expect": ["test_a_restore_that_fails_does_not_skip_the_others"],
    },

    # -- One capture event counted once -----------------------------------
    {
        # The original defect: an utterance happens inside a window, so
        # adding the two lists charges every overflow and every drop twice.
        "name": "the-two-line-kinds-are-added-together",
        "file": JUDGE,
        "old": "    return max(measured), utterances_partial or windows_partial\n",
        "new": "    return sum(measured), utterances_partial or windows_partial\n",
        "expect": ["test_an_overflow_counted_by_both_lines_is_one_overflow",
                   "test_a_drop_counted_by_both_lines_is_one_drop"],
    },
    {
        # The window lines preferred outright, as the finding suggested. A
        # slice that starts mid-window reports 0 over an utterance line
        # reporting 48, and `callback loss` becomes `neither`.
        "name": "the-window-lines-are-preferred-outright",
        "file": JUDGE,
        "old": "    measured = [v for v in (from_utterances, from_windows) if v is not None]\n"
               "    if not measured:\n"
               "        return None, True\n"
               "    return max(measured), utterances_partial or windows_partial\n",
        "new": "    measured = [v for v in (from_utterances, from_windows) if v is not None]\n"
               "    if not measured:\n"
               "        return None, True\n"
               "    total = from_windows if from_windows is not None else from_utterances\n"
               "    return total, utterances_partial or windows_partial\n",
        "expect": ["test_a_window_line_the_slice_missed_cannot_erase_an_overflow"],
    },
    {
        # An n/a on the utterance lines ignored. The run then claims a clean
        # reading over a counter that stopped partway.
        "name": "an-unmeasured-utterance-line-is-ignored",
        "file": JUDGE,
        "old": "    return max(measured), utterances_partial or windows_partial\n",
        "new": "    return max(measured), windows_partial\n",
        "expect": [
            "test_an_utterance_that_counted_nothing_still_blocks_a_clean_reading"],
    },
    {
        # A kind of line the slice caught none of read as a counter that
        # stopped. Every window-less run is then undetermined, whatever the
        # utterance lines said.
        "name": "an-empty-line-list-reads-as-a-stopped-counter",
        "file": JUDGE,
        "old": "        return (None, False) if not values else _total(values)\n",
        "new": "        return _total(values)\n",
        "expect": ["test_a_slice_with_no_window_lines_can_still_read_clean"],
    },
    # ---- Reading the transcript from the line that reaches the log ----
    # The live run of 2026-08-30 is what these protect: the tool read a line
    # that cannot appear in wheelhouse.log, so every sentence read `missing`
    # in both runs and the whole per-sentence column was worthless.
    {
        # The [FINAL] line ignored, which is the defect the live run had.
        "name": "the-final-line-is-not-read",
        "file": LOGPARSE,
        "old": "            record = _read_final(match.group(1), match.group(2))\n",
        "new": "            record = None\n",
        "expect": [
            "test_the_final_line_is_read_as_a_transcript",
            "test_the_six_sentences_are_judged_from_final_lines_alone"],
    },
    {
        # The final_reason suffix left on the end of the transcript. Every
        # sentence carrying one then compares as wrong.
        "name": "the-final-reason-is-left-in-the-transcript",
        "file": LOGPARSE,
        "old": "    match = _FINAL_REASON.search(body)\n",
        "new": "    match = None\n",
        "expect": ["test_a_final_reason_is_kept_out_of_the_transcript"],
    },
    {
        # The quotes left on the text. The transcript then never equals what
        # the sentence expects, and every sentence reads wrong.
        "name": "the-quotes-are-left-on-the-transcript",
        "file": LOGPARSE,
        "old": "        return Transcript(utt=int(number), kind=kind, text=body[1:-1],\n",
        "new": "        return Transcript(utt=int(number), kind=kind, text=body,\n",
        "expect": [
            "test_the_final_line_is_read_as_a_transcript",
            "test_an_apostrophe_in_the_transcript_survives"],
    },
    {
        # The quoting requirement dropped. An unquoted body then loses a
        # character from each end and is reported as what was heard.
        "name": "an-unquoted-transcript-is-accepted",
        "file": LOGPARSE,
        "old": '    if len(body) < 2 or not (body.startswith("\'") and body.endswith("\'")):\n        return None\n',
        "new": "    if False:\n        return None\n",
        "expect": ["test_a_final_line_whose_text_is_not_quoted_is_refused"],
    },
    {
        # One utterance stored under two keys. Two records for one utterance
        # shift every later sentence in the alignment by one, which reports
        # the tail of the script missing.
        "name": "one-utterance-is-counted-twice",
        "file": LOGPARSE,
        "old": "        into[record.utt] = record\n",
        "new": "        into[len(into)] = record\n",
        "expect": ["test_one_utterance_is_counted_once_when_both_lines_appear"],
    },
    {
        # First line wins instead of the [FINAL] line. A log carrying both
        # then reports the text written before the final was sent.
        "name": "the-vad-line-wins-over-the-final-line",
        "file": LOGPARSE,
        "old": "    if existing is None or (record.source == FINAL_SOURCE\n"
               "                            and existing.source != FINAL_SOURCE):\n",
        "new": "    if existing is None:\n",
        "expect": ["test_the_final_line_wins_over_the_vad_line"],
    },
    # ---- A partly measured run is still judged ----
    # The strict rule refused exactly the loaded runs this tool exists to
    # measure, and told the reader to fix a microphone that was working.
    {
        # The old strict rule back: one unavailable window refuses the run.
        "name": "any-unavailable-window-refuses-the-run",
        "file": JUDGE,
        "old": "    if unavailable and len(unavailable) == len(parsed.windows):\n",
        "new": "    if unavailable:\n",
        "expect": ["test_some_unavailable_windows_do_not_refuse_the_run"],
    },
    {
        # The refusal removed altogether. A run that measured nothing then
        # earns a failure verdict from numbers nobody took.
        "name": "a-run-that-measured-nothing-is-still-judged",
        "file": JUDGE,
        "old": "    if unavailable and len(unavailable) == len(parsed.windows):\n",
        "new": "    if False:\n",
        "expect": ["test_a_run_with_no_readings_at_all_is_still_refused",
                   "test_a_run_that_measured_nothing_earns_no_verdict"],
    },
    {
        # The size of the gap dropped. The verdict then reads as if the whole
        # run had been measured.
        "name": "the-missing-windows-are-not-counted",
        "file": JUDGE,
        # Refreshed after ea303b9f made gap a multi-line dict, which put
        # this field on a line of its own.
        "old": "        windows_missing_capture=len(unavailable),\n",
        "new": "        windows_missing_capture=0,\n",
        "expect": ["test_the_missing_windows_are_counted_on_the_verdict"],
    },
    {
        # The microphone advice back. It named a fault that was not there on
        # the one real run this tool has measured.
        "name": "the-refusal-blames-the-microphone-again",
        "file": JUDGE,
        "old": "            \"the provider's capture-stats reader returned nothing for those \"\n"
               "            'windows; its reason is logged at debug level and does not reach '\n"
               "            'this log')\n",
        "new": "            'fix the microphone and run the test again')\n",
        "expect": ["test_the_refusal_no_longer_blames_the_microphone"],
    },
    {
        # The gap line never printed. The reader then judges callback loss
        # against inference lag without knowing part of the run went
        # uncounted.
        "name": "the-gap-line-is-not-printed",
        "file": REPORT,
        "old": "    if verdict.windows_missing_capture:\n",
        "new": "    if False:\n",
        "expect": ["test_the_missing_count_prints_beside_the_verdict"],
    },
    {
        # The gap line printed on a fully measured run, where it reads as a
        # gap that does not exist.
        "name": "the-gap-line-prints-on-a-clean-run",
        "file": REPORT,
        "old": "    if verdict.windows_missing_capture:\n",
        "new": "    if True:\n",
        "expect": ["test_a_fully_measured_run_prints_no_gap_line"],
    },
    # ---- Nothing is dictated as punctuation ----
    {
        # A sentence ending in a period again. The reader speaks it, the word
        # reaches the transcript, and the sentence reads wrong for a reason
        # that has nothing to do with load.
        "name": "a-sentence-ends-with-spoken-punctuation",
        "file": SCRIPT,
        "old": '    "Close the window and stop listening now",\n',
        "new": '    "Close the window and stop listening now.",\n',
        "expect": ["test_no_sentence_ends_with_spoken_punctuation",
                   "test_the_six_sentences_are_the_ones_the_procedure_lists"],
    },
    {
        # The expected text keeping a punctuation word. Nothing the
        # recognizer writes would ever equal it.
        "name": "the-expected-text-keeps-a-punctuation-word",
        "file": SCRIPT,
        "old": "    return apply_itn(normalize_transcript(sentence))\n",
        "new": "    return apply_itn(normalize_transcript(sentence)) + ' period'\n",
        "expect": ["test_the_expected_text_carries_no_punctuation_word"],
    },
    # ---- The compound-word tolerance, and its limits ----
    {
        # The tolerance gone. The live run's 'seashells' and 'seashore' mark
        # sentence five wrong again, for a rendering difference no script
        # change can avoid.
        "name": "the-compound-tolerance-is-gone",
        "file": JUDGE,
        "old": "    return _same_words(heard.strip().casefold().split(),\n"
               "                       expected.strip().casefold().split())\n",
        "new": "    return False\n",
        "expect": ["test_a_compound_the_recognizer_joined_is_correct",
                   "test_a_compound_the_recognizer_split_is_correct",
                   "test_the_fifth_sentence_reads_correct_when_joined"],
    },
    {
        # Only the recognizer's direction forgiven. A log that split what the
        # script joins still reads wrong.
        "name": "joined-expected-words-are-not-accepted",
        "file": JUDGE,
        "old": "                if (i + 1 < len(expected)\n"
               "                        and heard[j] == expected[i] + expected[i + 1]):\n",
        "new": "                if False:\n",
        "expect": ["test_a_compound_the_recognizer_joined_is_correct",
                   "test_the_fifth_sentence_reads_correct_when_joined"],
    },
    {
        "name": "joined-heard-words-are-not-accepted",
        "file": JUDGE,
        "old": "                if (j + 1 < len(heard)\n"
               "                        and heard[j] + heard[j + 1] == expected[i]):\n",
        "new": "                if False:\n",
        "expect": ["test_a_compound_the_recognizer_split_is_correct"],
    },
    {
        # Every word forgiven. The tolerance then hides the wrong, missing
        # and extra words this whole test exists to find.
        "name": "the-tolerance-forgives-any-word",
        "file": JUDGE,
        "old": "                if heard[j] == expected[i]:\n",
        "new": "                if True:\n",
        # Not the missing-word and extra-word tests: the walk still has to
        # consume all of both sides to reach its corner, so a length
        # difference stays unreachable however forgiving the comparison is.
        # Those two have their own mutations below.
        "expect": ["test_a_substituted_word_is_still_wrong",
                   "test_a_visible_transcript_that_differs_is_wrong",
                   "test_the_words_still_have_to_be_in_order"],
    },
    {
        # An expected word skipped. A sentence the recognizer dropped a word
        # from then reads correct, which is one of the failures under load
        # this test exists to report.
        "name": "a-missing-word-is-forgiven",
        "file": JUDGE,
        "old": "                if heard[j] == expected[i]:\n"
               "                    reachable[j + 1][i + 1] = True\n",
        "new": "                if heard[j] == expected[i]:\n"
               "                    reachable[j + 1][i + 1] = True\n"
               "                reachable[j][i + 1] = True\n",
        "expect": ["test_a_missing_word_is_still_wrong"],
    },
    {
        # A heard word skipped. A dictated punctuation word, or anything else
        # the recognizer added, then stops marking a sentence wrong.
        "name": "an-extra-word-is-forgiven",
        "file": JUDGE,
        # Anchored outside the "i < len(expected)" guard on purpose. A word
        # added after the last expected one is the case that matters here --
        # the dictated period is exactly that -- and a skip placed inside
        # that guard can never reach it. Anchored inside, this mutation
        # survived.
        "old": "            if not reachable[j][i]:\n"
               "                continue\n",
        "new": "            if not reachable[j][i]:\n"
               "                continue\n"
               "            if j < len(heard):\n"
               "                reachable[j + 1][i] = True\n",
        "expect": ["test_an_extra_word_is_still_wrong"],
    },
    {
        # A third word joined in. That starts forgiving a dropped word, which
        # is one of the failures under load this test reports.
        "name": "three-joined-words-are-accepted",
        "file": JUDGE,
        "old": "                    reachable[j + 1][i + 2] = True\n",
        "new": "                    reachable[j + 1][i + 2] = True\n"
               "                if (i + 2 < len(expected) and heard[j] ==\n"
               "                        expected[i] + expected[i + 1]\n"
               "                        + expected[i + 2]):\n"
               "                    reachable[j + 1][i + 3] = True\n",
        "expect": ["test_three_words_joined_into_one_is_still_wrong"],
    },

    # -- An interrupted write must not leave a flag flipped (.1.4) --------
    {
        # The defect exactly as it was: the cleanup claimed after the write
        # instead of before it. An interrupt in between leaves the config
        # file flipped while changed is still false, and restore returns at
        # its first line.
        "name": "the-cleanup-is-claimed-after-the-write",
        "file": RUN,
        "old": """        self.changed = True
        _atomic_write(self.path, updated)
        return True""",
        "new": """        _atomic_write(self.path, updated)
        self.changed = True
        return True""",
        "expect": ["test_an_interrupt_straight_after_the_write_still_restores"],
    },
    {
        # A write that never landed reported as somebody else's edit sends
        # the operator looking for a change nobody made.
        "name": "a-write-that-never-landed-is-called-a-concurrent-edit",
        "file": RUN,
        "old": """        current = self.path.read_bytes()
        if current == self._original:""",
        "new": """        current = self.path.read_bytes()
        if current == self._original and False:""",
        "expect": [
            "test_a_write_that_never_landed_is_not_called_someone_elses_edit"],
    },
    {
        # A leftover temporary file beside config.toml is litter in the
        # directory the application reads.
        "name": "the-config-temporary-file-is-left-behind",
        "file": RUN,
        "old": """    temp = path.with_name(f'{path.name}.stt-load-test.{os.getpid()}.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()""",
        "new": """    temp = path.with_name(f'{path.name}.stt-load-test.{os.getpid()}.tmp')
    temp.write_bytes(data)
    os.replace(temp, path)""",
        "expect": ["test_a_failed_write_leaves_no_temporary_file_behind"],
    },
    {
        # A plain write truncates first, so an interrupt in the middle
        # leaves bytes matching neither state -- the one case restore reads
        # as a concurrent edit and refuses to touch.
        "name": "the-config-write-truncates-instead-of-replacing",
        "file": RUN,
        "old": """    temp = path.with_name(f'{path.name}.stt-load-test.{os.getpid()}.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()""",
        "new": """    path.write_bytes(data)""",
        "expect": [
            "test_an_interrupt_straight_after_the_write_still_restores",
            "test_a_write_that_never_landed_is_not_called_someone_elses_edit",
            "test_a_failed_write_leaves_no_temporary_file_behind"],
    },

    # -- The gate must not leave its own mutant in the tree (.1.6) --------
    {
        # The defect exactly as it was: the mutant write one line above the
        # try whose finally restores it.
        "name": "the-mutant-write-sits-outside-the-restore",
        "file": GATE_SELF,
        # The target file is this file, so a triple-quoted pattern
        # would appear twice -- once in the real function, once
        # here -- and the gate would refuse it as ambiguous. The
        # escaped newlines keep the joined pattern out of this
        # literal, so the one match is the real function.
        # Refreshed after .2.1.1 put ``failure`` between the two lines
        # this used to join.
        "old": ('    failure = None\n    try:\n'
                '        _atomic_write(path, mutated)\n'
                '        run = _run_pytest(SHARED, TESTS, mutant=True)'),
        "new": """    failure = None
    _atomic_write(path, mutated)
    try:
        run = _run_pytest(SHARED, TESTS, mutant=True)""",
        "expect": [
            "test_an_interrupt_straight_after_the_mutant_write_restores"],
    },
    {
        "name": "the-gate-restores-nothing-when-the-run-is-interrupted",
        "file": GATE_SELF,
        # The target file is this file, so a triple-quoted pattern
        # would appear twice -- once in the real function, once
        # here -- and the gate would refuse it as ambiguous. The
        # escaped newlines keep the joined pattern out of this
        # literal, so the one match is the real function.
        # Refreshed when .1.33 put the call behind ``_call_cleanup``. The
        # replacement still defines every name below it, so the tests that
        # fail are the ones that look for a restored file.
        "old": ('        (restore_error, interrupt), deferred = '
                '_call_cleanup(\n'
                '            _restore, path, original, mutated, name)\n'),
        "new": ('        (restore_error, interrupt), deferred = '
                '(None, None), None\n'),
        "expect": [
            "test_an_interrupt_while_the_suite_runs_restores_the_source",
            "test_an_interrupt_straight_after_the_mutant_write_restores",
            "test_a_concurrent_edit_during_the_run_is_still_refused"],
    },
    {
        "name": "the-mutant-write-truncates-instead-of-replacing",
        "file": GATE_SELF,
        # The target file is this file, so a triple-quoted pattern
        # would appear twice -- once in the real function, once
        # here -- and the gate would refuse it as ambiguous. The
        # escaped newlines keep the joined pattern out of this
        # literal, so the one match is the real function.
        "old": ('    temp = path.with_name(f"{path.name}'
                '.mutation-gate.{os.getpid()}.tmp")\n'
                '    try:\n        temp.write_bytes(data)\n'
                '        os.replace(temp, path)\n    finally:\n'
                '        if temp.exists():\n            temp.unlink()'),
        "new": "    path.write_bytes(data)",
        "expect": [
            "test_a_failed_mutant_write_leaves_no_temporary_file_behind"],
    },

    # -- A sentence with no numbers must be named (.1.5) ------------------
    {
        # `and` instead of `or`: a sentence that WAS heard but carries no
        # per-utterance line stops being named, which is the whole case.
        "name": "an-unmeasured-sentence-is-not-named",
        "file": JUDGE,
        "old": """            if r.utt is None or r.utt not in measured),""",
        "new": """            if r.utt is None and r.utt not in measured),""",
        "expect": ["test_the_unmeasured_sentences_are_named",
                   "test_the_missing_sentences_print_beside_the_verdict"],
    },
    {
        "name": "the-sentence-total-counts-utterances-not-sentences",
        "file": JUDGE,
        "old": """        sentences_total=len(sentences.results))""",
        "new": """        sentences_total=len(parsed.utterances))""",
        "expect": ["test_the_unmeasured_sentences_are_named"],
    },
    {
        # The ruling's second requirement: the all-empty path still refuses
        # to name a verdict.
        "name": "a-run-with-no-per-utterance-line-earns-a-verdict",
        "file": JUDGE,
        "old": """    if worst_ratio is None:
        evidence.append('no per-utterance line carried an engine_ratio')""",
        "new": """    if worst_ratio is None and False:
        evidence.append('no per-utterance line carried an engine_ratio')""",
        "expect": [
            "test_a_run_with_no_per_utterance_line_is_still_undetermined"],
    },
    {
        "name": "the-unmeasured-sentences-are-not-printed",
        "file": REPORT,
        "old": """    if verdict.sentences_missing_metrics:""",
        "new": """    if verdict.sentences_missing_metrics and False:""",
        "expect": ["test_the_missing_sentences_print_beside_the_verdict"],
    },
    {
        # Counted but not named. The reader cannot match a count against the
        # per-sentence block below it.
        "name": "the-unmeasured-sentences-are-counted-but-not-named",
        "file": REPORT,
        "old": """            + ', '.join(str(i) for i in verdict.sentences_missing_metrics))""",
        "new": """            + ', '.join(str(i) for i in ()))""",
        "expect": ["test_the_missing_sentences_print_beside_the_verdict"],
    },
    {
        # The defect exactly as it was: a fixed count in the heading.
        "name": "the-heading-claims-every-sentence-was-measured",
        "file": REPORT,
        "old": """    lines += _block(f'Under-load per-utterance lines: {counted} of '""",
        "new": """    lines += _block(f'Under-load per-utterance lines: 6 of '""",
        "expect": ["test_the_heading_states_how_many_sentences_were_measured"],
    },

    # -- The file went back; the running application did not (.1.7) ------
    {
        "name": "the-restart-notice-is-not-printed",
        "file": RUN,
        "old": """    for edit in put_back:
        interrupt = _say(
            out,
            f'[!] {edit.key} went back to '
            f'{str(not edit.value).lower()} in {edit.path}, but WheelHouse '
            'read it once when it started. The running application keeps '
            'the value this test set until you restart it.',
            interrupt)""",
        "new": """    for edit in put_back:
        pass""",
        # Only the first test. The dictated-text test asserts on the
        # LOG_TRANSCRIPTS line, which this mutation leaves standing and
        # which carries the word restart itself.
        "expect": ["test_a_flag_that_went_back_still_needs_a_restart"],
    },
    {
        "name": "the-dictated-text-line-is-dropped",
        "file": RUN,
        "old": """    if any(edit.key == 'LOG_TRANSCRIPTS' for edit in put_back):
        interrupt = _say(
            out,
            '[!] Until that restart, DICTATED TEXT keeps reaching the log.',
            interrupt)""",
        "new": """    if any(edit.key == 'LOG_TRANSCRIPTS' for edit in put_back):
        pass""",
        "expect": ["test_the_dictated_text_keeps_reaching_the_log_until_then"],
    },
    {
        # A flag already set to the wanted value was never this run's to put
        # back, and telling the operator to restart for it is noise that
        # trains them to ignore the line that matters.
        "name": "a-flag-that-never-flipped-is-announced",
        "file": RUN,
        # Refreshed after restore() started reporting whether it wrote: the
        # bookkeeping moved out of restore_all and into restore itself.
        "old": """        if not self.changed or self._original is None:
            return False""",
        "new": """        if not self.changed or self._original is None:
            return True""",
        "expect": ["test_a_flag_that_was_already_on_says_nothing"],
    },
    {
        # A refused restore leaves the setting ON. Announcing it as put back
        # tells the operator the opposite of the truth.
        "name": "a-flag-that-did-not-go-back-is-announced",
        "file": RUN,
        # Refreshed with the same move.
        "old": """        if restored:
            put_back.append(edit)""",
        "new": """        if True:
            put_back.append(edit)""",
        "expect": ["test_a_flag_the_run_refused_to_put_back_is_not_announced"],
    },

    # -- The gate restore is all or nothing (.1.8) -----------------------
    {
        # The target file is this file, so a triple-quoted pattern would
        # appear twice -- once in the real function, once here -- and the
        # gate would refuse it as ambiguous. The escaped newlines keep the
        # joined pattern out of this literal, so the one match is the real
        # function.
        "name": "the-gate-restore-truncates-instead-of-replacing",
        "file": GATE_SELF,
        "old": ('            _atomic_write(path, original)\n'
                '        except OSError as e:\n'),
        "new": ('            path.write_bytes(original)\n'
                '        except OSError as e:\n'),
        "expect": ["test_a_restore_that_cannot_finish_leaves_the_mutant_whole"],
    },

    # -- A sentence spoken out of order is not clean (.1.9) --------------
    {
        # The lower bound. Without it the leftover pass reaches back before
        # a sentence already paired, which is what rebuilt a reordered run
        # as a clean one: 131 of the 719 non-identity orders.
        "name": "the-leftover-pass-reaches-back-before-its-neighbour",
        "file": JUDGE,
        "old": """        index = next((i for i in spare if low < i < high), None)""",
        "new": """        index = next((i for i in spare if i < high), None)""",
        "expect": ["test_a_swapped_pair_is_not_reported_as_six_correct",
                   "test_the_sentence_that_was_not_heard_in_place_reads_missing",
                   "test_the_words_nothing_claimed_become_an_extra"],
    },
    {
        # The upper bound. Without it a sentence is handed words that
        # arrived after a LATER sentence was already heard.
        "name": "the-leftover-pass-reaches-past-its-neighbour",
        "file": JUDGE,
        "old": """        index = next((i for i in spare if low < i < high), None)""",
        "new": """        index = next((i for i in spare if low < i), None)""",
        "expect": [
            "test_words_after_a_later_sentence_are_not_given_to_an_earlier_one"],
    },
    {
        # The whole bound removed: the old chronological hand-out.
        "name": "the-leftover-pass-takes-any-spare-in-order",
        "file": JUDGE,
        "old": """        low = max((paired[p] for p in range(position)
                   if paired[p] is not None), default=-1)
        high = min((paired[p] for p in range(position + 1, len(expected))
                    if paired[p] is not None), default=len(transcripts))
        index = next((i for i in spare if low < i < high), None)""",
        "new": """        index = spare[0] if spare else None""",
        "expect": ["test_a_swapped_pair_is_not_reported_as_six_correct",
                   "test_the_sentence_that_was_not_heard_in_place_reads_missing",
                   "test_the_words_nothing_claimed_become_an_extra"],
    },

    # -- The recording is replaced whole or not at all (.1.10) -----------
    {
        "name": "the-recording-export-truncates-instead-of-replacing",
        "file": RECORD,
        "old": """        track.export(str(temp), format='wav')
        os.replace(temp, wav_path)""",
        "new": """        track.export(str(wav_path), format='wav')""",
        "expect": [
            "test_a_failed_export_leaves_the_previous_recording_intact",
            "test_the_export_never_opens_the_recording_itself"],
    },
    {
        "name": "the-recording-temporary-file-is-left-behind",
        "file": RECORD,
        "old": """    finally:
        if temp.exists():
            temp.unlink()""",
        "new": """    finally:
        pass""",
        "expect": ["test_a_failed_export_leaves_no_temporary_file_behind"],
    },

    # -- A replacement log is not a continuation (.1.11) -----------------
    {
        # Back to deciding rotation from the size alone, which is what let a
        # replacement that had grown past the mark be read as the same file.
        "name": "a-replacement-log-is-treated-as-a-continuation",
        "file": RUN,
        "old": """        rotated = replaced or size < mark.size""",
        "new": """        rotated = size < mark.size""",
        "expect": [
            "test_a_replacement_that_grew_past_the_mark_is_still_a_rotation"],
    },
    {
        "name": "the-mark-carries-no-file-identity",
        "file": RUN,
        # Refreshed after the mark became one stat rather than two.
        "old": """    return LogMark(size=info.st_size, identity=_identity_of(info))""",
        "new": """    return LogMark(size=info.st_size, identity=None)""",
        "expect": [
            "test_a_replacement_that_grew_past_the_mark_is_still_a_rotation"],
    },
    {
        # The other direction: a log that merely grew must not be called a
        # rotation, or every run reads the whole file and reports lines from
        # before it started.
        "name": "a-growing-log-is-called-a-rotation",
        "file": RUN,
        # Refreshed after the identity started coming from the open
        # handle, which removed the second None test.
        # Refreshed again for .1.36: the read moved inside a try/with,
        # so the block sits one level deeper.
        "old": """            replaced = (mark.identity is not None and identity is not None
                        and identity != mark.identity)""",
        "new": """            replaced = True""",
        "expect": ["test_the_same_file_growing_is_not_a_rotation"],
    },

    # -- The gate checks its own patterns (.1.13) ------------------------
    {
        # Self-targeting, so the pattern is written as fragments with
        # escaped newlines: a triple-quoted form would appear twice, once
        # in check_patterns and once in this entry, and the exactly-one
        # rule would refuse it. The bare "if count != 1:" is also in main,
        # so the line above it is what makes this one unique.
        "name": "the-check-never-counts-a-second-match",
        "file": GATE_SELF,
        "old": ('        count = data.count(_translate(m["old"], data))\n'
                '        if count != 1:'),
        "new": ('        count = data.count(_translate(m["old"], data))\n'
                '        if count == 0:'),
        "expect": ["test_a_pattern_that_matches_twice_is_reported"],
    },
    {
        # The check stops translating line endings, so every multi-line
        # pattern in a CRLF file reads as stale and the reader hunts for a
        # rewrite that never happened.
        "name": "the-check-ignores-line-endings",
        "file": GATE_SELF,
        "old": ('        data = cache[path]\n'
                '        count = data.count(_translate(m["old"], data))'),
        "new": ('        data = cache[path]\n'
                '        count = data.count(m["old"].encode("utf-8"))'),
        "expect": ["test_a_crlf_file_is_read_through_the_same_translation"],
    },
    {
        # --check falls through into a full run, which is the hours-long
        # thing it exists to avoid.
        "name": "the-check-mode-runs-the-suite-anyway",
        "file": GATE_SELF,
        "old": ('    argv = sys.argv[1:]\n'
                '    if "--check" in argv:'),
        "new": ('    argv = sys.argv[1:]\n'
                '    if False:'),
        "expect": ["test_the_check_reports_a_stale_pattern_and_fails",
                   "test_the_check_passes_when_every_pattern_matches"],
    },
    {
        # Back to the gap that let four patterns rot: a name-filtered run
        # says nothing about the entries it did not select.
        "name": "the-filtered-run-does-not-warn-about-staleness",
        "file": GATE_SELF,
        "old": ('    skipped = len(MUTATIONS) - len(selected)\n'
                '    if only:'),
        "new": ('    skipped = len(MUTATIONS) - len(selected)\n'
                '    if False:'),
        "expect": [
            "test_a_filtered_run_names_the_stale_pattern_it_did_not_select"],
    },
    {
        # The mutant run stops carrying its marker, so the whole-set
        # self-check runs against a file holding a mutant and reports that
        # mutant's own entry as stale.
        "name": "the-mutant-run-is-not-marked",
        "file": GATE_SELF,
        "old": ('    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")\n'
                '    if mutant:'),
        "new": ('    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")\n'
                '    if False:'),
        "expect": [
            "test_the_mutant_run_carries_the_marker_and_the_baseline_does_not"],
    },
    {
        # The other direction: the baseline carries the marker too, so the
        # one run that should catch staleness early skips the check.
        "name": "the-baseline-run-is-marked-as-a-mutant",
        "file": GATE_SELF,
        "old": ('    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")\n'
                '    if mutant:'),
        "new": ('    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")\n'
                '    if True:'),
        "expect": [
            "test_the_mutant_run_carries_the_marker_and_the_baseline_does_not"],
    },

    # -- One look at the log (.1.14) -------------------------------------
    {
        # Back to two stat calls for one mark, which is the torn snapshot.
        "name": "the-mark-takes-two-looks-at-the-file",
        "file": RUN,
        "old": """    return LogMark(size=info.st_size, identity=_identity_of(info))""",
        "new": """    return LogMark(size=path.stat().st_size,
                   identity=_identity_of(path.stat()))""",
        "expect": ["test_the_mark_is_taken_from_one_look_at_the_file",
                   "test_a_rotation_while_the_mark_is_taken_cannot_tear_it"],
    },
    {
        # The decision comes from a separate look at the path again, so a
        # rotation between that look and the open goes unseen.
        "name": "the-read-decides-from-the-path-not-the-handle",
        "file": RUN,
        "old": """        info = os.fstat(handle.fileno())""",
        "new": """        info = path.stat()""",
        "expect": ["test_the_decision_follows_the_file_that_was_opened"],
    },
    {
        # The creation time back in the identity. Windows does not report
        # it stably across these two calls, so an ordinary growing log
        # reads as a replacement about a third of the time.
        "name": "the-identity-carries-an-unstable-creation-time",
        "file": RUN,
        "old": """    return (info.st_ino, info.st_dev)""",
        "new": """    return (info.st_ino, info.st_dev, info.st_ctime_ns)""",
        "expect": [
            "test_a_growing_log_is_not_a_rotation_however_often_it_is_read"],
    },
    {
        # A file index of zero is taken as an identity, so on a filesystem
        # that reports none, every file carries the same one.
        "name": "a-file-index-of-zero-is-an-identity",
        "file": RUN,
        "old": """    if not info.st_ino:
        return None""",
        "new": """    if False:
        return None""",
        "expect": [
            "test_a_filesystem_that_reports_no_file_index_has_no_identity"],
    },

    # -- A temporary file per process (.1.12, approved half) -------------
    {
        "name": "the-config-temporary-name-is-shared",
        "file": RUN,
        "old": """    temp = path.with_name(f'{path.name}.stt-load-test.{os.getpid()}.tmp')""",
        "new": """    temp = path.with_name(f'{path.name}.stt-load-test.tmp')""",
        "expect": [
            "test_the_config_write_names_its_temporary_file_for_this_process"],
    },
    {
        "name": "the-recording-temporary-name-is-shared",
        "file": RECORD,
        "old": """    temp = wav_path.with_name(
        f'{wav_path.name}.stt-load-test.{os.getpid()}.tmp')""",
        "new": """    temp = wav_path.with_name(f'{wav_path.name}.stt-load-test.tmp')""",
        "expect": [
            "test_the_recording_export_names_its_temporary_file_for_this_process"],
    },
    {
        # Self-targeting again, so escaped fragments.
        "name": "the-mutant-temporary-name-is-shared",
        "file": GATE_SELF,
        "old": ('    temp = path.with_name(f"{path.name}'
                '.mutation-gate.{os.getpid()}.tmp")\n    try:'),
        "new": ('    temp = path.with_name(f"{path.name}'
                '.mutation-gate.tmp")\n    try:'),
        "expect": [
            "test_the_gate_write_names_its_temporary_file_for_this_process"],
    },

    # -- A baseline says what it measured (.1.15) ------------------------
    {
        # A calibration that measured nothing reports that it saved one.
        # Refreshed after .1.25 added the finite half to the same line.
        "name": "a-baseline-with-no-ratio-is-reported-as-saved",
        "file": RUN,
        "old": """    if ratio is None or not math.isfinite(ratio):
        return False""",
        "new": """    if ratio is None or not math.isfinite(ratio):
        return True""",
        # Every test this really fails, read off a run of it. The refusal
        # still removes the earlier file, so the tests that check the
        # removal fail on the returned True beside them.
        "expect": ["test_a_run_with_no_ratio_writes_no_baseline",
                   "test_a_run_with_no_ratio_still_removes_the_earlier_"
                   "calibration",
                   "test_the_refusal_removes_an_earlier_baseline",
                   "test_a_not_a_number_ratio_is_not_persisted",
                   "test_an_infinite_ratio_is_not_persisted",
                   "test_a_negatively_infinite_ratio_is_not_persisted",
                   "test_an_earlier_baseline_does_not_outlive_a_refused_one"],
    },
    {
        # The provenance is never compared, so a baseline measured against
        # another checkout is handed to the verdict ladder.
        "name": "a-mismatched-baseline-is-used-anyway",
        "file": RUN,
        "old": """    differs = [k for k in provenance if value.get(k) != provenance[k]]""",
        "new": """    differs = []""",
        "expect": [
            "test_a_baseline_measured_against_another_checkout_is_refused",
            "test_a_baseline_measured_with_another_endpoint_setting_is_refused",
            "test_a_baseline_from_before_provenance_existed_is_refused"],
    },
    {
        "name": "the-baseline-does-not-record-the-checkout",
        "file": RUN,
        "old": """        'repo_root': str(paths.repo_root),""",
        "new": """        'repo_root': '',""",
        "expect": [
            "test_a_baseline_measured_against_another_checkout_is_refused"],
    },
    {
        "name": "the-baseline-does-not-record-the-endpoint-setting",
        "file": RUN,
        "old": """        'endpoint_silence_ms': endpoint_silence_ms,""",
        "new": """        'endpoint_silence_ms': 0.0,""",
        "expect": [
            "test_a_baseline_measured_with_another_endpoint_setting_is_refused"],
    },
    {
        # The old baseline survives a calibration that measured nothing,
        # and the next load run reads it as this machine's.
        "name": "a-failed-calibration-leaves-the-old-baseline",
        "file": RUN,
        "old": """    baseline_file(cache_dir).unlink(missing_ok=True)""",
        "new": """    pass""",
        "expect": [
            "test_an_earlier_baseline_does_not_outlive_a_failed_calibration"],
    },
    {
        # Clearing a baseline that is not there raises instead of passing.
        #
        # The .1.17 fix wrapped this unlink in `except OSError: return
        # False`, which MASKED the mutation's original catcher: the raise
        # is swallowed and the call returns False instead, so a test that
        # only checked "does not raise" stayed green. That test asserted
        # nothing once the fix landed and is gone; the catcher is now the
        # one that asserts the answer, which the mutation turns from True
        # into False. A fresh machine has no baseline, so the mutant makes
        # save_baseline refuse every calibration.
        "name": "clearing-a-missing-baseline-raises",
        "file": RUN,
        "old": """    baseline_file(cache_dir).unlink(missing_ok=True)""",
        "new": """    baseline_file(cache_dir).unlink()""",
        "expect": ["test_a_clear_of_a_baseline_that_is_not_there_says_so"],
    },

    # -- A failed log mark is not an empty log (.1.16) -------------------
    {
        # Back to the zero mark for a refused stat, which is the whole
        # defect: main refuses unless the log exists, so that value could
        # only mean the stat failed, and the run read the history as its
        # own.
        "name": "a-refused-mark-reads-as-an-empty-log",
        "file": RUN,
        # Carries the FileNotFoundError arm above it because .1.36 gave
        # read_since an `except OSError: return None` of its own, and the
        # bare two lines then matched in two functions.
        "old": """    except FileNotFoundError:
        return LogMark(size=0, identity=None)
    except OSError:
        return None""",
        "new": """    except FileNotFoundError:
        return LogMark(size=0, identity=None)
    except OSError:
        return LogMark(size=0, identity=None)""",
        "expect": ["test_a_mark_that_could_not_be_taken_is_no_mark",
                   "test_a_log_that_is_absent_is_not_a_log_that_was_refused",
                   "test_the_readiness_check_ignores_a_line_written_before_"
                   "its_mark"],
    },
    {
        # The other direction: an absent log is treated as a refusal, so a
        # log created after the mark is never read at all.
        "name": "an-absent-log-is-treated-as-a-refusal",
        "file": RUN,
        "old": """    except FileNotFoundError:
        return LogMark(size=0, identity=None)""",
        "new": """    except FileNotFoundError:
        return None""",
        "expect": ["test_a_log_that_did_not_exist_at_the_mark_is_read_whole",
                   "test_a_log_that_is_absent_is_not_a_log_that_was_refused"],
    },
    {
        # The structural guard gives way. A zero mark is substituted rather
        # than the guard deleted: deleting it leaves mark.size raising
        # AttributeError, and a mutant that crashes upstream of the
        # assertion earns no verdict.
        "name": "no-mark-still-reads-from-the-start",
        "file": RUN,
        "old": """    if mark is None:
        return None""",
        "new": """    if mark is None:
        mark = LogMark(size=0, identity=None)""",
        "expect": [
            "test_no_mark_reads_nothing_rather_than_the_whole_history"],
    },
    {
        # The readiness check stops re-marking, so a cleared refusal never
        # produces a mark and the check reports the same False whether or
        # not the provider restarted.
        "name": "the-readiness-check-never-retries-its-mark",
        "file": RUN,
        "old": """            mark = log_mark(path)
            if mark is None:
                time.sleep(2.0)
            continue""",
        "new": """            mark = None
            if mark is None:
                time.sleep(2.0)
            continue""",
        "expect": [
            "test_the_readiness_check_takes_a_mark_once_the_refusal_clears"],
    },

    # -- A failed cache operation fails closed (.1.17) -------------------
    {
        # The old baseline is no longer removed before the new one is
        # written, so a refused move leaves the previous calibration in
        # place with its provenance unchanged.
        "name": "the-old-baseline-outlives-a-refused-write",
        "file": RUN,
        "old": """    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')""",
        "new": """    if False:
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')""",
        "expect": [
            "test_a_write_that_failed_leaves_no_earlier_calibration_behind",
            "test_a_removal_that_failed_refuses_instead_of_reporting_success",
            "test_a_run_with_no_ratio_still_removes_the_earlier_calibration"],
    },
    {
        # A removal the filesystem refused is reported as a clear, which is
        # the report of an operation that did not happen.
        "name": "a-refused-removal-is-reported-as-a-clear",
        "file": RUN,
        "old": """        except OSError:
            break
        except KeyboardInterrupt as stop:""",
        "new": """        except OSError:
            gone = True
            break
        except KeyboardInterrupt as stop:""",
        "expect": [
            "test_a_clear_that_could_not_remove_the_file_says_so",
            "test_a_removal_that_failed_refuses_instead_of_reporting_success"],
    },
    {
        # And the opposite: a removal that worked is reported as refused,
        # which turns every calibration into a refusal.
        "name": "a-clear-that-worked-is-reported-as-refused",
        "file": RUN,
        "old": """        gone = True
        break""",
        "new": """        gone = False
        break""",
        "expect": ["test_a_clear_that_removed_the_file_says_so",
                   "test_a_clear_of_a_baseline_that_is_not_there_says_so"],
    },

    # -- The one invalidation the tool has survives a Ctrl+C (.1.28) -----
    {
        # The defect exactly as it stood: KeyboardInterrupt inherits from
        # BaseException, so the OSError arm never saw it. It left
        # clear_baseline and save_baseline before either the old cache was
        # removed or a replacement was written, and the old file stayed on
        # disk with provenance the next load run matches.
        "name": "an-interrupt-at-the-removal-escapes-the-invalidation",
        "file": RUN,
        "old": ('    interrupt = None\n'
                '    gone = False\n'
                '    for _attempt in (1, 2):\n'
                '        try:\n'
                '            baseline_file(cache_dir).unlink(missing_ok=True)\n'
                '        except OSError:\n'
                '            break\n'
                '        except KeyboardInterrupt as stop:\n'
                '            if interrupt is None:\n'
                '                interrupt = stop\n'
                '            continue\n'
                '        gone = True\n'
                '        break\n'),
        "new": ('    gone = False\n'
                '    try:\n'
                '        baseline_file(cache_dir).unlink(missing_ok=True)\n'
                '    except OSError:\n'
                '        return False\n'
                '    return True\n'
                '    interrupt = None\n'),
        "expect": [
            "test_an_interrupted_removal_is_finished_before_it_is_raised",
            "test_a_removal_no_attempt_finished_is_never_left_unsaid",
            "test_the_removal_is_attempted_twice_and_no_more"],
    },
    {
        # One attempt, so the interrupt that arrives during it is the end
        # of the invalidation and the old calibration outlives the run.
        "name": "a-removal-attempt-is-made-only-once",
        "file": RUN,
        "old": ('    for _attempt in (1, 2):\n'
                '        try:\n'
                '            baseline_file(cache_dir).unlink(missing_ok=True)\n'),
        "new": ('    for _attempt in (1,):\n'
                '        try:\n'
                '            baseline_file(cache_dir).unlink(missing_ok=True)\n'),
        "expect": [
            "test_an_interrupted_removal_is_finished_before_it_is_raised",
            "test_the_removal_is_attempted_twice_and_no_more",
            "test_a_removal_that_finished_says_nothing_about_deleting_it"],
    },
    {
        # The other side of the bound, still finite, so only a test that
        # counts the attempts can tell it from two.
        "name": "a-removal-attempt-is-made-a-third-time",
        "file": RUN,
        "old": ('    for _attempt in (1, 2):\n'
                '        try:\n'
                '            baseline_file(cache_dir).unlink(missing_ok=True)\n'),
        "new": ('    for _attempt in (1, 2, 3):\n'
                '        try:\n'
                '            baseline_file(cache_dir).unlink(missing_ok=True)\n'),
        "expect": ["test_the_removal_is_attempted_twice_and_no_more"],
    },
    {
        # The interrupt is dropped instead of raised, so the run carries on
        # and writes a new baseline after the operator stopped it.
        "name": "an-interrupt-at-the-removal-is-swallowed",
        "file": RUN,
        "old": """                interrupt)
            _mark(handled, 'the operator was told to delete it')
        raise interrupt
    return gone""",
        "new": """                interrupt)
            _mark(handled, 'the operator was told to delete it')
    return gone""",
        "expect": [
            "test_an_interrupted_removal_is_finished_before_it_is_raised",
            "test_a_removal_no_attempt_finished_is_never_left_unsaid",
            "test_the_removal_is_attempted_twice_and_no_more",
            "test_a_removal_that_finished_says_nothing_about_deleting_it"],
    },
    {
        # The file is still there and nobody is told. That is the silent
        # reuse the whole finding is about: the next load run matches the
        # provenance and reads the old ratio as this machine's.
        "name": "the-manual-deletion-warning-is-never-said",
        "file": RUN,
        "old": "        if not gone:\n",
        "new": "        if False:\n",
        "expect": [
            "test_a_removal_no_attempt_finished_is_never_left_unsaid"],
    },
    {
        # And the opposite: the operator is sent to delete a file that is
        # already gone, which trains them to ignore the line that matters.
        "name": "the-manual-deletion-warning-is-said-either-way",
        "file": RUN,
        "old": "        if not gone:\n",
        "new": "        if True:\n",
        "expect": [
            "test_a_removal_that_finished_says_nothing_about_deleting_it"],
    },
    {
        # The instruction emptied. Both paths that reach it -- a refused
        # removal and an interrupted one -- leave the operator holding a
        # file whose deletion nothing else will do.
        "name": "the-manual-deletion-sentence-says-nothing-to-do",
        "file": RUN,
        "old": ("    'Delete that file yourself before the next load run: "
                "until it is gone, '\n"
                "    'that run can reuse a calibration this run did not "
                "make.')\n"),
        "new": "    'The next load run sorts it out.')\n",
        "expect": [
            "test_a_removal_no_attempt_finished_is_never_left_unsaid"],
    },

    # -- Nothing runs before the invalidation (.1.29) --------------------
    {
        # The defect exactly as it stood: save_baseline opened with the
        # directory setup and reached clear_baseline only on the next
        # line. A Ctrl+C delivered during that setup left the function
        # past every guard clear_baseline carries, because none of them
        # had started, and the old calibration stayed on disk with
        # provenance the next load run matches.
        "name": "the-directory-is-made-before-the-old-baseline-goes",
        "file": RUN,
        "old": """    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')
    # Recorded after the invalidation, never before it: from here on, a
    # baseline file on disk is one this run wrote, so the caller has
    # nothing to say about the file it finds.
    _mark(handled, 'the old baseline is gone')
    if ratio is None or not math.isfinite(ratio):
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(""",
        "new": """    cache_dir.mkdir(parents=True, exist_ok=True)
    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')
    _mark(handled, 'the old baseline is gone')
    if ratio is None or not math.isfinite(ratio):
        return False
    _atomic_write(""",
        "expect": [
            "test_an_interrupt_making_the_directory_leaves_no_old_baseline",
            "test_the_old_baseline_goes_before_the_directory_is_made"],
    },
    {
        # The directory is never made, so the write into a cache that does
        # not exist yet fails and the first calibration on a fresh machine
        # records nothing.
        "name": "the-cache-directory-is-never-made",
        "file": RUN,
        "old": ('    cache_dir.mkdir(parents=True, exist_ok=True)\n'
                '    _atomic_write(\n'),
        "new": '    _atomic_write(\n',
        "expect": ["test_the_invalidation_needs_no_cache_directory"],
    },
    {
        # The interrupt is retried away rather than let out, so the
        # operator's Ctrl+C is swallowed and the run writes the new
        # baseline it was stopped from writing. Bounded, so only a test
        # that watches the attempts and the raise can tell.
        "name": "an-interrupt-making-the-directory-is-swallowed",
        "file": RUN,
        "old": ('    cache_dir.mkdir(parents=True, exist_ok=True)\n'
                '    _atomic_write(\n'),
        "new": ('    for _try in (1, 2):\n'
                '        try:\n'
                '            cache_dir.mkdir(parents=True, exist_ok=True)\n'
                '        except KeyboardInterrupt:\n'
                '            continue\n'
                '        break\n'
                '    _atomic_write(\n'),
        "expect": [
            "test_an_interrupt_making_the_directory_leaves_no_old_baseline"],
    },

    # -- The caller's own guard over the save (.1.30) --------------------
    {
        # The defect exactly as it stood: main caught only OSError around
        # the save, so a Ctrl+C delivered on the call into save_baseline --
        # or on the call into clear_baseline, before its first statement --
        # left through the outer finally with the config put back and
        # nothing said about the cache. The earlier baseline.json stayed on
        # disk with provenance the next load run matches.
        "name": "the-interrupt-entering-the-save-is-unguarded-again",
        "file": RUN,
        "old": r"""            # What the save got past, for the interrupt handler below. A
            # Ctrl+C is delivered at a bytecode boundary, so it can land on
            # the CALL itself -- before ``save_baseline``'s first
            # statement, or before ``clear_baseline``'s -- and no
            # arrangement of statements inside them closes that. Only the
            # caller can, and only if it can tell which file is on disk.
            handled: list[str] = []
            try:
                saved = save_baseline(record.CACHE_DIR, ratio, provenance,
                                      handled=handled)
            except OSError as error:
                print('[x] the baseline cache at '
                      f'{baseline_file(record.CACHE_DIR)} could not be '
                      f'updated ({error}). '
                      + DELETE_THE_BASELINE_YOURSELF)
                return 1
            except KeyboardInterrupt as stop:
                # Three outcomes, not two. An existence check on its own
                # decides only whether a file is there, and once the save
                # has handled anything at all the file there is either the
                # one this run wrote -- the interrupt can land after
                # ``_atomic_write`` finished and before ``saved`` is
                # stored -- or one the operator has already been sent to
                # delete. Sending them to delete a valid current
                # calibration, or a file that is not there, is the same
                # false instruction as saying nothing about a stale one.
                # So the line is said only when the save got past nothing
                # AND a file is still sitting there
                # (finding wh-load-test-script.1.30).
                if not handled and baseline_file(record.CACHE_DIR).exists():
                    # Through ``_say``, because this line is the only thing
                    # between the operator and a calibration nobody
                    # measured: a second Ctrl+C landing while it prints
                    # must not carry it away.
                    stop = _say(
                        print,
                        '[x] the run was interrupted before the baseline '
                        f'cache at {baseline_file(record.CACHE_DIR)} was '
                        'invalidated, so the file there is the EARLIER '
                        'calibration and not this run\'s. '
                        + DELETE_THE_BASELINE_YOURSELF,
                        stop)
                raise stop
""",
        "new": """            try:
                saved = save_baseline(record.CACHE_DIR, ratio, provenance)
            except OSError as error:
                print('[x] the baseline cache at '
                      f'{baseline_file(record.CACHE_DIR)} could not be '
                      f'updated ({error}). '
                      + DELETE_THE_BASELINE_YOURSELF)
                return 1
""",
        "expect": [
            "test_an_interrupt_entering_the_save_names_the_stale_baseline",
            "test_an_interrupt_entering_the_invalidation_names_it_too",
            "test_a_second_interrupt_printing_the_line_does_not_carry_it_away"],
    },
    {
        # The trap the finding names. Keyed on existence alone, the guard
        # sends the operator to delete whatever file it finds -- and after
        # a completed write that file is THIS run's calibration, valid and
        # current, because the interrupt can land between _atomic_write and
        # the store of what the save returned.
        "name": "a-baseline-this-run-wrote-is-called-stale",
        "file": RUN,
        "old": ("                if not handled and "
                "baseline_file(record.CACHE_DIR).exists():\n"),
        "new": ("                if "
                "baseline_file(record.CACHE_DIR).exists():\n"),
        "expect": [
            "test_the_baseline_this_run_wrote_is_never_called_stale",
            "test_the_operator_is_told_to_delete_it_once"],
    },
    {
        # The same defect pointing the other way: the operator is sent
        # after a file that is not there, which is how a line stops being
        # read.
        "name": "a-baseline-that-is-not-there-is-called-stale",
        "file": RUN,
        "old": ("                if not handled and "
                "baseline_file(record.CACHE_DIR).exists():\n"),
        "new": "                if not handled:\n",
        "expect": [
            "test_a_baseline_that_is_not_there_is_never_called_stale"],
    },
    {
        # The guard is left deciding with nothing to decide from: every
        # interrupt then looks like one that arrived before the
        # invalidation began.
        "name": "the-save-is-never-asked-what-it-got-past",
        "file": RUN,
        "old": ("                saved = save_baseline(record.CACHE_DIR, "
                "ratio, provenance,\n"
                "                                      handled=handled)\n"),
        "new": ("                saved = save_baseline(record.CACHE_DIR, "
                "ratio, provenance)\n"),
        "expect": [
            "test_the_baseline_this_run_wrote_is_never_called_stale",
            "test_the_operator_is_told_to_delete_it_once"],
    },
    {
        # And the opposite: the caller starts out believing a step it never
        # saw, so the stale baseline is never named.
        "name": "the-caller-starts-out-with-a-step-nothing-took",
        "file": RUN,
        "old": "            handled: list[str] = []\n",
        "new": "            handled: list[str] = ['a step nothing took']\n",
        "expect": [
            "test_an_interrupt_entering_the_save_names_the_stale_baseline",
            "test_an_interrupt_entering_the_invalidation_names_it_too",
            "test_a_second_interrupt_printing_the_line_does_not_carry_it_away"],
    },
    {
        # The invalidation is never recorded, so a baseline this run wrote
        # reads to the caller as one it never removed.
        "name": "the-invalidation-is-never-recorded",
        "file": RUN,
        "old": ("    _mark(handled, 'the old baseline is gone')\n"
                "    if ratio is None or not math.isfinite(ratio):\n"),
        "new": "    if ratio is None or not math.isfinite(ratio):\n",
        "expect": [
            "test_the_baseline_this_run_wrote_is_never_called_stale"],
    },
    {
        # Recorded before the step it names rather than after it, which is
        # the whole discipline: an interrupt entering clear_baseline then
        # leaves the caller believing an invalidation that never happened.
        "name": "the-invalidation-is-recorded-before-it-happens",
        "file": RUN,
        "old": """    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')
    # Recorded after the invalidation, never before it: from here on, a
    # baseline file on disk is one this run wrote, so the caller has
    # nothing to say about the file it finds.
    _mark(handled, 'the old baseline is gone')
""",
        "new": """    _mark(handled, 'the old baseline is gone')
    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')
""",
        "expect": [
            "test_an_interrupt_entering_the_invalidation_names_it_too"],
    },
    {
        # The invalidation is never asked what it said, so the caller says
        # the same sentence about the same file a second time.
        "name": "the-invalidation-is-never-asked-what-it-said",
        "file": RUN,
        "old": "    if not clear_baseline(cache_dir, handled=handled):\n",
        "new": "    if not clear_baseline(cache_dir):\n",
        "expect": ["test_the_operator_is_told_to_delete_it_once"],
    },
    {
        # The same duplication reached from the other end: clear_baseline
        # says the line and records nothing about having said it.
        "name": "the-deletion-instruction-is-never-recorded",
        "file": RUN,
        "old": ("                interrupt)\n"
                "            _mark(handled, 'the operator was told to "
                "delete it')\n"
                "        raise interrupt\n"),
        "new": ("                interrupt)\n"
                "        raise interrupt\n"),
        "expect": ["test_the_operator_is_told_to_delete_it_once"],
    },
    {
        # Nothing is ever recorded, so every caller reading the list is
        # told the save got past nothing.
        "name": "nothing-the-save-got-past-is-recorded",
        "file": RUN,
        "old": ("    if handled is not None:\n"
                "        handled.append(step)\n"),
        "new": ("    if False:\n"
                "        handled.append(step)\n"),
        "expect": [
            "test_the_baseline_this_run_wrote_is_never_called_stale",
            "test_the_operator_is_told_to_delete_it_once"],
    },
    {
        # The other half of that check. Every caller with nothing to decide
        # passes nothing, and appending to it is an AttributeError that
        # takes out the ordinary save.
        "name": "a-step-is-recorded-into-nothing",
        "file": RUN,
        "old": ("    if handled is not None:\n"
                "        handled.append(step)\n"),
        "new": ("    if True:\n"
                "        handled.append(step)\n"),
        "expect": ["test_the_invalidation_needs_no_cache_directory"],
    },
    {
        # The line goes out through a bare print, so a second Ctrl+C
        # landing while it prints carries away the only warning the
        # operator gets.
        "name": "an-interrupt-printing-the-stale-baseline-line-carries-it-away",
        "file": RUN,
        "old": r"""                    stop = _say(
                        print,
                        '[x] the run was interrupted before the baseline '
""",
        "new": r"""                    print(
                        '[x] the run was interrupted before the baseline '
""",
        "expect": [
            "test_a_second_interrupt_printing_the_line_does_not_carry_it_away"],
    },
    {
        # The two signature lines. Neither carries behaviour of its own, so
        # the only mutation either admits is its own removal: the catch is
        # a NameError from the body that reads the argument, or a TypeError
        # from the caller that passes it, rather than a wrong answer. They
        # are here so a later edit cannot quietly drop the way the caller
        # asks what the save got past. What the argument DOES is guarded by
        # the-save-is-never-asked-what-it-got-past,
        # the-invalidation-is-never-asked-what-it-said and
        # nothing-the-save-got-past-is-recorded.
        "name": "the-save-cannot-be-asked-what-it-got-past",
        "file": RUN,
        "old": "                  provenance: dict, handled=None) -> bool:\n",
        "new": "                  provenance: dict) -> bool:\n",
        "expect": ["test_the_invalidation_needs_no_cache_directory"],
    },
    {
        "name": "the-invalidation-cannot-be-asked-what-it-said",
        "file": RUN,
        "old": ("def clear_baseline(cache_dir: Path, out=print, "
                "handled=None) -> bool:\n"),
        "new": "def clear_baseline(cache_dir: Path, out=print) -> bool:\n",
        "expect": ["test_the_invalidation_needs_no_cache_directory"],
    },
    {
        # The operator's Ctrl+C is swallowed after the warning, so the run
        # ends on an ordinary exit code and the stop they asked for is
        # never delivered.
        "name": "the-interrupt-is-swallowed-after-the-stale-baseline-line",
        "file": RUN,
        "old": "                raise stop\n",
        "new": "                return 1\n",
        "expect": [
            "test_an_interrupt_entering_the_save_names_the_stale_baseline",
            "test_the_baseline_this_run_wrote_is_never_called_stale",
            "test_a_baseline_that_is_not_there_is_never_called_stale"],
    },

    # -- A redacted transcript is never called correct (.1.18) -----------
    {
        # Length matches back under the field headed "correct", which under
        # the privacy default is every sentence in the run.
        "name": "a-length-match-is-listed-as-correct-again",
        "file": REPORT,
        "old": """    late = _late_sentences(sentences, parsed, (judge.CORRECT,))""",
        "new": """    late = _late_sentences(sentences, parsed,
                           (judge.CORRECT, judge.LENGTH_MATCHES))""",
        "expect": ["test_a_redacted_late_sentence_is_not_called_correct",
                   "test_a_length_match_is_never_counted_in_both_fields"],
    },
    {
        # The filter is ignored, so both fields list every sentence,
        # including the wrong and the missing ones.
        "name": "the-late-outcome-filter-is-ignored",
        "file": REPORT,
        "old": """        if result.outcome not in outcomes:""",
        "new": """        if False:""",
        "expect": ["test_a_redacted_late_sentence_is_not_called_correct",
                   "test_a_length_match_is_never_counted_in_both_fields",
                   "test_a_visible_late_sentence_is_still_called_correct"],
    },
    {
        # The length-match field disappears, so the slow redacted
        # sentences are reported nowhere and the template loses a line the
        # procedure asks for.
        "name": "the-length-match-field-is-not-printed",
        "file": REPORT,
        "old": """    lines.append(_field(
        'Sentences of the right length but late:',
        late_field(late_length, '; their words are not in the log')))""",
        "new": """    pass""",
        "expect": [
            "test_a_redacted_late_sentence_is_reported_under_its_own_field",
            "test_every_field_the_procedure_asks_for_is_filled",
            "test_a_visible_late_sentence_is_still_called_correct"],
    },

    # -- an unmeasured run must not print a measured zero (.4.1.5) -------
    {
        # The count alone, with the coverage dropped. A Google run
        # carries no engine_ratio on any utterance, so both fields go
        # back to reading 0 while nothing was timed -- the false
        # negative the finding names, for one of the two failures this
        # load test exists to tell apart.
        "name": "the-late-field-drops-what-it-could-not-time",
        "file": REPORT,
        "old": """        if lateness.eligible and not timed:""",
        "new": """        if False:""",
        "expect": [
            "test_a_google_run_never_says_zero_sentences_were_correct"
            "_but_late",
            "test_a_google_run_never_says_zero_of_the_right_length"
            "_were_late"],
    },
    {
        # The partial-coverage half. A run where some utterances were
        # timed and some were not reports its count with no word about
        # the ones it could not time, so the reader takes a partial
        # count for the whole run.
        "name": "the-late-field-hides-partial-coverage",
        "file": REPORT,
        "old": """        if not unmeasured:
            return counted""",
        "new": """        if True:
            return counted""",
        "expect": [
            "test_a_partly_timed_run_says_how_many_sentences"
            "_it_could_not_time"],
    },

    # -- A flag never written is never announced (.1.19) -----------------
    {
        # The interrupted-early case says it wrote, which is the false
        # restart notice the finding names.
        "name": "a-restore-that-wrote-nothing-says-it-wrote",
        "file": RUN,
        "old": """            self.changed = False
            return False
        if current != self._written:""",
        "new": """            self.changed = False
            return True
        if current != self._written:""",
        "expect": [
            "test_a_write_that_failed_is_not_announced_as_a_restore",
            "test_the_dictated_text_warning_is_silent_when_nothing_turned_on",
            "test_a_restore_with_nothing_to_put_back_says_it_wrote_nothing"],
    },
    {
        # A restore refused because someone else edited the file says it
        # wrote, so the setting is announced as put back while it is on.
        "name": "a-refused-restore-says-it-wrote",
        "file": RUN,
        "old": """                  f'{str(not self.value).lower()} yourself.')
            return False""",
        "new": """                  f'{str(not self.value).lower()} yourself.')
            return True""",
        "expect": [
            "test_a_flag_the_run_refused_to_put_back_is_not_announced"],
    },
    {
        # And the opposite: a real restore says it wrote nothing, which
        # silences the restart notice the .1.7 fix added.
        "name": "a-real-restore-says-it-wrote-nothing",
        "file": RUN,
        "old": """        _atomic_write(self.path, self._original)
        self.changed = False
        return True""",
        "new": """        _atomic_write(self.path, self._original)
        self.changed = False
        return False""",
        "expect": ["test_a_restore_that_wrote_says_it_wrote",
                   "test_a_flag_that_went_back_still_needs_a_restart",
                   "test_the_dictated_text_keeps_reaching_the_log_until_then",
                   "test_a_real_restore_still_carries_both_notices"],
    },

    # -- The procedure describes the default run (.1.38) -----------------
    {
        # The unqualified playback claim, put back. An operator who reads
        # it and runs the documented default command waits for audio that
        # never arrives: the default leaves wav_path as None and asks a
        # person to speak the six sentences.
        "name": "the-procedure-says-the-default-run-plays-audio",
        "file": PROCEDURE_DOC,
        "old": ("  the run is the real capture path. Both modes reach the "
                "recognizer through\n"
                "  the microphone: the default run needs a person to speak "
                "the six sentences,\n"
                "  and `--playback` plays them out of the speakers instead. "
                "Neither mode hands"),
        "new": ("  the run is the real capture path. The script plays audio "
                "and reads the log;\n"
                "  it never hands"),
        "expect": [
            "test_the_bullet_never_promises_playback_without_the_flag",
            "test_the_bullet_says_the_default_run_needs_a_person_to_speak"],
    },
    # -- The procedure matches the capture-gap ruling (.1.20) -------------
    {
        # The instruction the ruling removed, put back. An operator
        # following it throws away every loaded run the tool exists to
        # measure and hunts a fault the tool declines to claim.
        "name": "the-procedure-stops-the-run-for-one-marked-window",
        "file": PROCEDURE_DOC,
        "old": ("  One such window does not spoil the run, and it is not a "
                "reason to stop. The\n"),
        "new": ("  so stop the test and fix the microphone before reading "
                "anything else. The\n"),
        "expect": [
            "test_the_procedure_does_not_stop_the_run_for_one_marked_window"],
    },
    {
        # The document stops naming the line the report prints, so the
        # operator has nothing to match the printed count against.
        "name": "the-procedure-drops-the-printed-gap-line",
        "file": PROCEDURE_DOC,
        "old": "  `capture readings missing for N of M windows` beside that",
        "new": "  a count of the windows it could not measure beside that",
        "expect": [
            "test_the_procedure_names_the_line_the_report_prints_for_the_gap"],
    },
    {
        # The all-or-nothing refusal condition becomes a partial one, which
        # is the same wrong reading in different words.
        "name": "the-procedure-refuses-a-partly-measured-run",
        "file": PROCEDURE_DOC,
        "old": ("  tool refuses a run, with the verdict `capture "
                "unavailable`, only when every\n  window in it reads "
                "unavailable, because"),
        "new": ("  tool refuses a run as soon as one\n  window in it reads "
                "unavailable, because"),
        "expect": [
            "test_the_procedure_states_the_condition_for_refusing_a_run"],
    },
    {
        # The other end of the tie: the report renames the field and the
        # document is now describing a line that is never printed. This is
        # what makes the document test more than a spelling check.
        "name": "the-printed-gap-line-is-renamed-under-the-procedure",
        "file": REPORT,
        "old": """            f'  capture readings missing for '""",
        "new": """            f'  capture numbers absent for '""",
        # Not test_a_fully_measured_run_prints_no_gap_line: it asserts the
        # line is ABSENT, which a rename keeps true.
        "expect": [
            "test_the_procedure_names_the_line_the_report_prints_for_the_gap",
            "test_the_missing_count_prints_beside_the_verdict"],
    },

    # -- A second interrupt still finishes the cleanup (.1.21) ------------
    {
        # The kill loop stops deferring the interrupt, so the spinners
        # after the one it arrived on are never asked to stop.
        "name": "an-interrupt-abandons-the-remaining-kills",
        "file": RUN,
        "old": """            spinner.kill()
        except OSError:
            pass
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop""",
        "new": """            spinner.kill()
        except OSError:
            pass""",
        "expect": [
            "test_an_interrupt_killing_one_spinner_still_kills_the_rest"],
    },
    {
        # The wait loop stops deferring it. The kills are issued by then,
        # but a child nobody waits on stays a zombie process and the run
        # never reports which spinner refused to go.
        "name": "an-interrupt-abandons-the-remaining-waits",
        "file": RUN,
        "old": """                interrupt)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop""",
        "new": """                interrupt)""",
        "expect": [
            "test_an_interrupt_waiting_on_one_spinner_still_reaps_the_rest"],
    },

    # -- The timeout warning is deferred output too (.1.32) --------------
    {
        # The defect exactly as it stood, with the injection point kept so
        # the mutant is the behaviour and not a signature error: the
        # warning is said OUTSIDE the deferral. An exception raised inside
        # an `except` arm is not caught by a sibling arm of the same
        # `try`, so a Ctrl+C landing while the warning prints leaves the
        # whole statement and the spinners after it are never waited on.
        # Measured against the real function: killed=[1, 2, 3] waited=[1].
        "name": "the-timeout-warning-is-said-outside-the-deferral",
        "file": RUN,
        "old": """            interrupt = _say(
                out,
                f'[!] spinner pid {spinner.pid} did not exit; stop it '
                'yourself',
                interrupt)""",
        "new": """            out(f'[!] spinner pid {spinner.pid} did not exit; stop it '
                'yourself')""",
        "expect": [
            "test_an_interrupt_in_the_timeout_warning_still_reaps_the_rest"],
    },
    {
        # The pre-fix line restored verbatim, bare `print` and all. The
        # warning then goes round the injection point entirely, so a test
        # cannot see it and cannot interrupt it -- which is how this defect
        # sat under a passing spinner suite for a whole review round.
        "name": "the-timeout-warning-goes-back-to-a-bare-print",
        "file": RUN,
        "old": """            interrupt = _say(
                out,
                f'[!] spinner pid {spinner.pid} did not exit; stop it '
                'yourself',
                interrupt)""",
        "new": """            print(f'[!] spinner pid {spinner.pid} did not exit; stop it '
                  'yourself')""",
        "expect": [
            "test_an_interrupt_in_the_timeout_warning_still_reaps_the_rest",
            "test_a_timeout_warning_keeps_an_interrupt_the_kill_loop_stored"],
    },
    {
        # The interrupt `_say` caught is thrown away instead of carried,
        # so a run the operator interrupted during the warning finishes
        # its waits and then exits as though nothing happened.
        "name": "the-timeout-warning-drops-the-interrupt-it-caught",
        "file": RUN,
        "old": """            interrupt = _say(
                out,
                f'[!] spinner pid {spinner.pid} did not exit; stop it '""",
        "new": """            _say(
                out,
                f'[!] spinner pid {spinner.pid} did not exit; stop it '""",
        "expect": [
            "test_an_interrupt_in_the_timeout_warning_still_reaps_the_rest"],
    },
    {
        # The other direction: an interrupt the KILL loop already stored is
        # erased on the way in, because `_say` returns what it was given
        # when the line prints without incident. Only a test that stores
        # one before the warning can tell.
        "name": "the-timeout-warning-forgets-an-interrupt-already-held",
        "file": RUN,
        "old": """                'yourself',
                interrupt)""",
        "new": """                'yourself',
                None)""",
        "expect": [
            "test_a_timeout_warning_keeps_an_interrupt_the_kill_loop_stored"],
    },
    {
        # The injection point is removed, so the warning can only ever go
        # to stdout and no test can interrupt it. The call site in `main`
        # passes nothing, so nothing there notices.
        "name": "the-spinner-stop-loses-its-output-hook",
        "file": RUN,
        "old": "def stop_spinners(spinners: list[subprocess.Popen], "
               "out=print) -> None:",
        "new": "def stop_spinners(spinners: list[subprocess.Popen]) -> None:",
        "expect": [
            "test_an_interrupt_in_the_timeout_warning_still_reaps_the_rest",
            "test_a_timeout_warning_keeps_an_interrupt_the_kill_loop_stored"],
    },
    {
        # The hook survives but its default stops being stdout, so the
        # operator whose spinner refused to die is told nothing at all --
        # and every injecting test still passes.
        "name": "the-spinner-stop-defaults-away-from-stdout",
        "file": RUN,
        "old": "def stop_spinners(spinners: list[subprocess.Popen], "
               "out=print) -> None:",
        "new": "def stop_spinners(spinners: list[subprocess.Popen], "
               "out=lambda _m: None) -> None:",
        "expect": [
            "test_the_timeout_warning_goes_to_stdout_by_default"],
    },
    {
        # The deferred interrupt is swallowed instead of raised, so a run
        # the operator interrupted twice exits as though nothing happened.
        # `pass` rather than a deletion: an empty `if` body is a
        # SyntaxError, and a mutant the interpreter rejects reports as
        # caught while proving nothing.
        "name": "the-deferred-spinner-interrupt-is-swallowed",
        "file": RUN,
        "old": """                interrupt = stop
    if interrupt is not None:
        raise interrupt""",
        "new": """                interrupt = stop
    if interrupt is not None:
        pass""",
        "expect": [
            "test_an_interrupt_killing_one_spinner_still_kills_the_rest",
            "test_an_interrupt_waiting_on_one_spinner_still_reaps_the_rest"],
    },
    {
        # restore_all stops deferring it, so the flags after the one the
        # interrupt arrived on stay at the test value -- for
        # LOG_TRANSCRIPTS, dictated text keeps reaching the log.
        "name": "an-interrupt-abandons-the-remaining-restores",
        "file": RUN,
        "old": """        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            interrupt = _say(
                out,
                f'[!] interrupted before {edit.path} went back. Set '
                f'{edit.key} to {str(not edit.value).lower()} yourself.',
                interrupt)
            continue""",
        "new": """        except KeyboardInterrupt:
            raise""",
        "expect": [
            "test_an_interrupt_restoring_one_flag_still_restores_the_rest",
            "test_the_restart_notice_survives_the_interrupt",
            "test_an_interrupt_printing_the_deferred_warning_restores_the_rest"],
    },
    {
        # The interrupt is raised after the loop instead of after the whole
        # function, so the operator whose LOG_TRANSCRIPTS did go back is
        # never told the running application keeps the test value.
        "name": "the-restore-interrupt-preempts-the-restart-notice",
        "file": RUN,
        "old": """        if restored:
            put_back.append(edit)""",
        "new": """        if restored:
            put_back.append(edit)
    if interrupt is not None:
        raise interrupt""",
        "expect": ["test_the_restart_notice_survives_the_interrupt"],
    },

    # -- The cleanup notices are inside the deferral too (.1.27) ----------
    {
        # The defect exactly as it stood: the line is printed with no try
        # above it, so a Ctrl+C arriving while it prints leaves restore_all
        # at once. The flags after it stay at the test value, and for
        # LOG_TRANSCRIPTS dictated text keeps reaching the log.
        # Written with escaped newlines, like the GATE_SELF patterns below
        # and for the same reason: the gate's own ``_restore`` opens with
        # the same bounded-retry line, so a pattern carrying that line as
        # real text would appear twice in THIS file and make the gate's
        # own retry patterns ambiguous.
        "name": "a-cleanup-line-is-printed-outside-the-interrupt-guard",
        "file": RUN,
        "old": ('    for _attempt in (1, 2):\n'
                '        try:\n'
                '            out(message)\n'
                '        except KeyboardInterrupt as stop:\n'
                '            if interrupt is None:\n'
                '                interrupt = stop\n'
                '            continue\n'
                '        return interrupt\n'
                '    return interrupt\n'),
        "new": ('    out(message)\n'
                '    return interrupt\n'),
        "expect": [
            "test_an_interrupt_printing_a_failure_warning_restores_the_rest",
            "test_an_interrupt_printing_the_deferred_warning_restores_the_rest",
            "test_an_interrupt_printing_the_restart_notice_keeps_the_rest",
            "test_an_interrupt_printing_the_dictated_text_line_still_stops",
            "test_the_interrupt_the_caller_gets_is_the_one_that_was_stored",
            "test_a_console_that_never_finishes_a_line_still_ends"],
    },
    {
        # One attempt, so an interrupted line is simply lost. The operator
        # is never told which file was left flipped or which value to set.
        "name": "a-cleanup-line-is-attempted-only-once",
        "file": RUN,
        "old": ('    for _attempt in (1, 2):\n'
                '        try:\n'
                '            out(message)\n'),
        "new": ('    for _attempt in (1,):\n'
                '        try:\n'
                '            out(message)\n'),
        "expect": [
            "test_an_interrupt_printing_a_failure_warning_restores_the_rest",
            "test_a_console_that_never_finishes_a_line_still_ends"],
    },
    {
        # The other side of the bound. Three attempts is still finite, so
        # only a test that counts them can tell it from two -- and the
        # count is what keeps an operator holding Ctrl+C down out of a
        # loop the cleanup cannot leave.
        "name": "a-cleanup-line-is-attempted-a-third-time",
        "file": RUN,
        "old": ('    for _attempt in (1, 2):\n'
                '        try:\n'
                '            out(message)\n'),
        "new": ('    for _attempt in (1, 2, 3):\n'
                '        try:\n'
                '            out(message)\n'),
        "expect": ["test_a_console_that_never_finishes_a_line_still_ends"],
    },
    {
        # The interrupt is caught and dropped rather than deferred, so the
        # run ends with a zero exit and the operator is told the test
        # finished normally.
        "name": "a-cleanup-interrupt-is-swallowed-instead-of-deferred",
        "file": RUN,
        "old": """        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        return interrupt""",
        "new": """        except KeyboardInterrupt:
            continue
        return interrupt""",
        "expect": [
            "test_an_interrupt_printing_a_failure_warning_restores_the_rest",
            "test_an_interrupt_printing_the_restart_notice_keeps_the_rest",
            "test_a_console_that_never_finishes_a_line_still_ends"],
    },
    {
        # The held interrupt is thrown away at the call site instead. Each
        # of the four sites carries it back, and a site that does not ends
        # the run as though nothing had interrupted it.
        "name": "the-failure-warning-drops-the-interrupt-it-held",
        "file": RUN,
        "old": """            interrupt = _say(
                out,
                f'[!] could not put {edit.path} back ({error}). Set '""",
        "new": """            _say(
                out,
                f'[!] could not put {edit.path} back ({error}). Set '""",
        "expect": [
            "test_an_interrupt_printing_a_failure_warning_restores_the_rest"],
    },
    {
        "name": "the-restart-notice-drops-the-interrupt-it-held",
        "file": RUN,
        "old": """        interrupt = _say(
            out,
            f'[!] {edit.key} went back to '""",
        "new": """        _say(
            out,
            f'[!] {edit.key} went back to '""",
        # Only this test. The all-interrupting console test still ends on
        # an interrupt, because the dictated-text line that follows carries
        # one of its own back.
        "expect": [
            "test_an_interrupt_printing_the_restart_notice_keeps_the_rest"],
    },
    {
        "name": "the-dictated-text-line-drops-the-interrupt-it-held",
        "file": RUN,
        "old": """        interrupt = _say(
            out,
            '[!] Until that restart, DICTATED TEXT keeps reaching the log.',
            interrupt)""",
        "new": """        _say(
            out,
            '[!] Until that restart, DICTATED TEXT keeps reaching the log.',
            interrupt)""",
        "expect": [
            "test_an_interrupt_printing_the_dictated_text_line_still_stops"],
    },

    # -- A rotated run records no baseline (.1.22) ------------------------
    {
        # The fragment is offered as this machine's calibration again,
        # which is the whole finding: nothing marks a saved baseline as
        # partial, so every later load run reads it as the whole truth.
        "name": "a-rotated-run-still-offers-its-fragment-as-a-baseline",
        "file": RUN,
        "old": """    if rotated:
        return None
    return worst_ratio(parsed)""",
        "new": """    return worst_ratio(parsed)""",
        "expect": ["test_a_rotated_run_offers_no_ratio_to_save",
                   "test_the_refusal_removes_an_earlier_baseline"],
    },
    {
        # The other direction: a whole run is treated as rotated, so no
        # calibration can ever be recorded.
        "name": "no-run-at-all-offers-a-baseline",
        "file": RUN,
        "old": """    if rotated:
        return None
    return worst_ratio(parsed)""",
        "new": """    return None""",
        "expect": ["test_a_whole_run_still_offers_its_worst_ratio"],
    },
    {
        # The finding itself: the rotation is printed before the report,
        # and the procedure tells the operator to copy the block. Drop the
        # line from the block and every later reader of that template sees
        # a verdict with nothing saying how much of the run it rests on.
        # The body becomes `pass` rather than being deleted, because an
        # empty if body is a SyntaxError and a mutant the interpreter
        # rejects reports as caught while proving nothing.
        "name": "the-report-drops-the-rotation-line",
        "file": REPORT,
        "old": """    if rotated:
        lines.append(
            '  the log rotated during this run, so the numbers above rest '
            'on part of it and may include lines from before the playback '
            'started')""",
        "new": """    if rotated:
        pass""",
        "expect": ["test_a_rotated_run_names_the_rotation_in_the_block",
                   "test_the_rotation_sits_between_the_verdict_and_its_evidence",
                   "test_the_rotation_line_says_what_it_costs_the_reader"],
    },
    {
        # The other direction, and the worse one to read: every ordinary
        # run carries a warning about a rotation that did not happen, so
        # the notice means nothing on the run where it is true.
        "name": "the-report-names-a-rotation-that-did-not-happen",
        "file": REPORT,
        "old": """    if rotated:
        lines.append(
            '  the log rotated""",
        "new": """    if not rotated:
        lines.append(
            '  the log rotated""",
        "expect": ["test_a_whole_run_names_no_rotation",
                   "test_a_rotated_run_names_the_rotation_in_the_block",
                   "test_the_rotation_sits_between_the_verdict_and_its_evidence",
                   "test_the_rotation_line_says_what_it_costs_the_reader"],
    },
    {
        # The line survives but says only that something happened. A
        # reader who is not told the numbers above cover part of the run
        # has no reason to treat the block differently from a whole one.
        "name": "the-rotation-line-drops-what-it-costs-the-reader",
        "file": REPORT,
        "old": """            '  the log rotated during this run, so the numbers above rest '
            'on part of it and may include lines from before the playback '
            'started')""",
        "new": """            '  the log rotated during this run')""",
        "expect": ["test_the_rotation_line_says_what_it_costs_the_reader"],
    },
    {
        # Placement, which is the half of this finding a present-or-absent
        # check cannot see. The line is moved, not copied: appending a
        # second one below the evidence leaves the first where it was, and
        # the placement assertion passes over the mutant. Here the rotation
        # prints above the verdict instead, ahead of the line the reader is
        # looking for.
        "name": "the-rotation-line-prints-above-the-verdict",
        "file": REPORT,
        "old": """    lines.append(f'VERDICT: {verdict.verdict}')
    # The widest of the three statements about how much of this run the
    # verdict rests on, so it is read first. The run prints this before the
    # report as well, but the procedure tells the operator to copy the
    # block into the verdict template -- so anything outside the block is
    # lost to every later reader (finding wh-load-test-script.1.22, and the
    # same weakness .1.18 fixed for the late-sentence field).
    if rotated:
        lines.append(
            '  the log rotated during this run, so the numbers above rest '
            'on part of it and may include lines from before the playback '
            'started')""",
        "new": """    if rotated:
        lines.append(
            '  the log rotated during this run, so the numbers above rest '
            'on part of it and may include lines from before the playback '
            'started')
    lines.append(f'VERDICT: {verdict.verdict}')""",
        "expect": ["test_the_rotation_sits_between_the_verdict_and_its_evidence"],
    },
    {
        # The tie between the printed line and the rule that explains it,
        # broken from the report end. The line still says a rotation
        # happened and still says what it costs, so every other test here
        # stays green -- only the match against the document fails.
        "name": "the-rotation-line-is-renamed-under-the-procedure",
        "file": REPORT,
        "old": "'  the log rotated during this run, so the numbers above rest '",
        "new": "'  the log rotated mid-run, so the numbers above rest '",
        "expect": ["test_the_procedure_names_the_line_the_report_prints"],
    },
    {
        # The same tie broken from the document end, and the ruling with
        # it: a procedure that says to discard a rotated load run throws
        # away the sentences the run did measure.
        "name": "the-procedure-discards-a-rotated-run",
        "file": PROCEDURE_DOC,
        "old": "The run keeps its verdict, on the same rule",
        "new": "Discard the run rather than reading it, on the same rule",
        "expect": [
            "test_the_procedure_keeps_the_verdict_and_refuses_the_baseline"],
    },
    {
        # --check goes back to answering only "do the patterns match",
        # which is the state that let a mutant that does not parse sit in
        # this set until the full sweep found it hours in.
        "name": "the-check-stops-compiling-the-mutants",
        "file": GATE_SELF,
        "old": "        broken = check_compiles()\n",
        "new": "        broken = []\n",
        "expect": ["test_the_check_reports_a_mutant_that_does_not_compile"],
    },
    {
        # The other direction: prose is compiled as Python, so every
        # document mutation in the set reports as broken and the check
        # can never pass again.
        "name": "the-check-compiles-a-document-mutation",
        "file": GATE_SELF,
        "old": '        if path.suffix != ".py":\n            continue\n',
        "new": "        if False:\n            continue\n",
        "expect": ["test_the_check_does_not_compile_a_document_mutation"],
    },
    {
        # The whole guard removed. float() still accepts "NaN", so the
        # reading reaches main, and every comparison against it is false:
        # the loaded run calls saturation confirmed and the baseline run
        # calls the same machine idle. Negatives get through with it.
        "name": "the-counter-guard-is-removed",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n"
               "        return None\n",
        "new": "    if False:\n        return None\n",
        "expect": [
            "test_a_not_a_number_reading_is_refused",
            "test_an_infinite_reading_is_refused",
            "test_a_negatively_infinite_reading_is_refused",
            "test_the_python_spellings_of_the_same_values_are_refused",
            "test_a_negative_reading_is_refused",
            "test_a_barely_negative_reading_is_refused",
        ],
    },
    {
        # The finite half narrowed to the NaN spelling alone. NaN is still
        # refused by the "busy != busy" form, and -Infinity is still
        # refused -- but by the NEGATIVE half, not the finite one, since
        # -inf < 0.0 is true. Positive infinity is what gets through, and
        # it defeats the loaded gate: inf < 90 is false, so the run calls
        # saturation confirmed. The expect list names only the two tests
        # that really fail, because a mutation credited with a catch it
        # did not cause is a false catch.
        "name": "the-counter-guard-misses-the-infinities",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n",
        "new": "    if busy != busy or busy < 0.0:\n",
        "expect": [
            "test_an_infinite_reading_is_refused",
            "test_the_python_spellings_of_the_same_values_are_refused",
        ],
    },
    {
        # The negative half dropped. -5 is finite, so it reaches main,
        # where the baseline gate is the one that gets it wrong: -5 > 30
        # is false, so a --baseline run calls that machine idle and can
        # save a calibration no run ever measured.
        "name": "the-counter-accepts-a-negative-reading",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n",
        "new": "    if not math.isfinite(busy):\n",
        "expect": [
            "test_a_negative_reading_is_refused",
            "test_a_barely_negative_reading_is_refused",
        ],
    },
    {
        # The boundary moved by one comparison. An idle machine reads 0.0,
        # which is exactly what a --baseline run wants, so refusing it
        # means no baseline can ever be taken.
        "name": "the-counter-refuses-an-idle-machine",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n",
        "new": "    if not math.isfinite(busy) or busy <= 0.0:\n",
        "expect": [
            "test_a_zero_reading_is_still_returned",
            "test_an_idle_reading_of_zero_is_accepted",
        ],
    },
    {
        # A guard written as a truth test rather than a sign test. 0.0 is
        # falsy, so the same idle reading is thrown away for a different
        # reason, and this one also lets every negative through.
        "name": "the-counter-guard-refuses-a-real-reading",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n",
        "new": "    if not busy:\n",
        "expect": [
            "test_a_zero_reading_is_still_returned",
            "test_an_idle_reading_of_zero_is_accepted",
            "test_a_negative_reading_is_refused",
            "test_a_barely_negative_reading_is_refused",
        ],
    },
    {
        # The upper end closed. Left open on purpose (finding
        # wh-load-test-script.1.23, boss ruling of 2026-08-30): a Windows
        # processor counter can read slightly over 100 on a healthy
        # saturated machine, so this refusal would fire in exactly the
        # case the tool exists for. The mutation exists so a later change
        # cannot close it without reading that reasoning.
        "name": "the-counter-closes-the-upper-end",
        "file": RUN,
        "old": "    if not math.isfinite(busy) or busy < 0.0:\n",
        "new": "    if not math.isfinite(busy) or busy < 0.0 or busy > 100.0:\n",
        "expect": ["test_a_reading_above_one_hundred_is_still_accepted"],
    },

    # -- A second Ctrl+C during the gate restore (.1.24) ------------------
    {
        # The write's interrupt arm removed. A second Ctrl+C escapes the
        # restore and the real source file keeps the mutant, which every
        # later run and every other session in the checkout then reads as
        # the real code.
        "name": "the-gate-lets-a-second-interrupt-out-of-the-restore",
        "file": GATE_SELF,
        "old": ('            _atomic_write(path, original)\n'
                '        except OSError as e:\n'
                '            return (f"{name}: restore failed; the MUTANT '
                'REMAINS in "\n'
                '                    f"{path}: {e}"), interrupt\n'
                '        except KeyboardInterrupt as stop:\n'
                '            if interrupt is None:\n'
                '                interrupt = stop\n'
                '            continue\n'),
        "new": ('            _atomic_write(path, original)\n'
                '        except OSError as e:\n'
                '            return (f"{name}: restore failed; the MUTANT '
                'REMAINS in "\n'
                '                    f"{path}: {e}"), interrupt\n'),
        # NOT the temporary-file test: _atomic_write unlinks its own
        # temporary file in its own finally, so that one passes with or
        # without this arm and would be a catch this mutation did not
        # cause.
        # Widened to every test this really fails, read off a run of it,
        # once .1.26 added a class that drives the same cleanup.
        "expect": [
            "test_a_second_interrupt_during_the_restore_still_restores",
            "test_a_second_interrupt_does_not_skip_the_bytecode_clearing",
            "test_an_interrupt_on_every_attempt_says_the_mutant_may_remain",
            "test_an_interrupt_during_the_restore_alone_still_stops_the_run",
            "test_a_pending_restore_warning_survives_an_interrupt_in_the_walk",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator",
        ],
    },
    {
        # The read's interrupt arm removed. The other seam: the restore
        # reads the file before deciding what to do, and an interrupt
        # there leaves it having decided nothing.
        "name": "the-gate-lets-an-interrupt-out-of-the-restore-read",
        "file": GATE_SELF,
        "old": ('        except KeyboardInterrupt as stop:\n'
                '            if interrupt is None:\n'
                '                interrupt = stop\n'
                '            continue\n'
                '        if current == original:\n'),
        "new": ('        if current == original:\n'),
        "expect": ["test_a_second_interrupt_reading_the_file_still_restores"],
    },
    {
        # The retry removed. One attempt, so the interrupt that arrives
        # during it is the end of the restore.
        "name": "the-gate-gives-the-restore-one-attempt-only",
        "file": GATE_SELF,
        "old": "    for _attempt in (1, 2):\n",
        "new": "    for _attempt in (1,):\n",
        # Widened to every test this really fails, read off a run of it.
        "expect": [
            "test_a_second_interrupt_during_the_restore_still_restores",
            "test_a_second_interrupt_reading_the_file_still_restores",
            "test_a_restore_that_worked_says_nothing_about_a_mutant",
            "test_an_interrupt_during_the_restore_alone_still_stops_the_run",
        ],
    },
    {
        # The bound removed. An operator holding Ctrl+C down would keep
        # the gate in a loop it cannot leave.
        "name": "the-gate-retries-the-restore-without-a-bound",
        "file": GATE_SELF,
        "old": "    for _attempt in (1, 2):\n",
        "new": "    while True:\n",
        # Widened to every test this really fails, read off a run of it,
        # once .1.26 added a class that drives the same cleanup.
        "expect": [
            "test_an_interrupt_on_every_attempt_says_the_mutant_may_remain",
            "test_a_pending_restore_warning_survives_an_interrupt_in_the_walk",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator"],
    },
    {
        # The cleanup stops clearing the cache at all. The source goes
        # back and the mutant's bytecode stays beside it, for the next run
        # to execute. Refreshed after .1.26 gave the clearing a return
        # value; the call is replaced by the answer a clean walk gives, so
        # the names below it still exist.
        "name": "the-gate-raises-before-clearing-the-bytecode",
        "file": GATE_SELF,
        # Refreshed again when .1.33 put the call behind ``_call_cleanup``.
        "old": ('        (cache_error, cache_interrupt), cache_deferred = '
                '_call_cleanup(\n'
                '            _clear_pycache, SHARED)\n'),
        "new": ('        (cache_error, cache_interrupt), cache_deferred = '
                '(None, None), None\n'),
        # Every test this really fails, read off a run of it: nothing is
        # cleared, so every test that looks for an empty cache afterwards
        # goes down with the one that counts the call.
        "expect": [
            "test_a_second_interrupt_does_not_skip_the_bytecode_clearing",
            "test_a_cleanup_that_worked_says_nothing_and_clears_the_cache",
            "test_the_bytecode_is_still_cleared_when_the_walk_is_interrupted",
            "test_an_interrupt_in_the_walk_alone_still_stops_the_sweep",
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator"],
    },
    {
        # The deferred interrupt swallowed. The file goes back, but the
        # operator who pressed Ctrl+C twice is not stopped.
        "name": "the-gate-swallows-the-deferred-interrupt",
        "file": GATE_SELF,
        "old": "            raise interrupt\n",
        "new": "            pass\n",
        # Only the interrupt-during-restore-alone test can see this.
        # Every other test here has a FIRST interrupt already in flight
        # through the finally, and that one propagates whether or not the
        # deferred one is re-raised -- so they would pass over the mutant
        # and prove nothing. The cache-walk companion added by .1.26 is
        # the same shape and sees it for the same reason.
        "expect": [
            "test_an_interrupt_during_the_restore_alone_still_stops_the_run",
            "test_an_interrupt_in_the_walk_alone_still_stops_the_sweep"],
    },
    {
        # The report dropped. Raising skips the sweep loop that prints
        # restore errors, so a mutant left in a real source file is left
        # there with nobody told.
        # Refreshed after .1.26 made the same loop print the cache half
        # of the cleanup as well, and again after .2.1.1 moved the
        # emptiness test into the list the loop now walks.
        "name": "the-gate-says-nothing-about-a-mutant-it-left-behind",
        "file": GATE_SELF,
        "old": ('            for problem in problems:\n'
                '                print(f"ERROR {problem}")\n'),
        "new": ('            for problem in ():\n'
                '                print(f"ERROR {problem}")\n'),
        "expect": [
            "test_an_interrupt_on_every_attempt_says_the_mutant_may_remain",
            "test_a_pending_restore_warning_survives_an_interrupt_in_the_walk",
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator"],
    },

    # -- A ratio that is not a number (.1.25) ----------------------------
    {
        # The parser guard removed. float() still accepts "nan", so the
        # value reaches judge_run, where every comparison against it is
        # false and the ladder falls through to `neither` with evidence
        # calling the NaN ratio within 1.5 times the idle reading.
        "name": "a-non-finite-engine-ratio-is-read-as-a-measurement",
        "file": LOGPARSE,
        "old": "    if metrics.engine_ratio is not None and not math.isfinite(\n"
               "            metrics.engine_ratio):\n",
        "new": "    if False:\n",
        "expect": [
            "test_a_not_a_number_ratio_is_not_read_as_a_measurement",
            "test_an_infinite_ratio_is_not_read_as_a_measurement",
            "test_a_negatively_infinite_ratio_is_not_read_as_a_measurement",
            "test_every_spelling_of_those_values_is_refused",
            "test_a_not_a_number_ratio_cannot_earn_a_neither_verdict",
            "test_an_infinite_ratio_cannot_earn_a_confident_verdict",
            "test_a_negatively_infinite_ratio_cannot_earn_a_neither_verdict",
            "test_the_evidence_never_calls_a_non_finite_ratio_idle",
        ],
    },
    {
        # The guard narrowed to the NaN spelling alone. NaN is still
        # refused by the "x != x" form, so the tests that use it pass --
        # both infinities are what get through, and +inf then clears 1.0
        # and reports `inference lag` from a value nobody measured.
        "name": "the-ratio-guard-misses-the-infinities",
        "file": LOGPARSE,
        "old": "    if metrics.engine_ratio is not None and not math.isfinite(\n"
               "            metrics.engine_ratio):\n",
        "new": "    if metrics.engine_ratio != metrics.engine_ratio:\n",
        "expect": [
            "test_an_infinite_ratio_is_not_read_as_a_measurement",
            "test_a_negatively_infinite_ratio_is_not_read_as_a_measurement",
            "test_every_spelling_of_those_values_is_refused",
            "test_an_infinite_ratio_cannot_earn_a_confident_verdict",
            "test_a_negatively_infinite_ratio_cannot_earn_a_neither_verdict",
        ],
    },
    {
        # A guard written as a truth test rather than a finiteness test.
        # An utterance whose engine time fell below the timer's resolution
        # reads 0.0, and this throws that real measurement away.
        "name": "the-ratio-guard-refuses-a-zero-reading",
        "file": LOGPARSE,
        "old": "    if metrics.engine_ratio is not None and not math.isfinite(\n"
               "            metrics.engine_ratio):\n",
        "new": "    if not math.isfinite(metrics.engine_ratio) or "
               "not metrics.engine_ratio:\n",
        "expect": ["test_a_zero_ratio_is_still_read"],
    },
    {
        # The finite half of the writer removed. json.dumps writes NaN and
        # Infinity by default, so one unusable log line becomes this
        # machine's persisted calibration.
        "name": "a-non-finite-baseline-is-written-to-disk",
        "file": RUN,
        "old": "    if ratio is None or not math.isfinite(ratio):\n"
               "        return False\n",
        "new": "    if ratio is None:\n        return False\n",
        "expect": [
            "test_a_not_a_number_ratio_is_not_persisted",
            "test_an_infinite_ratio_is_not_persisted",
            "test_a_negatively_infinite_ratio_is_not_persisted",
            "test_an_earlier_baseline_does_not_outlive_a_refused_one",
        ],
    },
    {
        # The writer refuses a genuinely idle machine. 0.0 is falsy, and a
        # --baseline run on an idle machine is exactly what this tool
        # wants to be able to record.
        "name": "the-baseline-writer-refuses-a-zero-ratio",
        "file": RUN,
        "old": "    if ratio is None or not math.isfinite(ratio):\n",
        "new": "    if ratio is None or not math.isfinite(ratio) or "
               "not ratio:\n",
        "expect": ["test_a_zero_baseline_is_still_written_and_read"],
    },
    {
        # The finite half of the reader removed. json.loads reads NaN,
        # Infinity and -Infinity back, so a cache poisoned before the
        # writer was fixed is still reused as a calibration.
        "name": "a-non-finite-baseline-on-disk-is-reused",
        "file": RUN,
        "old": ("    if not math.isfinite(ratio):\n"
                "        out(f'[!] the saved baseline at "
                "{baseline_file(cache_dir)} holds '\n"
                "            f'{ratio!r}, which is not a measurement. "
                "Ignoring it. Run '\n"
                "            'again with --baseline to calibrate this "
                "machine.')\n"
                "        return None\n"),
        "new": "",
        "expect": [
            "test_a_not_a_number_baseline_on_disk_is_ignored",
            "test_an_infinite_baseline_on_disk_is_ignored",
            "test_a_negatively_infinite_baseline_on_disk_is_ignored",
            "test_a_poisoned_baseline_cannot_earn_a_neither_verdict",
        ],
    },
    {
        # The reader's finite half narrowed to the NaN spelling alone, so
        # the infinities are what get back through.
        "name": "the-baseline-reader-misses-the-infinities",
        "file": RUN,
        "old": "    if not math.isfinite(ratio):\n",
        "new": "    if ratio != ratio:\n",
        "expect": [
            "test_an_infinite_baseline_on_disk_is_ignored",
            "test_a_negatively_infinite_baseline_on_disk_is_ignored",
        ],
    },
    {
        # The reader throws away a genuinely idle calibration, for the
        # same falsy-zero reason as the writer.
        "name": "the-baseline-reader-refuses-a-zero-ratio",
        "file": RUN,
        "old": "    if not math.isfinite(ratio):\n",
        "new": "    if not math.isfinite(ratio) or not ratio:\n",
        "expect": ["test_a_zero_baseline_is_still_written_and_read"],
    },
    {
        # The poisoned file is ignored in silence. The operator is left
        # reading "no baseline" with a baseline file sitting on disk and
        # nothing saying why it was not used.
        "name": "the-poisoned-baseline-is-ignored-without-saying-so",
        "file": RUN,
        "old": ("        out(f'[!] the saved baseline at "
                "{baseline_file(cache_dir)} holds '\n"
                "            f'{ratio!r}, which is not a measurement. "
                "Ignoring it. Run '\n"
                "            'again with --baseline to calibrate this "
                "machine.')\n"),
        "new": "",
        "expect": ["test_a_not_a_number_baseline_on_disk_is_ignored"],
    },
    {
        # bool accepted again. isinstance(True, (int, float)) is True and
        # float(True) is 1.0, so a JSON `true` becomes a calibration of
        # 1.0 that no run ever measured.
        "name": "a-boolean-baseline-is-read-as-a-ratio",
        "file": RUN,
        "old": "    if isinstance(ratio, bool) or not isinstance("
               "ratio, (int, float)):\n",
        "new": "    if not isinstance(ratio, (int, float)):\n",
        "expect": ["test_a_true_baseline_is_not_a_ratio",
                   "test_a_false_baseline_is_not_a_ratio"],
    },
    {
        # bool rejected by narrowing the accepted type instead. JSON
        # writes a whole number as 1, and the reader has always taken an
        # int, so this refuses a real calibration.
        "name": "the-baseline-reader-refuses-an-integer-ratio",
        "file": RUN,
        "old": "    if isinstance(ratio, bool) or not isinstance("
               "ratio, (int, float)):\n",
        "new": "    if isinstance(ratio, bool) or not isinstance("
               "ratio, float):\n",
        "expect": ["test_an_integer_baseline_is_still_read"],
    },

    # -- Ctrl+C while the bytecode cache is cleared (.1.26) --------------
    {
        # The walk's interrupt arm removed, which is the state the finding
        # describes: the Ctrl+C escapes the finally, so the pending
        # restore warning is never printed and the cache is left part
        # cleared.
        "name": "the-gate-lets-an-interrupt-out-of-the-cache-walk",
        "file": GATE_SELF,
        "old": ('    for _pass in (1, 2):\n'
                '        try:\n'
                '            for d in service.rglob("__pycache__"):\n'
                '                if ".venv" not in d.parts:\n'
                '                    shutil.rmtree(d, ignore_errors=True)\n'
                '        except KeyboardInterrupt as stop:\n'
                '            if interrupt is None:\n'
                '                interrupt = stop\n'
                '            continue\n'),
        "new": ('    for _pass in (1, 2):\n'
                '        for d in service.rglob("__pycache__"):\n'
                '            if ".venv" not in d.parts:\n'
                '                shutil.rmtree(d, ignore_errors=True)\n'),
        "expect": [
            "test_a_pending_restore_warning_survives_an_interrupt_in_the_walk",
            "test_the_bytecode_is_still_cleared_when_the_walk_is_interrupted",
            "test_an_interrupt_in_the_walk_alone_still_stops_the_sweep",
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator",
            "test_a_pre_run_clearing_stops_the_sweep_and_says_why"],
    },
    {
        # One pass, so the interrupt that arrives during it is the end of
        # the clearing and the cache stays as the interrupt left it.
        "name": "the-gate-gives-the-cache-walk-one-pass-only",
        "file": GATE_SELF,
        "old": "    for _pass in (1, 2):\n",
        "new": "    for _pass in (1,):\n",
        "expect": [
            "test_the_bytecode_is_still_cleared_when_the_walk_is_interrupted",
            "test_an_interrupt_in_the_walk_alone_still_stops_the_sweep",
            "test_an_interrupt_on_every_pass_is_reported_and_bounded"],
    },
    {
        # The bound removed. An operator holding Ctrl+C down would keep
        # the gate walking the tree in a loop it cannot leave.
        "name": "the-gate-retries-the-cache-walk-without-a-bound",
        "file": GATE_SELF,
        "old": "    for _pass in (1, 2):\n",
        "new": "    while True:\n",
        # Every test whose interrupts run out before the loop does: the
        # pass after the last one succeeds, so the walk reports success
        # and none of the three sees the warning it is waiting for.
        "expect": [
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator",
            "test_a_pre_run_clearing_stops_the_sweep_and_says_why"],
    },
    {
        # The clearing gives up in silence. The bytecode that may still
        # hold the mutant is left with nobody told about it.
        "name": "the-gate-says-nothing-about-a-cached-mutant",
        "file": GATE_SELF,
        "old": ('    return (f"interrupted during every attempt to clear '
                '__pycache__ under "\n'
                '            f"{service}; a MUTANT .pyc MAY REMAIN there '
                '-- delete those "\n'
                '            "directories before the next run"), '
                'interrupt\n'),
        "new": "    return None, interrupt\n",
        "expect": [
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator",
            "test_a_pre_run_clearing_stops_the_sweep_and_says_why"],
    },
    {
        # The held interrupt dropped on the floor. The cleanup finishes,
        # but the operator who pressed Ctrl+C during the walk is not
        # stopped and the sweep runs on for hours.
        "name": "the-gate-swallows-the-cache-walk-interrupt",
        "file": GATE_SELF,
        "old": ('        if interrupt is None:\n'
                '            interrupt = cache_interrupt\n'),
        "new": ('        if False:\n'
                '            interrupt = cache_interrupt\n'),
        # Only a test whose sole interrupt is the walk's can see this
        # line. A test whose suite also raised has ``failure`` set, and
        # the print guard's failure arm (fa57bfc6) prints the warning
        # without this handoff, so that fixture reads the same either
        # way; the full sweep of 2026-09-02 found exactly that blindness.
        "expect": [
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it",
            "test_an_interrupt_in_the_walk_alone_on_every_pass_is_reported",
            "test_an_interrupt_in_the_walk_alone_still_stops_the_sweep"],
    },
    {
        # Only the source half of a failed cleanup is reported, so a
        # mutant .pyc left behind goes unmentioned.
        # Refreshed after .2.1.1 moved the pair into the list the
        # report walks.
        "name": "the-gate-reports-only-the-source-half-of-the-cleanup",
        "file": GATE_SELF,
        "old": ("        problems = [p for p in (restore_error, "
                "cache_error) if p]\n"),
        "new": "        problems = [p for p in (restore_error,) if p]\n",
        "expect": [
            "test_an_interrupt_on_every_pass_is_reported_and_bounded",
            "test_both_halves_of_a_failed_cleanup_reach_the_operator"],
    },
    {
        # The pre-run clearing swallows the operator's Ctrl+C, so the
        # sweep starts a suite the operator meant to stop.
        "name": "the-pre-run-cache-clearing-swallows-an-interrupt",
        "file": GATE_SELF,
        "old": ('    if interrupt is not None:\n'
                '        raise interrupt\n'),
        "new": "    return None\n",
        "expect": ["test_a_pre_run_clearing_stops_the_sweep_and_says_why"],
    },
    {
        # The pre-run clearing stops the sweep without saying why.
        "name": "the-pre-run-cache-clearing-says-nothing",
        "file": GATE_SELF,
        "old": ('    if error:\n'
                '        print(f"ERROR {error}")\n'),
        "new": ('    if False:\n'
                '        print(f"ERROR {error}")\n'),
        "expect": ["test_a_pre_run_clearing_stops_the_sweep_and_says_why"],
    },
    {
        # The venv guard removed. Every pass would delete thousands of
        # cached files this gate never mutates, and the environment would
        # rebuild itself between mutations.
        "name": "the-gate-clears-the-virtual-environment-too",
        "file": GATE_SELF,
        "old": ('                if ".venv" not in d.parts:\n'
                '                    shutil.rmtree(d, ignore_errors=True)\n'),
        "new": "                shutil.rmtree(d, ignore_errors=True)\n",
        "expect": ["test_the_walk_leaves_the_virtual_environment_alone"],
    },
    # --- wh-load-test-script.1.33: the cleanup-call guard, both copies ---
    {
        # The guard taken off the spinner stop, back to the pre-fix call.
        # A Ctrl+C at that call's door skips every kill and every wait,
        # and one busy process per core holds the machine at full load.
        "name": "the-cleanup-guard-is-gone-from-the-spinner-stop",
        "file": RUN,
        "old": ("            _, deferred = _call_cleanup(stop_spinners, "
                "spinners)\n"
                "            if deferred is not None:\n"
                "                raise deferred\n"),
        "new": "            stop_spinners(spinners)\n",
        "expect": [
            "test_an_interrupt_entering_the_spinner_stop_still_stops_them"],
    },
    {
        # The same, on the call that puts the config flags back. This is
        # the pre-fix code that could leave LOG_TRANSCRIPTS true with
        # nobody told.
        "name": "the-cleanup-guard-is-gone-from-the-flag-restore",
        "file": RUN,
        "old": ("        _, deferred = _call_cleanup(restore_all, edits)\n"
                "        if deferred is not None:\n"
                "            raise deferred\n"),
        "new": "        restore_all(edits)\n",
        "expect": [
            "test_an_interrupt_entering_the_flag_restore_still_restores"],
    },
    {
        # The trap this bead names, written into the guard: a plain
        # catch-and-retry, which calls a helper that ALREADY RAN a second
        # time every time it re-raises its own held interrupt.
        "name": "the-cleanup-guard-retries-a-call-that-already-ran",
        "file": RUN,
        "old": ("                if (step.tb_frame.f_code is code\n"
                "                        and step.tb_lineno != "
                "code.co_firstlineno):\n"
                "                    started = True\n"),
        "new": ("                if False:\n"
                "                    started = True\n"),
        "expect": [
            "test_a_call_that_ran_and_raised_is_not_made_again",
            "test_an_interrupt_inside_the_spinner_stop_stops_them_once",
            "test_an_interrupt_inside_the_flag_restore_restores_once"],
    },
    {
        # The other direction: a frame the interpreter had only just made
        # is read as a body that ran, so the call the interrupt reached at
        # its door is never made again and the cleanup is skipped exactly
        # as it was before this guard existed.
        "name": "the-cleanup-guard-calls-an-entered-frame-a-started-body",
        "file": RUN,
        "old": ("                if (step.tb_frame.f_code is code\n"
                "                        and step.tb_lineno != "
                "code.co_firstlineno):\n"),
        "new": "                if step.tb_frame.f_code is code:\n",
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_spinner_stop_still_stops_them",
            "test_an_interrupt_entering_the_flag_restore_still_restores"],
    },
    {
        # The retry removed. One attempt, so the interrupt that arrives at
        # the door is the end of the cleanup.
        "name": "the-cleanup-guard-gives-the-call-one-attempt-only",
        "file": RUN,
        "old": "    for _entry in (1, 2):\n",
        "new": "    for _entry in (1,):\n",
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_spinner_stop_still_stops_them",
            "test_an_interrupt_entering_the_flag_restore_still_restores"],
    },
    {
        # The bound removed. An operator holding Ctrl+C down would be put
        # in a loop they cannot leave.
        "name": "the-cleanup-guard-retries-the-call-without-a-bound",
        "file": RUN,
        "old": "    for _entry in (1, 2):\n",
        "new": "    while True:\n",
        "expect": [
            "test_a_call_stopped_at_its_door_every_time_is_bounded_at_two"],
    },
    {
        # The interrupt is not held, so a retry that succeeds swallows the
        # Ctrl+C: the run carries on as though the operator had not
        # pressed it.
        "name": "the-cleanup-guard-does-not-hold-the-first-interrupt",
        "file": RUN,
        "old": ("            if deferred is None:\n"
                "                deferred = stop\n"),
        "new": ("            if False:\n"
                "                deferred = stop\n"),
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_spinner_stop_still_stops_them",
            "test_an_interrupt_entering_the_flag_restore_still_restores"],
    },
    {
        # The held interrupt dropped at the spinner stop. The spinners go
        # down and the operator is not stopped.
        "name": "the-cleanup-guard-swallows-the-spinner-stop-interrupt",
        "file": RUN,
        "old": ("            if deferred is not None:\n"
                "                raise deferred\n"),
        "new": ("            if False:\n"
                "                raise deferred\n"),
        "expect": [
            "test_an_interrupt_entering_the_spinner_stop_still_stops_them"],
    },
    {
        # The same at the flag restore.
        "name": "the-cleanup-guard-swallows-the-flag-restore-interrupt",
        "file": RUN,
        "old": ("        if deferred is not None:\n"
                "            raise deferred\n"),
        "new": ("        if False:\n"
                "            raise deferred\n"),
        "expect": [
            "test_an_interrupt_entering_the_flag_restore_still_restores"],
    },
    {
        # Everything is deferred, not only a Ctrl+C. An OSError at the
        # door would be caught and the call made again.
        "name": "the-cleanup-guard-defers-more-than-a-keyboard-interrupt",
        "file": RUN,
        "old": ("        except KeyboardInterrupt as stop:\n"
                "            code = getattr(call, '__code__', None)\n"),
        "new": ("        except BaseException as stop:\n"
                "            code = getattr(call, '__code__', None)\n"),
        "expect": ["test_an_oserror_at_the_door_is_not_deferred"],
    },
    {
        # A callee with no code object leaves no frame to read, so the
        # guard cannot tell whether it ran; calling it again is the
        # double-run this whole distinction exists to avoid.
        "name": "the-cleanup-guard-retries-a-callee-with-no-code-object",
        "file": RUN,
        "old": "            started = code is None\n",
        "new": "            started = False\n",
        "expect": [
            "test_a_callee_with_no_code_object_is_never_called_twice"],
    },
    {
        # The gate's copy of the guard, taken off the restore call. The
        # pre-fix line, with the name the fold below it needs still
        # defined.
        "name": "the-gate-cleanup-guard-is-gone-from-the-restore",
        "file": GATE_SELF,
        "old": ('        (restore_error, interrupt), deferred = '
                '_call_cleanup(\n'
                '            _restore, path, original, mutated, name)\n'),
        "new": ('        restore_error, interrupt = _restore(\n'
                '            path, original, mutated, name)\n'
                '        deferred = None\n'),
        "expect": [
            "test_an_interrupt_entering_the_restore_still_puts_the_"
            "source_back"],
    },
    {
        # The same on the cache clearing.
        "name": "the-gate-cleanup-guard-is-gone-from-the-cache-clearing",
        "file": GATE_SELF,
        "old": ('        (cache_error, cache_interrupt), cache_deferred = '
                '_call_cleanup(\n'
                '            _clear_pycache, SHARED)\n'),
        "new": ('        cache_error, cache_interrupt = '
                '_clear_pycache(SHARED)\n'
                '        cache_deferred = None\n'),
        "expect": [
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it"],
    },
    {
        # The held interrupt never reaches the restore's own slot, so the
        # operator's second Ctrl+C is dropped.
        "name": "the-gate-drops-the-deferred-restore-interrupt",
        "file": GATE_SELF,
        "old": ('        if interrupt is None:\n'
                '            interrupt = deferred\n'),
        "new": ('        if False:\n'
                '            interrupt = deferred\n'),
        "expect": [
            "test_an_interrupt_entering_the_restore_still_puts_the_"
            "source_back"],
    },
    {
        # The same for the cache clearing's slot.
        "name": "the-gate-drops-the-deferred-cache-interrupt",
        "file": GATE_SELF,
        "old": ('        if cache_interrupt is None:\n'
                '            cache_interrupt = cache_deferred\n'),
        "new": ('        if False:\n'
                '            cache_interrupt = cache_deferred\n'),
        "expect": [
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it"],
    },
    {
        # The trap written into the gate's copy.
        "name": "the-gate-cleanup-guard-retries-a-call-that-already-ran",
        "file": GATE_SELF,
        "old": ('                if (step.tb_frame.f_code is code\n'
                '                        and step.tb_lineno != '
                'code.co_firstlineno):\n'
                '                    started = True\n'),
        "new": ('                if False:\n'
                '                    started = True\n'),
        "expect": [
            "test_a_call_that_ran_and_raised_is_not_made_again",
            "test_an_interrupt_leaving_the_restore_does_not_restore_twice"],
    },
    {
        # The gate's copy reading a just-made frame as a body that ran.
        "name": "the-gate-guard-calls-an-entered-frame-a-started-body",
        "file": GATE_SELF,
        "old": ('                if (step.tb_frame.f_code is code\n'
                '                        and step.tb_lineno != '
                'code.co_firstlineno):\n'),
        "new": '                if step.tb_frame.f_code is code:\n',
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_restore_still_puts_the_"
            "source_back",
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it"],
    },
    {
        # One attempt in the gate's copy.
        "name": "the-gate-cleanup-guard-gives-the-call-one-attempt-only",
        "file": GATE_SELF,
        "old": "    for _entry in (1, 2):\n",
        "new": "    for _entry in (1,):\n",
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_restore_still_puts_the_"
            "source_back",
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it"],
    },
    {
        # The bound removed in the gate's copy.
        "name": "the-gate-cleanup-guard-retries-the-call-without-a-bound",
        "file": GATE_SELF,
        "old": "    for _entry in (1, 2):\n",
        "new": "    while True:\n",
        "expect": [
            "test_a_call_stopped_at_its_door_every_time_is_bounded_at_two"],
    },
    {
        # The gate's copy dropping the interrupt it was meant to hold.
        "name": "the-gate-cleanup-guard-does-not-hold-the-first-interrupt",
        "file": GATE_SELF,
        "old": ('            if deferred is None:\n'
                '                deferred = stop\n'),
        "new": ('            if False:\n'
                '                deferred = stop\n'),
        "expect": [
            "test_a_call_stopped_at_its_door_is_made_again",
            "test_an_interrupt_entering_the_restore_still_puts_the_"
            "source_back",
            "test_an_interrupt_entering_the_cache_clearing_still_clears_it"],
    },
    {
        # The gate's copy deferring everything rather than a Ctrl+C.
        "name": "the-gate-cleanup-guard-defers-more-than-an-interrupt",
        "file": GATE_SELF,
        "old": ('        except KeyboardInterrupt as stop:\n'
                '            code = getattr(call, "__code__", None)\n'),
        "new": ('        except BaseException as stop:\n'
                '            code = getattr(call, "__code__", None)\n'),
        "expect": ["test_an_oserror_at_the_door_is_not_deferred"],
    },
    {
        # The gate's copy calling something with no code object twice.
        "name": "the-gate-guard-retries-a-callee-with-no-code-object",
        "file": GATE_SELF,
        "old": "            started = code is None\n",
        "new": "            started = False\n",
        "expect": [
            "test_a_callee_with_no_code_object_is_never_called_twice"],
    },
    {
        # A read the run could not make answering as an empty slice again.
        # The whole of finding wh-load-test-script.1.36: the two answers
        # were the same pair, so main judged a log it never read, printed
        # a completed template, returned 0, and under --baseline handed
        # save_baseline a run with no ratio -- which REMOVES the
        # operator's calibration.
        "name": "a-refused-read-answers-as-an-empty-log",
        "file": RUN,
        "old": ("            return handle.read(), rotated\n"
                "    except OSError:\n"
                "        return None\n"),
        "new": ("            return handle.read(), rotated\n"
                "    except OSError:\n"
                "        return '', False\n"),
        "expect": [
            "test_a_read_it_could_not_make_is_told_apart_from_an_empty_"
            "slice",
            "test_a_load_run_that_cannot_read_its_log_refuses_rather_"
            "than_judging",
            "test_a_baseline_run_that_cannot_read_its_log_keeps_the_"
            "calibration"],
    },
    {
        # The guard narrowed back to the absence of the file. os.fstat,
        # seek and read all run after the open has already succeeded, and
        # a sharing violation on any of them then escapes as a traceback
        # instead of being the same refused read.
        "name": "only-a-missing-log-is-a-read-it-could-not-make",
        "file": RUN,
        "old": ("            return handle.read(), rotated\n"
                "    except OSError:\n"
                "        return None\n"),
        "new": ("            return handle.read(), rotated\n"
                "    except FileNotFoundError:\n"
                "        return None\n"),
        "expect": [
            "test_a_read_it_could_not_make_is_told_apart_from_an_empty_"
            "slice",
            "test_a_failure_after_the_open_is_a_refused_read_not_a_"
            "traceback",
            "test_a_load_run_that_cannot_read_its_log_refuses_rather_"
            "than_judging"],
    },
    {
        # main judging a log it could not read. The refusal is what keeps
        # save_baseline away from the calibration, so this mutation is
        # also the one that costs the operator something real.
        "name": "an-unreadable-log-is-judged-as-an-empty-one",
        "file": RUN,
        "old": ("        after_playback = read_since_retrying"
                "(paths.log_file, mark)\n"
                "        if after_playback is None:\n"),
        "new": ("        after_playback = (read_since_retrying"
                "(paths.log_file, mark)\n"
                "                          or ('', False))\n"
                "        if after_playback is None:\n"),
        "expect": [
            "test_a_load_run_that_cannot_read_its_log_refuses_rather_"
            "than_judging",
            "test_a_baseline_run_that_cannot_read_its_log_keeps_the_"
            "calibration"],
    },
    {
        # The caller reading once. A rotation's sharing violation clears
        # in milliseconds, and refusing on the first one throws away a
        # run whose machine was saturated for minutes to produce it.
        "name": "the-final-read-is-not-retried",
        "file": RUN,
        "old": ("        after_playback = read_since_retrying"
                "(paths.log_file, mark)\n"),
        "new": ("        after_playback = read_since"
                "(paths.log_file, mark)\n"),
        "expect": ["test_a_refused_read_is_retried_before_the_run_is_"
                   "refused"],
    },
    {
        # The retry helper spending a single attempt, which is the same
        # defect one level down from the caller above.
        "name": "the-retry-gives-the-read-one-attempt",
        "file": RUN,
        "old": "    for remaining in range(attempts, 0, -1):\n",
        "new": "    for remaining in range(1, 0, -1):\n",
        "expect": ["test_a_refused_read_is_retried_before_the_run_is_"
                   "refused"],
    },
    {
        # The preflight letting a refused stat escape. Path.is_file
        # re-raises every OSError its own ignore list does not cover, and
        # measured on this interpreter that list does NOT hold EACCES,
        # EPERM or a Windows sharing violation.
        "name": "the-preflight-does-not-catch-a-refused-stat",
        "file": RUN,
        "old": ("        log_present = paths.log_file.is_file()\n"
                "    except OSError as error:\n"),
        "new": ("        log_present = paths.log_file.is_file()\n"
                "    except ValueError as error:\n"),
        "expect": [
            "test_the_preflight_says_so_when_the_log_cannot_be_stated"],
    },
    {
        # No mark answering as an empty slice rather than as a read the
        # run could not make. Distinct from
        # no-mark-still-reads-from-the-start, which puts a zero mark back
        # in place; this one keeps the early return and softens only its
        # answer.
        "name": "no-mark-answers-as-an-empty-log",
        "file": RUN,
        "old": ("    if mark is None:\n"
                "        return None\n"),
        "new": ("    if mark is None:\n"
                "        return '', False\n"),
        "expect": [
            "test_no_mark_reads_nothing_rather_than_the_whole_history"],
    },
    {
        # The readiness poll unpacking a result that can be None. Its
        # ANSWER does not change either way, so the caller contract is
        # what there is to guard: a later edit that unpacks again dies
        # here rather than in a live run.
        "name": "the-readiness-poll-unpacks-a-read-it-could-not-make",
        "file": RUN,
        "old": ("        result = read_since(path, mark)\n"
                "        if result is not None and "
                "logparse.parse_log(result[0]).windows:\n"),
        "new": ("        result = read_since(path, mark)\n"
                "        if logparse.parse_log(result[0]).windows:\n"),
        "expect": [
            "test_the_readiness_poll_survives_a_read_it_could_not_make",
            "test_a_refusal_does_not_end_the_readiness_poll_early"],
    },

    # -- A timed-out run must not outlive its timeout (.2) ----------------
    {
        # The pre-fix launch: the child is held only after the whole
        # constructor has returned, so an interrupt on the way out of
        # Popen leaves a pytest in no job and the job the gate closes is
        # empty. _HeldChild stays defined and unused, so what runs is
        # exactly the code this fix replaced.
        "name": "the-gate-child-is-held-after-the-constructor-returns",
        "file": GATE_SELF,
        "old": "        proc = _HeldChild(\n"
               "            tree, argv, cwd=cwd, env=env, stdout=subprocess.PIPE,\n"
               "            stderr=subprocess.PIPE, text=True)\n",
        "new": "        proc = subprocess.Popen(\n"
               "            argv, cwd=cwd, env=env, stdout=subprocess.PIPE,\n"
               "            stderr=subprocess.PIPE, text=True)\n"
               "        tree.hold(proc)\n",
        "expect": [
            "test_an_interrupt_on_the_way_out_of_popen_still_ends_the_child"],
    },
    {
        # The hold MOVES out of _execute_child and back to the end of
        # __init__, which is the same window a local variable leaves
        # open: the constructor has to return before anything holds the
        # child, and the interrupt lands before it does. It moves rather
        # than being copied, because a copy would leave the real hold in
        # place and the mutation would read as a survivor.
        "name": "the-gate-child-is-held-only-once-its-constructor-is-done",
        "file": GATE_SELF,
        "old": "        super().__init__(*args, **kwargs)\n"
               "\n"
               "    def _execute_child(self, *args, **kwargs) -> None:\n"
               "        super()._execute_child(*args, **kwargs)\n"
               "        try:\n"
               "            self._tree.hold(self)\n",
        "new": "        super().__init__(*args, **kwargs)\n"
               "        self._tree.hold(self)\n"
               "\n"
               "    def _execute_child(self, *args, **kwargs) -> None:\n"
               "        super()._execute_child(*args, **kwargs)\n"
               "        try:\n"
               "            pass\n",
        "expect": [
            "test_an_interrupt_on_the_way_out_of_popen_still_ends_the_child"],
    },
    {
        "name": "a-failed-restore-is-reported-only-for-an-interrupt",
        "file": GATE_SELF,
        "old": ("        if problems and (interrupt is not None or "
                "failure is not None):\n"),
        "new": "        if problems and interrupt is not None:\n",
        "expect": [
            "test_a_restore_refused_while_the_tree_did_not_end_is_reported"],
    },
    {
        # The child runs outside the job, which is the pre-fix state
        # with the job left standing empty: the timeout ends nothing
        # pytest started, the empty job reports zero active at once,
        # and the grandchild is still there when the call returns.
        "name": "the-run-is-not-held-as-a-tree",
        "file": GATE_SELF,
        # Escaped newlines keep the joined pattern out of this literal,
        # so the one match is the real function.
        # Refreshed after .2.1.2 moved the hold inside the child's own
        # constructor; the behaviour it breaks is unchanged.
        "old": "            self._tree.hold(self)\n",
        "new": "            pass\n",
        "expect": ["test_a_timed_out_run_takes_its_grandchildren_with_it"],
    },
    {
        # pytest through uv again: the process the gate holds is uv, and
        # a timeout ends uv while pytest carries on.
        "name": "pytest-runs-through-uv-again",
        "file": GATE_SELF,
        "old": ('    return [str(_interpreter(service)), "-m", "pytest", '
                'test_file, *extra]\n'),
        "new": '    return ["uv", "run", "pytest", test_file, *extra]\n',
        "expect": [
            "test_pytest_is_started_through_the_venv_interpreter_as_a_direct_child"],
    },
    {
        # A tree the gate could not end is reported and then the sweep
        # walks on to the next mutation, whose pytest starts on the
        # machine that tree is still saturating.
        "name": "a-tree-that-did-not-end-does-not-stop-the-sweep",
        "file": GATE_SELF,
        "old": ('            print("aborting remaining mutations: a process of '
                'the last "\n'
                '                  "run may still be running")\n'
                '            break\n'),
        "new": ('            print("aborting remaining mutations: a process of '
                'the last "\n'
                '                  "run may still be running")\n'
                '            continue\n'),
        "expect": ["test_a_tree_that_did_not_end_stops_the_sweep"],
    },
    # -- One parser reads both providers (wh-stt-load-metrics.4) ---------
    {
        # The five engine reads put back the way they were. int('n/a')
        # raises, the except drops the line, and a Google run loses every
        # capture and queue number it carried -- which is the defect the
        # optional readers exist to remove.
        "name": "the-engine-fields-are-read-strictly-again",
        "file": LOGPARSE,
        "old": "            engine_calls=_optional_int(values.get('engine_calls')),",
        "new": "            engine_calls=int(values['engine_calls']),",
        "expect": [
            "test_the_google_line_keeps_its_numbers_when_the_engine_fields_are_na",
            "test_an_engine_field_the_provider_never_measured_is_not_a_number",
            "test_the_stall_fields_are_read",
        ],
    },
    {
        # The shortcut the ruling refused: an unmeasured ratio read as
        # 0.00. That is the reading that rules inference lag OUT, and no
        # Google run has ever measured it.
        "name": "an-unmeasured-engine-ratio-is-reported-as-zero",
        "file": LOGPARSE,
        "old": "            engine_ratio=_optional_float(values.get('engine_ratio')),",
        "new": "            engine_ratio=_optional_float(values.get('engine_ratio')) or 0.0,",
        "expect": [
            "test_an_engine_field_the_provider_never_measured_is_not_a_number"],
    },
    {
        # The None arm of the finiteness guard removed. math.isfinite(None)
        # raises TypeError, which the except above does not catch, so
        # parse_log stops on the first Google line. The catch here IS a
        # TypeError at the mutated line, not an unrelated crash upstream of
        # the assertion: losing the arm is exactly a crash.
        "name": "the-finiteness-guard-stops-skipping-an-unmeasured-ratio",
        "file": LOGPARSE,
        "old": "    if metrics.engine_ratio is not None and not math.isfinite(\n"
               "            metrics.engine_ratio):\n",
        "new": "    if not math.isfinite(metrics.engine_ratio):\n",
        "expect": [
            "test_the_google_line_keeps_its_numbers_when_the_engine_fields_are_na",
            "test_an_engine_field_the_provider_never_measured_is_not_a_number",
            "test_the_stall_fields_are_read",
        ],
    },
    {
        "name": "the-utterance-stall-count-is-dropped",
        "file": LOGPARSE,
        "old": "            stalls=_optional_int(values.get('stalls')),",
        "new": "            stalls=None,",
        "expect": ["test_the_stall_fields_are_read"],
    },
    {
        "name": "the-utterance-worst-stall-is-dropped",
        "file": LOGPARSE,
        "old": "            stall_max_ms=_optional_float(values.get('stall_max_ms')),",
        "new": "            stall_max_ms=None,",
        "expect": ["test_the_stall_fields_are_read"],
    },
    {
        # A run that measured no engine time at all reads as a clean one.
        # This is the quiet version of the defect: no crash, no gap in the
        # report, just NO_FAILURE printed for a question nobody asked.
        "name": "a-ratio-nobody-measured-is-counted-as-zero",
        "file": JUDGE,
        "old": "    ratios = [u.engine_ratio for u in parsed.utterances\n"
               "              if u.engine_ratio is not None]\n",
        "new": "    ratios = [(u.engine_ratio or 0.0) for u in parsed.utterances]\n",
        "expect": [
            "test_utterances_without_a_ratio_do_not_earn_a_clean_reading"],
    },
    {
        # The filter removed outright. A run holding one measured and one
        # unmeasured utterance then raises inside max(). The catch is that
        # TypeError, at the mutated line.
        "name": "the-verdict-compares-a-ratio-against-nothing",
        "file": JUDGE,
        "old": "    ratios = [u.engine_ratio for u in parsed.utterances\n"
               "              if u.engine_ratio is not None]\n",
        "new": "    ratios = [u.engine_ratio for u in parsed.utterances]\n",
        "expect": ["test_a_run_that_measured_some_ratios_uses_the_ones_it_has"],
    },
    {
        # The saved calibration compares against an unmeasured ratio. Same
        # crash, in the file that writes this machine's baseline.
        "name": "the-baseline-compares-a-ratio-against-nothing",
        "file": RUN,
        "old": "    ratios = [u.engine_ratio for u in parsed.utterances\n"
               "              if u.engine_ratio is not None]\n",
        "new": "    ratios = [u.engine_ratio for u in parsed.utterances]\n",
        "expect": ["test_the_saved_baseline_ignores_an_unmeasured_ratio"],
    },
    {
        # The report's format specs put back. f'{None:.1f}' raises, so the
        # tool parses the Google run and then cannot print it.
        "name": "the-report-formats-a-number-nobody-measured",
        "file": REPORT,
        "old": "            f'engine_ms_total={figure(metrics.engine_ms_total, \".1f\")} '",
        "new": "            f'engine_ms_total={metrics.engine_ms_total:.1f} '",
        "expect": [
            "test_the_per_utterance_report_line_survives_a_google_utterance"],
    },
    {
        # The stall fields dropped from the report line. The numbers are
        # the whole reason the Google line exists, and a report without
        # them sends the reader back to the raw log.
        "name": "the-report-drops-the-stall-numbers",
        "file": REPORT,
        "old": "            f'stalls={shown(metrics.stalls)} '\n"
               "            f'stall_max_ms={figure(metrics.stall_max_ms, \".1f\")} '",
        "new": "",
        "expect": ["test_the_report_line_carries_the_stall_numbers"],
    },
]


def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


# Set for a mutant run and not for the baseline. A mutant replaces the
# very bytes its own entry names, so the whole-set self-check has to stand
# down while one is in the tree; the test that runs it skips on this.
MUTANT_ENV = "STT_LOAD_TEST_MUTANT"


def check_patterns(mutations=None) -> list:
    """Every ``old`` pattern that does not match its file exactly once.

    A pattern goes stale the moment a fix rewrites the code it names. The
    run already refuses a SELECTED entry whose pattern misses, but a
    name-filtered run selects almost nothing, so staleness in the rest of
    the set stayed invisible until the next full sweep. Four patterns rotted
    that way across two rounds of fixes while every targeted run printed a
    clean scope. This walks every entry and touches no file.
    """
    problems = []
    cache: dict = {}
    for m in mutations if mutations is not None else MUTATIONS:
        path = m["file"]
        if path not in cache:
            cache[path] = path.read_bytes()
        data = cache[path]
        count = data.count(_translate(m["old"], data))
        if count != 1:
            problems.append(f"{m['name']}: " + (
                "pattern not found" if count == 0
                else f"pattern ambiguous ({count} matches)"))
    return problems


def check_compiles(mutations=None) -> list:
    """Every Python mutant this set would build that is not valid Python.

    The sweep already refuses such a mutant, but it finds out hours in,
    one mutation at a time, after a run that proves nothing. ``--check``
    was blind to it: a pattern can match exactly once while the text it
    produces does not parse, and the fast check then reported the set
    clean. The full sweep of 2026-08-30 over head 70b3d017 found exactly
    that -- the ``.1.21`` fix added a second ``except`` arm to
    ``restore_all``'s loop, and a mutation written when there was one arm
    replaced the ``try`` with ``if``, leaving the second arm with no
    ``try`` above it.

    A mutation whose pattern is stale is skipped rather than reported
    twice: ``check_patterns`` already names it, and a replacement that
    landed nowhere says nothing about compiling.

    Non-Python files are skipped for the reason the sweep skips them:
    prose is not Python, so compiling a document mutation would report
    every one of them as broken.
    """
    problems = []
    cache: dict = {}
    for m in mutations if mutations is not None else MUTATIONS:
        path = m["file"]
        if path.suffix != ".py":
            continue
        if path not in cache:
            cache[path] = path.read_bytes()
        data = cache[path]
        old = _translate(m["old"], data)
        if data.count(old) != 1:
            continue
        mutated = data.replace(old, _translate(m["new"], data), 1)
        try:
            compile(mutated.decode("utf-8"), str(path), "exec")
        except SyntaxError as e:
            problems.append(f"{m['name']}: mutant does not compile: {e}")
    return problems


def _clear_pycache(service: Path):
    """Remove every __pycache__ under ``service``; return (error, interrupt).

    A KeyboardInterrupt is held rather than allowed out, on the same rule
    as ``_restore``, and returned for the caller to re-raise once the
    cleanup is done. The call this matters for is the one in
    ``_mutate_and_run``'s finally: an interrupt escaping from here skipped
    the print that tells the operator a mutant may still be sitting in a
    real source file, so the operator got a traceback and no warning. It
    could also stop the clearing part way through, which is the state that
    clearing exists to prevent -- a source mutation can keep the timestamp
    and the size Python checks before reusing a cached .pyc, so a mutant
    .pyc left beside a restored source is executed by the next process
    (finding wh-load-test-script.1.26).

    The retry is bounded at two passes, for the same reason the restore's
    is: an operator holding Ctrl+C down must not put the gate in a loop it
    cannot leave. ``rglob`` restarts on the second pass, so it reaches
    whatever the interrupted pass did not.
    """
    interrupt = None
    for _pass in (1, 2):
        try:
            for d in service.rglob("__pycache__"):
                if ".venv" not in d.parts:
                    shutil.rmtree(d, ignore_errors=True)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        return None, interrupt
    return (f"interrupted during every attempt to clear __pycache__ under "
            f"{service}; a MUTANT .pyc MAY REMAIN there -- delete those "
            "directories before the next run"), interrupt


def _clear_pycache_or_stop(service: Path) -> None:
    """Clear the cache before a run, and let an interrupt end the sweep.

    The two calls before a suite runs have no restore result pending, so
    there is nothing to hold the interrupt back for: it ends the sweep as
    it always did, but only after the clearing has finished or reported
    that it could not.
    """
    error, interrupt = _clear_pycache(service)
    if error:
        print(f"ERROR {error}")
    if interrupt is not None:
        raise interrupt


PYTEST_TIMEOUT_S = 300
# How long an ended tree is given to be gone. TerminateJobObject returns
# once every process in the job has been told to end; the job's active
# count reaching zero is what proves they did.
TREE_KILL_WAIT_S = 30

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class ProcessTreeNotHeld(Exception):
    """The gate cannot vouch that a pytest run left nothing running.

    Raised when the job object could not be made or the child could not
    be put in it, and when a tree that was ended still had processes in
    it after the wait. Never a verdict on a mutation: a sweep that
    carried on would start its next pytest on a machine the last one may
    still be saturating, which is how five 300 s timeouts in a row became
    a hang (wh-load-test-script.2).

    ``cleanup_problems`` carries whatever ``_mutate_and_run``'s finally
    found on the way out -- a restore that failed or refused, a cache
    that could not be cleared. The normal return that would have carried
    them never happens on this path, so the sweep reads them from here
    and counts them among its errors (.2.1.1).
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.cleanup_problems: list = []


if sys.platform == "win32":
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    def _kernel32():
        """kernel32 with the types declared, as ai/server_launcher.py does.

        Without an explicit restype ctypes assumes a 32-bit int, which
        cuts the top half off every handle these calls return.
        """
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.CreateJobObjectW.restype = wintypes.HANDLE
        dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        dll.SetInformationJobObject.restype = wintypes.BOOL
        dll.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        dll.QueryInformationJobObject.restype = wintypes.BOOL
        dll.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID]
        dll.AssignProcessToJobObject.restype = wintypes.BOOL
        dll.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE]
        dll.TerminateJobObject.restype = wintypes.BOOL
        dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        dll.CloseHandle.restype = wintypes.BOOL
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        return dll


class _HeldTree:
    """One child and everything it starts, held in a Windows job object.

    ``subprocess.run``'s timeout kills the one process it started. While
    that process was ``uv``, pytest and every spinner pytest had started
    lived on past the timeout, and the next mutation's run began on a
    machine the last one was still saturating. A job holds the child and
    its whole tree: a process joins its parent's job as it is created, so
    a spinner started by pytest is in the job before the gate could have
    seen it, ``TerminateJobObject`` ends every member in one call, and
    the job's own active count says when they are gone. The job is made
    with kill-on-close, so closing it after a run that finished normally
    also ends anything that run left behind, and the tree ends with the
    gate if the gate dies holding it.

    crewcut: the child is put in the job right after ``Popen`` returns,
    not created inside it, so a process the child starts before that
    call is not held. pytest cannot start anything in the milliseconds
    before it has imported itself. Closing the gap for good needs
    CREATE_SUSPENDED and a thread handle ``Popen`` does not expose.
    """

    def __init__(self) -> None:
        self._dll = _kernel32()
        self._job = self._dll.CreateJobObjectW(None, None)
        if not self._job:
            raise ProcessTreeNotHeld(
                f"CreateJobObject failed ({ctypes.get_last_error()})")
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        if not self._dll.SetInformationJobObject(
                self._job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ProcessTreeNotHeld(
                f"SetInformationJobObject failed ({error})")

    def hold(self, proc: subprocess.Popen) -> None:
        handle = self._dll.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not handle:
            raise ProcessTreeNotHeld(
                f"OpenProcess({proc.pid}) failed ({ctypes.get_last_error()})")
        try:
            if not self._dll.AssignProcessToJobObject(self._job, handle):
                raise ProcessTreeNotHeld(
                    f"AssignProcessToJobObject({proc.pid}) failed "
                    f"({ctypes.get_last_error()})")
        finally:
            self._dll.CloseHandle(handle)

    def active(self) -> int:
        """How many processes of the tree are still running."""
        accounting = _BasicAccountingInformation()
        if not self._dll.QueryInformationJobObject(
                self._job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ProcessTreeNotHeld(
                f"QueryInformationJobObject failed "
                f"({ctypes.get_last_error()})")
        return accounting.ActiveProcesses

    def end_all(self, wait_s: float) -> int:
        """End every process in the tree; return how many remain."""
        if not self._dll.TerminateJobObject(self._job, 1):
            raise ProcessTreeNotHeld(
                f"TerminateJobObject failed ({ctypes.get_last_error()})")
        deadline = time.monotonic() + wait_s
        left = self.active()
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = self.active()
        return left

    def close(self) -> None:
        job, self._job = self._job, None
        if job:
            self._dll.CloseHandle(job)


class _HeldChild(subprocess.Popen):
    """A child that joins the job the moment it exists.

    ``proc = subprocess.Popen(...)`` followed by ``tree.hold(proc)``
    puts the child in the job only after the whole constructor has
    returned. A ``KeyboardInterrupt`` delivered in between -- Windows
    has already made the process, and ``__init__`` has not handed it
    back yet -- leaves a pytest nobody holds. Read on this interpreter
    (CPython 3.12.10): ``Popen.__init__``'s failure cleanup closes the
    pipes and re-raises without killing (Lib/subprocess.py lines
    1035-1062), ``Popen.__del__`` does not kill either, and the job's
    kill-on-close ends only its members, so nothing ends a child whose
    object never reached the caller. That orphan runs the test file to
    the end with whatever mutant was in the source it imported, which
    is the harm this bead exists for. ``run._HeldSpinner`` closes the
    same window one process further in; this is the same override
    (.2.1.2).

    ``_execute_child`` is CPython's own private hook, and the earliest
    point at which the caller can be handed a process that exists: it
    returns with ``self.pid`` already set, which is all
    ``_HeldTree.hold`` needs. Its twenty-four positional parameters are
    all internal, so they are taken through ``*args``/``**kwargs`` and
    passed straight back, and a CPython that adds or renames one cannot
    break this override.

    crewcut: this narrows the window, it does not close it. An
    interrupt arriving inside ``_execute_child`` itself, after
    ``CreateProcess`` has returned but before this override regains
    control, still orphans that child. Closing the gap for good needs
    CREATE_SUSPENDED and a thread handle ``Popen`` does not expose --
    the same limit ``_HeldTree`` records for the grandchildren.
    """

    def __init__(self, tree: "_HeldTree", *args, **kwargs) -> None:
        self._tree = tree
        super().__init__(*args, **kwargs)

    def _execute_child(self, *args, **kwargs) -> None:
        super()._execute_child(*args, **kwargs)
        try:
            self._tree.hold(self)
        except ProcessTreeNotHeld:
            # Started and not held: the one process the gate can reach
            # is ended before the refusal goes up, because the
            # constructor's own cleanup will not do it.
            self.kill()
            self.wait(timeout=TREE_KILL_WAIT_S)
            raise


def _interpreter(service: Path) -> Path:
    """The service's own venv python, which pytest is started through.

    Not ``uv run``: uv is then the process the gate holds, and killing
    it leaves pytest and everything pytest started running. Not
    ``sys.executable`` either: the gate is run with whatever python is
    on the path, and that one has no pytest.
    """
    if sys.platform == "win32":
        return service / ".venv" / "Scripts" / "python.exe"
    return service / ".venv" / "bin" / "python"


def _pytest_argv(service: Path, test_file: str, *extra: str) -> list:
    return [str(_interpreter(service)), "-m", "pytest", test_file, *extra]


def _run_held(argv: list, *, cwd: Path, env, timeout: float):
    """Run ``argv`` to completion, or end it and everything it started.

    Returns a ``CompletedProcess`` as ``subprocess.run`` would. On
    timeout the whole tree is ended and waited for before
    ``TimeoutExpired`` is raised, so the caller reports the timeout
    against a machine that is idle again. A tree that cannot be held,
    or does not end within ``TREE_KILL_WAIT_S``, raises
    ``ProcessTreeNotHeld`` instead; that is not a verdict on any
    mutation, and the sweep stops on it.
    """
    if sys.platform != "win32":
        raise ProcessTreeNotHeld(
            "this gate holds pytest in a Windows job object and has no "
            "process-tree kill for other platforms")
    tree = _HeldTree()
    try:
        proc = _HeldChild(
            tree, argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            left = tree.end_all(TREE_KILL_WAIT_S)
            if left:
                raise ProcessTreeNotHeld(
                    f"{left} process(es) of the timed-out run are still "
                    f"running {TREE_KILL_WAIT_S} s after being ended; stop "
                    "them before the next run")
            # Every writer to the pipes is gone, so this returns at
            # once; without it the pipes stay open and the child is
            # never reaped.
            proc.communicate()
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        tree.close()


def _run_pytest(service: Path, test_file: str, mutant: bool = False):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if mutant:
        env[MUTANT_ENV] = "1"
    return _run_held(
        _pytest_argv(service, test_file, "-rf", "-q", "-p", "no:randomly"),
        cwd=service, env=env, timeout=PYTEST_TIMEOUT_S)


def _collected_names(service: Path, test_file: str):
    run = _run_held(
        _pytest_argv(service, test_file, "--collect-only", "-q"),
        cwd=service, env=None, timeout=PYTEST_TIMEOUT_S)
    names = set()
    for line in run.stdout.splitlines():
        if "::" in line:
            names.add(re.sub(r"\[.*\]$", "", line.rsplit("::", 1)[-1]))
    return names


def _failed_names(output: str):
    return {
        re.sub(r"\[.*\]$", "", line.split("::")[-1].split()[0])
        for line in output.splitlines()
        if line.startswith("FAILED")
    }


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace the whole file in one step, or not at all.

    A plain write truncates first, so an interrupt in the middle leaves
    bytes matching neither the original nor the mutant -- which ``_restore``
    then reads as somebody else's edit and refuses to touch, leaving a
    half-written source file in the tree.
    """
    # Named for this process. Two gate runs in one checkout used to
    # share this path and could each move the other's partial write over
    # the source.
    temp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _call_cleanup(call, *args):
    """Call a cleanup helper again if the interrupt beat it to its door.

    The twin of ``_call_cleanup`` in tools/stt_load_test/run.py, and the
    same function: a guard written inside a helper cannot cover that
    helper's own call boundary, because the interpreter checks for a
    pending signal at the callee's FIRST instruction. A Ctrl+C there
    skipped the whole call from the ``finally`` below -- leaving the
    mutant in a real source file for every later run and every other
    session in this checkout to read as the real code, or leaving a mutant
    .pyc beside a source file already put back
    (finding wh-load-test-script.1.33).

    It is a copy rather than an import on purpose. This gate is
    standard-library-only by design; ``run.py`` reaches ``shared_stt``
    through ``script.py`` and needs the environment, and, worse, this file
    exists to rewrite the very module it would be importing. A gate whose
    mutant recovery ran mutated code could not be trusted to recover
    anything. The two copies are kept identical in behaviour and each is
    mutated on its own.

    Returns ``(result, deferred)``: what ``call`` returned, and the
    KeyboardInterrupt held back here, or None. ``_restore`` and
    ``_clear_pycache`` RETURN their own interrupt inside an
    ``(error, interrupt)`` tuple instead of raising it, so their callers
    below fold the held one into that tuple's slot; raising it at the
    restore would skip the bytecode clearing that has to follow. The run
    tool's two helpers raise theirs, and its caller re-raises. This
    function is blind to the difference: it asks only where the interrupt
    was raised, never what the callee would have returned.

    The retry happens only when the body never ran, read off the
    traceback: a call the interrupt reached at its door leaves a frame of
    the callee's still sitting on the callee's own ``def`` line, or no
    frame of the callee's at all, while a body that ran left its frame on
    one of its statements. The line is compared rather than the
    instruction offset because a closure's frame begins past offset 0.
    For a return-shaped callee, an interrupt escaping the body means
    the body did not finish, so retrying it would be harmless -- both are
    written to be repeatable -- but it is not what this window is about
    and the call is not free, so the same rule is applied to both shapes
    and an escape from inside is left alone.

    Only KeyboardInterrupt is deferred; anything else propagates on the
    first attempt. The bound is two attempts, the bound ``_restore`` and
    ``_clear_pycache`` already use.

    crewcut: this does not close the window. The interrupt can land at
    this function's own call bytecode, or between the callee returning and
    its value being stored here. It makes the operator land two precisely
    timed interrupts where one used to do, rather than making the cleanup
    unskippable. Removing the remainder needs the calls to run with the
    signal blocked, which Python does not offer for SIGINT on Windows.
    """
    deferred = None
    for _entry in (1, 2):
        try:
            result = call(*args)
        except KeyboardInterrupt as stop:
            code = getattr(call, "__code__", None)
            started = code is None
            step = stop.__traceback__
            while step is not None:
                if (step.tb_frame.f_code is code
                        and step.tb_lineno != code.co_firstlineno):
                    started = True
                step = step.tb_next
            if started:
                raise
            if deferred is None:
                deferred = stop
            continue
        return result, deferred
    raise deferred


def _mutate_and_run(path: Path, original: bytes, mutated: bytes, name: str):
    """Write the mutant, run the suite, and always put the file back.

    The write belongs INSIDE this try. It used to sit above it, so a Ctrl+C
    or an OSError arriving between the write and the try skipped the finally
    and left the mutant in the working tree, where every later run -- and
    every other session sharing this checkout -- read it as the real code.
    """
    timed_out = False
    run = None
    # What is on its way out of this call, if anything. The finally
    # below reports a failed restore, and the only other way it can be
    # heard is the return value -- which an exception skips.
    failure = None
    try:
        _atomic_write(path, mutated)
        run = _run_pytest(SHARED, TESTS, mutant=True)
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException as problem:
        failure = problem
        raise
    finally:
        (restore_error, interrupt), deferred = _call_cleanup(
            _restore, path, original, mutated, name)
        # A Ctrl+C at the call's own door skipped the restore entirely and
        # left the mutant in the source file; the guard calls it once more
        # and hands back the interrupt it held, which belongs in the same
        # slot the restore's own would have used.
        if interrupt is None:
            interrupt = deferred
        # The cache is cleared even while an interrupt is in flight, and
        # before it is re-raised. Python decides whether cached bytecode
        # is current from the source file's timestamp and size, and a
        # mutation can keep both -- so a run that put the source back and
        # let the interrupt out here would leave the mutant's bytecode
        # beside the restored file, for the next run to execute. The
        # clearing holds its own interrupt for the same reason, so a
        # Ctrl+C landing inside the walk cannot carry the warning below
        # away with it or leave the cache half cleared.
        (cache_error, cache_interrupt), cache_deferred = _call_cleanup(
            _clear_pycache, SHARED)
        # The same door, on the call that stops a mutant .pyc being read
        # beside a source file already put back.
        if cache_interrupt is None:
            cache_interrupt = cache_deferred
        if interrupt is None:
            interrupt = cache_interrupt
        if cache_error:
            cache_error = f"{name}: {cache_error}"
        # Any exception leaving here skips the sweep loop that prints
        # restore errors, so this is the only place the operator can be
        # told the tree is still dirty. It was once only the interrupt
        # that reached this print, which was true while an interrupt was
        # the only exception anyone handled. ``ProcessTreeNotHeld`` is
        # now handled too, and it ends the sweep looking orderly: a
        # restore that failed or refused a concurrent edit went out
        # underneath it with nobody told, leaving the mutant in a
        # tracked file (.2.1.1). Both problems are printed: a mutant
        # left in a source file and a mutant .pyc left beside it are
        # separate repairs.
        problems = [p for p in (restore_error, cache_error) if p]
        if problems and (interrupt is not None or failure is not None):
            for problem in problems:
                print(f"ERROR {problem}")
            # The sweep counts what it was told. Only our own exception
            # reaches a handler that can read this; an interrupt ends
            # the run and anything else leaves a traceback, and neither
            # of those prints a summary line to be wrong.
            if isinstance(failure, ProcessTreeNotHeld):
                failure.cleanup_problems = problems
        if interrupt is not None:
            raise interrupt
    return run, timed_out, restore_error


def _restore(path: Path, original: bytes, mutated: bytes, name: str):
    """Put the pre-run bytes back; return (error string or None, interrupt).

    Never overwrite bytes this run did not write: a save landing from an
    editor while pytest ran would otherwise be replaced by the stale
    snapshot and lost.

    A KeyboardInterrupt is held rather than allowed out, and returned for
    the caller to re-raise once the whole cleanup is done. The first
    Ctrl+C stops pytest and reaches this restore; a second one used to
    escape from here, leaving a real source file holding the mutant, where
    every later run -- and every other session sharing the checkout --
    reads it as the real code. That is the worst outcome this gate has,
    and it is why the interrupt waits (finding wh-load-test-script.1.24;
    the same defect .1.21 fixed for the run tool's flag restore).

    The retry is bounded at two attempts. An operator holding Ctrl+C down
    must not put the gate in a loop it cannot leave, so the second failure
    reports instead of trying again. Re-reading on the second attempt is
    what makes the retry correct as well as safe: ``os.replace`` either
    happened or did not, so an interrupt arriving after a completed move
    finds the original already back and returns success.

    crewcut: the read and the replacement are not one transaction, so a
    save landing between this guard and ``_atomic_write`` is still lost.
    Removing that needs a per-path interprocess lock, which is not built
    because a lock binds only writers that take it and the writer here is
    an editor or another agent session. Ruling of 2026-08-30 on
    wh-load-test-script.1.12.
    """
    interrupt = None
    for _attempt in (1, 2):
        try:
            current = path.read_bytes()
        except OSError as e:
            return (f"{name}: cannot read {path} before restore; "
                    f"the MUTANT MAY REMAIN in source: {e}"), interrupt
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        if current == original:
            return None, interrupt
        if current != mutated:
            return (f"{name}: {path} changed while pytest ran; refusing to "
                    "overwrite the concurrent edit with the stale pre-run "
                    "snapshot -- reconcile the file by hand"), interrupt
        try:
            # Through the same temporary file and move as the mutant write.
            # A plain write opens the live source in truncating mode, so an
            # interrupt or a full disk part way through recovery leaves
            # bytes matching neither the original nor the mutant -- the one
            # state the next run refuses to touch.
            _atomic_write(path, original)
        except OSError as e:
            return (f"{name}: restore failed; the MUTANT REMAINS in "
                    f"{path}: {e}"), interrupt
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        return None, interrupt
    return (f"{name}: interrupted during every restore attempt; the "
            f"MUTANT MAY REMAIN in {path} -- check it before the next "
            "run"), interrupt


def main() -> int:
    argv = sys.argv[1:]
    if "--check" in argv:
        # Answer "are the patterns current" in seconds. The full sweep takes
        # hours, so starting it used to be the only way to find out.
        problems = check_patterns()
        broken = check_compiles()
        for p in problems + broken:
            print(f"ERROR {p}")
        # The counts stay separate. A stale pattern and a mutant that does
        # not parse are different repairs, and summing them would hide
        # which one this run found.
        print(f"checked {len(MUTATIONS)} patterns, {len(problems)} stale, "
              f"{len(broken)} that do not compile")
        return 1 if problems or broken else 0

    only = [a for a in argv if a != "--check"]
    selected = [m for m in MUTATIONS
                if not only or any(a in m["name"] for a in only)]
    if only and not selected:
        print(f"ERROR no mutation name matches {only}")
        return 1
    skipped = len(MUTATIONS) - len(selected)
    if only:
        # A filtered run checks only what it selected. Naming the rest here
        # is what stops the set decaying behind a clean-looking scope line.
        for p in check_patterns() + check_compiles():
            print(f"WARNING {p}")

    errors, survivors = [], []

    interpreter = _interpreter(SHARED)
    if not interpreter.exists():
        print(f"ERROR {interpreter} does not exist; run 'uv sync' in "
              f"{SHARED} first")
        return 1

    # Validate every expected name against real collection first: a renamed
    # test can never appear in the failed set, so its mutation would report
    # as a survivor while the test that catches it sits there passing.
    try:
        real = _collected_names(SHARED, TESTS)
    except ProcessTreeNotHeld as problem:
        print(f"ERROR collecting {TESTS}: {problem}")
        return 1
    if not real:
        print(f"ERROR {TESTS}: collected no tests")
        return 1
    for m in selected:
        for name in m["expect"]:
            if name not in real:
                errors.append(
                    f"{m['name']}: expected test {name} does not exist in "
                    f"{TESTS}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    _clear_pycache_or_stop(SHARED)
    try:
        base = _run_pytest(SHARED, TESTS)
    except ProcessTreeNotHeld as problem:
        print(f"ERROR baseline run of {TESTS}: {problem}")
        return 1
    if base.returncode != 0:
        print(f"ERROR baseline not green for {TESTS}; refusing to mutate")
        print(base.stdout[-2000:])
        return 1
    print(f"baseline green: {TESTS}")

    for m in selected:
        path = m["file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count != 1:
            problem = ("pattern not found" if count == 0
                       else f"pattern ambiguous ({count} matches)")
            errors.append(f"{m['name']}: {problem}")
            print(f"ERROR {m['name']}: {problem}")
            continue
        mutated = data.replace(old, new, 1)
        # Only Python is compiled. A markdown mutant has no syntax to check,
        # and compiling it would report every document mutation as a
        # SyntaxError -- which reads as "caught" and proves nothing.
        if path.suffix == ".py":
            try:
                compile(mutated.decode("utf-8"), str(path), "exec")
            except SyntaxError as e:
                errors.append(f"{m['name']}: mutant does not compile: {e}")
                print(f"ERROR {m['name']}: mutant does not compile: {e}")
                continue

        _clear_pycache_or_stop(SHARED)
        try:
            run, timed_out, restore_error = _mutate_and_run(
                path, data, mutated, m["name"])
        except ProcessTreeNotHeld as problem:
            # _mutate_and_run's finally ran on the way out, but a
            # restore can fail or refuse a concurrent edit without any
            # interrupt, so the source is not guaranteed to be back.
            # Whatever it found is printed there and counted here.
            # What may not be back either is the machine.
            errors.append(f"{m['name']}: {problem}")
            errors.extend(problem.cleanup_problems)
            print(f"ERROR {m['name']}: {problem}")
            print("aborting remaining mutations: a process of the last "
                  "run may still be running")
            break
        if restore_error:
            errors.append(restore_error)
            print(f"ERROR {restore_error}")
            print("aborting remaining mutations: source no longer matches "
                  "what this run snapshotted")
            break
        if timed_out or run is None:
            errors.append(f"{m['name']}: pytest timed out")
            print(f"ERROR {m['name']}: pytest timed out")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        failed = _failed_names(out)
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(f"{m['name']}: expected {missing}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
        else:
            print(f"caught {m['name']} by {sorted(failed & set(m['expect']))}")

    print(f"scope: ran {len(selected)} of {len(MUTATIONS)} mutations, "
          f"skipped {skipped} by name filter, "
          f"{len(survivors)} survivors, {len(errors)} errors")
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
