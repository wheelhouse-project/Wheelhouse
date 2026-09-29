"""WheelHouse reads a provider's error output into wheelhouse.log.

wh-provider-native-crash-trace. The provider's supervisor
(shared_stt/launcher.py) sends main.py's stderr to
%APPDATA%\\WheelHouse\\<provider>.stderr.log and appends one
"[launcher] ... exited with code N (0xHHHHHHHH)" line per exit. The
module under test copies the lines added since the launch into
wheelhouse.log, through an allow-list filter, and reads the exit code
for the notices.

Why an allow-list and not a copy of every line (ruling R3): a Python
exception message can hold recognized text, and wheelhouse.log is the
file people send when they report a problem. faulthandler lines carry
only file names, line numbers and function names.
"""
from __future__ import annotations

import logging
import os

import pytest

from stt import provider_stderr
from stt.provider_stderr import (
    ProviderStderrTail,
    filter_lines,
    is_native_crash,
    native_crash_message,
)

LOGGER = "stt.provider_stderr"

FAULTHANDLER_DUMP = [
    "Windows fatal exception: access violation",
    "",
    "Thread 0x00002c10 (most recent call first):",
    '  File "C:\\app\\services\\stt_providers\\x\\main.py", line 5 in <module>',
    "",
    "Current thread 0x0000bab8 (most recent call first):",
    '  File "C:\\app\\lib\\sherpa.py", line 88 in decode',
    "  <no Python frame>",
    "",
    "Extension modules: numpy._core._multiarray_umath, sherpa_onnx (total: 2)",
    "[launcher] parakeet_tdt process exited with code 3221225477 (0xC0000005) after 41.2s",
]


@pytest.fixture(autouse=True)
def _transcripts_off(monkeypatch):
    monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)


class TestTheFilter:

    def test_a_faulthandler_dump_is_kept_whole(self):
        kept = filter_lines(FAULTHANDLER_DUMP)

        assert kept == [line for line in FAULTHANDLER_DUMP if line]

    def test_a_python_traceback_keeps_its_frames_and_hides_the_message(self):
        """The exception message is where recognized text could appear."""
        kept = filter_lines([
            "Traceback (most recent call last):",
            '  File "C:\\app\\main.py", line 12, in handle',
            "    raise ValueError(text)",
            "ValueError: please type my password here",
        ])

        assert kept == [
            "Traceback (most recent call last):",
            '  File "C:\\app\\main.py", line 12, in handle',
            "ValueError: <redacted: 28 chars, 5 words>",
            "<1 other stderr lines not copied>",
        ]

    def test_a_dotted_exception_type_is_kept(self):
        kept = filter_lines(["sherpa_onnx.errors.DecodeError: bad input"])

        assert kept == ["sherpa_onnx.errors.DecodeError: <redacted: 9 chars, 2 words>"]

    def test_the_message_is_kept_when_transcript_logging_is_on(self, monkeypatch):
        """The one documented switch (LOG_TRANSCRIPTS) keeps its meaning
        here too."""
        monkeypatch.setenv("WHEELHOUSE_LOG_TRANSCRIPTS", "1")

        assert filter_lines(["ValueError: hello there"]) == [
            "ValueError: hello there"]

    def test_every_other_line_is_counted_not_copied(self):
        kept = filter_lines([
            "the quick brown fox said something private",
            "  another indented line",
            "Fatal Python error: Segmentation fault",
        ])

        assert kept == [
            "Fatal Python error: Segmentation fault",
            "<2 other stderr lines not copied>",
        ]
        assert not any("private" in line for line in kept)

    def test_a_long_line_is_cut(self):
        kept = filter_lines(['  File "' + "d" * 900 + '.py", line 1 in f'])

        assert len(kept) == 1
        assert len(kept[0]) == provider_stderr.MAX_LINE_CHARS


class TestNativeCrashCodes:

    @pytest.mark.parametrize("code", [
        pytest.param(3221225477, id="access-violation"),
        pytest.param(-1073741819, id="access-violation-signed"),
        pytest.param(0xC0000409, id="stack-buffer-overrun"),
        pytest.param(0xC00000FD, id="stack-overflow"),
        pytest.param(0xC0000000, id="lowest-error-status"),
    ])
    def test_error_status_codes_are_native_crashes(self, code):
        assert is_native_crash(code) is True

    @pytest.mark.parametrize("code", [
        pytest.param(0, id="clean"),
        pytest.param(1, id="python-error"),
        pytest.param(3, id="refusal"),
        pytest.param(0xC000013A, id="ctrl-c"),
        pytest.param(0x80000003, id="warning-status"),
        pytest.param(0xBFFFFFFF, id="just-below"),
        pytest.param(None, id="unknown"),
    ])
    def test_other_codes_are_not(self, code):
        assert is_native_crash(code) is False


class TestTheNoticeText:
    """David approved the first text at 09:07 2026-09-25; the second is
    ruling R4 (overridable). R5: the display name, and the code that
    actually occurred in hexadecimal."""

    def test_the_restarting_text(self):
        assert native_crash_message("Parakeet", 3221225477, restarting=True) == (
            "The Parakeet speech engine stopped because of an error in its "
            "program code (exit code 0xC0000005). WheelHouse is starting it "
            "again. The file wheelhouse.log has the details.")

    def test_the_stopped_text(self):
        assert native_crash_message("Parakeet", 3221225477, restarting=False) == (
            "The Parakeet speech engine stopped because of an error in its "
            "program code (exit code 0xC0000005) and did not start again. "
            "The file wheelhouse.log has the details.")

    def test_the_actual_code_is_shown(self):
        text = native_crash_message("Whisper", -1073740791, restarting=False)

        assert "(exit code 0xC0000409)" in text
        assert text.startswith("The Whisper speech engine")


_CRASH_EXIT_LINE = (
    "[launcher] parakeet_tdt process exited with code 3221225477 "
    "(0xC0000005) after 2.1s\n")


def _write(path, text):
    with open(path, "ab") as stream:
        stream.write(text.encode("utf-8"))


class TestTheTail:

    @pytest.fixture
    def tail(self, tmp_path):
        return ProviderStderrTail(tmp_path)

    @pytest.fixture
    def path(self, tmp_path):
        return tmp_path / "parakeet_tdt.stderr.log"

    def test_the_file_name(self, tmp_path):
        assert provider_stderr.stderr_log_path(tmp_path, "parakeet_tdt") == (
            tmp_path / "parakeet_tdt.stderr.log")

    def test_only_lines_after_the_launch_are_copied(self, tail, path, caplog):
        _write(path, "Fatal Python error: OLD RUN\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: NEW RUN\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: NEW RUN"]
        assert all(record.levelno == logging.WARNING for record in caplog.records)

    def test_each_line_is_copied_once(self, tail, path, caplog):
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: FIRST\n")
        tail.copy_new("parakeet_tdt")
        _write(path, "Fatal Python error: SECOND\n")
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: SECOND"]

    def test_the_last_exit_code_is_returned(self, tail, path):
        tail.mark_launch("parakeet_tdt")
        _write(path,
               "[launcher] parakeet_tdt process exited with code 1 (0x00000001) after 2.0s\n"
               "[launcher] parakeet_tdt process exited with code 3221225477 (0xC0000005) after 2.1s\n")

        assert tail.copy_new("parakeet_tdt") == 3221225477

    def test_no_exit_line_returns_none(self, tail, path):
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: x\n")

        assert tail.copy_new("parakeet_tdt") is None

    def test_an_exit_line_in_the_old_run_is_not_returned(self, tail, path):
        _write(path, "[launcher] parakeet_tdt process exited with code 3221225477 (0xC0000005) after 2.1s\n")
        tail.mark_launch("parakeet_tdt")

        assert tail.copy_new("parakeet_tdt") is None

    def test_a_partial_last_line_waits_for_its_end(self, tail, path, caplog):
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: HALF")
        tail.copy_new("parakeet_tdt")
        _write(path, " DONE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: HALF DONE"]

    def test_a_file_moved_aside_is_read_from_the_start(self, tail, path, caplog):
        """The supervisor moves a large file to .1 at child start, so the
        file can become shorter than the offset."""
        _write(path, "x" * 5000 + "\n")
        tail.mark_launch("parakeet_tdt")
        path.unlink()
        _write(path, "Fatal Python error: AFTER MOVE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: AFTER MOVE"]

    def test_lines_written_before_a_move_aside_are_copied(
            self, tail, path, caplog):
        """A crash, then the supervisor's restart moves the file aside:
        the crash's lines are in the .1 file after the recorded offset
        (wh-provider-native-crash-trace.2.1)."""
        _write(path, "x" * 5000 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path,
               "Windows fatal exception: access violation\n"
               "[launcher] parakeet_tdt process exited with code 3221225477 "
               "(0xC0000005) after 2.1s\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            code = tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] [launcher] parakeet_tdt process exited "
            "with code 3221225477 (0xC0000005) after 2.1s",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]
        assert code == 3221225477

    def test_a_move_is_seen_when_the_new_file_is_already_longer(
            self, tail, path, caplog):
        """The size cannot show this move: the new file is longer than
        the offset. The file identity shows it."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE " + "y" * 400 + "\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE " + "y" * 400,
        ]

    def test_a_launch_with_no_file_yet_still_sees_the_move(
            self, tail, path, caplog):
        """mark_launch creates the missing file, so the launch has an
        identity to compare."""
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]

    def test_without_file_identities_a_shorter_file_still_reads_the_moved_part(
            self, tail, path, caplog, monkeypatch):
        """A file system that reports no file index falls back to the
        size: a file shorter than the offset was moved."""
        monkeypatch.setattr(provider_stderr, "_file_id", lambda stat: None)
        _write(path, "x" * 5000 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]

    def test_a_moved_file_that_is_not_this_launchs_is_not_read(
            self, tail, path, caplog):
        """Two moves inside one launch: .1 holds another file, and its
        bytes after the offset are not this launch's first lines."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: SECOND FILE " + "z" * 200 + "\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: THIRD FILE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: THIRD FILE"]

    def test_a_move_aside_at_the_first_start_copies_no_old_lines(
            self, tail, path, caplog):
        """The move at the launch's first child start leaves the .1 file
        exactly as long as the offset: none of it is this launch's."""
        _write(path, "Fatal Python error: OLD RUN\n" * 200)
        tail.mark_launch("parakeet_tdt")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: NEW RUN\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: NEW RUN"]

    def test_the_moved_aside_part_is_read_after_the_move_even_with_no_new_line(
            self, tail, path, caplog):
        """The new file holds only a partial line: the .1 lines are still
        copied, and the partial line waits for its end."""
        _write(path, "x" * 5000 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: BEFORE MOVE\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: HALF")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")
            _write(path, " DONE\n")
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Fatal Python error: BEFORE MOVE",
            "[parakeet_tdt stderr] Fatal Python error: HALF DONE",
        ]

    def test_only_the_last_part_of_a_large_moved_aside_addition_is_read(
            self, tail, path, caplog):
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: EARLY\n" * 2000)
        _write(path, "Fatal Python error: LATE\n")
        os.replace(path, f"{path}.1")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages[-1] == "[parakeet_tdt stderr] Fatal Python error: LATE"
        assert "bytes before these lines not copied" in messages[0]
        line_bytes = len("Fatal Python error: EARLY\n")
        assert len(messages) <= provider_stderr.MAX_COPY_BYTES // line_bytes + 1

    def test_only_the_last_part_of_a_large_addition_is_read(
            self, tail, path, caplog):
        """The crash is at the end. The cap keeps one copy from filling
        wheelhouse.log."""
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: EARLY\n" * 2000)
        _write(path, "Fatal Python error: LATE\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages[-1] == "[parakeet_tdt stderr] Fatal Python error: LATE"
        assert messages[0].startswith("[parakeet_tdt stderr] <")
        assert "bytes before these lines not copied" in messages[0]
        early = [m for m in messages if m.endswith("EARLY")]
        line_bytes = len("Fatal Python error: EARLY\n")
        assert len(early) <= provider_stderr.MAX_COPY_BYTES // line_bytes
        assert len(early) >= provider_stderr.MAX_COPY_BYTES // line_bytes - 2

    def test_a_missing_file_copies_nothing_and_does_not_raise(
            self, tail, caplog):
        tail.mark_launch("parakeet_tdt")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert tail.copy_new("parakeet_tdt") is None

        assert caplog.records == []

    def test_an_unreadable_file_does_not_raise(self, tail, path, monkeypatch):
        tail.mark_launch("parakeet_tdt")
        _write(path, "Fatal Python error: x\n")

        def refuse(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(provider_stderr, "open", refuse, raising=False)

        assert tail.copy_new("parakeet_tdt") is None

    def test_a_passing_stat_failure_does_not_replay_old_lines(
            self, tail, path, caplog, monkeypatch):
        """A file that cannot be examined for a moment was not moved:
        after access returns, the lines already copied are not copied
        again, and their exit code is not returned again
        (wh-provider-native-crash-trace.2.2)."""
        tail.mark_launch("parakeet_tdt")
        _write(path, _CRASH_EXIT_LINE)
        tail.copy_new("parakeet_tdt")
        real_stat = os.stat

        def refuse(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError("locked")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(os, "stat", refuse)
        assert tail.copy_new("parakeet_tdt") is None
        monkeypatch.setattr(os, "stat", real_stat)
        _write(path, "Fatal Python error: NEW\n")
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            code = tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: NEW"]
        assert code is None

    def test_a_locked_new_file_still_logs_the_moved_aside_lines(
            self, tail, path, caplog, monkeypatch):
        """The .1 part is logged even when the new file cannot be opened,
        and the new file is read when it can be
        (wh-provider-native-crash-trace.2.2). The exit code in .1 is an
        earlier child's; the exit that ended the run may be in the file
        that could not be read, so no code is returned (.2.4)."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n"
               + _CRASH_EXIT_LINE)
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")
        real_open = open

        def refuse_new(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError("locked")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(provider_stderr, "open", refuse_new, raising=False)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            code = tail.copy_new("parakeet_tdt")
            monkeypatch.delattr(provider_stderr, "open")
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] " + _CRASH_EXIT_LINE.rstrip("\n"),
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]
        assert code is None

    @pytest.mark.parametrize("later_code, later_hex", [
        pytest.param(1, "0x00000001", id="ordinary"),
        pytest.param(3221226505, "0xC0000409", id="native"),
    ])
    def test_a_locked_new_file_with_a_later_exit_returns_that_exit_once_read(
            self, tail, path, caplog, monkeypatch, later_code, later_hex):
        """.1 holds an earlier child's native crash; the locked new file
        holds the exit that ended the run. The earlier code is never
        returned, and once the lock clears the later code is returned and
        each line is logged once (wh-provider-native-crash-trace.2.4)."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, _CRASH_EXIT_LINE)
        os.replace(path, f"{path}.1")
        later = (f"[launcher] parakeet_tdt process exited with code "
                 f"{later_code} ({later_hex}) after 3.0s")
        _write(path, later + "\n")
        real_open = open

        def refuse_new(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError("locked")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(provider_stderr, "open", refuse_new, raising=False)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            first = tail.copy_new("parakeet_tdt")
            monkeypatch.delattr(provider_stderr, "open")
            second = tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] " + _CRASH_EXIT_LINE.rstrip("\n"),
            "[parakeet_tdt stderr] " + later,
        ]
        assert first is None
        assert second == later_code

    def test_a_moved_file_that_cannot_be_examined_now_is_read_later(
            self, tail, path, caplog, monkeypatch):
        """A .1 file that cannot be examined for a moment is examined
        again next time, not skipped (wh-provider-native-crash-trace.2.3)."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")
        real_stat = os.stat
        moved = f"{path}.1"

        def refuse_moved(target, *args, **kwargs):
            if os.fspath(target) == moved:
                raise PermissionError("locked")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(os, "stat", refuse_moved)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert tail.copy_new("parakeet_tdt") is None
            monkeypatch.setattr(os, "stat", real_stat)
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]

    def test_one_look_gives_the_size_and_the_identity_together(
            self, tail, path, caplog, monkeypatch):
        """A second look at the file that fails after a first that worked
        cannot make the size and the identity disagree, so the lines are
        not read twice (wh-provider-native-crash-trace.2.3)."""
        tail.mark_launch("parakeet_tdt")
        _write(path, _CRASH_EXIT_LINE)
        tail.copy_new("parakeet_tdt")
        real_stat = os.stat
        looks = []

        def refuse_second_look(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                looks.append(target)
                if len(looks) == 2:
                    raise PermissionError("locked")
            return real_stat(target, *args, **kwargs)

        caplog.clear()

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            monkeypatch.setattr(os, "stat", refuse_second_look)
            first = tail.copy_new("parakeet_tdt")
            monkeypatch.setattr(os, "stat", real_stat)
            _write(path, "Fatal Python error: NEW\n")
            second = tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: NEW"]
        assert first is None and second is None

    def test_a_new_file_that_cannot_be_examined_returns_no_earlier_exit_code(
            self, tail, path, caplog, monkeypatch):
        """The new file exists but cannot be examined: its lines may hold
        the exit that ended the run, so neither .1's earlier exit code nor
        its lines are taken now. Both files are read together next time
        (wh-provider-native-crash-trace.2.4)."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, _CRASH_EXIT_LINE)
        os.replace(path, f"{path}.1")
        _write(path, "[launcher] parakeet_tdt process exited with code 1 "
               "(0x00000001) after 3.0s\n")
        real_stat = os.stat

        def refuse_new(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError("locked")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(os, "stat", refuse_new)
        assert tail.copy_new("parakeet_tdt") is None
        monkeypatch.setattr(os, "stat", real_stat)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            code = tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] " + _CRASH_EXIT_LINE.rstrip("\n"),
            "[parakeet_tdt stderr] [launcher] parakeet_tdt process exited "
            "with code 1 (0x00000001) after 3.0s",
        ]
        assert code == 1

    def test_a_moved_file_that_cannot_be_read_now_is_read_later(
            self, tail, path, caplog, monkeypatch):
        """A failed read of the .1 file does not mark it as read."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, "Windows fatal exception: access violation\n")
        os.replace(path, f"{path}.1")
        _write(path, "Fatal Python error: AFTER MOVE\n")
        real_open = open
        moved = f"{path}.1"

        def refuse_moved(target, *args, **kwargs):
            if os.fspath(target) == moved:
                raise PermissionError("locked")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(provider_stderr, "open", refuse_moved, raising=False)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert tail.copy_new("parakeet_tdt") is None
            monkeypatch.delattr(provider_stderr, "open")
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] Windows fatal exception: access violation",
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]

    def test_a_move_seen_before_the_new_file_exists_copies_the_moved_lines(
            self, tail, path, caplog):
        """Between the supervisor's rename and its new file, the path is
        missing and the .1 file is the launch's own: that is a move."""
        _write(path, "x" * 99 + "\n")
        tail.mark_launch("parakeet_tdt")
        _write(path, _CRASH_EXIT_LINE)
        os.replace(path, f"{path}.1")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            code = tail.copy_new("parakeet_tdt")
            _write(path, "Fatal Python error: AFTER MOVE\n")
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "[parakeet_tdt stderr] " + _CRASH_EXIT_LINE.rstrip("\n"),
            "[parakeet_tdt stderr] Fatal Python error: AFTER MOVE",
        ]
        assert code == 3221225477

    def test_a_provider_never_launched_starts_at_the_current_end(
            self, tail, path, caplog):
        """A provider WheelHouse did not start this run (one adopted from
        an earlier run) must not replay an old crash."""
        _write(path, "Fatal Python error: OLD\n")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert tail.copy_new("parakeet_tdt") is None
            _write(path, "Fatal Python error: NEW\n")
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["[parakeet_tdt stderr] Fatal Python error: NEW"]

    def test_bytes_that_are_not_utf8_do_not_raise(self, tail, path, caplog):
        tail.mark_launch("parakeet_tdt")
        with open(path, "ab") as stream:
            stream.write(b'  File "C:\\Users\\Jos\xe9\\main.py", line 3 in f\n')

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            tail.copy_new("parakeet_tdt")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            '[parakeet_tdt stderr]   File "C:\\Users\\Jos\\xe9\\main.py", line 3 in f']
