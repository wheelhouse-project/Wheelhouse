"""SpeechProcessor integration tests for STT-injected punctuation
(wh-9f51.2.3).

These tests cover the actual word-by-word execution flow that the
PatternMatcher-only unit tests in test_pattern_matcher_punctuation.py
and test_pattern_catalog_punctuation.py do not exercise:

    SpeechProcessor (buffers word)
        -> SpeechRouter._resolve_finalization
        -> Decision(Action.EXECUTE, payload=buffer_text)
        -> SpeechProcessor._execute_command
        -> CommandEngine.parse_and_execute
        -> PatternMatcher.match_single_pattern

The wh-9f51.1 fix only patched match_complete, which is on the routing
path. match_single_pattern lives on the execution path and was
originally untouched, so a "backspace," utterance still got dictated
as the literal text "backspace,". This test file is the missing
regression net that would have caught the wh-9f51.2.1 blocker.

The fix (wh-9f51.2.1 + wh-9f51.2.2) is:

  - Extract _match_command_with_punct_retry as a shared helper on
    PatternMatcher so both match_complete and match_single_pattern
    use the same two-stage logic.
  - Try fullmatch on the ORIGINAL text first so parameterized
    captures like ^press\\s*(.+)$ against "press." capture "." as
    the argument. Only on first-try failure retry with rstripped
    text so the backspace-comma case still wins.

The two tests below therefore both depend on both fixes: the first
needs match_single_pattern to apply the strip-retry at all, the
second needs the original-text first-try so the "." is captured as
the press_keys argument instead of being stripped off and dictated.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'


# Two minimal patterns lifted from production patterns.toml:
#   - ^back ?space\s*(\d+)?$ -- the backspace-comma motivating case
#     for wh-9f51.1. No capture is exercised in the test; the trailing
#     "," is folded into remainder.
#   - ^press\s*(.+)$ -- the wh-9f51.2.2 parameterized regression. The
#     ".(period)" must end up as the captured key for press_keys, not
#     stripped off and dictated.
_PATTERNS = """
[[pattern]]
pattern = '''^back ?space\\s*(\\d+)?$'''
actions = [
    { function = "press", params = ["backspace", "g1"] }
]

[[pattern]]
pattern = '''^press\\s*(.+)$'''
actions = [
    { function = "press_keys", params = ["g1"] }
]
"""


@pytest.fixture
def punctuation_patterns_path(tmp_path):
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + _PATTERNS, encoding="utf-8")
    return str(p)


@pytest.fixture
async def punctuation_harness(punctuation_patterns_path):
    harness = SpeechPipelineHarness(patterns_path=punctuation_patterns_path)
    await harness.start()
    yield harness
    await harness.stop()


def _press_key_actions(harness):
    """press(...) returns {'action': 'press_key_action', ...}."""
    return [
        out for out in harness.get_outputs() if out.action == "press_key_action"
    ]


def _hotkey_actions(harness):
    """press_keys(...) ultimately calls hotkey(), which returns
    {'action': 'hotkey_action', ...}."""
    return [
        out for out in harness.get_outputs() if out.action == "hotkey_action"
    ]


def _all_inserted_text(harness):
    """Every text-bearing payload, regardless of action name. We use
    this for "the literal X must NOT be typed" assertions so we catch
    both intelligent_insert_text and insert_text shapes."""
    pieces = []
    for out in harness.get_outputs():
        for key in ("insertion_string", "text"):
            val = out.params.get(key)
            if val:
                pieces.append(val)
    return pieces


# ----------------------------------------------------------------------
# wh-9f51.2.1: SpeechProcessor command-execution path must normalize
# the trailing punctuation. The blocker proved match_single_pattern
# was untouched in round 1.
# ----------------------------------------------------------------------


class TestBackspaceCommaIntegration:
    @pytest.mark.asyncio
    async def test_backspace_comma_fires_backspace_action(
        self, punctuation_harness,
    ):
        """The spoken "backspace comma" lands as the single STT token
        "backspace,". The processor must route this to the backspace
        command (one Backspace press) and dictate the "," as the
        remainder. Before wh-9f51.2.1 the entire literal "backspace,"
        was typed out and no Backspace was pressed."""
        await punctuation_harness.send_word(
            "backspace,", start_of_utterance=True, end_of_utterance=False,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        # The backspace key action must fire exactly once.
        press_actions = _press_key_actions(punctuation_harness)
        assert len(press_actions) == 1, (
            f"expected exactly one press_key_action for the backspace "
            f"command; got {press_actions!r}"
        )
        assert press_actions[0].params.get("key") == "backspace"

        # The literal text "backspace," must NEVER be inserted. The fix
        # is specifically about not falling back to dictation for this
        # token.
        inserted = _all_inserted_text(punctuation_harness)
        assert "backspace," not in inserted, (
            f"the raw STT token 'backspace,' must not be inserted as "
            f"text; inserted={inserted!r}"
        )
        # Defence-in-depth: the bare word "backspace" must not appear
        # as dictation either (it should be consumed by the command).
        assert "backspace" not in inserted, (
            f"'backspace' must be consumed by the command, not "
            f"dictated; inserted={inserted!r}"
        )


# ----------------------------------------------------------------------
# wh-9f51.2.2: parameterized command captures must survive. The
# unconditional rstrip from round 1 broke ^press\s*(.+)$.
# ----------------------------------------------------------------------


class TestPressPeriodIntegration:
    @pytest.mark.asyncio
    async def test_press_period_fires_press_keys_with_period_argument(
        self, punctuation_harness,
    ):
        """The spoken "press period" lands as the single STT token
        "press." (the STT/ITN attached the period as sentence
        punctuation). The pattern ^press\\s*(.+)$ MUST match this
        with capture="." -- the "." is the spoken key argument, not
        stray sentence punctuation.

        The round-1 unconditional rstrip stripped the "." before
        fullmatch, leaving "press" which fails the (.+) capture
        requirement. The two-stage matching (try original first,
        retry with rstrip only on failure) preserves the capture.
        """
        await punctuation_harness.send_word(
            "press.", start_of_utterance=True, end_of_utterance=False,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        # press_keys(".") routes through hotkey() and emits a
        # hotkey_action whose key list is ["."].
        hotkeys = _hotkey_actions(punctuation_harness)
        assert len(hotkeys) == 1, (
            f"expected exactly one hotkey_action for press_keys('.'); "
            f"got {hotkeys!r}"
        )
        assert hotkeys[0].params.get("keys") == ["."], (
            f"the captured key argument must be '.'; "
            f"got {hotkeys[0].params!r}"
        )

        # A naive rstrip-first implementation would (a) fail the
        # fullmatch, (b) leave "." in remainder, and (c) dictate "."
        # as a literal period AFTER the command runs (or instead of
        # it). Either of those would leave "." in the inserted-text
        # stream; assert it isn't.
        inserted = _all_inserted_text(punctuation_harness)
        assert "." not in inserted, (
            f"the spoken '.' must be the press_keys argument, not "
            f"dictated as literal text; inserted={inserted!r}"
        )
        assert "press." not in inserted, (
            f"the raw STT token 'press.' must not be inserted as "
            f"text; inserted={inserted!r}"
        )
        assert "press" not in inserted, (
            f"the bare word 'press' must be consumed by the command, "
            f"not dictated; inserted={inserted!r}"
        )


# ----------------------------------------------------------------------
# wh-9f51.3.1 / wh-9f51.3.3: multi-word utterances where the first
# word carries STT-injected punctuation. The round-2 fix only patched
# the single-word case; the multi-word case still fell through to
# dictation because the joined buffer text put the comma mid-string
# where the retry-rstrip cannot reach it.
# ----------------------------------------------------------------------


class TestMultiWordFirstWordPunctuation:
    @pytest.mark.asyncio
    async def test_backspace_comma_three_fires_count_suffix_backspace(
        self, punctuation_harness,
    ):
        """The spoken "backspace three" lands as the two STT tokens
        "backspace," + "3" (the STT/ITN attached sentence punctuation
        to the first word). The joined buffer "backspace, 3" must
        route to ^back ?space\\s*(\\d+)?$ with the count captured.

        Before wh-9f51.3.1 the joined text had the comma embedded
        between "backspace" and "3", where the retry helper's
        rstrip stripped only trailing characters and could not
        reach it. The fullmatch failed; the buffer fell through to
        dictation; "backspace, 3" was typed.

        After the fix the boundary normalization strips the comma at
        the first-word boundary BEFORE the fullmatch runs, so the
        retry helper sees "backspace 3" and matches the count-suffix
        pattern. The press(...) action emits a single
        press_key_action with repeat=3 (not three separate actions --
        the wheelhouse press() implementation packs the count into
        the payload).
        """
        await punctuation_harness.send_word(
            "backspace,", start_of_utterance=True, end_of_utterance=False,
        )
        await punctuation_harness.send_word(
            "3", start_of_utterance=False, end_of_utterance=True,
            delay_before_ms=50,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        press_actions = _press_key_actions(punctuation_harness)
        assert len(press_actions) == 1, (
            f"expected exactly one press_key_action with repeat=3 "
            f"for the count-suffix backspace command; got {press_actions!r}"
        )
        assert press_actions[0].params.get("key") == "backspace"
        assert press_actions[0].params.get("repeat") == 3, (
            f"expected count-suffix capture to set repeat=3; got "
            f"{press_actions[0].params!r}"
        )

        inserted = _all_inserted_text(punctuation_harness)
        # The literal mid-string punctuation must not surface as
        # dictated text -- this is exactly what the multi-word fix
        # prevents.
        assert "backspace, 3" not in inserted, (
            f"the joined buffer text 'backspace, 3' must not be "
            f"dictated; inserted={inserted!r}"
        )
        assert "backspace," not in inserted, (
            f"the raw STT token 'backspace,' must not be inserted "
            f"as text; inserted={inserted!r}"
        )
        assert "backspace" not in inserted, (
            f"the bare word 'backspace' must be consumed by the "
            f"command, not dictated; inserted={inserted!r}"
        )

    @pytest.mark.asyncio
    async def test_press_period_then_hello_dictates_hello_after_press(
        self, punctuation_harness,
    ):
        """A two-utterance flow that exercises the wh-9f51.2.2
        parameterized-capture path in a multi-utterance context.
        First utterance: "press period" arrives as the single STT
        token "press." -- the original-text first-try captures "." as
        the press_keys argument (wh-9f51.2.2 regression). Second
        utterance: "hello" arrives as a plain word and dictates.

        This test guards against a future refactor that would route
        "press." through the boundary-normalization helper and lose
        the captured argument. It also confirms the
        boundary-normalization fix did not accidentally consume the
        "." that the parameterized pattern wanted.
        """
        await punctuation_harness.send_word(
            "press.", start_of_utterance=True, end_of_utterance=True,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        await punctuation_harness.send_word(
            "hello", start_of_utterance=True, end_of_utterance=True,
            delay_before_ms=50,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        hotkeys = _hotkey_actions(punctuation_harness)
        assert len(hotkeys) == 1, (
            f"expected exactly one hotkey_action from press_keys('.'); "
            f"got {hotkeys!r}"
        )
        assert hotkeys[0].params.get("keys") == ["."], (
            f"the captured key argument must be '.'; "
            f"got {hotkeys[0].params!r}"
        )

        inserted = _all_inserted_text(punctuation_harness)
        # The "." must NOT appear in dictation -- it was the
        # press_keys argument. "hello" MUST appear -- it is the
        # second utterance.
        assert "." not in inserted, (
            f"the spoken '.' must be the press_keys argument, not "
            f"dictated as literal text; inserted={inserted!r}"
        )
        assert "press." not in inserted
        assert "press" not in inserted
        assert any("hello" in piece for piece in inserted), (
            f"the second utterance 'hello' must be dictated; "
            f"inserted={inserted!r}"
        )

    @pytest.mark.asyncio
    async def test_mid_utterance_backspace_comma_is_dictated_not_executed(
        self, punctuation_harness,
    ):
        """A mid-utterance "backspace," (start_of_utterance=False)
        MUST be dictated, not executed as a command. Per the router
        truth table at speech/router.py:_decide_idle, the
        ``(start_of_utterance=False, PatternType.COMMAND)`` cell
        returns DICTATE because the user is mid-sentence and the
        word should be typed literally.

        This test freezes that behavior so a future change that
        accidentally promotes mid-utterance command tokens (e.g. by
        also normalizing punctuation in get_pattern_type and then
        forwarding through the FRESH_COMMAND case) will fail
        loudly. It also confirms the wh-9f51.3.1 boundary
        normalization did not leak across the
        idle-vs-buffering decision.

        Flow: "hello" (idle DICTATE), "backspace," (idle DICTATE),
        "world" (idle DICTATE). No backspace command fires.
        """
        await punctuation_harness.send_word(
            "hello", start_of_utterance=True, end_of_utterance=False,
        )
        await punctuation_harness.send_word(
            "backspace,", start_of_utterance=False, end_of_utterance=False,
            delay_before_ms=50,
        )
        await punctuation_harness.send_word(
            "world", start_of_utterance=False, end_of_utterance=True,
            delay_before_ms=50,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        # No backspace command must fire. This is the freeze: the
        # mid-utterance word is dictated, not consumed.
        press_actions = _press_key_actions(punctuation_harness)
        assert press_actions == [], (
            f"mid-utterance 'backspace,' must NOT execute the "
            f"backspace command; got {press_actions!r}"
        )

        # Every spoken word must reach dictation in some form. We
        # accept either separate insertions per word or a coalesced
        # form; we only assert each word is present somewhere in the
        # joined dictation stream.
        inserted = _all_inserted_text(punctuation_harness)
        combined = " ".join(inserted)
        assert "hello" in combined, (
            f"expected 'hello' to be dictated; inserted={inserted!r}"
        )
        assert "backspace" in combined, (
            f"expected mid-utterance 'backspace' (with or without "
            f"trailing comma) to be dictated; inserted={inserted!r}"
        )
        assert "world" in combined, (
            f"expected 'world' to be dictated; inserted={inserted!r}"
        )

    @pytest.mark.asyncio
    async def test_backspace_multiple_trailing_punctuation_fires_command(
        self, punctuation_harness,
    ):
        """The single STT token "backspace.," (period then comma --
        the STT/ITN attached both characters as sentence punctuation)
        must still match the bare backspace command. rstrip handles
        runs of the strip set, so the retry path strips ".,".

        This is a regression test for the strip set rather than for
        the boundary normalization; it freezes the rstrip behavior
        so a future per-character strip refactor does not silently
        break double-punctuated tokens.
        """
        await punctuation_harness.send_word(
            "backspace.,", start_of_utterance=True, end_of_utterance=True,
        )
        await punctuation_harness.send_utterance_end_marker(
            punctuation_harness._utterance_counter,
        )
        await asyncio.sleep(0.1)

        press_actions = _press_key_actions(punctuation_harness)
        assert len(press_actions) == 1, (
            f"expected exactly one press_key_action for the bare "
            f"backspace command (rstrip should handle '.,' as a "
            f"run); got {press_actions!r}"
        )
        assert press_actions[0].params.get("key") == "backspace"

        inserted = _all_inserted_text(punctuation_harness)
        assert "backspace.," not in inserted
        assert "backspace" not in inserted, (
            f"the bare word 'backspace' must be consumed; "
            f"inserted={inserted!r}"
        )
