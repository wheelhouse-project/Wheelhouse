"""A hint command matches only when the running engine applies hints.

wh-boost-engine-qualification. David's design choice (2026-09-19): when the
active speech engine does not apply a saved hint, the word "boost" is typed
as dictation. The engine reports the value in its capabilities frame
(``applies_hints``); the Logic process keeps it tri-state:

* True  -- the engine applies hints; the command works as before.
* False -- the engine does not; the pattern is refused and the words
  become dictation.
* None  -- unknown (no frame yet, a provider build that does not send the
  key, or a stream boundary); the command works as before.

Which patterns: every pattern whose actions include ``add_hint_to_stt``
(ruling 1 of 2026-09-21: derived from the actions, no patterns.toml key).

The end-to-end proofs (typed word, no copy keystroke, no add_hint command,
no notice) are in tests/e2e/test_e2e_boost_hint_engine.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.pattern_catalog import PatternCatalog  # noqa: E402
from speech.pattern_matcher import PatternMatcher  # noqa: E402
from speech.router import SpeechRouter  # noqa: E402

WHEELHOUSE_ROOT = Path(__file__).parent.parent
SHIPPED_PATTERNS = WHEELHOUSE_ROOT / "speech" / "config" / "patterns.toml"

_SYSTEM_TOML = '''
COMMAND_HOTWORD = "x-ray"

[[pattern]]
pattern = \'\'\'^boost$\'\'\'
whole_utterance_only = true
actions = [
    { function = "skip_clipboard_restore", awaits_done = true },
    { function = "hk", params = ["ctrl", "c"], awaits_done = true },
    { function = "add_hint_to_stt", awaits_done = true }
]

[[pattern]]
pattern = \'\'\'^copy that$\'\'\'
actions = [
    { function = "hk", params = ["ctrl", "c"], awaits_done = true }
]
'''

# User-made patterns: not "boost", but their actions include the hint
# action, so the same rule applies to them. The second is greedy, which
# is what reaches the router's greedy-prefix probe.
_USER_TOML = '''
[[pattern]]
pattern = \'\'\'^remember this word$\'\'\'
actions = [
    { function = "hk", params = ["ctrl", "c"], awaits_done = true },
    { function = "add_hint_to_stt", awaits_done = true }
]

[[pattern]]
pattern = \'\'\'^learn spelling(.*)$\'\'\'
actions = [
    { function = "add_hint_to_stt", awaits_done = true }
]
'''


@pytest.fixture
def catalog(tmp_path):
    system = tmp_path / "patterns.toml"
    system.write_text(_SYSTEM_TOML, encoding="utf-8")
    user = tmp_path / "user_patterns.toml"
    user.write_text(_USER_TOML, encoding="utf-8")
    return PatternCatalog(str(system), user_patterns_file=str(user))


def _entry(catalog, raw_pattern):
    for p in catalog.get_all_patterns():
        if p["raw_pattern"] == raw_pattern:
            return p
    raise AssertionError(f"{raw_pattern!r} not in the catalog")


def _indexed_data(catalog, first_word, raw_fragment):
    for compiled, _ptype, data in catalog.get_matching_patterns(first_word):
        if raw_fragment in compiled.pattern:
            return data
    raise AssertionError(f"no indexed pattern for {first_word!r}")


# ---------------------------------------------------------------------------
# Catalog: the flag is derived from the actions, in both stored copies
# ---------------------------------------------------------------------------


class TestCatalogDerivesTheFlag:
    def test_boost_needs_a_hint_engine_in_the_built_list(self, catalog):
        assert _entry(catalog, "^boost$")["requires_hint_engine"] is True

    def test_boost_needs_a_hint_engine_in_the_first_word_index(self, catalog):
        assert _indexed_data(catalog, "boost", "boost").get(
            "requires_hint_engine") is True

    def test_a_pattern_without_the_hint_action_does_not(self, catalog):
        assert _entry(catalog, "^copy that$")["requires_hint_engine"] is False
        assert not _indexed_data(catalog, "copy", "copy").get(
            "requires_hint_engine", False)

    def test_a_user_made_pattern_with_the_hint_action_does(self, catalog):
        assert _entry(
            catalog, "^remember this word$")["requires_hint_engine"] is True
        assert _indexed_data(catalog, "remember", "remember").get(
            "requires_hint_engine") is True

    def test_the_shipped_boost_entry_carries_the_flag(self):
        shipped = PatternCatalog(str(SHIPPED_PATTERNS), user_patterns_file="")
        assert _entry(shipped, "^boost$")["requires_hint_engine"] is True


# ---------------------------------------------------------------------------
# Matcher: the refusal places
# ---------------------------------------------------------------------------


class TestMatchComplete:
    @pytest.mark.parametrize("value", [True, None])
    def test_matches_when_the_engine_applies_or_is_unknown(
            self, catalog, value):
        matcher = PatternMatcher(catalog)
        result = matcher.match_complete(
            "boost", pattern_type="command", hint_engine=value)
        assert result is not None and result.matched

    def test_refused_when_the_engine_does_not_apply_hints(self, catalog):
        matcher = PatternMatcher(catalog)
        assert matcher.match_complete(
            "boost", pattern_type="command", hint_engine=False) is None

    def test_default_is_unknown(self, catalog):
        """A caller that never passes the value keeps today's behaviour."""
        matcher = PatternMatcher(catalog)
        assert matcher.match_complete("boost", pattern_type="command")

    def test_other_patterns_are_not_refused(self, catalog):
        matcher = PatternMatcher(catalog)
        assert matcher.match_complete(
            "copy that", pattern_type="command", hint_engine=False)

    def test_routing_entry_points_forward_the_value(self, catalog):
        matcher = PatternMatcher(catalog)
        assert matcher.match_for_routing(
            ["boost"], "command", False, hint_engine=False) is None
        assert matcher.match_for_routing(
            ["boost"], "command", False, hint_engine=True)
        assert matcher.is_pattern_complete(
            ["boost"], "command", False, hint_engine=False) is False
        assert matcher.is_pattern_complete(
            ["boost"], "command", False, hint_engine=True) is True


class TestMatchSinglePattern:
    """The execution walk (TextParser.parse_and_execute) re-matches with
    match_single_pattern; it must apply the same gate the router applied."""

    @pytest.mark.parametrize("value", [True, None])
    def test_matches_when_the_engine_applies_or_is_unknown(
            self, catalog, value):
        matcher = PatternMatcher(catalog)
        entry = _entry(catalog, "^boost$")
        assert matcher.match_single_pattern(
            "boost", entry, authorized_command=True, hint_engine=value)

    def test_refused_when_the_engine_does_not_apply_hints(self, catalog):
        matcher = PatternMatcher(catalog)
        entry = _entry(catalog, "^boost$")
        assert matcher.match_single_pattern(
            "boost", entry, authorized_command=True,
            hint_engine=False) is None


class TestCanContinue:
    """A refused pattern cannot be a reason to keep buffering, so the
    router finalizes the words as dictation at once."""

    def test_refused_prefix_cannot_continue(self, catalog):
        matcher = PatternMatcher(catalog)
        assert matcher.can_continue(
            ["remember"], "command", False, hint_engine=False) is False
        assert matcher.cannot_match(
            ["remember"], "command", False, hint_engine=False) is True

    @pytest.mark.parametrize("value", [True, None])
    def test_prefix_continues_otherwise(self, catalog, value):
        matcher = PatternMatcher(catalog)
        assert matcher.can_continue(
            ["remember"], "command", False, hint_engine=value) is True


# ---------------------------------------------------------------------------
# Router: the greedy-prefix probe and the pushed value
# ---------------------------------------------------------------------------


class TestRouter:
    def test_starts_unknown(self, catalog):
        assert SpeechRouter(catalog, "x-ray").hint_engine is None

    def test_greedy_prefix_refused_when_false(self, catalog):
        router = SpeechRouter(catalog, "x-ray")
        router.hint_engine = False
        assert router._buffer_is_greedy_prefix(
            ["learn"], ("command",), False) is False

    @pytest.mark.parametrize("value", [True, None])
    def test_greedy_prefix_kept_otherwise(self, catalog, value):
        router = SpeechRouter(catalog, "x-ray")
        router.hint_engine = value
        assert router._buffer_is_greedy_prefix(
            ["learn"], ("command",), False) is True

    def test_greedy_timer_refused_for_a_whole_greedy_match_when_false(
            self, catalog):
        """The fullmatch half of _greedy_timeout_for_buffer: "learn
        spelling" fullmatches the greedy hint pattern, and a refused
        pattern must not attract the long greedy timer."""
        router = SpeechRouter(catalog, "x-ray")
        router.hint_engine = False
        assert router._greedy_timeout_for_buffer(
            ["learn", "spelling"], ("command",), False, 5000) is None
        router.hint_engine = True
        assert router._greedy_timeout_for_buffer(
            ["learn", "spelling"], ("command",), False, 5000) == 5000

    def test_a_completed_hint_command_is_not_executed_when_false(
            self, catalog):
        """The complete-pattern step of decide(): the last word of a hint
        command must not be routed to EXECUTE under an engine that does
        not apply hints. The execution walk refuses too, but the router
        decides first and must agree with it."""
        from speech.router import Action, ProcessingMode
        from speech.word_event import WordEvent

        router = SpeechRouter(catalog, "x-ray")
        event = WordEvent(
            word="word", start_of_utterance=False, end_of_utterance=False)
        router.hint_engine = False
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["remember", "this"])
        assert decision.action != Action.EXECUTE
        router.hint_engine = True
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["remember", "this"])
        assert decision.action == Action.EXECUTE

    def test_router_matches_use_the_pushed_value(self, catalog):
        router = SpeechRouter(catalog, "x-ray")
        router.hint_engine = False
        assert router._is_pattern_complete(["boost"], "command") is False
        assert router._cannot_match(["remember"], "command") is True
        router.hint_engine = True
        assert router._is_pattern_complete(["boost"], "command") is True
        assert router._cannot_match(["remember"], "command") is False

    def test_utterance_end_finalization_dictates_when_false(self, catalog):
        from speech.router import Action, ProcessingMode

        router = SpeechRouter(catalog, "x-ray")
        router.hint_engine = False
        decision = router.decide_timeout(
            ["boost"], False, mode=ProcessingMode.COMMAND_BUFFERING,
            utterance_words=["boost"],
        )
        assert decision.action != Action.EXECUTE
        router.hint_engine = True
        decision = router.decide_timeout(
            ["boost"], False, mode=ProcessingMode.COMMAND_BUFFERING,
            utterance_words=["boost"],
        )
        assert decision.action == Action.EXECUTE


# ---------------------------------------------------------------------------
# The push: SpeechHandler -> SpeechProcessor -> SpeechRouter
# ---------------------------------------------------------------------------


class TestPush:
    def test_processor_pushes_to_its_router(self, catalog):
        import asyncio

        from speech.speech_processor import SpeechProcessor

        processor = SpeechProcessor(
            word_queue=asyncio.Queue(),
            catalog=catalog,
            text_parser=MagicMock(),
            app=MagicMock(),
            hotword="x-ray",
        )
        assert processor.hint_engine is None
        processor.apply_hint_engine(False)
        assert processor.hint_engine is False
        assert processor.router.hint_engine is False
        assert processor.text_parser.hint_engine is False
        processor.apply_hint_engine(None)
        assert processor.router.hint_engine is None
        assert processor.text_parser.hint_engine is None

    @pytest.mark.asyncio
    async def test_the_execution_walk_refuses_when_false(self, catalog):
        """TextParser (command_engine.py) builds its own PatternMatcher and
        re-matches the command text; it must refuse what the router
        refused."""
        from speech.command_engine import TextParser

        parser = TextParser(MagicMock(), catalog)
        assert parser.hint_engine is None
        # Awaitable and answering True: a walk that wrongly matches must
        # reach the assertions below, not crash on awaiting a MagicMock.
        parser._execute_rule = AsyncMock(return_value=True)
        parser.hint_engine = False
        assert await parser.parse_and_execute(
            "boost", authorized_command=True) is False
        parser._execute_rule.assert_not_called()

    def test_handler_forwards_to_the_processor(self):
        from speech.speech_handler import SpeechHandler

        handler = SpeechHandler.__new__(SpeechHandler)
        handler.speech_processor = MagicMock()
        handler.hint_engine = None
        handler.apply_hint_engine(False)
        handler.speech_processor.apply_hint_engine.assert_called_once_with(
            False)

    def test_handler_keeps_the_value_before_the_processor_exists(self):
        """A capabilities frame can arrive before main.py builds the
        processor; the value must not be lost."""
        from speech.speech_handler import SpeechHandler

        handler = SpeechHandler.__new__(SpeechHandler)
        handler.speech_processor = None
        handler.hint_engine = None
        handler.apply_hint_engine(False)
        assert handler.hint_engine is False

    def test_a_new_processor_receives_the_kept_value(self, monkeypatch):
        import asyncio

        import speech.speech_handler as handler_mod
        from speech.speech_handler import SpeechHandler

        built = MagicMock()
        monkeypatch.setattr(
            handler_mod, "SpeechProcessor", MagicMock(return_value=built))
        monkeypatch.setattr(
            handler_mod, "_build_focus_redirect_policy", MagicMock())
        monkeypatch.setattr(
            handler_mod, "_build_default_focused_hwnd_provider", MagicMock())
        handler = SpeechHandler.__new__(SpeechHandler)
        handler.config = {
            "REPLACEMENT_TIMEOUT_MS": 400, "COMMAND_TIMEOUT_MS": 1000}
        handler.pattern_catalog = MagicMock(command_hotword="x-ray")
        handler.text_parser = MagicMock()
        handler.app = MagicMock(websocket_manager=None)
        handler.logic_controller = None
        handler.speech_processor = None
        handler.hint_engine = False

        handler.initialize_speech_processor(asyncio.Queue())

        built.apply_hint_engine.assert_called_once_with(False)


# ---------------------------------------------------------------------------
# WebSocketManager: stores the declared value, active client only, resets
# to unknown on a stream boundary
# ---------------------------------------------------------------------------


class _FakeWebsocket:
    def __init__(self):
        self.sent = []
        self.remote_address = ("127.0.0.1", 0)

    async def send(self, payload):
        self.sent.append(payload)


@pytest.fixture
def manager():
    import asyncio

    from integrations.websocket_manager import WebSocketManager

    loop = asyncio.new_event_loop()
    m = WebSocketManager(loop=loop)
    m.state_manager = MagicMock()
    m.state_manager.config_service.get = MagicMock(return_value=False)
    m.state_manager._get_current_stt_provider = MagicMock(
        return_value="google_stt")
    m.speech_handler = MagicMock()
    yield m
    loop.close()


def _frame(**fields):
    frame = {"provider": "parakeet_tdt", "emits_eos": False}
    frame.update(fields)
    return frame


class TestWebSocketManager:
    def test_starts_unknown(self, manager):
        assert manager.provider_applies_hints is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_stores_and_pushes_the_declared_value(self, manager, value):
        ws = _FakeWebsocket()
        await manager.add_client(ws)
        manager.speech_handler.reset_mock()
        manager._apply_capabilities(ws, _frame(applies_hints=value))
        assert manager.provider_applies_hints is value
        manager.speech_handler.apply_hint_engine.assert_called_once_with(
            value)

    @pytest.mark.asyncio
    async def test_frame_without_the_key_keeps_unknown(self, manager):
        """An older provider build declares nothing: the command must keep
        working, so the value stays None rather than turning False."""
        ws = _FakeWebsocket()
        await manager.add_client(ws)
        manager.speech_handler.reset_mock()
        manager._apply_capabilities(ws, _frame())
        assert manager.provider_applies_hints is None
        manager.speech_handler.apply_hint_engine.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_client_declaration_is_ignored(self, manager):
        client_a = _FakeWebsocket()
        client_b = _FakeWebsocket()
        await manager.add_client(client_a)
        await manager.add_client(client_b)  # B is the active stream.
        manager.speech_handler.reset_mock()
        manager._apply_capabilities(client_a, _frame(applies_hints=False))
        assert manager.provider_applies_hints is None
        manager.speech_handler.apply_hint_engine.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_new_active_client_resets_to_unknown(self, manager):
        """Not to False: the gap during an engine switch keeps the command."""
        client_a = _FakeWebsocket()
        await manager.add_client(client_a)
        manager._apply_capabilities(client_a, _frame(applies_hints=False))
        manager.speech_handler.reset_mock()

        await manager.add_client(_FakeWebsocket())

        assert manager.provider_applies_hints is None
        manager.speech_handler.apply_hint_engine.assert_called_with(None)

    @pytest.mark.asyncio
    async def test_the_last_client_leaving_resets_to_unknown(self, manager):
        ws = _FakeWebsocket()
        await manager.add_client(ws)
        manager._apply_capabilities(ws, _frame(applies_hints=False))
        manager.speech_handler.reset_mock()

        manager.remove_client(ws)

        assert manager.provider_applies_hints is None
        calls = manager.speech_handler.apply_hint_engine.call_args_list
        assert calls and all(args == (None,) for args, _kw in calls)

    @pytest.mark.asyncio
    async def test_no_speech_handler_is_survivable(self, manager):
        manager.speech_handler = None
        ws = _FakeWebsocket()
        await manager.add_client(ws)
        manager._apply_capabilities(ws, _frame(applies_hints=False))
        assert manager.provider_applies_hints is False
