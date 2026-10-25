"""Mutation gate for wh-first-sound-lost.22 (the lead-word join of the Parakeet engine).

Acceptance item 8 asks for a mutation gate over the new and changed code. The
code is in sherpa_engine.py (LEAD_WORD, LeadWordError, remove_lead_word,
load_lead_word_audio, SherpaOfflineEngine._load_lead_audio, __init__,
_recognize, _decode_plain, and the order of last_final_audio in
_run_final_inference), main.py (the lead_word_path argument), and config.toml
(the empty lead_word_path key). The tests are tests/test_lead_word_join.py.

Run it from services/stt_providers/sherpa_offline_parakeet_stt_server with the
provider interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_lead_word.py            # the whole set
    .venv/Scripts/python.exe tests/mutation_gate_lead_word.py name       # one, by name filter
    .venv/Scripts/python.exe tests/mutation_gate_lead_word.py --check    # patterns only
    .venv/Scripts/python.exe tests/mutation_gate_lead_word.py --equivalent
        # runs EQUIVALENT_MUTANTS; every one of them must be reported SURVIVED

Two notes on mutants whose catch is a crash:

  - "lead-guard-inverted-to-always-join" and "lead-guard-removed" remove the
    check that stops np.concatenate((None, audio)). The tests fail with the
    exception that the guard exists to prevent. That is the behavior the
    join-off tests pin, so the catch is genuine.
  - "run-without-empty-check-on-match" and "text-or-removed-in-search" remove
    the None checks that remove_lead_word needs for "" and None. The
    test_the_rule cases "" and None are the inputs that need them.

Every other mutant runs to the assertion that names it.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The shared runner sits in services/stt_providers/shared/tests. Running the
# gate as a script does not put that directory on sys.path.
HERE = Path(__file__).resolve().parent
PROVIDER = HERE.parent
PROVIDERS = PROVIDER.parent
SHARED_TESTS = PROVIDERS / "shared" / "tests"
sys.path.insert(0, str(SHARED_TESTS))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

ENGINE = PROVIDER / "sherpa_engine.py"
MAIN = PROVIDER / "main.py"
CONFIG = PROVIDER / "config.toml"
TESTS = "tests/test_lead_word_join.py"


# The launcher: start pytest directly under the provider's own interpreter,
# never through ``uv run``. A uv run in a worktree can build a .venv there,
# which makes the worktree undeletable (see mutation_gate_engine_audio.py).
# -p no:cacheprovider keeps pytest from creating a .pytest_cache in the
# provider directory.
def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    python = PROVIDER / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for {service}: {python} does not exist. Run "
            f".claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:cacheprovider",
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


# --------------------------------------------------------------------------
# Test names (the part after "::", without a parameter list)
# --------------------------------------------------------------------------
RULE = "test_the_rule"

LOAD_GOOD = "test_a_good_file_gives_a_read_only_float32_array"
LOAD_AS_IS = "test_the_file_is_used_as_it_is"
LOAD_ONE = "test_a_one_sample_file_is_good"
LOAD_WRONG_FORMAT = "test_a_wrong_format_is_refused"
LOAD_FORMAT_NOT_CUT = "test_a_wrong_format_is_refused_as_a_format_not_as_cut_off"
LOAD_CORRUPT_FMT = "test_a_corrupt_fmt_chunk_size_is_refused"
LOAD_FORMAT_MESSAGE = "test_a_wrong_format_message_names_the_format_it_found"
LOAD_EMPTY = "test_an_empty_file_is_refused"
LOAD_ZERO_BYTE = "test_a_zero_byte_file_is_refused"
LOAD_NOT_WAV = "test_a_file_that_is_not_a_wav_is_refused"
LOAD_CUT_OFF = "test_a_cut_off_file_is_refused"
LOAD_MISSING = "test_a_missing_file_is_refused"
LOAD_FOLDER = "test_a_folder_is_refused"
LOAD_NOT_TEXT = "test_a_value_that_is_not_text_is_refused"
LOAD_NULL_CHAR = "test_a_path_with_a_null_character_is_refused"

OFF_FINAL = "test_the_final_decode_input_is_unchanged"
OFF_PARTIAL = "test_the_partial_decode_input_is_unchanged"
OFF_TEXT = "test_the_text_is_not_changed_when_the_join_is_off"

ON_FINAL_INPUT = "test_the_final_pass_puts_the_word_in_front_with_no_gap"
ON_FINAL_TEXT = "test_the_word_is_removed_from_the_final_text"
ON_PARTIAL = "test_the_partial_pass_joins_and_removes_the_word"
ON_PERIODIC = "test_every_periodic_pass_inside_process_audio_joins"
ON_NORMALIZE = "test_the_removal_runs_before_normalization"
ON_SAME_ARRAY = "test_the_lead_array_is_the_same_object_on_every_pass"
ON_FALLBACK = "test_no_word_found_decodes_the_users_audio_again_alone"
ON_FALLBACK_PARTIAL = "test_no_word_found_on_a_partial_pass_uses_the_second_decode"
ON_ONLY_WORD = "test_a_found_word_with_nothing_after_it_gives_empty_text_and_one_decode"
ON_DECODE_PLAIN = "test_the_second_decode_is_a_method_a_later_feature_can_call"
ON_SAVER = "test_the_saver_gets_the_users_audio_without_the_word"
ON_SAVER_FALLBACK = "test_the_saver_gets_the_users_audio_after_a_fallback_decode"

SET_DURING = "test_a_load_during_an_utterance_changes_no_pass_of_it"
SET_NEXT = "test_the_next_utterance_uses_the_new_word"
SET_BEFORE_FIRST = "test_a_load_before_the_first_pass_applies_to_that_utterance"
SET_WAS_OFF = "test_a_word_set_while_the_join_was_off_waits_for_the_next_utterance"
SET_NONE = "test_none_ends_the_join_from_the_next_utterance_on"
SET_ONE_ASSIGN = "test_the_word_is_set_by_one_attribute_assignment"
SET_START_LOAD = "test_the_start_time_load_goes_through_the_method"

HAS_FOLLOWS = "test_follows_set_lead_word"
HAS_LOAD = "test_true_after_a_start_time_load"

BAD_MISSING = "test_a_missing_file_keeps_the_join_off_with_one_log_line"
BAD_FORMAT = "test_a_wrong_format_keeps_the_join_off_with_one_log_line"
BAD_NOT_WAV = "test_a_file_that_is_not_a_wav_keeps_the_join_off"
BAD_NOT_TEXT = "test_a_value_that_is_not_text_keeps_the_join_off"
BAD_FALSY = "test_a_false_value_that_is_not_text_still_warns"
GOOD_NO_WARNING = "test_a_good_file_turns_the_join_on_with_no_warning"
GOOD_INFO = "test_a_good_file_writes_one_info_line_with_the_size_and_path"
GOOD_RATE = "test_the_file_is_checked_against_the_engines_sample_rate"

CFG_EMPTY = "test_the_tracked_config_declares_the_key_empty"
MAIN_PASSES = "test_main_passes_the_key_to_the_engine"
MAIN_ABSENT = "test_main_passes_empty_when_the_key_is_absent"


def _m(name, old, new, expect, file=ENGINE):
    return {
        "name": name,
        "service": PROVIDER,
        "test_file": TESTS,
        "file": file,
        "old": old,
        "new": new,
        "expect": expect,
    }


# --------------------------------------------------------------------------
# Source snippets (each must match exactly once)
# --------------------------------------------------------------------------
_LEAD_WORD = 'LEAD_WORD = "hello"\n'
_RUN = '_LEAD_WORD_RUN = r"[A-Za-z\']+"\n'
_SEARCH = '    m = re.search(_LEAD_WORD_RUN, text or "")\n'
_IF_FOUND = '    if m and m.group().lower() == lead_word:\n'
_SUB = '        return re.sub(r"^[^A-Za-z0-9]+", "", text[m.end():]), True\n'
_NOT_FOUND = '    return text or "", False\n'

_OPEN = '        with wave.open(os.fspath(path), "rb") as w:\n'
_CATCH = (
    '    except (OSError, wave.Error, EOFError, RuntimeError, TypeError, ValueError) as e:\n'
    '        raise LeadWordError(f"cannot read it ({type(e).__name__}: {e})") from e\n'
)
_FORMAT_IF = '    if rate != sample_rate or channels != 1 or width != 2:\n'
_FORMAT_RAISE = (
    '        raise LeadWordError(\n'
    '            f"its format is {rate} Hz, {channels} channel(s), {width * 8}-bit; "\n'
    '            f"only {sample_rate} Hz, 1 channel, 16-bit is accepted"\n'
    '        )\n'
)
_FRAMES_IF = '    if frames < 1 or len(raw) != frames * 2:\n'
_FRAMES_MSG = '            "it holds no samples" if frames < 1 else "its data is cut off"\n'
_READ_FRAMES = '            raw = w.readframes(frames)\n'
_CONVERT = (
    '    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / '
    'np.float32(32768.0)\n'
)
_READONLY = '    audio.flags.writeable = False\n'

_LOAD_OFF_IF = '        if lead_word_path == "":\n            return None\n'
_LOAD_CALL = (
    '            audio = load_lead_word_audio(lead_word_path, self._sample_rate)\n'
)
_LOAD_EXCEPT = '        except LeadWordError as e:\n'
_WARN = (
    '            logger.warning(\n'
    '                f"Lead-word join is off: lead_word_path {lead_word_path}: {e}"\n'
    '            )\n'
)
_WARN_RETURN = '            return None\n        logger.info(\n'
_INFO = (
    '        logger.info(\n'
    '            f"Lead-word join is on: {len(audio)} samples from {lead_word_path}"\n'
    '        )\n'
)

_INIT_DEFAULT = '        lead_word_path: str = "",\n    ):\n'
_INIT_ASSIGN = (
    '        self._lead_audio: np.ndarray | None = None\n'
    '        self._utterance_lead = _NOT_COPIED\n'
    '        self.set_lead_word(self._load_lead_audio(lead_word_path))\n'
)
_SET_HEAD = '        """\n        self._lead_audio = audio\n'
_HAS_RETURN = '        return self._lead_audio is not None\n'
_NOT_COPIED_DEF = '_NOT_COPIED = object()\n'
_RESET_COPY = (
    '        self.last_final_audio = None\n'
    '        self._utterance_lead = _NOT_COPIED\n'
)
_COPY = (
    '        if self._utterance_lead is _NOT_COPIED:\n'
    '            self._utterance_lead = self._lead_audio\n'
    '        lead = self._utterance_lead\n'
)

_REC_GUARD = (
    '        lead = self._utterance_lead\n'
    '        if lead is None:\n'
    '            return self._decode_plain(audio)\n'
)
_REC_JOIN = (
    '        text, found = remove_lead_word(\n'
    '            self._decode_plain(np.concatenate((lead, audio))), LEAD_WORD\n'
    '        )\n'
    '        if found:\n'
    '            return text\n'
    '        return self._decode_plain(audio)\n'
)

_DECODE_ACCEPT = '        stream.accept_waveform(self._sample_rate, audio)\n'
_DECODE_RETURN = '        return "".join(stream.result.tokens).strip()\n'

_FINAL = (
    '        self.last_final_audio = audio\n'
    '\n'
    '        text = self._recognize(audio)\n'
    '        # Same stage order as _run_inference above.\n'
)

_MAIN_LINE = '            lead_word_path=engine_config.get("lead_word_path", ""),\n'
_CONFIG_LINE = 'lead_word_path = ""\n'


def _catch_without(*dropped):
    kept = [t for t in ("OSError", "wave.Error", "EOFError", "RuntimeError",
                        "TypeError", "ValueError")
            if t not in dropped]
    return (f'    except ({", ".join(kept)}) as e:\n'
            '        raise LeadWordError(f"cannot read it ({type(e).__name__}: {e})") from e\n')


MUTATIONS = [
    # ---- LEAD_WORD ---------------------------------------------------------
    _m("lead-word-constant-changed", _LEAD_WORD, 'LEAD_WORD = "hallo"\n',
       [ON_FINAL_TEXT]),
    _m("lead-word-constant-capitalized", _LEAD_WORD, 'LEAD_WORD = "Hello"\n',
       [ON_FINAL_TEXT]),

    # ---- remove_lead_word: the letter run ----------------------------------
    _m("run-without-apostrophe", _RUN, '_LEAD_WORD_RUN = r"[A-Za-z]+"\n', [RULE]),
    _m("run-lowercase-only", _RUN, '_LEAD_WORD_RUN = r"[a-z\']+"\n', [RULE]),
    _m("run-uppercase-only", _RUN, '_LEAD_WORD_RUN = r"[A-Z\']+"\n', [RULE]),
    _m("run-allows-empty", _RUN, '_LEAD_WORD_RUN = r"[A-Za-z\']*"\n', [RULE]),
    _m("run-includes-digits", _RUN, '_LEAD_WORD_RUN = r"[A-Za-z\'0-9]+"\n', [RULE]),
    _m("run-includes-space", _RUN, '_LEAD_WORD_RUN = r"[A-Za-z\' ]+"\n', [RULE]),
    _m("search-becomes-match", _SEARCH,
       '    m = re.match(_LEAD_WORD_RUN, text or "")\n', [RULE]),
    _m("text-or-removed-in-search", _SEARCH,
       '    m = re.search(_LEAD_WORD_RUN, text)\n', [RULE]),

    # ---- remove_lead_word: the test ----------------------------------------
    _m("run-without-empty-check-on-match", _IF_FOUND,
       '    if m.group().lower() == lead_word:\n', [RULE]),
    _m("compare-is-case-sensitive", _IF_FOUND,
       '    if m and m.group() == lead_word:\n', [RULE]),
    _m("compare-uppercases", _IF_FOUND,
       '    if m and m.group().upper() == lead_word:\n', [RULE]),
    _m("compare-flipped", _IF_FOUND,
       '    if m and m.group().lower() != lead_word:\n', [RULE]),
    _m("compare-is-startswith", _IF_FOUND,
       '    if m and m.group().lower().startswith(lead_word):\n', [RULE]),
    _m("compare-is-contains", _IF_FOUND,
       '    if m and lead_word in m.group().lower():\n', [RULE]),
    _m("match-check-or", _IF_FOUND,
       '    if m or m.group().lower() == lead_word:\n', [RULE]),

    # ---- remove_lead_word: what is left ------------------------------------
    _m("strip-keeps-no-digits", _SUB,
       '        return re.sub(r"^[^A-Za-z]+", "", text[m.end():]), True\n', [RULE]),
    _m("strip-only-before-digit", _SUB,
       '        return re.sub(r"^[^0-9]+", "", text[m.end():]), True\n', [RULE]),
    _m("strip-eats-lowercase", _SUB,
       '        return re.sub(r"^[^A-Z0-9]+", "", text[m.end():]), True\n', [RULE]),
    _m("strip-not-anchored", _SUB,
       '        return re.sub(r"[^A-Za-z0-9]+", "", text[m.end():]), True\n', [RULE]),
    _m("strip-one-character", _SUB,
       '        return re.sub(r"^[^A-Za-z0-9]", "", text[m.end():]), True\n', [RULE]),
    _m("strip-whitespace-only", _SUB,
       '        return text[m.end():].lstrip(), True\n', [RULE]),
    _m("no-strip", _SUB, '        return text[m.end():], True\n', [RULE]),
    _m("slice-from-word-start", _SUB,
       '        return re.sub(r"^[^A-Za-z0-9]+", "", text[m.start():]), True\n', [RULE]),
    _m("slice-one-past-word", _SUB,
       '        return re.sub(r"^[^A-Za-z0-9]+", "", text[m.end() + 1:]), True\n',
       [RULE]),
    _m("next-word-lowercased", _SUB,
       '        return re.sub(r"^[^A-Za-z0-9]+", "", text[m.end():]).lower(), True\n',
       [RULE]),
    _m("found-flag-false", _SUB,
       '        return re.sub(r"^[^A-Za-z0-9]+", "", text[m.end():]), False\n', [RULE]),
    _m("not-found-returns-empty", _NOT_FOUND, '    return "", False\n', [RULE]),
    _m("not-found-returns-raw-none", _NOT_FOUND, '    return text, False\n', [RULE]),
    _m("not-found-flag-true", _NOT_FOUND, '    return text or "", True\n', [RULE]),

    # ---- load_lead_word_audio: reading -------------------------------------
    _m("path-not-converted-to-text", _OPEN,
       '        with wave.open(path, "rb") as w:\n', [LOAD_NOT_TEXT]),
    _m("catch-without-oserror", _CATCH, _catch_without("OSError"),
       [LOAD_MISSING, LOAD_FOLDER]),
    _m("catch-without-wave-error", _CATCH, _catch_without("wave.Error"),
       [LOAD_NOT_WAV]),
    _m("catch-without-eoferror", _CATCH, _catch_without("EOFError"),
       [LOAD_ZERO_BYTE]),
    _m("catch-without-runtimeerror", _CATCH, _catch_without("RuntimeError"),
       [LOAD_CORRUPT_FMT]),
    _m("catch-without-typeerror", _CATCH, _catch_without("TypeError"),
       [LOAD_NOT_TEXT]),
    _m("catch-without-valueerror", _CATCH, _catch_without("ValueError"),
       [LOAD_NULL_CHAR]),
    _m("catch-message-without-type-name", _CATCH,
       _catch_without().replace("({type(e).__name__}: {e})", "({e})"),
       [LOAD_ZERO_BYTE]),
    _m("catch-message-without-reason", _CATCH,
       _catch_without().replace("({type(e).__name__}: {e})", "({type(e).__name__})"),
       [LOAD_MISSING]),

    # ---- load_lead_word_audio: the format checks ---------------------------
    _m("format-rate-check-removed", _FORMAT_IF,
       '    if channels != 1 or width != 2:\n', [LOAD_WRONG_FORMAT]),
    _m("format-channels-check-removed", _FORMAT_IF,
       '    if rate != sample_rate or width != 2:\n', [LOAD_FORMAT_NOT_CUT]),
    _m("format-width-check-removed", _FORMAT_IF,
       '    if rate != sample_rate or channels != 1:\n', [LOAD_FORMAT_NOT_CUT]),
    _m("format-rate-equal", _FORMAT_IF,
       '    if rate == sample_rate or channels != 1 or width != 2:\n', [LOAD_GOOD]),
    _m("format-rate-only-lower-refused", _FORMAT_IF,
       '    if rate < sample_rate or channels != 1 or width != 2:\n',
       [LOAD_WRONG_FORMAT]),
    _m("format-rate-only-higher-refused", _FORMAT_IF,
       '    if rate > sample_rate or channels != 1 or width != 2:\n',
       [LOAD_WRONG_FORMAT]),
    _m("format-channels-two", _FORMAT_IF,
       '    if rate != sample_rate or channels != 2 or width != 2:\n', [LOAD_GOOD]),
    _m("format-width-one", _FORMAT_IF,
       '    if rate != sample_rate or channels != 1 or width != 1:\n', [LOAD_GOOD]),
    _m("format-width-only-wider-refused", _FORMAT_IF,
       '    if rate != sample_rate or channels != 1 or width > 2:\n',
       [LOAD_FORMAT_NOT_CUT]),
    _m("format-width-only-narrower-refused", _FORMAT_IF,
       '    if rate != sample_rate or channels != 1 or width < 2:\n',
       [LOAD_FORMAT_NOT_CUT]),
    _m("format-conditions-and", _FORMAT_IF,
       '    if rate != sample_rate and channels != 1 and width != 2:\n',
       [LOAD_WRONG_FORMAT]),
    _m("format-message-bits-wrong", _FORMAT_RAISE,
       _FORMAT_RAISE.replace("{width * 8}", "{width * 4}"), [LOAD_FORMAT_MESSAGE]),
    _m("format-message-shows-wanted-rate", _FORMAT_RAISE,
       _FORMAT_RAISE.replace("{rate} Hz", "{sample_rate} Hz"), [LOAD_FORMAT_MESSAGE]),
    _m("format-message-wanted-format-wrong", _FORMAT_RAISE,
       _FORMAT_RAISE.replace("1 channel, 16-bit", "2 channel, 16-bit"),
       [LOAD_FORMAT_MESSAGE]),

    # ---- load_lead_word_audio: the frame checks ----------------------------
    _m("frames-check-accepts-empty", _FRAMES_IF,
       '    if frames < 0 or len(raw) != frames * 2:\n', [LOAD_EMPTY]),
    _m("frames-check-refuses-one-sample", _FRAMES_IF,
       '    if frames < 2 or len(raw) != frames * 2:\n', [LOAD_ONE]),
    _m("frames-conditions-and", _FRAMES_IF,
       '    if frames < 1 and len(raw) != frames * 2:\n', [LOAD_EMPTY]),
    _m("raw-length-only-longer-refused", _FRAMES_IF,
       '    if frames < 1 or len(raw) > frames * 2:\n', [LOAD_CUT_OFF]),
    _m("raw-length-wrong-multiple", _FRAMES_IF,
       '    if frames < 1 or len(raw) != frames * 4:\n', [LOAD_GOOD]),
    _m("raw-length-without-frames", _FRAMES_IF,
       '    if frames < 1 or len(raw) != frames:\n', [LOAD_GOOD]),
    _m("message-swapped", _FRAMES_MSG,
       '            "it holds no samples" if frames > 1 else "its data is cut off"\n',
       [LOAD_EMPTY, LOAD_CUT_OFF]),
    _m("reads-one-frame-too-few", _READ_FRAMES,
       '            raw = w.readframes(frames - 1)\n', [LOAD_GOOD]),

    # ---- load_lead_word_audio: the conversion ------------------------------
    _m("samples-read-as-unsigned", _CONVERT,
       _CONVERT.replace('"<i2"', '"<u2"'), [LOAD_GOOD, LOAD_AS_IS]),
    _m("samples-read-big-endian", _CONVERT,
       _CONVERT.replace('"<i2"', '">i2"'), [LOAD_GOOD, LOAD_AS_IS]),
    _m("samples-read-as-32-bit", _CONVERT,
       _CONVERT.replace('"<i2"', '"<i4"'), [LOAD_GOOD, LOAD_AS_IS]),
    _m("scale-32767", _CONVERT,
       _CONVERT.replace("32768.0", "32767.0"), [LOAD_GOOD, LOAD_AS_IS]),
    _m("scale-multiplies", _CONVERT,
       _CONVERT.replace(") / np.float32", ") * np.float32"), [LOAD_GOOD, LOAD_AS_IS]),
    _m("array-left-writeable", _READONLY,
       '    audio.flags.writeable = True\n', [LOAD_GOOD]),
    _m("array-writeable-line-removed", _READONLY, '    pass\n', [LOAD_GOOD]),

    # ---- SherpaOfflineEngine._load_lead_audio ------------------------------
    _m("empty-path-check-inverted", _LOAD_OFF_IF,
       '        if lead_word_path != "":\n            return None\n',
       [OFF_FINAL, GOOD_NO_WARNING]),
    _m("empty-path-loaded-anyway", _LOAD_OFF_IF,
       '        if False:\n            return None\n', [OFF_FINAL]),
    _m("empty-path-gives-an-empty-array", _LOAD_OFF_IF,
       '        if lead_word_path == "":\n'
       '            return np.zeros(0, dtype=np.float32)\n',
       [OFF_TEXT]),
    # A TOML `false` or `0` is falsy but is not "", so only the real check
    # writes the warning for it.
    _m("empty-check-is-falsy-test", _LOAD_OFF_IF,
       '        if not lead_word_path:\n            return None\n', [BAD_FALSY]),
    _m("rate-hardcoded-in-load", _LOAD_CALL,
       '            audio = load_lead_word_audio(lead_word_path, 16000)\n', [GOOD_RATE]),
    _m("catch-wrong-exception", _LOAD_EXCEPT,
       '        except ValueError as e:\n', [BAD_MISSING]),
    _m("warning-is-info", _WARN, _WARN.replace("logger.warning(", "logger.info("),
       [BAD_MISSING]),
    _m("warning-removed", _WARN, '            pass\n', [BAD_MISSING]),
    _m("warning-twice", _WARN, _WARN + _WARN, [BAD_MISSING]),
    _m("warning-without-path", _WARN,
       _WARN.replace("lead_word_path {lead_word_path}", "lead_word_path"),
       [BAD_MISSING]),
    _m("warning-without-reason", _WARN, _WARN.replace(": {e}", ""), [BAD_MISSING]),
    _m("failure-returns-an-array", _WARN_RETURN,
       '            return np.zeros(1, dtype=np.float32)\n        logger.info(\n',
       [BAD_MISSING]),
    _m("info-removed", _INFO, '        pass\n', [GOOD_INFO]),
    _m("info-is-warning", _INFO, _INFO.replace("logger.info(", "logger.warning("),
       [GOOD_NO_WARNING]),
    _m("info-is-debug", _INFO, _INFO.replace("logger.info(", "logger.debug("),
       [GOOD_INFO]),
    _m("info-size-wrong", _INFO,
       _INFO.replace("{len(audio)}", "{len(lead_word_path)}"), [GOOD_INFO]),
    _m("info-without-path", _INFO,
       _INFO.replace(" from {lead_word_path}", ""), [GOOD_INFO]),

    # ---- SherpaOfflineEngine.__init__ --------------------------------------
    _m("init-default-path-set", _INIT_DEFAULT,
       '        lead_word_path: str = "lead.wav",\n    ):\n', [OFF_FINAL]),
    _m("init-never-loads", _INIT_ASSIGN,
       '        self._lead_audio: np.ndarray | None = None\n'
       '        self._utterance_lead = _NOT_COPIED\n'
       '        self.set_lead_word(None)\n',
       [ON_FINAL_INPUT, ON_FINAL_TEXT, ON_PARTIAL]),
    _m("init-loads-the-empty-path", _INIT_ASSIGN,
       '        self._lead_audio: np.ndarray | None = None\n'
       '        self._utterance_lead = _NOT_COPIED\n'
       '        self.set_lead_word(self._load_lead_audio(""))\n',
       [ON_FINAL_INPUT, ON_FINAL_TEXT, ON_PARTIAL]),
    _m("init-assigns-without-the-method", _INIT_ASSIGN,
       '        self._lead_audio: np.ndarray | None = self._load_lead_audio(\n'
       '            lead_word_path\n'
       '        )\n'
       '        self._utterance_lead = _NOT_COPIED\n',
       [SET_START_LOAD]),

    # ---- SherpaOfflineEngine._recognize ------------------------------------
    _m("lead-guard-inverted-to-always-join", _REC_GUARD,
       _REC_GUARD.replace("if lead is None:", "if lead is not None:"),
       [OFF_FINAL, ON_FINAL_INPUT]),
    _m("lead-guard-removed", _REC_GUARD,
       _REC_GUARD.replace("if lead is None:", "if False:"), [OFF_FINAL]),
    _m("join-off-returns-empty", _REC_GUARD,
       _REC_GUARD.replace("return self._decode_plain(audio)", 'return ""'),
       [OFF_TEXT]),
    _m("join-order-reversed", _REC_JOIN,
       _REC_JOIN.replace("np.concatenate((lead, audio))",
                         "np.concatenate((audio, lead))"),
       [ON_FINAL_INPUT, ON_PARTIAL]),
    _m("join-leaves-the-word-out", _REC_JOIN,
       _REC_JOIN.replace("self._decode_plain(np.concatenate((lead, audio)))",
                         "self._decode_plain(audio)"),
       [ON_FINAL_INPUT, ON_PARTIAL]),
    _m("join-removes-the-wrong-word", _REC_JOIN,
       _REC_JOIN.replace(", LEAD_WORD\n", ', "hallo"\n'), [ON_FINAL_TEXT]),
    _m("join-removes-the-empty-word", _REC_JOIN,
       _REC_JOIN.replace(", LEAD_WORD\n", ', ""\n'), [ON_FINAL_TEXT]),
    _m("found-check-inverted", _REC_JOIN,
       _REC_JOIN.replace("if found:", "if not found:"), [ON_FINAL_TEXT]),
    _m("found-check-always-true", _REC_JOIN,
       _REC_JOIN.replace("if found:", "if True:"), [ON_FALLBACK]),
    _m("fallback-returns-the-joined-text", _REC_JOIN,
       _REC_JOIN.replace("        return self._decode_plain(audio)\n",
                         "        return text\n"),
       [ON_FALLBACK, ON_FALLBACK_PARTIAL]),
    _m("fallback-returns-empty", _REC_JOIN,
       _REC_JOIN.replace("        return self._decode_plain(audio)\n",
                         '        return ""\n'),
       [ON_FALLBACK, ON_FALLBACK_PARTIAL]),
    _m("fallback-decodes-the-joined-audio", _REC_JOIN,
       _REC_JOIN.replace(
           "        return self._decode_plain(audio)\n",
           "        return self._decode_plain(np.concatenate((lead, audio)))\n"),
       [ON_FALLBACK, ON_FALLBACK_PARTIAL]),
    _m("fallback-decodes-a-shortened-audio", _REC_JOIN,
       _REC_JOIN.replace("        return self._decode_plain(audio)\n",
                         "        return self._decode_plain(audio[1:])\n"),
       [ON_FALLBACK, ON_FALLBACK_PARTIAL]),

    # ---- SherpaOfflineEngine.set_lead_word and the per-utterance copy ------
    _m("set-method-does-nothing", _SET_HEAD,
       '        """\n        pass\n', [SET_NEXT, SET_ONE_ASSIGN, SET_START_LOAD]),
    _m("set-method-ignores-none", _SET_HEAD,
       '        """\n        if audio is not None:\n            self._lead_audio = audio\n',
       [SET_NONE]),
    _m("set-method-sets-the-copy-too", _SET_HEAD,
       '        """\n        self._lead_audio = audio\n        self._utterance_lead = audio\n',
       [SET_DURING]),
    _m("set-method-sets-another-attribute", _SET_HEAD,
       '        """\n        self._lead_word = audio\n', [SET_NEXT, SET_ONE_ASSIGN]),
    _m("pass-reads-the-reference-each-time", _COPY,
       '        self._utterance_lead = self._lead_audio\n'
       '        lead = self._utterance_lead\n', [SET_DURING, SET_WAS_OFF]),
    _m("copy-never-reset", _RESET_COPY,
       '        self.last_final_audio = None\n', [SET_NEXT, SET_NONE, SET_WAS_OFF]),
    _m("copy-retaken-while-off", _COPY,
       '        if self._utterance_lead is None:\n'
       '            self._utterance_lead = self._lead_audio\n'
       '        lead = self._utterance_lead\n', [SET_WAS_OFF]),
    _m("copy-never-taken", _COPY,
       '        lead = self._utterance_lead\n', [ON_FINAL_INPUT]),
    _m("sentinel-is-none", _NOT_COPIED_DEF, '_NOT_COPIED = None\n', [SET_WAS_OFF]),

    # ---- SherpaOfflineEngine.has_lead_word (wh-first-sound-lost.23) -------
    _m("has-lead-word-inverted", _HAS_RETURN,
       '        return self._lead_audio is None\n', [HAS_FOLLOWS, HAS_LOAD]),
    _m("has-lead-word-always-true", _HAS_RETURN, '        return True\n', [HAS_FOLLOWS]),
    _m("has-lead-word-always-false", _HAS_RETURN, '        return False\n',
       [HAS_FOLLOWS, HAS_LOAD]),
    _m("has-lead-word-returns-the-array", _HAS_RETURN,
       '        return self._lead_audio\n', [HAS_FOLLOWS, HAS_LOAD]),
    _m("has-lead-word-reads-the-utterance-copy", _HAS_RETURN,
       '        return self._utterance_lead is not None\n', [HAS_FOLLOWS]),

    # ---- SherpaOfflineEngine._decode_plain ---------------------------------
    _m("decode-feeds-a-shortened-audio", _DECODE_ACCEPT,
       '        stream.accept_waveform(self._sample_rate, audio[:-1])\n',
       [OFF_FINAL, ON_DECODE_PLAIN]),
    _m("decode-keeps-edge-spaces", _DECODE_RETURN,
       '        return "".join(stream.result.tokens)\n', [OFF_TEXT, ON_DECODE_PLAIN]),
    _m("decode-joins-tokens-with-spaces", _DECODE_RETURN,
       '        return " ".join(stream.result.tokens).strip()\n',
       [OFF_TEXT, ON_DECODE_PLAIN]),

    # ---- _run_final_inference: what the saver gets -------------------------
    _m("saver-gets-the-joined-audio", _FINAL,
       '        text = self._recognize(audio)\n'
       '        self.last_final_audio = (\n'
       '            audio if self._lead_audio is None\n'
       '            else np.concatenate((self._lead_audio, audio))\n'
       '        )\n'
       '        # Same stage order as _run_inference above.\n',
       [ON_SAVER, ON_SAVER_FALLBACK]),
    _m("saver-gets-nothing", _FINAL,
       '        self.last_final_audio = None\n'
       '\n'
       '        text = self._recognize(audio)\n'
       '        # Same stage order as _run_inference above.\n',
       [ON_SAVER, ON_SAVER_FALLBACK]),

    # ---- main.py and config.toml -------------------------------------------
    _m("main-passes-empty-always", _MAIN_LINE,
       '            lead_word_path="",\n', [MAIN_PASSES], MAIN),
    _m("main-default-is-a-path", _MAIN_LINE,
       '            lead_word_path=engine_config.get("lead_word_path", "x.wav"),\n',
       [MAIN_ABSENT], MAIN),
    _m("main-passes-nothing", _MAIN_LINE, '', [MAIN_PASSES, MAIN_ABSENT], MAIN),
    _m("main-reads-another-key", _MAIN_LINE,
       '            lead_word_path=engine_config.get("lead_word_file", ""),\n',
       [MAIN_PASSES], MAIN),
    _m("config-key-set", _CONFIG_LINE, 'lead_word_path = "x.wav"\n',
       [CFG_EMPTY], CONFIG),
    _m("config-key-removed", _CONFIG_LINE, '', [CFG_EMPTY], CONFIG),
]


# --------------------------------------------------------------------------
# Equivalent mutants. None of these changes behavior a caller can see, so a
# test cannot fail for them. `--equivalent` runs them and every one must be
# reported SURVIVED; one that is caught is no longer equivalent and needs a
# decision.
# --------------------------------------------------------------------------
EQUIVALENT_MUTANTS = [
    # wave accepts "r" for "rb" and always opens in binary mode.
    _m("eq-open-mode-r", _OPEN, '        with wave.open(os.fspath(path), "r") as w:\n',
       [LOAD_GOOD]),
    # Python's wave module raises on a zero-channel header, so channels is
    # never below 1 and "> 1" refuses exactly what "!= 1" refuses.
    _m("eq-channels-greater-than-one", _FORMAT_IF,
       '    if rate != sample_rate or channels > 1 or width != 2:\n',
       [LOAD_FORMAT_NOT_CUT]),
    # readframes(frames) never returns more than frames * width bytes and the
    # width check has already passed, so raw is never longer than frames * 2.
    _m("eq-raw-length-only-shorter-refused", _FRAMES_IF,
       '    if frames < 1 or len(raw) < frames * 2:\n', [LOAD_CUT_OFF]),
    # An int16 array divided by a float32 scalar is float32 (NumPy 2 rules and
    # NumPy 1 rules agree), with the same values.
    _m("eq-no-astype", _CONVERT,
       '    audio = np.frombuffer(raw, dtype="<i2") / np.float32(32768.0)\n',
       [LOAD_GOOD]),
    # A float32 array divided by a Python float stays float32 (NEP 50), with
    # the same values for this power of two.
    _m("eq-scalar-is-python-float", _CONVERT,
       _CONVERT.replace("np.float32(32768.0)", "32768.0"), [LOAD_GOOD]),
    # `from e` only sets __cause__; no caller reads it.
    _m("eq-cause-dropped", _CATCH,
       _catch_without().replace(") from e\n", ") from None\n"), [LOAD_MISSING]),
    # _load_model restores the working directory in its own finally block, so
    # reading the file after it gives the same array. A relative path sees the
    # same directory either way. If _load_model raises, __init__ raises too.
    _m("eq-lead-read-after-model-load",
       _INIT_ASSIGN + (
           '\n'
           '        # Timing thresholds (converted to sample counts)\n'
           '        self._re_inference_interval_samples = int(sample_rate * re_inference_interval_ms / 1000)\n'
           '        self._endpoint_silence_samples = int(sample_rate * endpoint_silence_ms / 1000)\n'
           '        self._silence_rms_threshold = silence_rms_threshold\n'
           '        self._max_buffer_samples = int(sample_rate * max_buffer_duration_s)\n'
           '\n'
           '        # Load model\n'
           '        self._load_model(\n'
           '            model_path, use_gpu, gpu_device_id, num_threads,\n'
           '            hotwords_file, hotwords_score,\n'
           '        )\n'),
       (
           '        self._lead_audio: np.ndarray | None = None\n'
           '        self._utterance_lead = _NOT_COPIED\n'
           '        self.set_lead_word(None)\n'
           '\n'
           '        # Timing thresholds (converted to sample counts)\n'
           '        self._re_inference_interval_samples = int(sample_rate * re_inference_interval_ms / 1000)\n'
           '        self._endpoint_silence_samples = int(sample_rate * endpoint_silence_ms / 1000)\n'
           '        self._silence_rms_threshold = silence_rms_threshold\n'
           '        self._max_buffer_samples = int(sample_rate * max_buffer_duration_s)\n'
           '\n'
           '        # Load model\n'
           '        self._load_model(\n'
           '            model_path, use_gpu, gpu_device_id, num_threads,\n'
           '            hotwords_file, hotwords_score,\n'
           '        )\n'
           '        self.set_lead_word(self._load_lead_audio(lead_word_path))\n'),
       [ON_FINAL_INPUT]),
    # An assert that only fires when the recognizer is missing; the next line
    # then raises AttributeError for the same condition. No test builds an
    # engine without a recognizer and decodes (code moved from _recognize
    # unchanged by this feature).
    _m("eq-recognizer-assert-removed",
       '        assert self._recognizer is not None\n        stream = self._recognizer.create_stream()\n',
       '        stream = self._recognizer.create_stream()\n', [OFF_FINAL]),
]


if __name__ == "__main__":
    if "--equivalent" in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != "--equivalent"]
        print(f"EQUIVALENT mutants: {len(EQUIVALENT_MUTANTS)}; "
              "each must print SURVIVED")
        sys.exit(run(EQUIVALENT_MUTANTS, argv))
    sys.exit(run(MUTATIONS))
