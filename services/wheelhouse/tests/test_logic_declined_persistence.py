"""Tests for LogicController's declined-tuple persistence path (wh-27gvv).

The No click on the three-strikes grant prompt persists the declined
identity triple to soft_allow_declined_tuples.toml. The Logic process
loads the file at startup so the No choice survives a restart.

Covered here:

  * ``add_declined`` writes the entry through the atomic writer and on
    success updates the in-memory ``_grant_prompt_no_suppressed`` set.
  * Disk failure leaves the in-memory set untouched and emits the
    ``declined_write_failed`` action onto the GUI state queue.
  * ``_handle_grant_prompt_no_clicked`` calls ``add_declined`` rather
    than mutating the in-memory set directly.
  * ``_load_declined_tuples`` seeds the in-memory set from the file.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _payload(**overrides) -> dict:
    base = {
        "action": "grant_prompt_no_clicked",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
    }
    base.update(overrides)
    return base


def _make_controller(declined_path: Path | None = None):
    """Build a MagicMock LogicController wired for declined persistence."""
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._handle_grant_prompt_no_clicked = (
        LogicController._handle_grant_prompt_no_clicked.__get__(controller)
    )
    controller.add_declined = (
        LogicController.add_declined.__get__(controller)
    )
    controller._resolve_declined_path = (
        LogicController._resolve_declined_path.__get__(controller)
    )
    controller._load_declined_tuples = (
        LogicController._load_declined_tuples.__get__(controller)
    )
    controller._grant_prompt_no_suppressed = set()
    controller.click_counter = MagicMock()
    controller.click_counter.reset_tuple = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller._declined_path = declined_path
    return controller


class TestAddDeclined:
    async def test_disk_success_returns_true_and_updates_in_memory_set(
        self, tmp_path,
    ):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        ok = await controller.add_declined(
            "zed.exe", "zed::Workspace", "Pane",
        )
        assert ok is True
        assert ("zed.exe", "zed::Workspace", "Pane") in (
            controller._grant_prompt_no_suppressed
        )

    async def test_disk_success_writes_the_file(self, tmp_path):
        import tomllib

        path = tmp_path / "soft_allow_declined_tuples.toml"
        controller = _make_controller(declined_path=path)
        await controller.add_declined(
            "zed.exe", "zed::Workspace", "Pane",
        )
        assert path.exists()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1
        entry = data["entries"][0]
        assert entry["process_name"] == "zed.exe"
        assert entry["class_name"] == "zed::Workspace"
        assert entry["control_type"] == "Pane"
        # added_at is a UTC ISO string ending with Z.
        assert entry["added_at"].endswith("Z")

    async def test_disk_failure_does_not_update_in_memory_set(
        self, tmp_path, monkeypatch,
    ):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        # Force the writer to return False (disk failure).
        import main as logic_main
        monkeypatch.setattr(
            logic_main, "append_declined_tuple",
            lambda new_tuple, path: False,
        )
        ok = await controller.add_declined(
            "zed.exe", "zed::Workspace", "Pane",
        )
        assert ok is False
        assert ("zed.exe", "zed::Workspace", "Pane") not in (
            controller._grant_prompt_no_suppressed
        )

    async def test_disk_failure_enqueues_declined_write_failed_event(
        self, tmp_path, monkeypatch,
    ):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        import main as logic_main
        monkeypatch.setattr(
            logic_main, "append_declined_tuple",
            lambda new_tuple, path: False,
        )
        await controller.add_declined(
            "zed.exe", "zed::Workspace", "Pane",
        )
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_called_once()
        msg = (
            controller.state_manager.state_to_gui_queue.put_nowait
            .call_args[0][0]
        )
        assert msg["action"] == "declined_write_failed"
        assert msg["process_name"] == "zed.exe"
        assert msg["class_name"] == "zed::Workspace"
        assert msg["control_type"] == "Pane"

    async def test_writer_raises_surfaces_declined_write_failed(
        self, tmp_path, monkeypatch,
    ):
        """wh-27gvv.1.1 (codex review): a non-OSError exception from
        the writer must still surface the "couldn't save" notice. The
        writer normally catches OSError and returns False, but a
        future writer change, a path override mistake, or a
        serialisation failure could raise something else. Without
        the inner try/except in add_declined the failure feedback
        never reaches the GUI: the handler's outer wrapper logs but
        does not enqueue declined_write_failed.
        """
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        import main as logic_main

        def raise_runtime(new_tuple, path):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            logic_main, "append_declined_tuple", raise_runtime,
        )
        ok = await controller.add_declined(
            "zed.exe", "zed::Workspace", "Pane",
        )
        assert ok is False
        assert ("zed.exe", "zed::Workspace", "Pane") not in (
            controller._grant_prompt_no_suppressed
        )
        controller.state_manager.state_to_gui_queue.put_nowait \
            .assert_called_once()
        msg = (
            controller.state_manager.state_to_gui_queue.put_nowait
            .call_args[0][0]
        )
        assert msg["action"] == "declined_write_failed"
        assert msg["process_name"] == "zed.exe"


class TestNoHandlerCallsAddDeclined:
    async def test_no_click_persists_declined_entry(self, tmp_path):
        import tomllib

        path = tmp_path / "soft_allow_declined_tuples.toml"
        controller = _make_controller(declined_path=path)
        await controller._handle_grant_prompt_no_clicked(_payload())
        assert path.exists()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1

    async def test_no_click_on_disk_failure_leaves_in_memory_set_alone(
        self, tmp_path, monkeypatch, caplog,
    ):
        controller = _make_controller(
            declined_path=tmp_path / "soft_allow_declined_tuples.toml",
        )
        import main as logic_main
        monkeypatch.setattr(
            logic_main, "append_declined_tuple",
            lambda new_tuple, path: False,
        )
        with caplog.at_level(logging.WARNING):
            await controller._handle_grant_prompt_no_clicked(_payload())
        assert ("zed.exe", "zed::Workspace", "Pane") not in (
            controller._grant_prompt_no_suppressed
        )


class TestLoadDeclinedTuples:
    def test_loads_entries_into_suppression_set(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        path.write_text(
            "[[entries]]\n"
            "process_name = 'zed.exe'\n"
            "class_name = 'zed::Workspace'\n"
            "control_type = 'Pane'\n"
            "added_at = '2026-05-13T15:00:00Z'\n"
            "\n"
            "[[entries]]\n"
            "process_name = 'explorer.exe'\n"
            "class_name = 'Button'\n"
            "control_type = 'ButtonControl'\n"
            "added_at = '2026-05-13T16:00:00Z'\n",
            encoding="utf-8",
        )
        controller = _make_controller(declined_path=path)
        controller._load_declined_tuples()
        assert controller._grant_prompt_no_suppressed == {
            ("zed.exe", "zed::Workspace", "Pane"),
            ("explorer.exe", "Button", "ButtonControl"),
        }

    def test_missing_file_leaves_set_empty(self, tmp_path):
        path = tmp_path / "soft_allow_declined_tuples.toml"
        controller = _make_controller(declined_path=path)
        controller._load_declined_tuples()
        assert controller._grant_prompt_no_suppressed == set()


class TestPersistsAcrossLifetimes:
    """End-to-end: a No click in lifetime A is honoured in lifetime B
    after a startup load. This is the property the bead is built to
    protect.

    The assertion couples the disk round-trip to the forwarder
    suppression effect: a regression that loads the file correctly
    but no longer consults the in-memory set in
    ``_on_retry_threshold_reached`` would pass a set-membership
    assertion while silently regressing the user-visible behaviour.
    Publishing a ``RetryThresholdReached`` in lifetime B and
    asserting nothing reaches the GUI queue nails the end-to-end
    property in one test.
    """

    async def test_decline_then_restart_suppresses_threshold_forward(
        self, tmp_path,
    ):
        from main import LogicController

        path = tmp_path / "soft_allow_declined_tuples.toml"

        # Lifetime A: user clicks No, the entry is persisted, and
        # the in-memory set is updated.
        controller_a = _make_controller(declined_path=path)
        await controller_a._handle_grant_prompt_no_clicked(_payload())
        assert ("zed.exe", "zed::Workspace", "Pane") in (
            controller_a._grant_prompt_no_suppressed
        )

        # Lifetime B: a fresh controller loads from disk and the
        # forwarder consults the set before publishing to the GUI.
        controller_b = _make_controller(declined_path=path)
        controller_b._load_declined_tuples()
        assert ("zed.exe", "zed::Workspace", "Pane") in (
            controller_b._grant_prompt_no_suppressed
        )

        # Bind the real forwarder and publish a threshold event for
        # the same control. The forwarder must drop the publish.
        controller_b._on_retry_threshold_reached = (
            LogicController._on_retry_threshold_reached.__get__(controller_b)
        )
        from services.wheelhouse.events import RetryThresholdReached

        await controller_b._on_retry_threshold_reached(RetryThresholdReached(
            process_name="zed.exe",
            class_name="zed::Workspace",
            control_type="Pane",
            app_friendly_name="Zed",
            count=3,
        ))
        controller_b.state_manager.state_to_gui_queue.put_nowait \
            .assert_not_called()
