"""Mutation gate for lead_word_store.py (wh-first-sound-lost.23, acceptance item 31).

The code is the folder, the reader and the line-based writer of the config key,
the candidate files, and the store actions Keep (with its first-Keep copy of a
file that the key names), restore, turn off and turn on, each with the
snapshot and the undo that put every file and the key back. The tests are
tests/test_lead_word_store.py.

Run it as lead_word_gate_common.py says. EQUIVALENT_MUTANTS holds the mutants
that no test can fail for; `--equivalent` runs them and each must be reported
SURVIVED.
"""
from __future__ import annotations

from lead_word_gate_common import STORE, main, make

T = "tests/test_lead_word_store.py"
m = make(T, STORE)

# --- Test names ----------------------------------------------------------
FOLDER = "test_folder_is_under_local_app_data"
NO_LOCALAPPDATA = "test_missing_local_app_data_raises"
FILE_NAMES = "test_the_four_file_names_and_the_measures_file"
KW_REPLACE = "test_replaces_the_value_and_keeps_every_other_byte"
KW_COMMENT = "test_the_same_line_comment_stays"
KW_EMPTY = "test_writing_an_empty_value"
KW_DOUBLE = "test_a_double_quoted_old_value_is_replaced"
KW_ADD_LINE = "test_a_missing_line_is_added_at_the_end_of_the_engine_body"
KW_NO_FINAL_NEWLINE = "test_a_missing_line_at_the_end_of_a_file_without_a_final_newline"
KW_ADD_SECTION = "test_a_missing_section_is_added"
KW_OTHER_SECTION = "test_a_key_of_another_section_is_not_touched"
KW_HEADER_COMMENT = "test_a_header_with_a_comment_is_the_engine_section"
KW_CRLF = "test_crlf_stays_crlf"
KW_ROUND_TRIP = "test_values_round_trip"
KW_MULTI_LINE = "test_a_multi_line_old_value_is_refused_and_the_file_is_unchanged"
KW_FAILED_REPLACE = "test_a_failed_replace_leaves_the_file_and_no_temp_file"
KW_READ_BACK = "test_the_read_back_check_refuses_a_wrong_edit"
KR_NOW = "test_reads_the_file_now_not_a_cached_value"
KR_MISSING = "test_a_missing_file_gives_the_fallback"
KR_BROKEN = "test_a_broken_file_gives_the_fallback"
KR_NOT_TEXT = "test_a_value_that_is_not_text_counts_as_empty"
KR_NO_SECTION = "test_no_engine_section_gives_empty"
C_SAVE = "test_save_candidate_writes_the_wav_and_the_facts"
C_NO_TOUCH = "test_save_candidate_never_touches_hello_wav"
C_SECOND = "test_a_second_candidate_replaces_the_first"
C_FAILED_WRITE = "test_a_failed_write_leaves_no_candidate"
C_DISCARD = "test_discard_candidate"
C_DISCARD_KEEPS = "test_discard_leaves_the_kept_files"
C_REFUSED = "test_delete_refused_removes_a_candidate"
KF_FIRST = "test_first_keep_creates_hello_wav_json_and_key_and_loads_the_engine"
KF_NEXT_START = "test_the_next_provider_start_loads_the_same_file_as_the_running_engine"
KF_OTHER_BYTES = "test_the_other_config_bytes_stay"
KF_STATUS = "test_keep_returns_the_new_status"
KF_NO_CANDIDATE = "test_keep_without_a_candidate_is_refused"
KF_NO_FACTS = "test_keep_with_a_candidate_whose_facts_are_gone_is_refused"
KF_UNUSABLE = "test_keep_with_an_unusable_candidate_changes_nothing"
KR_PREVIOUS = "test_the_old_recording_becomes_previous_and_its_facts_follow"
KR_THIRD = "test_a_third_keep_drops_the_oldest_copy"
KR_ENGINE = "test_the_engine_gets_the_new_word"
KR_KEY_IS_WAV = "test_a_key_that_is_already_the_wav_is_not_rewritten"
KR_KEY_EMPTY = "test_a_key_that_is_empty_is_set"
KR_SLASH = "test_a_key_in_another_slash_style_counts_as_the_wav"
KO_FIRST = "test_first_keep_copies_the_other_file_to_previous_and_never_changes_it"
KO_BUTTON = "test_the_button_data_before_keep"
KO_WITH_WAV = "test_with_a_kept_wav_the_old_wav_is_previous_and_the_other_file_is_not_copied"
KO_MISSING = "test_a_missing_other_file_is_not_an_error"
KO_EXISTING_PREVIOUS = "test_an_existing_previous_is_kept_when_there_is_no_wav"
KFAIL_REPLACEMENT = "test_replacement_keep_puts_everything_back"
KFAIL_FIRST_OTHER = "test_first_keep_from_another_file_puts_everything_back"
KFAIL_COPY = "test_a_failed_copy_of_the_other_file_puts_everything_back"
KFAIL_KEY_WROTE = "test_a_key_edit_that_wrote_before_failing_is_undone"
KFAIL_AGAIN = "test_a_failed_keep_can_be_tried_again"
ST_EMPTY = "test_an_empty_store"
ST_CANDIDATE = "test_the_candidate_shows"
ST_DAMAGED = "test_a_damaged_facts_file_gives_empty_facts"
ST_FRESH = "test_the_status_reads_the_key_fresh"
RS_SWAP = "test_swap_and_swap_back"
RS_NO_SWAP_FILE = "test_no_swap_file_is_left"
RS_NO_PREVIOUS = "test_no_previous_is_refused"
RS_KEY_OFF = "test_restore_sets_the_key_when_it_was_off"
RS_ONLY_PREVIOUS = "test_restore_when_only_a_previous_exists"
RS_FAIL = "test_a_failing_step_puts_everything_back"
RS_UNUSABLE = "test_an_unusable_previous_changes_nothing"
OFF_FULL = "test_turn_off_empties_the_key_keeps_the_files_and_clears_the_engine"
OFF_ONLY_KEY_LINE = "test_turn_off_changes_only_the_key_line"
OFF_EMPTY_NO_REWRITE = "test_turn_off_with_an_empty_key_does_not_rewrite_the_config"
ON_FULL = "test_turn_on_sets_the_key_to_hello_wav_and_loads_it"
ON_NO_REWRITE = "test_turn_on_when_the_key_already_names_it_does_not_rewrite"
ON_NO_RECORDING = "test_turn_on_without_a_recording_is_refused"
ON_UNUSABLE = "test_turn_on_with_an_unusable_file_changes_nothing"
OFF_FAIL = "test_turn_off_failure_puts_the_key_back"
ON_FAIL = "test_turn_on_failure_puts_the_key_back"
OFF_KEY_FAIL = "test_turn_off_key_write_failure_changes_nothing_and_leaves_the_engine"
N_FORM = "test_values_are_written_in_the_right_string_form"
N_REFUSED = "test_an_old_value_that_cannot_be_replaced_is_refused_with_its_reason"
N_ESC = "test_an_old_value_with_an_escaped_quote_is_replaced_whole"
N_BARE = "test_a_bare_old_value_ends_at_the_first_space"
N_NOFINAL_SECTION = "test_a_missing_section_after_a_last_line_without_a_newline"
N_LATER = "test_a_key_of_a_later_section_is_not_touched_when_engine_has_none"
N_FACTS_MISSING = "test_the_facts_of_a_missing_recording_are_not_shown"
N_NOT_OBJECT = "test_a_facts_file_that_is_not_an_object_gives_empty_facts"
N_TEMP = "test_a_failed_move_leaves_no_temp_file"
N_RETURNS = "test_save_candidate_returns_the_candidate_path"
N_NO_WARN = "test_discarding_nothing_logs_no_warning"
N_UNREADABLE = "test_a_file_that_cannot_be_read_stops_keep_with_a_store_error"
N_UNTOUCHED = "test_a_failure_does_not_write_a_key_that_it_did_not_change"
N_NAMED = "test_a_step_that_cannot_be_undone_is_named_in_the_error"
N_NO_COPY = "test_a_first_keep_copies_no_file_when_the_key_names_none"
N_RESTORE_KEY = "test_restore_does_not_rewrite_a_key_that_names_the_wav"
N_ON_CURRENT = "test_turn_on_loads_the_current_recording_not_the_previous_one"

MUTATIONS = [
    # ---- names, folder ------------------------------------------------------
    m("file-wav-renamed", 'FILE_WAV = "hello.wav"                  # the kept recording',
      'FILE_WAV = "word.wav"                  # the kept recording', [FILE_NAMES]),
    m("file-previous-renamed", 'FILE_PREVIOUS = "hello.previous.wav"    # one older copy',
      'FILE_PREVIOUS = "hello.old.wav"    # one older copy', [FILE_NAMES]),
    m("file-candidate-renamed", 'FILE_CANDIDATE = "hello.candidate.wav"  # passed, not kept yet',
      'FILE_CANDIDATE = "hello.new.wav"  # passed, not kept yet', [FILE_NAMES]),
    m("file-meta-renamed", 'FILE_META = "hello.json"                # measures and facts of the kept recording',
      'FILE_META = "word.json"                # measures and facts of the kept recording', [FILE_NAMES]),
    m("file-candidate-meta-renamed", 'FILE_CANDIDATE_META = "hello.candidate.json"',
      'FILE_CANDIDATE_META = "hello.new.json"', [FILE_NAMES]),
    m("key-name-changed", 'KEY_NAME = "lead_word_path"', 'KEY_NAME = "lead_word_file"', [KW_REPLACE]),
    m("recorded-format-changed", 'RECORDED_FORMAT = "%Y-%m-%d %H:%M"',
      'RECORDED_FORMAT = "%Y-%m-%d %H:%M:%S"', [C_SAVE]),
    m("folder-without-wheelhouse", '    return Path(base) / "WheelHouse" / "lead-word"',
      '    return Path(base) / "lead-word"', [FOLDER]),
    m("folder-without-lead-word", '    return Path(base) / "WheelHouse" / "lead-word"',
      '    return Path(base) / "WheelHouse"', [FOLDER]),
    m("folder-missing-variable-returns-cwd", '    if not base:\n        raise KeyError("LOCALAPPDATA")',
      '    if False:\n        raise KeyError("LOCALAPPDATA")', [NO_LOCALAPPDATA]),

    # ---- read_lead_word_key -------------------------------------------------
    m("read-key-section-default-wrong", '        value = config.get("engine", {}).get(KEY_NAME, "")',
      '        value = config.get("engine", {}).get(KEY_NAME, "x")', [KR_NO_SECTION]),
    m("read-key-from-top-level", '        value = config.get("engine", {}).get(KEY_NAME, "")',
      '        value = config.get(KEY_NAME, "")', [KR_NOW]),
    m("read-key-not-text-kept", '        return value if isinstance(value, str) else ""',
      '        return value', [KR_NOT_TEXT]),
    m("read-key-fallback-lost", "        return fallback", '        return ""', [KR_MISSING]),
    m("read-key-fallback-ignored-for-broken", "    except Exception as e:\n        logger.warning(",
      "    except FileNotFoundError as e:\n        logger.warning(", [KR_BROKEN]),

    # ---- _toml_string -------------------------------------------------------
    m("toml-literal-for-quote", "    if \"'\" not in value and not any(",
      "    if not any(", [N_FORM]),
    m("toml-control-char-literal", "        (ord(c) < 32 and c != \"\\t\") or ord(c) == 127 for c in value",
      "        ord(c) == 127 for c in value", [KW_ROUND_TRIP]),
    m("toml-tab-escaped", "        (ord(c) < 32 and c != \"\\t\") or ord(c) == 127 for c in value",
      "        (ord(c) < 32) or ord(c) == 127 for c in value", [N_FORM]),
    m("toml-delete-char-literal", "        (ord(c) < 32 and c != \"\\t\") or ord(c) == 127 for c in value",
      "        (ord(c) < 32 and c != \"\\t\") for c in value", [N_FORM]),
    m("toml-backslash-not-escaped", '            out.append("\\\\\\\\")', '            out.append("\\\\")',
      [N_FORM]),
    m("toml-quote-not-escaped", "            out.append('\\\\\"')", "            out.append('\"')",
      [N_FORM]),
    m("toml-control-not-escaped", '        elif ord(c) < 32 or ord(c) == 127:\n            out.append(f"\\\\u{ord(c):04x}")',
      '        elif False:\n            out.append(f"\\\\u{ord(c):04x}")', [N_FORM]),
    m("toml-delete-not-escaped", "        elif ord(c) < 32 or ord(c) == 127:",
      "        elif ord(c) < 32:", [N_FORM]),

    # ---- _value_end ---------------------------------------------------------
    m("value-end-triple-single-only", "    if rest.startswith((\"'''\", '\"\"\"')):",
      "    if rest.startswith((\"'''\",)):", [N_REFUSED]),
    m("value-end-triple-double-only", "    if rest.startswith((\"'''\", '\"\"\"')):",
      "    if rest.startswith(('\"\"\"',)):", [N_REFUSED]),
    m("value-end-literal-includes-quote", "        return end + 1", "        return end", [KW_COMMENT]),
    m("value-end-literal-unterminated-ok", "        if end < 0:\n            raise ValueError(f\"{KEY_NAME} holds an unterminated string\")\n        return end + 1",
      "        if end < -1:\n            raise ValueError(f\"{KEY_NAME} holds an unterminated string\")\n        return end + 1",
      [N_REFUSED]),
    m("value-end-escape-skip-one", "                i += 2\n                continue",
      "                i += 1\n                continue", [N_ESC]),
    m("value-end-basic-includes-quote", "            if rest[i] == '\"':\n                return i + 1",
      "            if rest[i] == '\"':\n                return i", [KW_DOUBLE]),
    m("value-end-bare-whitespace-allowed", '    return re.match(r"[^\\s#]*", rest).end()',
      '    return re.match(r"[^#]*", rest).end()', [N_BARE]),

    # ---- write_lead_word_key ------------------------------------------------
    m("write-key-newline-always-lf", '    newline = "\\r\\n" if "\\r\\n" in content else "\\n"',
      '    newline = "\\n"', [KW_CRLF]),
    m("write-key-newline-always-crlf", '    newline = "\\r\\n" if "\\r\\n" in content else "\\n"',
      '    newline = "\\r\\n"', [KW_ADD_LINE]),
    m("write-key-header-search-skipped", "        if _ENGINE_HEADER_RE.match(line.rstrip(\"\\r\\n\")):\n            engine_start = i\n            break",
      "        if False:\n            engine_start = i\n            break", [KW_REPLACE]),
    m("write-key-no-final-newline-added", "        if lines and not lines[-1].endswith(\"\\n\"):\n            lines[-1] += newline\n        if lines:",
      "        if lines and False:\n            lines[-1] += newline\n        if lines:", [N_NOFINAL_SECTION]),
    m("write-key-no-blank-line-before-section", "        if lines:\n            lines.append(newline)\n        lines.append(f\"[engine]{newline}\")",
      "        lines.append(f\"[engine]{newline}\")", [KW_ADD_SECTION]),
    m("write-key-section-header-wrong", '        lines.append(f"[engine]{newline}")',
      '        lines.append(f"[Engine]{newline}")', [KW_ADD_SECTION]),
    m("write-key-new-section-key-line-lost", '        lines.append(f"{KEY_NAME} = {formatted}{newline}")\n    else:',
      '        pass\n    else:', [KW_ADD_SECTION]),
    m("write-key-section-end-ignored", "            if _SECTION_HEADER_RE.match(lines[i]):\n                engine_end = i\n                break",
      "            if False:\n                engine_end = i\n                break", [N_LATER]),
    m("write-key-searches-whole-file", "        for i in range(engine_start + 1, engine_end):",
      "        for i in range(engine_start + 1, len(lines)):", [N_LATER]),
    m("write-key-value-end-not-used", "                    head.group(1) + formatted + rest[_value_end(rest):] + eol)",
      "                    head.group(1) + formatted + eol)", [KW_COMMENT]),
    m("write-key-eol-lost", "                    head.group(1) + formatted + rest[_value_end(rest):] + eol)",
      "                    head.group(1) + formatted + rest[_value_end(rest):])", [KW_REPLACE]),
    m("write-key-head-lost", "                    head.group(1) + formatted + rest[_value_end(rest):] + eol)",
      "                    formatted + rest[_value_end(rest):] + eol)", [KW_REPLACE]),
    m("write-key-first-line-not-final", "                    head.group(1) + formatted + rest[_value_end(rest):] + eol)\n                break",
      "                    head.group(1) + formatted + rest[_value_end(rest):] + eol)\n                pass", [KW_REPLACE]),
    m("write-key-insert-at-section-end-with-blank-lines", "            while insert_at > engine_start + 1 and lines[insert_at - 1].strip() == \"\":\n                insert_at -= 1",
      "            pass", [KW_ADD_LINE]),
    m("write-key-line-without-newline-kept", "            if not lines[insert_at - 1].endswith(\"\\n\"):\n                lines[insert_at - 1] += newline",
      "            if False:\n                lines[insert_at - 1] += newline", [KW_NO_FINAL_NEWLINE]),
    m("write-key-insert-index-off", "            lines.insert(insert_at, f\"{KEY_NAME} = {formatted}{newline}\")",
      "            lines.insert(insert_at + 1, f\"{KEY_NAME} = {formatted}{newline}\")", [KW_ADD_LINE]),
    m("write-key-read-back-skipped", "    if parsed != value:", "    if False:", [KW_READ_BACK]),
    m("write-key-read-back-always-refuses", "    if parsed != value:", "    if True:", [KW_REPLACE]),
    m("write-key-writes-str-not-bytes", "    _atomic_write(config_path, new_content.encode(\"utf-8\"))",
      "    _atomic_write(config_path, content.encode(\"utf-8\"))", [KW_REPLACE]),

    # ---- file helpers -------------------------------------------------------
    m("atomic-write-skips-replace", "        tmp.write_bytes(data)\n        os.replace(tmp, path)",
      "        tmp.write_bytes(data)", [KW_REPLACE]),
    m("atomic-write-temp-kept", "        os.replace(tmp, path)\n    finally:\n        if tmp.exists():\n            try:\n                tmp.unlink()\n            except OSError:\n                pass\n",
      "        os.replace(tmp, path)\n    finally:\n        pass\n", [KW_FAILED_REPLACE]),
    m("read-or-none-raises", "    except FileNotFoundError:\n        return None",
      "    except PermissionError:\n        return None", [C_DISCARD_KEEPS]),
    m("same-file-empty-is-same", "    if not key_value:\n        return False", "    if not key_value:\n        return True",
      [KR_KEY_EMPTY]),
    m("same-file-case-sensitive", "    return os.path.normcase(os.path.abspath(key_value)) == os.path.normcase(\n        os.path.abspath(path))",
      "    return os.path.abspath(key_value) == os.path.abspath(path)", [KR_SLASH]),
    m("same-file-compares-raw", "    return os.path.normcase(os.path.abspath(key_value)) == os.path.normcase(\n        os.path.abspath(path))",
      "    return key_value == str(path)", [KR_SLASH]),
    m("same-file-never-same", "    return os.path.normcase(os.path.abspath(key_value)) == os.path.normcase(\n        os.path.abspath(path))",
      "    return False", [KR_KEY_IS_WAV]),

    # ---- status -------------------------------------------------------------
    m("status-key-not-fresh", "        key = read_lead_word_key(self.config_path)\n        current = self.wav_path.exists()",
      "        key = ''\n        current = self.wav_path.exists()", [ST_FRESH]),
    m("status-other-file-for-empty-key", "            key_names_other_file=bool(key) and not _same_file(key, self.wav_path),",
      "            key_names_other_file=not _same_file(key, self.wav_path),", [ST_EMPTY]),
    m("status-other-file-inverted", "            key_names_other_file=bool(key) and not _same_file(key, self.wav_path),",
      "            key_names_other_file=bool(key) and _same_file(key, self.wav_path),", [KO_BUTTON]),
    m("status-facts-read-without-recording", "        meta = self._read_meta() if current else {}",
      "        meta = self._read_meta()", [N_FACTS_MISSING]),
    m("status-facts-never-read", "        meta = self._read_meta() if current else {}",
      "        meta = {}", [KF_STATUS]),
    m("status-previous-from-wav", "            previous_exists=self.previous_path.exists(),",
      "            previous_exists=self.wav_path.exists(),", [KF_STATUS]),
    m("status-candidate-from-previous", "            candidate_exists=self.candidate_path.exists(),",
      "            candidate_exists=self.previous_path.exists(),", [ST_CANDIDATE]),
    m("status-recorded-key-wrong", '            recorded=str(meta.get("recorded", "")),',
      '            recorded=str(meta.get("microphone", "")),', [KF_STATUS]),
    m("status-microphone-key-wrong", '            microphone=str(meta.get("microphone", "")),',
      '            microphone=str(meta.get("recorded", "")),', [KF_STATUS]),
    m("read-meta-not-dict-kept", "            return data if isinstance(data, dict) else {}",
      "            return data", [N_NOT_OBJECT]),
    m("read-meta-error-raises", "        except (OSError, ValueError):\n            return {}",
      "        except OSError:\n            return {}", [ST_DAMAGED]),

    # ---- the candidate ------------------------------------------------------
    m("candidate-facts-no-measures", '            "measures": measures,', '            "measures": {},', [C_SAVE]),
    m("candidate-facts-recorded-empty", '            "recorded": self._now().strftime(RECORDED_FORMAT),',
      '            "recorded": "",', [C_SAVE]),
    m("candidate-facts-no-microphone", '            "microphone": microphone,', '            "microphone": "",', [C_SAVE]),
    m("candidate-facts-no-decode", '            "decode": measures.get("decode", ""),', '            "decode": "",', [C_SAVE]),
    m("candidate-no-folder-made", "            self.folder.mkdir(parents=True, exist_ok=True)", "            pass", [C_SAVE]),
    m("candidate-wav-not-moved", "            os.replace(tmp, self.candidate_path)", "            pass", [C_SAVE]),
    m("candidate-meta-not-written", "            _atomic_write(\n                self.candidate_meta_path,\n                json.dumps(facts, indent=2, ensure_ascii=False).encode(\"utf-8\"))",
      "            pass", [C_SAVE]),
    m("candidate-failure-leaves-files", "            self.discard_candidate()\n            raise LeadWordStoreError(f\"Cannot save the new recording: {e}\") from e",
      "            raise LeadWordStoreError(f\"Cannot save the new recording: {e}\") from e", [C_FAILED_WRITE]),
    m("candidate-failure-message-lost", '            raise LeadWordStoreError(f"Cannot save the new recording: {e}") from e',
      '            raise LeadWordStoreError("Cannot save the new recording") from e', [C_FAILED_WRITE]),
    m("candidate-temp-left-behind", "        finally:\n            if tmp.exists():\n                try:\n                    tmp.unlink()\n                except OSError:\n                    pass\n        return self.candidate_path",
      "        finally:\n            pass\n        return self.candidate_path", [N_TEMP]),
    m("candidate-path-not-returned", "        return self.candidate_path", "        return self.wav_path", [N_RETURNS]),
    m("discard-keeps-meta", "        for path in (self.candidate_path, self.candidate_meta_path):",
      "        for path in (self.candidate_path,):", [C_DISCARD]),
    m("discard-keeps-wav", "        for path in (self.candidate_path, self.candidate_meta_path):",
      "        for path in (self.candidate_meta_path,):", [C_DISCARD]),
    m("discard-deletes-kept-wav", "        for path in (self.candidate_path, self.candidate_meta_path):",
      "        for path in (self.candidate_path, self.candidate_meta_path, self.wav_path):", [C_DISCARD_KEEPS]),
    m("discard-reports-nothing-removed", "                path.unlink()\n                removed = True",
      "                path.unlink()", [C_DISCARD]),
    m("discard-reports-removed-always", "        removed = False\n        for path in", "        removed = True\n        for path in",
      [C_DISCARD]),
    m("discard-missing-file-raises", "            except FileNotFoundError:\n                pass\n            except OSError as e:",
      "            except PermissionError:\n                pass\n            except OSError as e:", [N_NO_WARN]),
    m("delete-refused-does-nothing", "        \"\"\"A refused recording leaves no candidate. Same as discard.\"\"\"\n        self.discard_candidate()",
      "        \"\"\"A refused recording leaves no candidate. Same as discard.\"\"\"\n        pass", [C_REFUSED]),

    # ---- snapshot, undo, fail, load -----------------------------------------
    m("all-paths-without-swap", "            self.meta_path, self.candidate_meta_path, self.folder / _FILE_SWAP,",
      "            self.meta_path, self.candidate_meta_path,", [RS_FAIL]),
    m("all-paths-without-previous", "            self.wav_path, self.previous_path, self.candidate_path,",
      "            self.wav_path, self.candidate_path,", [KFAIL_REPLACEMENT]),
    m("snapshot-config-missing", '            snap["config"] = _read_or_none(self.config_path)',
      '            snap["config"] = None', [KFAIL_KEY_WROTE]),
    m("snapshot-error-not-wrapped", '        except OSError as e:\n            raise LeadWordStoreError(f"Cannot read the training word files: {e}") from e',
      '        except OSError as e:\n            raise', [N_UNREADABLE]),
    m("undo-skips-absent-files", "                if before is None:\n                    path.unlink(missing_ok=True)",
      "                if before is None:\n                    pass", [KFAIL_FIRST_OTHER]),
    m("undo-skips-changed-files", "                elif _read_or_none(path) != before:\n                    _atomic_write(path, before)",
      "                elif False:\n                    _atomic_write(path, before)", [KFAIL_REPLACEMENT]),
    m("undo-skips-the-key", "        if key_touched and snap[\"config\"] is not None:",
      "        if False and snap[\"config\"] is not None:", [KFAIL_KEY_WROTE]),
    m("undo-always-writes-the-key", "        if key_touched and snap[\"config\"] is not None:",
      "        if snap[\"config\"] is not None:", [N_UNTOUCHED]),
    m("undo-key-compare-skipped", "                if _read_or_none(self.config_path) != snap[\"config\"]:\n                    _atomic_write(self.config_path, snap[\"config\"])",
      "                if False:\n                    _atomic_write(self.config_path, snap[\"config\"])", [KFAIL_KEY_WROTE]),
    m("undo-problems-lost", "                problems.append(f\"{path.name}: {e}\")", "                pass", [N_NAMED]),
    m("fail-problems-not-reported", "        if problems:\n            message += ",
      "        if False:\n            message += ", [N_NAMED]),
    m("fail-message-without-what", '        message = f"{what}: {error}"', '        message = f"{error}"', [KFAIL_REPLACEMENT]),
    m("load-error-not-wrapped", '            raise LeadWordStoreError(f"{path.name} cannot be used: {e}") from e',
      "            raise", [KF_UNUSABLE]),
    m("load-uses-other-rate", "            return load_lead_word_audio(str(path), lead_word_recorder.SAMPLE_RATE)",
      "            return load_lead_word_audio(str(path), 8000)", [KF_FIRST]),

]

# Mutants that no test can fail for. `--equivalent` runs them; each must be
# reported SURVIVED, and one that is caught is no longer equivalent.
EQUIVALENT_MUTANTS = [
    # The header line starts with "[", and _KEY_HEAD_RE needs the line to start
    # with optional white space and then the key name, so the header line can
    # never match; starting the search at the header changes nothing.
    m("write-key-searches-from-header", "        for i in range(engine_start + 1, engine_end):",
      "        for i in range(engine_start, engine_end):", [KW_HEADER_COMMENT]),
    # lines[engine_start] is the header line, never blank, so the loop stops at
    # insert_at == engine_start + 1 whether or not it tests that bound.
    m("write-key-insert-skips-header", "            while insert_at > engine_start + 1 and lines[insert_at - 1].strip() == \"\":",
      "            while insert_at > engine_start and lines[insert_at - 1].strip() == \"\":", [KW_ADD_LINE]),
    # Keep deletes hello.candidate.json only after the last step that can fail,
    # and no other action touches it, so at an undo it always equals its
    # snapshot and the undo has nothing to write or delete for it.
    m("all-paths-without-candidate-meta", "            self.meta_path, self.candidate_meta_path, self.folder / _FILE_SWAP,",
      "            self.meta_path, self.folder / _FILE_SWAP,", [KFAIL_REPLACEMENT]),
]


if __name__ == "__main__":
    main(MUTATIONS, EQUIVALENT_MUTANTS)
