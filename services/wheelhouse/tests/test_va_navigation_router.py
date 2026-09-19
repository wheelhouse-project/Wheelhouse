"""Router-level guards for the fixed Voice Access navigation entries.

Finding wh-voice-access-parity.3.1 (DeepSeek, round 1 on commit e5d7e99a).
The fixed go/move navigation entries added by wh-voice-access-parity.1.5
carried no whole_utterance_only flag, so the router's mid-utterance
complete-pattern execution fired them as soon as their final word arrived:
"go to end of line" pressed End at the word "end" and typed "of line",
and "go to end then grab left" pressed End and typed "then grab left".

placement-verify.py and the test_va_* files are whole-utterance
first-regex-match walks, so none of them could see this. These tests
drive the production catalog through the router word-by-word, the same
harness shape as TestDeleteAllIsWholeUtteranceOnly.
"""
import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)


@pytest.fixture
async def nav_harness():
    harness = SpeechPipelineHarness()
    await harness.start()
    yield harness
    await harness.stop()


class TestFixedNavEntriesAreWholeUtteranceOnly:
    @pytest.mark.asyncio
    async def test_qualified_go_to_end_of_line_is_not_severed(self, nav_harness):
        """The bare 'go to end' entry must not fire at word three."""
        await _send_phrase(nav_harness, ("go", "to", "end", "of", "line"))

        assert ["end"] in _hotkey_key_lists(nav_harness)
        assert _inserted_text(nav_harness) == [], (
            "the tail of 'go to end of line' was typed as text; the bare "
            "'go to end' entry fired mid-utterance"
        )

    @pytest.mark.asyncio
    async def test_chain_go_to_end_then_grab_left_still_parses_whole(
        self, nav_harness
    ):
        """Chains resolve through cursor-navigate at utterance end."""
        await _send_phrase(
            nav_harness, ("go", "to", "end", "then", "grab", "left")
        )

        hotkeys = _hotkey_key_lists(nav_harness)
        assert ["end"] in hotkeys
        assert ["shift", "left"] in hotkeys
        assert _inserted_text(nav_harness) == [], (
            "the chain tail was typed as text instead of parsing"
        )

    @pytest.mark.asyncio
    async def test_counted_chain_go_left_2_words_then_grab_right(
        self, nav_harness
    ):
        """A digit-counted entry must not sever a chain at its last word."""
        await _send_phrase(
            nav_harness, ("go", "left", "2", "words", "then", "grab", "right")
        )

        assert ["ctrl", "left"] in _hotkey_key_lists(nav_harness)
        assert _inserted_text(nav_harness) == [], (
            "the chain tail was typed as text instead of parsing"
        )

    @pytest.mark.asyncio
    async def test_dictated_sentence_with_landmark_prefix_types_whole(
        self, nav_harness
    ):
        """A sentence that merely starts like a landmark stays dictation."""
        words = (
            "go", "to", "the", "beginning", "of", "the", "line",
            "and", "select", "all",
        )
        await _send_phrase(nav_harness, words)

        assert _hotkey_key_lists(nav_harness) == [], (
            "a navigation entry fired inside an ordinary sentence"
        )
        dictated = " ".join(_inserted_text(nav_harness))
        for word in words:
            assert word in dictated, (
                f"{word!r} was lost from dictation; dictated={dictated!r}"
            )

    @pytest.mark.asyncio
    async def test_bare_landmark_alone_still_fires_at_utterance_end(
        self, nav_harness
    ):
        """The flag must not break the plain single-command case."""
        await _send_phrase(nav_harness, ("go", "to", "top"))

        assert ["ctrl", "home"] in _hotkey_key_lists(nav_harness)
        assert _inserted_text(nav_harness) == []


class TestSwitchToAppFixedSetCoverage:
    """Natural landmark phrasings must beat switch-to-app under the hotword.

    Finding wh-voice-access-parity.3.2 (DeepSeek, round 1). The fixed
    go-to entries above switch-to-app missed the article forms
    ("go to the top"), the start-of synonyms ("go to start of word",
    parser _COMPOUND_PREFIXES accepts "start"), and "go to home"
    (parser maps the simple landmark "home" to the Home key, wh-ed4
    strips the optional "to"). With the hotword active those utterances
    first-matched switch-to-app and activated an app named by the
    landmark words. The prefix ("x-ray",) speaks the harness hotword.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("words", "expected_keys"),
        (
            (("go", "to", "home"), ["home"]),
            (("go", "to", "the", "top"), ["ctrl", "home"]),
            (("go", "to", "the", "bottom"), ["ctrl", "end"]),
            (("go", "to", "the", "end"), ["end"]),
            (("go", "to", "start", "of", "word"), ["ctrl", "left"]),
            (("go", "to", "start", "of", "paragraph"), ["ctrl", "up"]),
            (("go", "to", "the", "top", "of", "the", "document"),
             ["ctrl", "home"]),
            (("go", "to", "the", "bottom", "of", "the", "document"),
             ["ctrl", "end"]),
        ),
    )
    async def test_landmark_phrasing_navigates_under_hotword(
        self, nav_harness, words, expected_keys
    ):
        await _send_phrase(nav_harness, words, prefix=("x-ray",))

        actions = nav_harness.mock_app.get_all_actions()
        assert "activate_window" not in actions, (
            f"{' '.join(words)!r} activated an app instead of navigating"
        )
        assert expected_keys in _hotkey_key_lists(nav_harness)

    @pytest.mark.asyncio
    async def test_go_to_app_name_still_activates_under_hotword(
        self, nav_harness
    ):
        """The fixed set must not break the real app-switch command."""
        await _send_phrase(nav_harness, ("go", "to", "notepad"),
                           prefix=("x-ray",))

        assert "activate_window" in nav_harness.mock_app.get_all_actions()


class TestPerAppCapturesDoNotSwallowFixedPhrases:
    """Fixed window phrases must beat the end-of-file per-app captures.

    Finding wh-voice-access-parity.3.3 (DeepSeek, round 1), the subset
    with existing actions. Under the hotword, show-app / minimize-app /
    maximize-app captured "show desktop", "minimize all",
    "maximize window", and "minimize window" as app names: the
    activation failed (or hit an arbitrary title match) and the
    second key step fired against whatever had focus.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("words", "expected_keys"),
        (
            (("show", "desktop"), ["win", "d"]),
            (("minimize", "all"), ["win", "m"]),
            (("maximize", "window"), ["win", "up"]),
            (("minimize", "window"), ["win", "down"]),
        ),
    )
    async def test_fixed_phrase_beats_per_app_capture(
        self, nav_harness, words, expected_keys
    ):
        await _send_phrase(nav_harness, words, prefix=("x-ray",))

        actions = nav_harness.mock_app.get_all_actions()
        assert "activate_window" not in actions, (
            f"{' '.join(words)!r} was captured as an app name"
        )
        assert expected_keys in _hotkey_key_lists(nav_harness)

    @pytest.mark.asyncio
    async def test_real_app_name_still_reaches_the_capture(self, nav_harness):
        await _send_phrase(nav_harness, ("minimize", "notepad"),
                           prefix=("x-ray",))

        assert "activate_window" in nav_harness.mock_app.get_all_actions()


class TestEngineRematchHonorsHotwordGate:
    """The engine's re-match must apply the router's hotword gate.

    Finding wh-voice-access-parity.3.5 ([claude], round 1). The router
    resolves the buffer with the hotword gate applied, then the
    processor re-matched it with authorized_command=True, which
    disabled the gate. switch-to-app (hotword required) sits earlier in
    the file than cursor-navigate, so with the hotword INACTIVE it
    stole every "go to ..." buffer at the execution layer:
    "go to somewhere nice" became activate_window("somewhere nice")
    instead of dictation.
    """

    @pytest.mark.asyncio
    async def test_go_to_unparseable_tail_dictates_without_hotword(
        self, nav_harness
    ):
        words = ("go", "to", "somewhere", "nice")
        await _send_phrase(nav_harness, words)

        actions = nav_harness.mock_app.get_all_actions()
        assert "activate_window" not in actions, (
            "a hotword-required pattern executed without the hotword"
        )
        dictated = " ".join(_inserted_text(nav_harness))
        for word in words:
            assert word in dictated, (
                f"{word!r} was lost from dictation; dictated={dictated!r}"
            )
class TestGoToDesktopIsEliminated:
    """David's elimination ruling of 2026-08-24, wh-voice-access-parity.3.3.

    The fixed '^go to desktop$' entry is gone. "go home" is now the one
    show-desktop command, so keeping a second wording for the same action
    was redundant.

    The accepted consequence is recorded here rather than left to be
    rediscovered: with the fixed entry removed, "go to desktop" falls into
    switch-to-app's '^(?:switch to|go to)\\s+(.+)$' capture and asks the
    input process to activate a window called "desktop". David chose that
    over keeping the entry. This test fails if anyone restores a fixed
    entry for the phrase, and it fails if the capture stops claiming it.
    """

    @pytest.mark.asyncio
    async def test_go_to_desktop_reaches_switch_to_app(self, nav_harness):
        await _send_phrase(
            nav_harness, ("go", "to", "desktop"), prefix=("x-ray",)
        )

        targets = [
            out.params.get("target")
            for out in nav_harness.mock_app.outputs
            if out.action == "activate_window"
        ]
        assert targets == ["desktop"], (
            "'go to desktop' did not reach switch-to-app with the captured "
            f"value 'desktop'; activate_window targets were {targets!r}"
        )

    @pytest.mark.asyncio
    async def test_go_to_desktop_no_longer_shows_the_desktop(
        self, nav_harness
    ):
        await _send_phrase(
            nav_harness, ("go", "to", "desktop"), prefix=("x-ray",)
        )

        assert ["win", "d"] not in _hotkey_key_lists(nav_harness), (
            "'go to desktop' still fires Win+D; a fixed entry for the "
            "phrase was restored, against the elimination ruling"
        )
class TestCloseTabDoesNotCloseTheWindow:
    """David ruling 1 of 2026-08-24, wh-voice-access-parity.3.3.

    Under the hotword the end-of-file close-app capture,
    '^(?:close|exit|quit)\\s+(.+)$', claimed "close tab" as an application
    name. activate_window cannot find an application called "tab", but
    command_engine._execute_rule sends every step of a rule without
    inspecting the activate response, so the alt+f4 still fired against
    whatever window had focus. The user asked to close one tab and lost the
    whole window. Before the hotword existed the phrase was dictated as
    text, so this was a new way to lose work.
    """

    @pytest.mark.asyncio
    async def test_close_tab_sends_ctrl_w(self, nav_harness):
        await _send_phrase(nav_harness, ("close", "tab"), prefix=("x-ray",))

        hotkeys = _hotkey_key_lists(nav_harness)
        assert ["ctrl", "w"] in hotkeys, (
            f"'close tab' did not send Ctrl+W; hotkeys were {hotkeys!r}"
        )
        assert ["alt", "f4"] not in hotkeys, (
            "'close tab' still fires Alt+F4; the close-app capture claimed "
            "it and the window would be closed instead of the tab"
        )
        assert "activate_window" not in nav_harness.mock_app.get_all_actions(), (
            "'close tab' was captured as an application name"
        )

    @pytest.mark.asyncio
    async def test_close_notepad_still_closes_the_application(
        self, nav_harness
    ):
        """The control: the per-app capture must keep its real work."""
        await _send_phrase(
            nav_harness, ("close", "notepad"), prefix=("x-ray",)
        )

        assert "activate_window" in nav_harness.mock_app.get_all_actions(), (
            "'close notepad' no longer reaches the per-app close capture"
        )
        assert ["alt", "f4"] in _hotkey_key_lists(nav_harness), (
            "'close notepad' no longer sends Alt+F4"
        )


class TestNavHomophoneChainsReachTheParser:
    """Chained "go write ..." runs as navigation. wh-voice-access-parity.4.

    The two staged blocks in wh-voice-access-parity.1.13 cover the bare
    utterance only. A chained utterance goes through the cursor-navigate
    entry into NavigationParser, which knew only "right", so the whole
    sentence was typed as text. Recorded as an observed limit on
    wh-voice-access-parity.3.8; David ruled extend on 2026-08-25.

    These run over the production catalog with no splice, because the
    chain never touches the staged blocks. They therefore keep working
    once those blocks are spliced in.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words,keys,repeat", [
        (("go", "write", "three", "characters", "then", "grab", "left"),
         ["right"], 3),
        (("go", "write", "two", "words", "then", "grab", "left"),
         ["ctrl", "right"], 2),
    ])
    async def test_the_chain_runs_as_navigation(
        self, nav_harness, words, keys, repeat
    ):
        await _send_phrase(nav_harness, words)

        hotkeys = [
            (out.params.get("keys"), out.params.get("repeat"))
            for out in nav_harness.mock_app.outputs
            if out.action == "hotkey_action"
        ]
        assert hotkeys == [(keys, repeat), (["shift", "left"], 1)], (
            f"{' '.join(words)!r} did not run as a navigation chain; "
            f"hotkeys={hotkeys!r}"
        )
        assert _inserted_text(nav_harness) == [], (
            "the chain was typed as text instead of parsing"
        )

    @pytest.mark.asyncio
    async def test_the_correctly_heard_chain_is_unchanged(self, nav_harness):
        """The control: the word the homophone stands for still works."""
        await _send_phrase(
            nav_harness,
            ("go", "right", "three", "characters", "then", "grab", "left"),
        )

        hotkeys = _hotkey_key_lists(nav_harness)
        assert hotkeys == [["right"], ["shift", "left"]]
        assert _inserted_text(nav_harness) == []

    @pytest.mark.asyncio
    async def test_ordinary_speech_starting_with_go_write_is_still_typed(
        self, nav_harness
    ):
        """The guard on the widening: dictation must not move the caret."""
        await _send_phrase(nav_harness, ("go", "write", "three", "emails"))

        assert _hotkey_key_lists(nav_harness) == [], (
            "'go write three emails' moved the caret; the parser accepted "
            "an utterance that is ordinary speech"
        )
        assert _inserted_text(nav_harness) == ["go write three emails"]
