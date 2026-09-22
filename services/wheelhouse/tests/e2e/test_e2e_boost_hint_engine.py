"""End-to-end: "boost" follows whether the running engine applies hints.

wh-boost-engine-qualification, criteria B1 to B4. The harness runs the real
SpeechProcessor, router, matcher, TextParser and UIActionHandler with
OS-level effects recorded. The engine's value is pushed the way production
pushes it: through SpeechProcessor.apply_hint_engine, or (B4) through
WebSocketManager._apply_capabilities and a stream boundary.

B1: with the value False the utterance "boost" is dictation. Five proofs:
the word is typed, no ctrl+c, no add_hint command to the provider, so no
hint is saved (the provider writes a hint only on that command), and no
notice.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness

_SELECTED = "Zwicky"


@pytest.fixture
async def harness(pattern_catalog):
    h = E2EPipelineHarness(catalog=pattern_catalog)
    # add_hint_to_stt reads the selection from the clipboard and sends it
    # through app.websocket_manager; both are stand-ins here.
    h.app.websocket_manager = MagicMock()
    h.app.websocket_manager.send_command_to_stt = AsyncMock()
    await h.start()
    with patch("pyperclip.paste", return_value=_SELECTED):
        yield h
    await h.stop()


async def _say_boost(harness):
    """Speak "boost" as the whole utterance, the way the help text tells
    the user to, and let the end marker finalize it."""
    await harness.send_word("boost", start_of_utterance=True)
    await harness.send_utterance_end_marker(
        utterance_id=harness._utterance_counter)
    await harness.wait_for_timeout(300)


def _typed(harness):
    return " ".join(harness.recording.text_deliveries).lower()


def _hint_commands(harness):
    return [
        c for c in harness.app.websocket_manager.send_command_to_stt
        .await_args_list
        if c.args and c.args[0] == "add_hint"
    ]


def _assert_boost_ran(harness):
    keys = harness.recording.get_keystroke_keys()
    assert ("ctrl", "c") in keys, f"expected ctrl+c, got {keys}"
    harness.app.websocket_manager.send_command_to_stt.assert_awaited_with(
        "add_hint", hint=_SELECTED)
    assert "boost" not in _typed(harness)


def _assert_boost_was_dictation(harness):
    # 1. The word is typed.
    assert "boost" in _typed(harness), (
        f"expected the word typed, got {harness.recording.text_deliveries}")
    # 2. No copy keystroke.
    keys = harness.recording.get_keystroke_keys()
    assert ("ctrl", "c") not in keys, f"ctrl+c was sent: {keys}"
    # 3. No add_hint command to the provider, and 4. so no hint is saved:
    # the provider writes hints.txt only on receipt of this command.
    assert _hint_commands(harness) == []
    harness.app.websocket_manager.send_command_to_stt.assert_not_awaited()
    # 5. No notice.
    assert harness.recording.notifications == []


class TestB1EngineDoesNotApplyHints:
    @pytest.mark.asyncio
    async def test_boost_is_dictation(self, harness):
        harness.processor.apply_hint_engine(False)
        await _say_boost(harness)
        _assert_boost_was_dictation(harness)

    @pytest.mark.asyncio
    async def test_boost_after_the_wake_word_is_dictation_too(self, harness):
        """The shape test_e2e_cmd_055_boost drives: wake word, then boost."""
        harness.processor.apply_hint_engine(False)
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("boost", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "c") not in keys, f"ctrl+c was sent: {keys}"
        harness.app.websocket_manager.send_command_to_stt.assert_not_awaited()
        assert harness.recording.notifications == []


class TestB2EngineAppliesHints:
    @pytest.mark.asyncio
    async def test_boost_runs(self, harness):
        harness.processor.apply_hint_engine(True)
        await _say_boost(harness)
        _assert_boost_ran(harness)


class TestB3ValueUnknown:
    @pytest.mark.asyncio
    async def test_boost_runs_with_no_frame_yet(self, harness):
        assert harness.processor.hint_engine is None
        await _say_boost(harness)
        _assert_boost_ran(harness)

    @pytest.mark.asyncio
    async def test_boost_runs_after_a_frame_without_the_key(self, harness):
        manager = _manager_feeding(harness)
        ws = _FakeWebsocket()
        await manager.add_client(ws)
        manager._apply_capabilities(
            ws, {"provider": "old_provider", "emits_eos": False})
        await _say_boost(harness)
        _assert_boost_ran(harness)


# ---------------------------------------------------------------------------
# B4: the value follows the running engine
# ---------------------------------------------------------------------------


class _FakeWebsocket:
    def __init__(self):
        self.sent = []
        self.remote_address = ("127.0.0.1", 0)

    async def send(self, payload):
        self.sent.append(payload)


class _HandlerStandIn:
    """The one SpeechHandler method the manager calls, forwarding to the
    harness's real processor exactly as SpeechHandler.apply_hint_engine
    does."""

    def __init__(self, processor):
        self._processor = processor

    def apply_hint_engine(self, value):
        self._processor.apply_hint_engine(value)


def _manager_feeding(harness):
    from integrations.websocket_manager import WebSocketManager

    manager = WebSocketManager(loop=asyncio.get_running_loop())
    manager.state_manager = MagicMock()
    manager.state_manager.config_service.get = MagicMock(return_value=False)
    manager.speech_handler = _HandlerStandIn(harness.processor)
    return manager


async def _switch_engine(manager, applies_hints):
    """A new provider connection (the stream boundary), then its frame."""
    ws = _FakeWebsocket()
    await manager.add_client(ws)
    manager._apply_capabilities(
        ws, {"provider": "next", "emits_eos": False,
             "applies_hints": applies_hints})


def _clear(harness):
    harness.recording.clear()
    harness.app.websocket_manager.send_command_to_stt.reset_mock()


class TestB4ValueFollowsTheEngine:
    @pytest.mark.asyncio
    async def test_false_then_true(self, harness):
        manager = _manager_feeding(harness)
        await _switch_engine(manager, False)
        await _say_boost(harness)
        _assert_boost_was_dictation(harness)

        _clear(harness)
        await _switch_engine(manager, True)
        await _say_boost(harness)
        _assert_boost_ran(harness)

    @pytest.mark.asyncio
    async def test_true_then_false(self, harness):
        manager = _manager_feeding(harness)
        await _switch_engine(manager, True)
        await _say_boost(harness)
        _assert_boost_ran(harness)

        _clear(harness)
        await _switch_engine(manager, False)
        await _say_boost(harness)
        _assert_boost_was_dictation(harness)

    @pytest.mark.asyncio
    async def test_the_boundary_alone_restores_the_command(self, harness):
        """After a False engine leaves, the next connection is unknown
        until it declares, and unknown keeps the command."""
        manager = _manager_feeding(harness)
        await _switch_engine(manager, False)
        await manager.add_client(_FakeWebsocket())
        await _say_boost(harness)
        _assert_boost_ran(harness)


class TestUserMadePattern:
    """A user-made pattern (not boost) whose actions include the hint
    action obeys the same rule."""

    @pytest.fixture
    async def user_harness(self, tmp_path):
        from pathlib import Path

        from speech.pattern_catalog import PatternCatalog

        user = tmp_path / "user_patterns.toml"
        user.write_text(
            "[[pattern]]\n"
            "pattern = '''^remember this word$'''\n"
            "actions = [\n"
            '    { function = "hk", params = ["ctrl", "c"], '
            "awaits_done = true },\n"
            '    { function = "add_hint_to_stt", awaits_done = true }\n'
            "]\n",
            encoding="utf-8",
        )
        shipped = (Path(__file__).parent.parent.parent
                   / "speech" / "config" / "patterns.toml")
        catalog = PatternCatalog(str(shipped), user_patterns_file=str(user))
        h = E2EPipelineHarness(catalog=catalog)
        h.app.websocket_manager = MagicMock()
        h.app.websocket_manager.send_command_to_stt = AsyncMock()
        await h.start()
        with patch("pyperclip.paste", return_value=_SELECTED):
            yield h
        await h.stop()

    async def _say(self, h):
        await h.send_utterance(["remember", "this", "word"])
        await h.send_utterance_end_marker(utterance_id=h._utterance_counter)
        await h.wait_for_timeout(1100)

    @pytest.mark.asyncio
    async def test_refused_when_false(self, user_harness):
        user_harness.processor.apply_hint_engine(False)
        await self._say(user_harness)
        assert "remember" in _typed(user_harness)
        assert ("ctrl", "c") not in user_harness.recording.get_keystroke_keys()
        user_harness.app.websocket_manager.send_command_to_stt \
            .assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_when_true(self, user_harness):
        user_harness.processor.apply_hint_engine(True)
        await self._say(user_harness)
        assert ("ctrl", "c") in user_harness.recording.get_keystroke_keys()
        user_harness.app.websocket_manager.send_command_to_stt \
            .assert_awaited_with("add_hint", hint=_SELECTED)


# ---------------------------------------------------------------------------
# The same rule on the two paths that do not go through the matcher:
# trailing-position commands, and replacements found in a remainder by
# SpeechProcessor._find_earliest_replacement.
# ---------------------------------------------------------------------------

_HINT_ACTIONS = (
    "actions = [\n"
    '    { function = "hk", params = ["ctrl", "c"], awaits_done = true },\n'
    '    { function = "add_hint_to_stt", awaits_done = true }\n'
    "]\n"
)


async def _user_harness(tmp_path, entry):
    from pathlib import Path

    from speech.pattern_catalog import PatternCatalog

    user = tmp_path / "user_patterns.toml"
    user.write_text("[[pattern]]\n" + entry + _HINT_ACTIONS, encoding="utf-8")
    shipped = (Path(__file__).parent.parent.parent
               / "speech" / "config" / "patterns.toml")
    catalog = PatternCatalog(str(shipped), user_patterns_file=str(user))
    h = E2EPipelineHarness(catalog=catalog)
    h.app.websocket_manager = MagicMock()
    h.app.websocket_manager.send_command_to_stt = AsyncMock()
    await h.start()
    return h


def _assert_refused(h, word):
    assert word in _typed(h), (
        f"expected {word!r} typed, got {h.recording.text_deliveries}")
    assert ("ctrl", "c") not in h.recording.get_keystroke_keys()
    h.app.websocket_manager.send_command_to_stt.assert_not_awaited()


def _assert_ran(h, word):
    assert ("ctrl", "c") in h.recording.get_keystroke_keys()
    h.app.websocket_manager.send_command_to_stt.assert_awaited_with(
        "add_hint", hint=_SELECTED)
    assert word not in _typed(h)


class TestUserMadeTrailingPattern:
    """A user-made ``position = "trailing"`` pattern with the hint action."""

    @pytest.fixture
    async def trailing_harness(self, tmp_path):
        h = await _user_harness(
            tmp_path, "pattern = 'zapword'\nposition = 'trailing'\n")
        assert "zapword" in h.catalog.trailing_commands
        with patch("pyperclip.paste", return_value=_SELECTED):
            yield h
        await h.stop()

    async def _say_alone(self, h):
        await h.send_word("zapword", start_of_utterance=True)
        await h.send_utterance_end_marker(h._utterance_counter)
        await h.wait_for_timeout(300)

    async def _say_after_words(self, h):
        """The end-of-utterance split path: head dictates, last word held."""
        await h.send_word("backspace", start_of_utterance=True)
        utterance_id = h._utterance_counter
        await h.send_word("hello", utterance_id=utterance_id)
        await h.send_word("zapword", utterance_id=utterance_id)
        await h.send_utterance_end_marker(utterance_id)
        await h.wait_for_timeout(300)

    @pytest.mark.asyncio
    async def test_alone_refused_when_false(self, trailing_harness):
        trailing_harness.processor.apply_hint_engine(False)
        await self._say_alone(trailing_harness)
        _assert_refused(trailing_harness, "zapword")

    @pytest.mark.asyncio
    async def test_after_words_refused_when_false(self, trailing_harness):
        trailing_harness.processor.apply_hint_engine(False)
        await self._say_after_words(trailing_harness)
        _assert_refused(trailing_harness, "zapword")

    @pytest.mark.asyncio
    async def test_false_is_not_held_as_a_candidate(self, trailing_harness):
        """Refused at hold time, not only at fire time: under an engine
        that does not apply hints the word is ordinary dictation and is
        never held back as a command candidate."""
        h = trailing_harness
        h.processor.apply_hint_engine(False)
        await h.send_word("zapword", start_of_utterance=True)
        assert h.processor._pending_trailing_word is None
        await h.send_utterance_end_marker(h._utterance_counter)
        await h.wait_for_timeout(300)
        _assert_refused(h, "zapword")

    @pytest.mark.asyncio
    async def test_false_between_hold_and_fire_refuses(self, trailing_harness):
        h = trailing_harness
        await h.send_word("zapword", start_of_utterance=True)
        assert h.processor._pending_trailing_word == "zapword"
        h.processor.apply_hint_engine(False)
        await h.send_utterance_end_marker(h._utterance_counter)
        await h.wait_for_timeout(300)
        _assert_refused(h, "zapword")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, True])
    async def test_alone_runs_otherwise(self, trailing_harness, value):
        trailing_harness.processor.apply_hint_engine(value)
        await self._say_alone(trailing_harness)
        _assert_ran(trailing_harness, "zapword")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, True])
    async def test_after_words_runs_otherwise(self, trailing_harness, value):
        trailing_harness.processor.apply_hint_engine(value)
        await self._say_after_words(trailing_harness)
        _assert_ran(trailing_harness, "zapword")


class TestUserMadeReplacementInARemainder:
    """A user-made replacement (unanchored) pattern with the hint action,
    reached through the remainder of "hello period zapreplace"."""

    @pytest.fixture
    async def replacement_harness(self, tmp_path):
        h = await _user_harness(tmp_path, "pattern = '''zapreplace'''\n")
        with patch("pyperclip.paste", return_value=_SELECTED):
            yield h
        await h.stop()

    async def _say(self, h):
        await h.send_word("hello", start_of_utterance=True)
        await h.send_word("period", delay_before_ms=50)
        await h.send_word("zapreplace", delay_before_ms=50)
        await h.send_utterance_end_marker(h._utterance_counter)
        await h.wait_for_timeout(800)

    def test_the_finder_skips_it_when_false(self, replacement_harness):
        processor = replacement_harness.processor
        processor.apply_hint_engine(False)
        assert processor._find_earliest_replacement("zapreplace") is None

    @pytest.mark.parametrize("value", [None, True])
    def test_the_finder_returns_it_otherwise(
            self, replacement_harness, value):
        processor = replacement_harness.processor
        processor.apply_hint_engine(value)
        winner = processor._find_earliest_replacement("zapreplace")
        assert winner is not None
        assert winner[1].get("requires_hint_engine") is True

    @pytest.mark.asyncio
    async def test_refused_when_false(self, replacement_harness):
        replacement_harness.processor.apply_hint_engine(False)
        await self._say(replacement_harness)
        _assert_refused(replacement_harness, "zapreplace")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, True])
    async def test_runs_otherwise(self, replacement_harness, value):
        replacement_harness.processor.apply_hint_engine(value)
        await self._say(replacement_harness)
        _assert_ran(replacement_harness, "zapreplace")
