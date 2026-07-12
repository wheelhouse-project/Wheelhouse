"""Tests for logging setup -- queued architecture and rotating log files.

Architecture (post wh-rus5u): the root logger has a _DroppingQueueHandler
plus an ErrorNotificationHandler. The ConcurrentRotatingFileHandler and
stderr StreamHandler live inside a WheelHouseQueueListener owned by
setup_logging. Tests inspect the listener via get_listener() /
get_file_handler() instead of walking root.handlers.

Behaviour preserved from the previous synchronous design:
- Log rotates on every startup/restart (previous session -> backup)
- Log rotates at 10MB during a session, keeps 5 backups
- Backup files named wheelhouse.log.1, .2, etc.
"""
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRotatingLogHandler:
    """Tests for rotating log file behavior."""

    @pytest.fixture
    def temp_log_dir(self, tmp_path):
        """Create a temporary directory for log files."""
        return tmp_path

    @pytest.fixture
    def clean_root_logger(self):
        """Ensure root logger is clean and listener is torn down between tests."""
        from utils import logging_setup

        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers.copy()
        original_level = root_logger.level
        # Make sure no prior listener/notifier worker survives.
        logging_setup.shutdown_logging()
        root_logger.handlers.clear()
        yield root_logger
        # Tear down listener/worker we may have started, then restore.
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

    def _get_log_file_path(self, temp_log_dir):
        """Helper to get the log file path in temp dir."""
        return str(temp_log_dir / "wheelhouse.log")

    def test_log_rotates_previous_session_on_restart(self, temp_log_dir, clean_root_logger):
        """On restart, previous session's log is rotated to backup; new session starts fresh."""
        from utils import logging_setup
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"
        backup_file = temp_log_dir / "wheelhouse.log.1"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            # First "startup" - write some logs
            setup_logging(config)
            logger = logging.getLogger("test_append_1")
            logger.info("First startup message")

            # Drain queue to file before reading.
            logging_setup.shutdown_logging()

            first_content = log_file.read_text()
            assert "First startup message" in first_content

            # Second "startup" - triggers startup rotation
            setup_logging(config)
            logger2 = logging.getLogger("test_append_2")
            logger2.info("Second startup message")

            # Drain queue again before reading.
            logging_setup.shutdown_logging()

            assert backup_file.exists(), "Previous session should be rotated to .1"
            backup_content = backup_file.read_text()
            assert "First startup message" in backup_content

            main_content = log_file.read_text()
            assert "Second startup message" in main_content
            assert "First startup message" not in main_content

    def test_log_rotates_at_10mb(self, temp_log_dir, clean_root_logger):
        """File handler is configured with maxBytes=10MB."""
        from utils import logging_setup
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            setup_logging(config)
            file_handler = logging_setup.get_file_handler()

        assert file_handler is not None, "Expected a file handler on the listener"
        assert file_handler.__class__.__name__ == "ConcurrentRotatingFileHandler"
        assert file_handler.maxBytes == 10 * 1024 * 1024

    def test_keeps_5_backup_files(self, temp_log_dir, clean_root_logger):
        """Listener-owned file handler has backupCount=5."""
        from utils import logging_setup
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            setup_logging(config)
            file_handler = logging_setup.get_file_handler()

        assert file_handler is not None
        assert file_handler.backupCount == 5

    def test_backup_files_named_correctly(self, temp_log_dir, clean_root_logger):
        """Backup files are named wheelhouse.log.1, .2, etc.

        Uses a stdlib RotatingFileHandler directly to verify naming
        convention; this is independent of our setup_logging plumbing.
        """
        from logging.handlers import RotatingFileHandler

        log_file = temp_log_dir / "wheelhouse.log"
        handler = RotatingFileHandler(
            str(log_file),
            mode="a",
            maxBytes=100,
            backupCount=3,
            encoding="utf-8",
        )

        logger = logging.getLogger("test_naming")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        for i in range(50):
            logger.info(f"Message {i}: " + "x" * 50)

        handler.close()
        logger.removeHandler(handler)

        backup_1 = temp_log_dir / "wheelhouse.log.1"
        assert log_file.exists(), "Main log file should exist"
        assert backup_1.exists(), "wheelhouse.log.1 should exist after rotation"

    def test_uses_rotating_file_handler_not_file_handler(self, temp_log_dir, clean_root_logger):
        """setup_logging uses ConcurrentRotatingFileHandler under the listener."""
        from utils import logging_setup
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            setup_logging(config)

            root_logger = logging.getLogger()
            file_handlers_on_root = [
                h for h in root_logger.handlers
                if h.__class__.__name__ in ("FileHandler", "ConcurrentRotatingFileHandler")
            ]
            assert file_handlers_on_root == [], (
                "File handlers should be owned by the listener, not on root"
            )

            file_handler = logging_setup.get_file_handler()
            assert file_handler is not None
            assert file_handler.__class__.__name__ == "ConcurrentRotatingFileHandler"

    def test_log_rotates_on_startup_when_existing_log_has_content(
        self, temp_log_dir, clean_root_logger
    ):
        """Log file is rotated on startup so each run starts with a fresh log."""
        from utils import logging_setup
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"
        backup_file = temp_log_dir / "wheelhouse.log.1"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        log_file.write_text("Previous session log line\n")

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            setup_logging(config)
            # Drain queued setup-time records to disk so the main log
            # file is materialised by the listener thread.
            logging_setup.shutdown_logging()

            assert backup_file.exists(), "Previous log should be rotated to wheelhouse.log.1"
            backup_content = backup_file.read_text()
            assert "Previous session log line" in backup_content

            main_content = log_file.read_text()
            assert "Previous session log line" not in main_content

    def test_log_does_not_rotate_on_startup_when_log_is_empty(
        self, temp_log_dir, clean_root_logger
    ):
        """No rotation on startup if the log file doesn't exist or is empty."""
        from utils.logging_setup import setup_logging

        log_file = temp_log_dir / "wheelhouse.log"  # noqa: F841 - referenced via patched_join
        backup_file = temp_log_dir / "wheelhouse.log.1"
        log_file_str = str(log_file)
        config = {"LOG_LEVEL": "INFO"}

        original_join = os.path.join

        def patched_join(*args):
            if args and args[-1] == "wheelhouse.log":
                return log_file_str
            return original_join(*args)

        with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
            setup_logging(config)

            assert not backup_file.exists(), "No backup should exist on fresh start"
