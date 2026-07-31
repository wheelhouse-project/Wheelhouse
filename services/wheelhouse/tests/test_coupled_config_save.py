"""Tests for saving several configuration values as one change.

The floating button's size and position are one coupled setting. A resize
changes both at once: the button grows around its centre, so its stored
top-left corner moves with its diameter. Saving them as two independent
changes has two failure modes, and neither is visible from the GUI side:

* Only one of the two survives -- the Logic process stops, or one write
  fails, between them -- and the next start places a differently sized
  button at a corner that belonged to the old size.
* The two whole-file writes overlap. Each one truncates the settings file
  and rewrites it, so two running together can leave it torn.

So the pair travels to the Logic process in one message and is written once.

See docs/superpowers/specs/2026-07-25-floating-button-drag-resize-design.md.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestTheGuiSendsOneMessage:
    def test_a_finished_resize_sends_a_single_command(self):
        from PySide6.QtCore import QPoint

        from gui import GuiManager

        manager = MagicMock()
        GuiManager.send_resize_commit_command(manager, 80, QPoint(385, 285))

        assert manager.send_command.call_count == 1

    def test_that_command_carries_both_the_size_and_the_position(self):
        from PySide6.QtCore import QPoint

        from gui import GuiManager

        manager = MagicMock()
        GuiManager.send_resize_commit_command(manager, 80, QPoint(385, 285))

        command = manager.send_command.call_args[0][0]
        assert command["action"] == "set_config_values"
        assert command["values"] == {
            "FLOATING_BUTTON_SIZE": 80,
            "FLOATING_BUTTON_POS": [385, 285],
        }


class TestTheLogicSideWritesOnce:
    @pytest.fixture
    def state_manager(self):
        from state_manager import StateManager

        manager = MagicMock(spec=StateManager)
        manager.config_service = MagicMock()
        manager.config_service.save = AsyncMock()
        return manager

    def _apply(self, state_manager, values):
        from state_manager import StateManager

        asyncio.run(StateManager.set_config_values(state_manager, values))

    def test_every_value_is_applied(self, state_manager):
        self._apply(
            state_manager,
            {"FLOATING_BUTTON_SIZE": 80, "FLOATING_BUTTON_POS": [385, 285]},
        )

        applied = dict(call[0] for call in state_manager.config_service.set.call_args_list)
        assert applied == {
            "FLOATING_BUTTON_SIZE": 80,
            "FLOATING_BUTTON_POS": [385, 285],
        }

    def test_the_file_is_written_once_for_the_whole_group(self, state_manager):
        self._apply(
            state_manager,
            {"FLOATING_BUTTON_SIZE": 80, "FLOATING_BUTTON_POS": [385, 285]},
        )

        state_manager.config_service.save.assert_awaited_once()

    def test_every_value_is_applied_before_the_write(self, state_manager):
        order = []
        state_manager.config_service.set.side_effect = lambda k, v: order.append(k)
        state_manager.config_service.save.side_effect = lambda: order.append("save")

        self._apply(
            state_manager,
            {"FLOATING_BUTTON_SIZE": 80, "FLOATING_BUTTON_POS": [385, 285]},
        )

        assert order.index("save") == len(order) - 1

    def test_an_empty_group_writes_nothing(self, state_manager):
        self._apply(state_manager, {})

        state_manager.config_service.save.assert_not_awaited()


class TestConcurrentWritesDoNotOverlap:
    """Two settings writes must never be inside the file at the same time.

    Saving rewrites the whole file, so an overlap can leave it torn. This is
    reachable from more than the resize: any two settings changes close
    together do it, which is why the guard lives in the writer rather than in
    the one caller that made it likely.
    """

    @pytest.fixture
    def config_service(self, tmp_path):
        from config_service import ConfigService

        path = tmp_path / "config.toml"
        path.write_text("FLOATING_BUTTON_SIZE = 50\n", encoding="utf-8")
        service = ConfigService(str(path))
        return service

    def test_two_saves_started_together_do_not_interleave(self, config_service, monkeypatch):
        import time

        import tomli_w

        overlaps = []
        inside = []
        real_dump = tomli_w.dump

        def slow_dump(*args, **kwargs):
            # Watch the write itself, and hold the file open long enough that a
            # second writer would genuinely be inside it at the same time.
            # Watching only the open() call would miss the overlap entirely,
            # because open() returns immediately and the writing comes after.
            inside.append(1)
            if len(inside) > 1:
                overlaps.append(True)
            try:
                time.sleep(0.05)
                return real_dump(*args, **kwargs)
            finally:
                inside.pop()

        monkeypatch.setattr(tomli_w, "dump", slow_dump)

        async def both():
            await asyncio.gather(config_service.save(), config_service.save())

        asyncio.run(both())

        assert len(inside) == 0
        assert overlaps == []

    def test_the_file_is_still_readable_after_concurrent_saves(self, config_service):
        import tomllib

        config_service.set("FLOATING_BUTTON_SIZE", 80)
        config_service.set("FLOATING_BUTTON_POS", [385, 285])

        async def many():
            await asyncio.gather(*(config_service.save() for _ in range(8)))

        asyncio.run(many())

        with open(config_service.config_path, "rb") as handle:
            written = tomllib.load(handle)
        assert written["FLOATING_BUTTON_SIZE"] == 80
        assert written["FLOATING_BUTTON_POS"] == [385, 285]

    def test_a_failed_write_leaves_the_previous_file_intact(self, config_service, monkeypatch):
        """A half-written settings file is worse than an out-of-date one."""
        import tomllib

        import tomli_w

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(tomli_w, "dump", explode)
        config_service.set("FLOATING_BUTTON_SIZE", 999)
        asyncio.run(config_service.save())

        with open(config_service.config_path, "rb") as handle:
            written = tomllib.load(handle)
        assert written["FLOATING_BUTTON_SIZE"] == 50


class TestAWriteRecordsTheSettingsAsTheyWereWhenItWasAsked:
    """The lock stops two writes overlapping, but it does not stop a change.

    Every caller changes the settings in memory first and only then asks for a
    write. While one write is running in its worker thread, another task on the
    event loop can change one value and then wait its turn. The running write
    is reading that same live set of settings, so it can record half of the
    later change -- the new size with the old position, exactly the mismatched
    pair that sending them together was meant to prevent.
    """

    @pytest.fixture
    def config_service(self, tmp_path):
        from config_service import ConfigService

        path = tmp_path / "config.toml"
        path.write_text(
            "FLOATING_BUTTON_SIZE = 50\nFLOATING_BUTTON_POS = [400, 300]\n",
            encoding="utf-8",
        )
        return ConfigService(str(path))

    def test_a_change_made_during_a_write_does_not_enter_that_write(
        self, config_service, monkeypatch
    ):
        import threading
        import time

        import tomli_w

        real_dump = tomli_w.dump
        recorded = []
        dump_started = threading.Event()

        def watching_dump(config, stream, *args, **kwargs):
            dump_started.set()
            # Hold the write open long enough for the other task to change the
            # settings underneath it, then record exactly what got written.
            time.sleep(0.05)
            recorded.append(dict(config))
            return real_dump(config, stream, *args, **kwargs)

        monkeypatch.setattr(tomli_w, "dump", watching_dump)

        async def change_settings_during_the_write():
            writer = asyncio.create_task(config_service.save())
            await asyncio.to_thread(dump_started.wait, 2.0)
            config_service.set("FLOATING_BUTTON_SIZE", 90)
            config_service.set("FLOATING_BUTTON_POS", [370, 270])
            await writer

        asyncio.run(change_settings_during_the_write())

        assert recorded[0]["FLOATING_BUTTON_SIZE"] == 50
        assert recorded[0]["FLOATING_BUTTON_POS"] == [400, 300]

    def test_a_change_made_during_a_write_survives_its_own_write(
        self, config_service, monkeypatch
    ):
        """The later change must still reach the file, just in its own write."""
        import threading
        import time
        import tomllib

        import tomli_w

        real_dump = tomli_w.dump
        dump_started = threading.Event()
        slowed = []

        def slow_first_dump(config, stream, *args, **kwargs):
            if not slowed:
                slowed.append(True)
                dump_started.set()
                time.sleep(0.05)
            return real_dump(config, stream, *args, **kwargs)

        monkeypatch.setattr(tomli_w, "dump", slow_first_dump)

        async def change_then_save_again():
            writer = asyncio.create_task(config_service.save())
            await asyncio.to_thread(dump_started.wait, 2.0)
            config_service.set("FLOATING_BUTTON_SIZE", 90)
            await asyncio.gather(writer, config_service.save())

        asyncio.run(change_then_save_again())

        with open(config_service.config_path, "rb") as handle:
            written = tomllib.load(handle)
        assert written["FLOATING_BUTTON_SIZE"] == 90

    def test_a_nested_section_changed_during_a_write_does_not_enter_it(
        self, config_service, monkeypatch
    ):
        """A shallow copy would still share the nested sections underneath."""
        import threading
        import time

        import tomli_w

        config_service.set("stt.mode", "remote")
        real_dump = tomli_w.dump
        recorded = []
        dump_started = threading.Event()

        def watching_dump(config, stream, *args, **kwargs):
            dump_started.set()
            time.sleep(0.05)
            recorded.append(config["stt"]["mode"])
            return real_dump(config, stream, *args, **kwargs)

        monkeypatch.setattr(tomli_w, "dump", watching_dump)

        async def change_nested_during_the_write():
            writer = asyncio.create_task(config_service.save())
            await asyncio.to_thread(dump_started.wait, 2.0)
            config_service.set("stt.mode", "in_process")
            await writer

        asyncio.run(change_nested_during_the_write())

        assert recorded[0] == "remote"


class TestAFailedWriteIsReportedToTheCaller:
    """A write that fails must not be reported to the user as a success.

    Every path that saves a setting does something afterwards on the strength
    of it: the button reports its new size, a provider switch logs success, and
    a speech-mode change restarts the program. If the write silently failed,
    each of those acts on settings that never reached the disk, and the next
    start quietly brings the old ones back.
    """

    @pytest.fixture
    def config_service(self, tmp_path):
        from config_service import ConfigService

        path = tmp_path / "config.toml"
        path.write_text("FLOATING_BUTTON_SIZE = 50\n", encoding="utf-8")
        return ConfigService(str(path))

    def test_a_successful_write_reports_success(self, config_service):
        assert asyncio.run(config_service.save()) is True

    def test_a_failed_write_reports_failure(self, config_service, monkeypatch):
        import tomli_w

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(tomli_w, "dump", explode)

        assert asyncio.run(config_service.save()) is False

    def test_a_failed_replace_reports_failure(self, config_service, monkeypatch):
        """The write can succeed and still fail to become the real file."""
        import os

        def explode(*args, **kwargs):
            raise OSError("in use by another process")

        monkeypatch.setattr(os, "replace", explode)

        assert asyncio.run(config_service.save()) is False

    def test_the_group_writer_reports_a_failed_write(self):
        from state_manager import StateManager

        manager = MagicMock()
        manager.config_service.save = AsyncMock(return_value=False)

        result = asyncio.run(
            StateManager.set_config_values(manager, {"FLOATING_BUTTON_SIZE": 80})
        )

        assert result is False

    def test_the_group_writer_reports_a_successful_write(self):
        from state_manager import StateManager

        manager = MagicMock()
        manager.config_service.save = AsyncMock(return_value=True)

        result = asyncio.run(
            StateManager.set_config_values(manager, {"FLOATING_BUTTON_SIZE": 80})
        )

        assert result is True

    def test_the_single_writer_reports_a_failed_write(self):
        from state_manager import StateManager

        manager = MagicMock()
        manager.config_service.save = AsyncMock(return_value=False)

        result = asyncio.run(
            StateManager.set_config_value(manager, "LOG_LEVEL", "DEBUG")
        )

        assert result is False
