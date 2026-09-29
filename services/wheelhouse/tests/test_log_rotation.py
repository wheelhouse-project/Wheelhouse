"""One application start rotates wheelhouse.log exactly once (wh-log-triple-rotation).

The launcher starts the Logic, Input, and GUI processes at the same moment,
and each of them calls setup_logging. Before this fix each of those calls
rotated a non-empty wheelhouse.log, so one start left two small startup
logs as backups and pushed the previous run two places further back.

The launcher now rotates once per start cycle, and setup_logging skips its
own rotation when the launcher says it has already rotated. These tests use
a temporary directory; they never touch the repo-root wheelhouse.log.
"""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import logging_setup  # noqa: E402
from utils.log_rotation import (  # noqa: E402
    LAUNCHER_ROTATED_ENV,
    LOG_BACKUP_COUNT,
    rotate_log_for_new_run,
)

PREVIOUS_RUN = "previous run line\n"
PROCESS_NAMES = ("LogicProcess", "InputProcess", "GuiProcess")


@pytest.fixture
def clean_root_logger():
    """Tear down any listener and restore the root logger around a test."""
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers.copy()
    original_level = root_logger.level
    logging_setup.shutdown_logging()
    root_logger.handlers.clear()
    yield root_logger
    logging_setup.shutdown_logging()
    for handler in root_logger.handlers:
        try:
            handler.close()
        except Exception:
            pass
    root_logger.handlers.clear()
    for handler in original_handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(original_level)


def _start_process(log_file: Path, name: str) -> None:
    """Run one service process's logging start against log_file.

    setup_logging builds the log path with os.path.join(..., "wheelhouse.log");
    the patch sends that one join to the temporary file. The process writes
    one startup line and stops its listener, so the line is on disk before
    the next process starts. That order is the worst case: every later
    process then finds a non-empty file.
    """
    original_join = os.path.join

    def patched_join(*args):
        if args and args[-1] == "wheelhouse.log":
            return str(log_file)
        return original_join(*args)

    with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
        logging_setup.setup_logging({"LOG_LEVEL": "INFO"})
        logging.getLogger(f"test.{name}").info("%s startup line", name)
        logging_setup.shutdown_logging()


def _backups(directory: Path) -> list:
    return sorted(
        p.name for p in directory.glob("wheelhouse.log.*")
        if p.name.split(".")[-1].isdigit()
    )


class TestOneStartRotatesOnce:
    def test_setup_logging_skips_rotation_when_the_launcher_already_rotated(
        self, tmp_path, monkeypatch, clean_root_logger
    ):
        """Three processes that start after the launcher's rotation add no backup."""
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text(PREVIOUS_RUN, encoding="utf-8")
        monkeypatch.setenv(LAUNCHER_ROTATED_ENV, "1")

        for name in PROCESS_NAMES:
            _start_process(log_file, name)

        assert _backups(tmp_path) == [], (
            "setup_logging rotated although the launcher had already rotated"
        )
        content = log_file.read_text(encoding="utf-8")
        for name in PROCESS_NAMES:
            assert f"{name} startup line" in content

    def test_one_app_start_rotates_the_log_exactly_once(
        self, tmp_path, monkeypatch, clean_root_logger
    ):
        """A2: the launcher's rotation plus three process starts leave one new backup."""
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text(PREVIOUS_RUN, encoding="utf-8")
        monkeypatch.setenv(LAUNCHER_ROTATED_ENV, "1")

        assert rotate_log_for_new_run(str(log_file)) is True
        for name in PROCESS_NAMES:
            _start_process(log_file, name)

        assert _backups(tmp_path) == ["wheelhouse.log.1"]
        assert (tmp_path / "wheelhouse.log.1").read_text(encoding="utf-8") == PREVIOUS_RUN
        content = log_file.read_text(encoding="utf-8")
        for name in PROCESS_NAMES:
            assert f"{name} startup line" in content
        assert "previous run line" not in content

    def test_setup_logging_still_rotates_without_the_launcher(
        self, tmp_path, monkeypatch, clean_root_logger
    ):
        """A process started without the launcher still begins a fresh log."""
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text(PREVIOUS_RUN, encoding="utf-8")
        monkeypatch.delenv(LAUNCHER_ROTATED_ENV, raising=False)

        _start_process(log_file, "LogicProcess")

        assert _backups(tmp_path) == ["wheelhouse.log.1"]
        assert (tmp_path / "wheelhouse.log.1").read_text(encoding="utf-8") == PREVIOUS_RUN


class TestRotateLogForNewRun:
    def test_keeps_five_backups_and_shifts_each_one_place(self, tmp_path):
        """Ruling 1 (13:36): a start keeps .1 to .5; the oldest backup drops off."""
        assert LOG_BACKUP_COUNT == 5
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text("run 0\n", encoding="utf-8")
        for i in range(1, 6):
            (tmp_path / f"wheelhouse.log.{i}").write_text(f"run {i}\n", encoding="utf-8")

        assert rotate_log_for_new_run(str(log_file)) is True

        assert _backups(tmp_path) == [f"wheelhouse.log.{i}" for i in range(1, 6)]
        for i in range(1, 6):
            assert (tmp_path / f"wheelhouse.log.{i}").read_text(encoding="utf-8") == f"run {i - 1}\n"

    def test_empty_log_is_not_rotated(self, tmp_path):
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text("", encoding="utf-8")

        assert rotate_log_for_new_run(str(log_file)) is False
        assert _backups(tmp_path) == []

    def test_missing_log_is_not_rotated(self, tmp_path):
        assert rotate_log_for_new_run(str(tmp_path / "wheelhouse.log")) is False
        assert _backups(tmp_path) == []


def _rotate_names(directory: Path) -> list:
    return sorted(p.name for p in directory.glob("wheelhouse.log.rotate.*"))


def _log_files(directory: Path) -> list:
    """Every wheelhouse.log file; the library's lock file is left out."""
    return sorted(p.name for p in directory.glob("wheelhouse.log*"))


def _fail_moves_of(monkeypatch, suffix: str) -> None:
    """Make every rename or replace from or onto a path ending in suffix fail.

    This is what Windows does when another program holds that file open,
    for example a text editor showing an old backup.
    """
    original_rename = os.rename
    original_replace = os.replace

    def refuse(original):
        def move(source, destination, *args, **kwargs):
            if str(source).endswith(suffix) or str(destination).endswith(suffix):
                raise PermissionError(13, "held open by another program", str(source))
            return original(source, destination, *args, **kwargs)
        return move

    monkeypatch.setattr(os, "rename", refuse(original_rename))
    monkeypatch.setattr(os, "replace", refuse(original_replace))


class TestRotationFailureKeepsThePreviousLog:
    """wh-log-triple-rotation.1.2: a held backup must not strand the previous run.

    The library's doRollover renames wheelhouse.log to a temporary
    wheelhouse.log.rotate.* name before it shifts the backups. When a
    backup cannot move, it raised with the previous run's log still under
    that temporary name, and the new run started an empty wheelhouse.log.
    """

    def _write_run_and_backups(self, tmp_path: Path) -> Path:
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text("run 0\n", encoding="utf-8")
        for i in range(1, 4):
            (tmp_path / f"wheelhouse.log.{i}").write_text(f"run {i}\n", encoding="utf-8")
        return log_file

    def test_a_backup_that_cannot_move_leaves_the_log_in_place(
        self, tmp_path, monkeypatch
    ):
        log_file = self._write_run_and_backups(tmp_path)
        _fail_moves_of(monkeypatch, "wheelhouse.log.1")

        with pytest.raises(OSError):
            rotate_log_for_new_run(str(log_file))

        assert _rotate_names(tmp_path) == []
        assert log_file.read_text(encoding="utf-8") == "run 0\n"
        assert (tmp_path / "wheelhouse.log.1").read_text(encoding="utf-8") == "run 1\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename an open file")
    def test_a_backup_held_open_by_another_program_leaves_the_log_in_place(
        self, tmp_path
    ):
        log_file = self._write_run_and_backups(tmp_path)

        with open(tmp_path / "wheelhouse.log.1", encoding="utf-8"):
            with pytest.raises(OSError):
                rotate_log_for_new_run(str(log_file))

        assert _rotate_names(tmp_path) == []
        assert log_file.read_text(encoding="utf-8") == "run 0\n"

    def test_a_standalone_start_whose_rotation_fails_still_logs_to_the_file(
        self, tmp_path, monkeypatch, clean_root_logger
    ):
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text(PREVIOUS_RUN, encoding="utf-8")
        monkeypatch.delenv(LAUNCHER_ROTATED_ENV, raising=False)

        def boom(path):
            raise PermissionError(13, "held open by another program", path)

        monkeypatch.setattr(logging_setup, "rotate_log_for_new_run", boom)
        _start_process(log_file, "LogicProcess")

        content = log_file.read_text(encoding="utf-8")
        assert "LogicProcess startup line" in content
        assert "Could not start a new wheelhouse.log for this run" in content


class TestFailedRotationKeepsEveryBackup:
    """wh-log-triple-rotation.1.3: a failed rotation must not delete a backup.

    The shift moved .4 over .5, .3 over .4, and so on, and when a lower
    backup could not move it restored only the log. Each failed start then
    deleted one more old backup without adding a new one.
    """

    def _write_run_and_five_backups(self, tmp_path: Path) -> Path:
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text("run 0\n", encoding="utf-8")
        for i in range(1, 6):
            (tmp_path / f"wheelhouse.log.{i}").write_text(f"run {i}\n", encoding="utf-8")
        return log_file

    def _assert_every_backup_kept(self, tmp_path: Path) -> None:
        assert _log_files(tmp_path) == ["wheelhouse.log"] + [
            f"wheelhouse.log.{i}" for i in range(1, 6)
        ]
        for i in range(1, 6):
            assert (tmp_path / f"wheelhouse.log.{i}").read_text(encoding="utf-8") == f"run {i}\n"

    @pytest.mark.parametrize("held", [1, 2, 3, 4, 5])
    def test_repeated_failed_rotations_keep_every_backup(self, tmp_path, monkeypatch, held):
        log_file = self._write_run_and_five_backups(tmp_path)
        _fail_moves_of(monkeypatch, f"wheelhouse.log.{held}")

        for attempt in range(3):
            with pytest.raises(OSError):
                rotate_log_for_new_run(str(log_file))
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"start {attempt}\n")

        self._assert_every_backup_kept(tmp_path)
        assert log_file.read_text(encoding="utf-8") == "run 0\nstart 0\nstart 1\nstart 2\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename an open file")
    @pytest.mark.parametrize("held", [1, 2])
    def test_a_backup_held_open_across_restarts_keeps_every_backup(self, tmp_path, held):
        log_file = self._write_run_and_five_backups(tmp_path)

        with open(tmp_path / f"wheelhouse.log.{held}", encoding="utf-8"):
            for attempt in range(3):
                with pytest.raises(OSError):
                    rotate_log_for_new_run(str(log_file))
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"start {attempt}\n")

        self._assert_every_backup_kept(tmp_path)

    def test_a_successful_rotation_leaves_no_extra_file(self, tmp_path):
        log_file = self._write_run_and_five_backups(tmp_path)

        assert rotate_log_for_new_run(str(log_file)) is True

        assert _log_files(tmp_path) == [
            f"wheelhouse.log.{i}" for i in range(1, 6)
        ]
        for i in range(1, 6):
            assert (tmp_path / f"wheelhouse.log.{i}").read_text(encoding="utf-8") == f"run {i - 1}\n"



class TestFailedUndoStillRestoresTheLog:
    """wh-log-triple-rotation.1.4: one failed undo must not strand the log.

    A failed rotation undoes its moves newest first, and the log's own move
    is undone last. When one backup could not move back (a program had just
    opened it to read it), the undo stopped there, and the previous run
    stayed under the temporary wheelhouse.log.rotate.* name.
    """

    def _fail_forward_and_one_undo(self, monkeypatch, undo_source: str, undo_destination: str):
        """.1 cannot move at all, and one backup cannot move back."""
        original_rename = os.rename

        def rename(source, destination, *args, **kwargs):
            if str(source).endswith("wheelhouse.log.1") or str(destination).endswith(
                "wheelhouse.log.1"
            ):
                raise PermissionError(13, "held open by another program", str(source))
            if str(source).endswith(undo_source) and str(destination).endswith(undo_destination):
                raise PermissionError(13, "opened by a reader", str(source))
            return original_rename(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "rename", rename)

    @pytest.mark.parametrize(
        "undo_source, undo_destination",
        [
            ("wheelhouse.log.3", "wheelhouse.log.2"),
            ("wheelhouse.log.4", "wheelhouse.log.3"),
            ("wheelhouse.log.5", "wheelhouse.log.4"),
        ],
    )
    def test_the_log_returns_to_its_own_name(
        self, tmp_path, monkeypatch, undo_source, undo_destination
    ):
        log_file = tmp_path / "wheelhouse.log"
        log_file.write_text("run 0\n", encoding="utf-8")
        for i in range(1, 6):
            (tmp_path / f"wheelhouse.log.{i}").write_text(f"run {i}\n", encoding="utf-8")
        self._fail_forward_and_one_undo(monkeypatch, undo_source, undo_destination)

        with pytest.raises(PermissionError) as raised:
            rotate_log_for_new_run(str(log_file))

        # The original failure is raised, and the failed undo is named.
        assert raised.value.strerror == "held open by another program"
        notes = getattr(raised.value, "__notes__", [])
        assert any("opened by a reader" in note for note in notes), notes
        assert log_file.read_text(encoding="utf-8") == "run 0\n"
        assert _rotate_names(tmp_path) == []
        # No payload is lost, whatever name it has now.
        payloads = sorted(
            p.read_text(encoding="utf-8") for p in tmp_path.glob("wheelhouse.log*")
        )
        assert payloads == [f"run {i}\n" for i in range(6)]
