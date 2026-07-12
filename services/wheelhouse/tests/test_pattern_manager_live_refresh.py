# tests/test_pattern_manager_live_refresh.py
"""Integration tests for the Logic-side glue that makes a Pattern Manager
change take effect without a restart.

Workflow finding (test-coverage): main.py
``_handle_pattern_manager_action._reload_and_refresh`` reloads both pattern
files, pushes any wake-word change onto the running recognizer via
``apply_hotword``, and refreshes the text parser's pattern list. Nothing tested
this integration point, so deleting the ``apply_hotword`` line (the whole point
of the live-refresh feature) left every existing test green. These tests drive
the real Logic handler end to end over temp files.

Review findings covered on top of that:

* wh-pattern-editor-r0.2 (main.py part): a failure BEFORE the per-action
  dispatch (speech handler unavailable, PatternManager construction) must
  still emit the ``{"success": False}`` envelope with action
  ``f"{action}_result"`` -- the GUI blocks on that envelope for every request.
* wh-pattern-editor-r0.8: ``catalog.reload()`` returning False (file write
  succeeded, live refresh did not) must leave the running hotword and parser
  patterns untouched and add a ``warning`` to the still-successful envelope.
"""
import tomllib
from unittest.mock import MagicMock

import pytest

from speech.pattern_catalog import PatternCatalog


SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
)


class _FakeTextParser:
    def __init__(self):
        self.patterns = []


class _FakeSpeechHandler:
    """Minimal stand-in exposing exactly what _reload_and_refresh touches."""

    def __init__(self, patterns_file, user_patterns_file):
        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file
        self.pattern_catalog = PatternCatalog(patterns_file, user_patterns_file)
        self.text_parser = _FakeTextParser()
        self.applied_hotwords = []

    def apply_hotword(self, hotword):
        # Records what the Logic glue pushed onto the running recognizer.
        self.applied_hotwords.append(hotword)


class _CapturingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


@pytest.fixture
def system_file(tmp_path):
    f = tmp_path / "patterns.toml"
    f.write_text(SYSTEM_CONTENT, encoding="utf-8")
    return str(f)


@pytest.fixture
def user_file(tmp_path):
    return str(tmp_path / "user_patterns.toml")


def _make_controller(system_file, user_file):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._handle_pattern_manager_action = (
        LogicController._handle_pattern_manager_action.__get__(controller)
    )
    handler = _FakeSpeechHandler(system_file, user_file)
    controller.service_manager = MagicMock()
    controller.service_manager.speech_handler = handler
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _CapturingQueue()
    return controller, handler


def _results(controller, action):
    return [
        m for m in controller.state_manager.state_to_gui_queue.items
        if m.get("action") == action
    ]


class TestLiveReloadRefresh:
    async def test_set_hotword_applies_live(self, system_file, user_file):
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_set_hotword", {"data": {"hotword": "jarvis"}},
        )
        # (a) the new wake word was written to the user file
        with open(user_file, "rb") as fh:
            assert tomllib.load(fh)["COMMAND_HOTWORD"] == "jarvis"
        # (b) the catalog reloaded to the new wake word
        assert handler.pattern_catalog.command_hotword == "jarvis"
        # (c) the running recognizer got the reloaded wake word -- this is the
        #     apply_hotword call whose removal the finding says goes undetected
        assert handler.applied_hotwords == ["jarvis"]
        # (d) the text parser's pattern list was refreshed from the catalog
        assert (
            handler.text_parser.patterns
            == handler.pattern_catalog.get_all_patterns()
        )
        # (e) the GUI received a success result with no stale-refresh warning
        results = _results(controller, "pm_set_hotword_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is True
        assert "warning" not in results[0]["data"]

    async def test_create_pattern_reloads_and_refreshes(self, system_file, user_file):
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "trigger": "deploy",
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "d"]},
                "requires_hotword": False,
            }},
        )
        # the new pattern is live: the reloaded catalog contains ^deploy$ and
        # the text parser was refreshed to the same list.
        patterns = handler.pattern_catalog.get_all_patterns()
        assert any(p["compiled_pattern"].pattern == "^deploy$" for p in patterns)
        assert handler.text_parser.patterns == patterns
        results = _results(controller, "pm_create_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is True
        assert "warning" not in results[0]["data"]


CREATE_DATA = {
    "trigger": "deploy",
    "pattern_type": "command",
    "action_type": "hotkey",
    "action_params": {"keys": ["ctrl", "d"]},
    "requires_hotword": False,
}

SAVED_WARNING = (
    "Saved, but the running patterns could not be refreshed. "
    "Restart WheelHouse or try saving again."
)
DELETED_WARNING = (
    "Deleted, but the running patterns could not be refreshed. "
    "Restart WheelHouse or try saving again."
)


class TestPreHandlerFailureEnvelope:
    """wh-pattern-editor-r0.2 (main.py part): failures before the per-action
    dispatch -- e.g. the speech handler is not up yet -- must still emit the
    failure envelope instead of raising into the listener loop's generic
    except, where the GUI would wait forever."""

    @pytest.mark.parametrize("action", ["pm_get_patterns", "pm_create_pattern"])
    async def test_missing_speech_handler_emits_failure_envelope(
        self, system_file, user_file, action,
    ):
        controller, handler = _make_controller(system_file, user_file)
        # Attribute access on None raises, exactly like a not-yet-started
        # speech handler would.
        controller.service_manager.speech_handler = None
        await controller._handle_pattern_manager_action(
            action, {"data": dict(CREATE_DATA)},
        )
        results = _results(controller, f"{action}_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is False
        assert results[0]["data"]["error"]


class TestReloadFailureWarning:
    """wh-pattern-editor-r0.8: catalog.reload() is non-raising and returns
    False on failure. The file write already succeeded, so the envelope stays
    success=True but gains a warning -- and the running state (hotword, parser
    patterns) must be left exactly as it was, not re-applied from the stale
    catalog."""

    async def test_create_reload_failure_warns_and_keeps_running_state(
        self, system_file, user_file,
    ):
        controller, handler = _make_controller(system_file, user_file)
        handler.pattern_catalog.reload = lambda: False
        patterns_before = handler.text_parser.patterns
        await controller._handle_pattern_manager_action(
            "pm_create_pattern", {"data": dict(CREATE_DATA)},
        )
        results = _results(controller, "pm_create_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["warning"] == SAVED_WARNING
        # Running state untouched: no stale hotword re-applied, and the
        # parser's pattern list object was not reassigned.
        assert handler.applied_hotwords == []
        assert handler.text_parser.patterns is patterns_before

    async def test_update_reload_failure_warns_and_keeps_running_state(
        self, system_file, user_file,
    ):
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern", {"data": dict(CREATE_DATA)},
        )
        pattern_id = _results(
            controller, "pm_create_result",
        )[0]["data"]["pattern_id"]
        handler.applied_hotwords.clear()
        handler.pattern_catalog.reload = lambda: False
        patterns_before = handler.text_parser.patterns
        await controller._handle_pattern_manager_action(
            "pm_update_pattern",
            {"data": {
                "pattern_id": pattern_id,
                "data": dict(CREATE_DATA, trigger="redeploy"),
            }},
        )
        results = _results(controller, "pm_update_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["warning"] == SAVED_WARNING
        assert handler.applied_hotwords == []
        assert handler.text_parser.patterns is patterns_before

    async def test_delete_reload_failure_warns_with_deleted_verb(
        self, system_file, user_file,
    ):
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern", {"data": dict(CREATE_DATA)},
        )
        pattern_id = _results(
            controller, "pm_create_result",
        )[0]["data"]["pattern_id"]
        handler.applied_hotwords.clear()
        handler.pattern_catalog.reload = lambda: False
        patterns_before = handler.text_parser.patterns
        await controller._handle_pattern_manager_action(
            "pm_delete_pattern", {"data": {"pattern_id": pattern_id}},
        )
        results = _results(controller, "pm_delete_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["warning"] == DELETED_WARNING
        assert handler.applied_hotwords == []
        assert handler.text_parser.patterns is patterns_before

    async def test_set_hotword_reload_failure_skips_apply_and_warns(
        self, system_file, user_file,
    ):
        controller, handler = _make_controller(system_file, user_file)
        handler.pattern_catalog.reload = lambda: False
        patterns_before = handler.text_parser.patterns
        await controller._handle_pattern_manager_action(
            "pm_set_hotword", {"data": {"hotword": "jarvis"}},
        )
        results = _results(controller, "pm_set_hotword_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["warning"] == SAVED_WARNING
        # The stale catalog's hotword must NOT be pushed onto the recognizer.
        assert handler.applied_hotwords == []
        assert handler.text_parser.patterns is patterns_before
