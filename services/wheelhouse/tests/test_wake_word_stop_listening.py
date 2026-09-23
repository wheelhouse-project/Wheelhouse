"""The wake word resumes listening after any pause; "stop listening" pauses it.

wh-wake-word-stop-listening. David asked on 2026-09-22 for two things: the
wake word "computer" turns listening on whatever switched it off, not only
the idle pause, and the phrase "stop listening" switches listening off.

Boss ruling R1: the wake word does what pressing the floating button does
while listening is off -- it switches listening on and clears every pause.
Ruling R3: "stop listening" runs the same disabling path the button uses,
with transcription status reason "manual", and does nothing while listening
is already off.

Covers:
- a WakeWordDetectedEvent after each way listening goes off: the user's
  toggle, startup with SPEECH_ENABLED_ON_STARTUP false, the idle pause, sound
  from this computer, Sonos playback, and an interaction-mode switch
- the "stop listening" pattern, its action function, and its catalog entry
- saying "stop listening" while listening is already off
- the toggle, the wake word, and the new action share one enabling method
  and one disabling method (acceptance A6)
"""

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from events import (
    SystemIdleStateChangedEvent,
    WakeWordDetectedEvent,
)
from state_manager import StateManager


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
    """A StateManager whose loop never runs, so nothing it schedules fires."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    mgr._speech_enabled = True
    mgr.speech_notifier._send_notification = Mock()
    yield mgr
    loop.close()


def _off_by_toggle(sm):
    sm.toggle_speech_enabled_state()


async def _off_by_idle(sm):
    await sm._handle_idle_state_changed(
        SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
    )


def _off_by_sound(sm):
    sm.set_speech_suppressed_by_audio(True)


def _off_by_sonos(sm):
    sm._set_speech_suppressed_by_sonos(True)


def _off_by_switch_to_push_to_talk(sm):
    sm.set_speech_interaction_mode("push_to_talk")


def _off_by_switch_to_toggle(sm):
    sm.set_speech_interaction_mode("toggle")


_WAYS_LISTENING_GOES_OFF = [
    pytest.param(_off_by_toggle, id="user-toggle"),
    pytest.param(_off_by_idle, id="idle"),
    pytest.param(_off_by_sound, id="sound"),
    pytest.param(_off_by_sonos, id="sonos"),
    pytest.param(_off_by_switch_to_toggle, id="mode-switch-toggle"),
]
# A switch INTO push-to-talk mode is not in this list: in push-to-talk mode
# the wake word ends only the idle pause (boss ruling option 2, 11:14
# 2026-09-22). TestWakeWordInPushToTalkMode covers that mode.


async def _switch_off(sm, how):
    result = how(sm)
    if asyncio.iscoroutine(result):
        await result
    assert sm.speech_enabled is False


async def _wake_word(sm, keyword="computer"):
    await sm._handle_wake_word_detected(WakeWordDetectedEvent(keyword=keyword))


class TestWakeWordAfterAnyPause:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", _WAYS_LISTENING_GOES_OFF)
    async def test_the_wake_word_turns_listening_on(self, sm, how):
        await _switch_off(sm, how)

        await _wake_word(sm)

        assert sm.speech_enabled is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", _WAYS_LISTENING_GOES_OFF)
    async def test_the_wake_word_clears_every_pause_flag(self, sm, how):
        await _switch_off(sm, how)

        await _wake_word(sm)

        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False
        assert sm._speech_suppressed_by_sonos is False
        assert sm._speech_suppressed_by_idle is False
        assert sm._speech_enabled_before_idle is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", _WAYS_LISTENING_GOES_OFF)
    async def test_the_engine_is_told_listening_is_on(
        self, sm, how, mock_websocket_manager, mock_gui_queue
    ):
        await _switch_off(sm, how)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _wake_word(sm)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(True)
        mock_websocket_manager.broadcast.assert_called_once()
        mock_gui_queue.put_nowait.assert_called()

    @pytest.mark.asyncio
    async def test_the_wake_word_while_listening_changes_nothing(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        assert sm.speech_enabled is True

        await _wake_word(sm)

        assert sm.speech_enabled is True
        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()


class TestWakeWordAfterStartingWithListeningOff:
    """A start with SPEECH_ENABLED_ON_STARTUP false, through the real
    constructor (StateManager.__init__ reads the key into _speech_enabled,
    and attaching the websocket manager tells the engine reason "startup").
    """

    @pytest.mark.asyncio
    async def test_the_wake_word_turns_listening_on_after_a_start_with_it_off(
        self, mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager
    ):
        mock_config.set("SPEECH_ENABLED_ON_STARTUP", False)
        mock_config.set("speech.interaction_mode", "toggle")
        loop = asyncio.new_event_loop()
        loop.create_task = Mock()
        try:
            mgr = StateManager(
                config_service=mock_config,
                event_bus=mock_event_bus,
                loop=loop,
                state_to_gui_queue=mock_gui_queue,
                websocket_manager=None,
            )
            # main.py attaches the manager after construction, through the
            # setter that reports the startup state to the engine.
            mgr.websocket_manager = mock_websocket_manager
            mgr.speech_notifier._send_notification = Mock()
            assert mgr._speech_enabled is False
            assert mgr.speech_enabled is False
            mock_websocket_manager.set_transcription_status.assert_called_once_with(
                False, reason="startup")
            mock_websocket_manager.reset_mock()

            await _wake_word(mgr)

            assert mgr.speech_enabled is True
            assert mgr._speech_enabled is True
            assert mgr._speech_suppressed_by_audio is False
            assert mgr._speech_suppressed_by_sonos is False
            assert mgr._speech_suppressed_by_idle is False
            assert mgr._speech_enabled_before_idle is None
            assert mgr._speech_interaction_mode == "toggle"
            mock_websocket_manager.set_transcription_status.assert_called_once_with(True)
        finally:
            loop.close()


def _pause_state(sm):
    return {
        "mode": sm._speech_interaction_mode,
        "user_enabled": sm._speech_enabled,
        "audio": sm._speech_suppressed_by_audio,
        "sonos": sm._speech_suppressed_by_sonos,
        "idle": sm._speech_suppressed_by_idle,
        "before_idle": sm._speech_enabled_before_idle,
        "speech_enabled": sm.speech_enabled,
    }


class TestWakeWordInPushToTalkMode:
    """Boss ruling option 2 (11:14 2026-09-22) on the R1 vs R4 block.

    In push-to-talk mode the wake word keeps the behaviour it had before
    this bead: it ends only the idle pause, stays in push-to-talk mode, and
    changes nothing during any other pause. A voice command that silently
    changed the interaction mode would surprise a push-to-talk user.
    """

    @pytest.fixture
    def ptt(self, sm):
        _off_by_switch_to_push_to_talk(sm)
        assert sm._speech_interaction_mode == "push_to_talk"
        assert sm.speech_enabled is False
        return sm

    @pytest.mark.asyncio
    async def test_the_wake_word_ends_the_idle_pause_and_stays_in_push_to_talk(
        self, ptt, mock_websocket_manager
    ):
        await _off_by_idle(ptt)
        assert ptt._speech_suppressed_by_idle is True
        mock_websocket_manager.reset_mock()

        await _wake_word(ptt)

        assert ptt._speech_suppressed_by_idle is False
        assert ptt._speech_enabled_before_idle is None
        assert ptt._speech_interaction_mode == "push_to_talk"
        assert ptt._speech_enabled is False
        mock_websocket_manager.set_transcription_status.assert_called_once_with(False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", [
        pytest.param(_off_by_sound, id="sound"),
        pytest.param(_off_by_sonos, id="sonos"),
        pytest.param(lambda sm: None, id="mode-switch-only"),
    ])
    async def test_the_wake_word_changes_nothing_during_another_pause(
        self, ptt, how, mock_websocket_manager, mock_gui_queue, caplog
    ):
        how(ptt)
        before = _pause_state(ptt)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        with caplog.at_level(logging.INFO, logger="state_manager"):
            await _wake_word(ptt)

        assert _pause_state(ptt) == before
        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()
        lines = [r for r in caplog.records
                 if r.name == "state_manager" and "push-to-talk" in r.getMessage()]
        assert len(lines) == 1

    def test_the_floating_button_still_leaves_push_to_talk(self, ptt):
        # Unchanged behaviour: the button's enabling branch switches to
        # toggle mode before it switches listening on. Passes before and
        # after the ruling's edit; it guards against the edit being too broad.
        ptt.toggle_speech_enabled_state()

        assert ptt._speech_interaction_mode == "toggle"
        assert ptt.speech_enabled is True


class TestSharedEnableAndDisable:
    """Acceptance A6: one enabling method and one disabling method.

    The toggle, the wake word, and the "stop listening" action reach the
    state through these two, so the three cannot drift apart.
    """

    @pytest.mark.asyncio
    async def test_the_wake_word_enables_through_the_shared_method(self, sm):
        _off_by_sound(sm)
        sm.enable_speech_clearing_pauses = Mock()

        await _wake_word(sm)

        sm.enable_speech_clearing_pauses.assert_called_once()

    def test_the_toggle_enables_through_the_shared_method(self, sm):
        sm._speech_enabled = False
        sm.enable_speech_clearing_pauses = Mock()

        sm.toggle_speech_enabled_state()

        sm.enable_speech_clearing_pauses.assert_called_once()

    def test_the_toggle_disables_through_the_shared_method(self, sm):
        sm.disable_speech_by_user = Mock()

        sm.toggle_speech_enabled_state()

        sm.disable_speech_by_user.assert_called_once()


# -----------------------------------------------------------------------
# "stop listening"
# -----------------------------------------------------------------------

@pytest.fixture
def actions(sm):
    """ActionFunctions wired to the real StateManager above."""
    from speech.actions import ActionFunctions

    handler = MagicMock()
    handler.logic_controller = MagicMock()
    handler.logic_controller.state_manager = sm
    return ActionFunctions(handler)


class TestStopListeningAction:

    def test_the_function_is_registered(self, actions):
        assert "stop_listening" in actions.get_functions()

    def test_it_turns_listening_off(self, sm, actions):
        assert sm.speech_enabled is True

        actions.get_functions()["stop_listening"]()

        assert sm.speech_enabled is False
        assert sm._speech_enabled is False

    def test_the_engine_is_told_the_reason_is_manual(
        self, sm, actions, mock_websocket_manager
    ):
        actions.stop_listening()

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="manual"
        )
        mock_websocket_manager.broadcast.assert_called_once()

    def test_it_disables_through_the_shared_method(self, sm, actions):
        sm.disable_speech_by_user = Mock()

        actions.stop_listening()

        sm.disable_speech_by_user.assert_called_once()

    def test_the_user_is_told_listening_is_off(self, sm, actions):
        actions.stop_listening()

        sm.speech_notifier._send_notification.assert_called_once_with(
            "Wheelhouse", "Speech is off. You switched it off."
        )

    def test_it_returns_none(self, actions):
        assert actions.stop_listening() is None

    def test_while_listening_is_off_it_changes_nothing_and_logs_once(
        self, sm, actions, mock_websocket_manager, mock_gui_queue, caplog
    ):
        _off_by_sound(sm)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        with caplog.at_level(logging.INFO, logger="speech.actions"):
            actions.stop_listening()

        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is True
        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()
        sm.speech_notifier._send_notification.assert_not_called()
        lines = [r for r in caplog.records
                 if r.name == "speech.actions" and "already off" in r.getMessage()]
        assert len(lines) == 1

    def test_without_a_state_manager_it_logs_a_warning(self, actions, caplog):
        actions.speech_handler.logic_controller.state_manager = None

        with caplog.at_level(logging.WARNING, logger="speech.actions"):
            actions.stop_listening()

        assert "state_manager not available" in caplog.text

    def test_the_wake_word_turns_listening_back_on(self, sm, actions):
        actions.stop_listening()

        asyncio.run(_wake_word(sm))

        assert sm.speech_enabled is True


class TestStopListeningPattern:

    @pytest.fixture
    def pattern(self):
        import tomllib
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "speech" / "config" / "patterns.toml"
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        found = [p for p in data["pattern"] if p.get("doc_id") == "stop-listening"]
        assert len(found) == 1
        return found[0]

    def test_the_pattern_is_the_whole_phrase(self, pattern):
        assert pattern["pattern"] == "^stop listening$"

    def test_the_pattern_is_whole_utterance_only(self, pattern):
        assert pattern["whole_utterance_only"] is True

    def test_the_pattern_calls_stop_listening(self, pattern):
        assert pattern["actions"] == [{"function": "stop_listening"}]

    def test_the_shipped_matcher_routes_the_phrase_to_stop_listening(self):
        from pathlib import Path

        from speech.pattern_catalog import PatternCatalog
        from speech.pattern_matcher import PatternMatcher

        path = Path(__file__).resolve().parents[1] / "speech" / "config" / "patterns.toml"
        matcher = PatternMatcher(PatternCatalog(str(path)))

        result = matcher.match_complete(
            "stop listening",
            pattern_type="command",
            hotword_active=False,
            first_word="stop",
        )

        assert result is not None and result.matched
        assert [a.get("function") for a in result.actions] == ["stop_listening"]

    def test_the_catalog_describes_the_function(self):
        from speech import action_catalog

        entries = [e for e in action_catalog.ACTION_CATALOG
                   if e["name"] == "stop_listening"]
        assert len(entries) == 1


class TestPushToTalkRecoveryGuidance:
    """wh-wake-word-stop-listening.1.2: the help states the push-to-talk limit.

    Codex round 1 found that four shipped surfaces told every user to say
    the wake word after "stop listening". In push-to-talk mode that cannot
    work: stop_listening calls disable_speech_by_user whatever the
    interaction mode is (speech/actions.py), the wake word there runs
    StateManager._wake_word_ends_idle_pause_only and returns when no idle
    pause is active, and ptt_stop sends reason "ptt", which
    WAKE_WORD_ARMED_REASONS arms for no mode. The way back is the next hold
    of the floating button, so each surface has to say so.
    """

    HELPDOC = Path(__file__).resolve().parents[1] / "knowledge" / "helpdoc"

    @pytest.fixture
    def helpdoc(self):
        """The help-document sources, which the public export leaves out."""
        if not self.HELPDOC.is_dir():
            pytest.skip("development-only help-document sources are absent from this checkout")
        return self.HELPDOC

    @staticmethod
    def _entry(text, marker):
        """The [[...]] block of a descriptions sidecar that holds `marker`."""
        blocks = text.split("\n[[")
        found = [b for b in blocks if marker in b]
        assert len(found) == 1, f"expected one block with {marker!r}, got {len(found)}"
        return found[0]

    def test_the_catalog_entry_names_the_push_to_talk_route(self):
        from speech import action_catalog

        entries = [e for e in action_catalog.ACTION_CATALOG
                   if e["name"] == "stop_listening"]
        assert len(entries) == 1
        summary = entries[0]["summary"]

        assert "toggle mode" in summary, summary
        assert "push-to-talk" in summary, summary
        assert "floating button" in summary, summary

    def test_the_command_description_names_the_push_to_talk_route(self, helpdoc):
        text = (helpdoc / "command_descriptions.toml").read_text(encoding="utf-8")
        entry = self._entry(text, 'ids = ["stop-listening"]')

        assert "In toggle mode" in entry, entry
        assert "In push-to-talk mode the wake word ends only the idle pause" in entry, entry
        assert "next hold of the floating button" in entry, entry

    def test_the_voice_commands_section_names_the_push_to_talk_route(self, helpdoc):
        body = (helpdoc / "sections" / "070-voice-commands.md").read_text(
            encoding="utf-8"
        )
        passages = [
            p for p in body.split("\n\n")
            if '"stop listening", said as the whole utterance' in p
        ]
        assert len(passages) == 1, len(passages)
        passage = passages[0]

        # The unqualified promise Codex found: it told a push-to-talk user
        # to say the wake word after a switch-off the wake word cannot undo.
        # Every statement of that route now carries the toggle-mode clause.
        route = 'the wake word ("computer") is the voice route back on'
        assert passage.count(route) == passage.count("In toggle mode " + route), passage
        assert "In push-to-talk mode the wake word ends only the idle pause" in passage, passage
        assert "next hold of the floating button" in passage, passage

    def test_the_wake_word_mode_setting_names_the_push_to_talk_limit(self, helpdoc):
        text = (helpdoc / "config_descriptions.toml").read_text(encoding="utf-8")
        entry = self._entry(text, 'description = "What the wake word is used for')

        assert "in toggle mode" in entry, entry
        assert "in push-to-talk mode" in entry, entry
        assert "idle pause" in entry, entry
