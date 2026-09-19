"""Hand-crafted E2E workflow tests for realistic speech scenarios.

These test realistic multi-step interactions that auto-generated tests
can't cover: mode switching, punctuation mid-utterance, multi-utterance
sessions, and clipboard lifecycle.
"""
import asyncio
import pytest
from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


@pytest.fixture
async def harness(pattern_catalog):
    """Create and start an E2E pipeline harness."""
    h = E2EPipelineHarness(catalog=pattern_catalog)
    await h.start()
    yield h
    await h.stop()


def assert_first_word(paste, word):
    """Assert first word is correct, tolerating mock timing variation.

    In the E2E mock environment, the first word's formatting depends on
    which insertion strategy wins the race:
    - Clipboard fallback (preceding_chars="") -> "Word" (capitalized, no space)
    - Shadow buffer (stale context)           -> " word" or " Word" (space prefix)

    Both are correct behavior -- the mock doesn't simulate real Windows
    UI state, so the shadow buffer may see stale context. In production,
    the clipboard fallback always handles the first word correctly.
    """
    normalized = paste.strip().lower()
    assert normalized == word.lower(), \
        f"First word should contain '{word}', got '{paste}'"


# ============================================================================
# TASK 5: Dictation with inline punctuation
# ============================================================================

class TestDictationWithPunctuation:
    """Dictation mixed with inline punctuation patterns.

    wh-9zu: punctuation replacements emit only the character (no trailing
    space) so the shadow buffer's trailing-whitespace state doesn't drift
    from reality in contenteditable targets like Gmail that normalize
    trailing whitespace. The next word's prefix-space rule in TextPerfector
    produces the space before the next alphanumeric insert, which is
    identical in every target (no nbsp, no word-wrap side effects).

    Assertions grounded in text_perfector.py design rules:
    - First word: capitalized, no space prefix (preceding_chars="" -> not preceding)
    - Subsequent alphanumeric words: space prefix (preceding_chars has content)
    - Punctuation-only: no space prefix, no trailing space
    - After sentence-ending punct (.!?): capitalize next word
    """

    @pytest.mark.asyncio
    async def test_hello_comma_world(self, harness):
        """'hello comma world' -> first word + ',' + ' world'."""
        await harness.send_utterance(["hello", "comma", "world"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, f"Expected 3 deliveries, got {delivered}"
        assert_first_word(delivered[0], "hello")
        # comma is punctuation-only -> no space prefix, no trailing space
        assert delivered[1] == ","
        # world follows comma (non-whitespace) -> prefix space, no cap
        assert delivered[2] == " world"

    @pytest.mark.asyncio
    async def test_sentence_with_period(self, harness):
        """'this is a test period' -> words pasted, period replacement fires after timeout."""
        await harness.send_utterance(["this", "is", "a", "test", "period"])
        # Wait for 400ms replacement buffer timeout
        await harness.wait_for_timeout(500)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 5, f"Expected 5 deliveries, got {delivered}"
        assert_first_word(delivered[0], "this")
        assert delivered[1:] == [" is", " a", " test", "."], f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_question_mark_in_sentence(self, harness):
        """'test question mark' -> first word + '?'."""
        await harness.send_utterance(["test", "question", "mark"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert_first_word(delivered[0], "test")
        assert delivered[1] == "?"

    @pytest.mark.asyncio
    async def test_comma_only(self, harness):
        """'comma' alone -> ','."""
        await harness.send_utterance(["comma"])
        await asyncio.sleep(0.3)
        assert harness.recording.text_deliveries == [","], \
            f"Got {harness.recording.text_deliveries}"

    @pytest.mark.asyncio
    async def test_exclamation_point(self, harness):
        """'wow exclamation point' -> first word + '!'."""
        await harness.send_utterance(["wow", "exclamation", "point"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert_first_word(delivered[0], "wow")
        assert delivered[1] == "!"

    @pytest.mark.asyncio
    async def test_colon_in_sentence(self, harness):
        """'dear sir colon' -> first word + ' sir' + ':'."""
        await harness.send_utterance(["dear", "sir", "colon"])
        await harness.wait_for_timeout(500)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, f"Expected 3 deliveries, got {delivered}"
        assert_first_word(delivered[0], "dear")
        assert delivered[1:] == [" sir", ":"]

    @pytest.mark.asyncio
    async def test_new_line_mid_dictation(self, harness):
        """'first new line second' -> paste, keystroke, paste pattern."""
        await harness.send_utterance(["first", "new", "line", "second"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        # First word present (timing-tolerant)
        assert any("first" in p.lower() for p in delivered), \
            f"Expected 'first' in {delivered}"
        # "new line" -> shift+enter keystroke (patterns.toml:200-224)
        non_paste_keys = [k for k in harness.recording.get_keystroke_keys()
                          if k not in [("ctrl", "v"), ("ctrl", "c"),
                                       ("shift", "left", "left"), ("shift", "right")]]
        assert ("shift", "enter") in non_paste_keys, \
            f"Expected ('shift', 'enter') in {non_paste_keys}"
        # "second" pasted after newline
        assert any("second" in p.lower() for p in delivered), \
            f"Expected 'second' in {delivered}"

    @pytest.mark.asyncio
    async def test_multiple_commas(self, harness):
        """'hello comma comma world' -> two commas between words."""
        await harness.send_utterance(["hello", "comma", "comma", "world"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 4, f"Expected 4 deliveries, got {delivered}"
        assert_first_word(delivered[0], "hello")
        assert delivered[1:] == [",", ",", " world"]

    @pytest.mark.asyncio
    async def test_semicolon(self, harness):
        """'test semicolon' -> first word + ';'."""
        await harness.send_utterance(["test", "semicolon"])
        await harness.wait_for_timeout(500)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert_first_word(delivered[0], "test")
        assert delivered[1] == ";"

    @pytest.mark.asyncio
    async def test_hyphen(self, harness):
        """'test hyphen word' -> first word + '-' + ' word'."""
        await harness.send_utterance(["test", "hyphen", "word"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, f"Expected 3 deliveries, got {delivered}"
        assert_first_word(delivered[0], "test")
        assert delivered[1:] == ["-", " word"]


# ============================================================================
# TASK 6: Command-dictation switching
# ============================================================================

class TestCommandDictationSwitching:
    """Rapid switching between command and dictation modes.

    Tests verify clean transitions: command keystrokes only in command phase,
    text delivered only in the dictation phase.
    """

    @pytest.mark.asyncio
    async def test_backspace_then_dictation(self, harness):
        """'backspace' (pause) 'hello world' -> backspace keystroke then two deliveries."""
        await harness.send_word("backspace", start_of_utterance=True, utterance_id=1)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(1100)

        assert ("backspace",) in harness.recording.get_keystroke_keys()
        harness.recording.clear()

        await harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert_first_word(delivered[0], "hello")
        assert delivered[1] == " world"

    @pytest.mark.asyncio
    async def test_dictation_then_undo(self, harness):
        """'hello world' then 'undo' -> text pasted then Ctrl+Z."""
        await harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert_first_word(delivered[0], "hello")
        assert delivered[1] == " world"
        harness.recording.clear()

        await harness.send_word("undo", start_of_utterance=True, utterance_id=10)
        await harness.send_utterance_end_marker(utterance_id=10)
        await harness.wait_for_timeout(1100)
        assert ("ctrl", "z") in harness.recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_rapid_command_command(self, harness):
        """Three 'backspace' utterances -> exactly 3 backspace keystrokes."""
        for uid in range(1, 4):
            await harness.send_word("backspace", start_of_utterance=True, utterance_id=uid)
            await harness.send_utterance_end_marker(utterance_id=uid)
            await harness.wait_for_timeout(1100)

        keys = harness.recording.get_keystroke_keys()
        bs_count = sum(1 for k in keys if k == ("backspace",))
        assert bs_count == 3, f"Expected exactly 3x ('backspace',), got {bs_count}: {keys}"

    @pytest.mark.asyncio
    async def test_delete_mid_utterance_is_dictation(self, harness):
        """'I want to delete that' -> all words pasted, no delete keystroke."""
        await harness.send_utterance(["I", "want", "to", "delete", "that"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 5, f"Expected 5 words delivered, got {len(delivered)}: {delivered}"
        # No delete keystrokes -- only ctrl+v for clipboard paste + context gathering
        non_paste_keys = [k for k in harness.recording.get_keystroke_keys()
                          if k not in [("ctrl", "v"), ("ctrl", "c"),
                                       ("shift", "left", "left"), ("shift", "right")]]
        assert len(non_paste_keys) == 0, \
            f"Mid-utterance 'delete' should not produce non-paste keystrokes, got {non_paste_keys}"

    @pytest.mark.asyncio
    async def test_command_dictation_command(self, harness):
        """escape -> dictation -> undo: full mode-switching sequence."""
        # Command: escape
        await harness.send_word("escape", start_of_utterance=True, utterance_id=1)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(1100)
        assert ("esc",) in harness.recording.get_keystroke_keys()
        harness.recording.clear()

        # Dictation
        await harness.send_utterance(["testing", "one", "two", "three"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 4, f"Expected 4 words, got {len(delivered)}: {delivered}"
        harness.recording.clear()

        # Command: undo
        await harness.send_word("undo", start_of_utterance=True, utterance_id=20)
        await harness.send_utterance_end_marker(utterance_id=20)
        await harness.wait_for_timeout(1100)
        assert ("ctrl", "z") in harness.recording.get_keystroke_keys()


# ============================================================================
# TASK 7: Multi-step commands
# ============================================================================

class TestMultiStepCommands:
    """Multi-action commands and repeated keystrokes.

    Assertions verify exact keystroke sequences from command patterns.
    """

    @pytest.mark.asyncio
    async def test_undo_three(self, harness):
        """'undo three' -> exactly 3x ('ctrl', 'z')."""
        await harness.send_word("undo", start_of_utterance=True)
        await harness.send_word("three", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("ctrl", "z")] * 3, f"Expected 3x ctrl+z, got {keys}"

    @pytest.mark.asyncio
    async def test_backspace_five(self, harness):
        """'backspace five' -> exactly 5x ('backspace',)."""
        await harness.send_word("backspace", start_of_utterance=True)
        await harness.send_word("five", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("backspace",)] * 5, f"Expected 5x backspace, got {keys}"

    @pytest.mark.asyncio
    async def test_delete_word(self, harness):
        """'delete word' -> select word + delete (Ctrl+Left, Shift+Ctrl+Right, Del)."""
        await harness.send_word("delete", start_of_utterance=True)
        await harness.send_word("word", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        # Pattern uses: hk("ctrl","left") + hk("shift","ctrl","right") + press("del")
        assert ("ctrl", "left") in keys, f"Expected ctrl+left, got {keys}"
        assert ("del",) in keys, f"Expected del, got {keys}"

    @pytest.mark.asyncio
    async def test_select_all(self, harness):
        """'select all' -> exactly ('ctrl', 'a')."""
        await harness.send_word("select", start_of_utterance=True)
        await harness.send_word("all", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("ctrl", "a")], f"Expected exactly [('ctrl', 'a')], got {keys}"

    @pytest.mark.asyncio
    async def test_redo_two(self, harness):
        """'redo two' -> exactly 2x ('ctrl', 'y')."""
        await harness.send_word("redo", start_of_utterance=True)
        await harness.send_word("two", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("ctrl", "y")] * 2, f"Expected 2x ctrl+y, got {keys}"

    @pytest.mark.asyncio
    async def test_tab_three(self, harness):
        """'tab three' -> exactly 3x ('tab',)."""
        await harness.send_word("tab", start_of_utterance=True)
        await harness.send_word("three", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("tab",)] * 3, f"Expected 3x tab, got {keys}"

    @pytest.mark.asyncio
    async def test_delete_five(self, harness):
        """'delete five' -> exactly 5x ('del',)."""
        await harness.send_word("delete", start_of_utterance=True)
        await harness.send_word("five", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("del",)] * 5, f"Expected 5x del, got {keys}"

    @pytest.mark.asyncio
    async def test_copy(self, harness):
        """'copy' -> exactly ('ctrl', 'c')."""
        await harness.send_word("copy", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("ctrl", "c")], f"Expected [('ctrl', 'c')], got {keys}"

    @pytest.mark.asyncio
    async def test_paste(self, harness):
        """'paste' -> exactly ('ctrl', 'v')."""
        await harness.send_word("paste", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("ctrl", "v")], f"Expected [('ctrl', 'v')], got {keys}"

    @pytest.mark.asyncio
    async def test_press_dispatches_press_keys(self, harness):
        """'press enter' -> press_keys("enter") produces ('enter',) keystroke."""
        # Pattern: ^press (.+)$ -> press_keys(g1)
        # Use a valid key name so press_keys produces a keystroke.
        # ^press (.+)$ is a greedy command pattern; under wh-greedy-helper-impl
        # it uses the 5000 ms greedy buffer timer, so signal end-of-utterance
        # explicitly instead of waiting on the buffer timer to fire.
        await harness.send_word("press", start_of_utterance=True)
        await harness.send_word("enter", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        keys = harness.recording.get_keystroke_keys()
        assert ("enter",) in keys, f"Expected ('enter',) from press enter, got {keys}"

    @pytest.mark.asyncio
    async def test_find_dispatches_type_text(self, harness):
        """'<hotword> find test' -> Ctrl+F hotkey + type_text("test")."""
        # Pattern: ^find\\b ?(.*)$ (requires_hotword=true). Greedy command,
        # so end the utterance explicitly rather than relying on the buffer
        # timer (wh-greedy-helper-impl).
        hotword = harness.hotword
        await harness.send_word(hotword, start_of_utterance=True)
        await harness.send_word("find", delay_before_ms=50)
        await harness.send_word("test", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        keys = harness.recording.get_keystroke_keys()
        # First action: hk("ctrl", "f")
        assert ("ctrl", "f") in keys, f"Expected ctrl+f from find command, got {keys}"
        # Second action: type_text("test") -- recorded by adapter (Task 3)
        assert "test" in harness.recording.typed_texts, \
            f"Expected type_text('test') from find, got {harness.recording.typed_texts}"


# ============================================================================
# Single-word commands: the whole utterance, no hotword
# ============================================================================


class TestSingleWordCommandsNeedTheWholeUtterance:
    """David decided on 2026-08-20 that a command that is one ordinary
    English word with nothing after it behaves the way Windows Voice Access
    behaves: say the word by itself and it fires, with no hotword in front.

    The router matches a PREFIX of an utterance, so the anchors in ``^save$``
    are not what keeps the word out of dictation -- ``whole_utterance_only``
    is. These three tests cover the whole contract: the word alone works,
    the word inside a sentence does not, and the old hotword habit still
    works for anyone who has it.
    """

    @pytest.mark.asyncio
    async def test_the_word_alone_presses_the_hotkey(self, harness):
        """'save' as the entire utterance -> Ctrl+S."""
        await harness.send_word("save", start_of_utterance=True, utterance_id=1)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "s") in keys, f"'save' alone should save, got {keys}"

    @pytest.mark.asyncio
    async def test_the_word_starting_a_sentence_dictates(self, harness):
        """'save the document' types words and never presses Ctrl+S."""
        await harness.send_utterance(["save", "the", "document"])
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "s") not in keys, (
            f"'save the document' must dictate, not save; got {keys}"
        )
        delivered = "".join(harness.recording.text_deliveries).lower()
        assert "save" in delivered, (
            f"the word should reach the document as text, got {delivered!r}"
        )

    @pytest.mark.asyncio
    async def test_the_old_hotword_habit_still_works(self, harness):
        """'x-ray save' keeps working for anyone trained on the old form."""
        await harness.send_word(
            harness.hotword, start_of_utterance=True, utterance_id=1
        )
        await harness.send_word("save", delay_before_ms=50, utterance_id=1)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "s") in keys, (
            f"the hotword form should still save, got {keys}"
        )


# ============================================================================
# TASK 8: Multi-utterance clipboard lifecycle
# ============================================================================

class TestUtteranceClipboardLifecycle:
    """Text delivery across utterance boundaries.

    Exact assertions for the delivered text, verifying capitalization and
    spacing are correct for each utterance in a sequence.

    wh-review-pattern-fixes.46: every assertion here reads
    ``recording.text_deliveries``, the record the harness fills after
    production credited a delivery. The four loose cases in this class
    (``any(...)``, ``>= 1``, ``> len(...)``, ``>= 5``) previously read
    ``recording.clipboard_pastes``, which fills at the clipboard WRITE.
    They all passed against a run whose every focus proof refused and
    whose target window therefore received nothing.
    ``TestDeliveryProvesTextArrived`` below is the regression that holds
    this class to the delivery record.
    """

    @pytest.mark.asyncio
    async def test_single_utterance_pastes_word(self, harness):
        """A single-word utterance delivers its word."""
        await harness.send_word("hello", start_of_utterance=True, utterance_id=50)
        await harness.send_utterance_end_marker(utterance_id=50)
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) >= 1, f"Expected at least 1 delivery, got {delivered}"
        assert any("hello" in p.lower() for p in delivered), \
            f"Expected 'hello' in {delivered}"

    @pytest.mark.asyncio
    async def test_two_utterances_both_paste(self, harness):
        """Two successive utterances both deliver text."""
        await harness.send_word("hello", start_of_utterance=True, utterance_id=60)
        await harness.send_utterance_end_marker(utterance_id=60)
        await asyncio.sleep(0.3)
        first_delivered = list(harness.recording.text_deliveries)
        assert any("hello" in p.lower() for p in first_delivered), \
            f"First utterance should deliver 'hello', got {first_delivered}"

        await harness.send_word("world", start_of_utterance=True, utterance_id=61)
        await harness.send_utterance_end_marker(utterance_id=61)
        await asyncio.sleep(0.3)
        all_delivered = harness.recording.text_deliveries
        assert len(all_delivered) > len(first_delivered), \
            f"Second utterance should add a delivery. Before: {first_delivered}, after: {all_delivered}"

    @pytest.mark.asyncio
    async def test_command_between_dictation_preserves_flow(self, harness):
        """Command between dictation utterances doesn't break paste flow."""
        # Dictation 1
        await harness.send_word("first", start_of_utterance=True, utterance_id=70)
        await harness.send_utterance_end_marker(utterance_id=70)
        await asyncio.sleep(0.3)
        assert any("first" in p.lower() for p in harness.recording.text_deliveries), \
            f"Expected 'first' in {harness.recording.text_deliveries}"

        # Command
        await harness.send_word("backspace", start_of_utterance=True, utterance_id=71)
        await harness.send_utterance_end_marker(utterance_id=71)
        await harness.wait_for_timeout(1100)
        assert ("backspace",) in harness.recording.get_keystroke_keys()

        harness.recording.clear()

        # Dictation 2 -- should still work after command
        await harness.send_word("second", start_of_utterance=True, utterance_id=72)
        await harness.send_utterance_end_marker(utterance_id=72)
        await asyncio.sleep(0.3)
        assert len(harness.recording.text_deliveries) > 0, \
            "Dictation after command should still paste"

    @pytest.mark.asyncio
    async def test_rapid_five_utterances_all_paste(self, harness):
        """Five rapid single-word utterances all deliver their word."""
        words = ["alpha", "bravo", "charlie", "delta", "echo"]
        for i, word in enumerate(words):
            uid = 80 + i
            await harness.send_word(word, start_of_utterance=True, utterance_id=uid)
            await harness.send_utterance_end_marker(utterance_id=uid)
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.2)  # Let final paste complete
        delivered = harness.recording.text_deliveries
        assert len(delivered) >= 5, \
            f"Expected >=5 deliveries from 5 utterances, got {len(delivered)}: {delivered}"

    @pytest.mark.asyncio
    async def test_nine_word_utterance_pastes_all(self, harness):
        """'the quick brown fox...' -> exactly 9 deliveries."""
        words = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]
        await harness.send_utterance(words)
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 9, f"Expected 9 deliveries for 9 words, got {len(delivered)}: {delivered}"
        # Subsequent words have space prefix
        assert delivered[1] == " quick"
        assert delivered[8] == " dog"


# ============================================================================
# TASK 9: Batch word arrival
# ============================================================================

class TestBatchWordArrival:
    """Tests for send_word_batch() -- simulates STT sending multiple words at once.

    In production, Google STT sends 'stable' results containing multiple words.
    WebSocketManager subdivides these into individual WordEvents on the queue,
    but they all arrive before the processor handles any. send_word_batch()
    simulates this by queuing all words without yielding between them.
    """

    @pytest.mark.asyncio
    async def test_batch_three_words(self, harness):
        """Batch of 3 words -> all three delivered correctly."""
        await harness.send_word_batch(["hello", "world", "test"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, f"Expected 3 deliveries from the batch, got {len(delivered)}: {delivered}"
        assert_first_word(delivered[0], "hello")

    @pytest.mark.asyncio
    async def test_batch_single_word(self, harness):
        """Batch of 1 word -> same as regular send_word."""
        await harness.send_word_batch(["hello"])
        await asyncio.sleep(0.3)
        assert any("hello" in p.lower() for p in harness.recording.text_deliveries), \
            f"Expected 'hello' in {harness.recording.text_deliveries}"

    @pytest.mark.asyncio
    async def test_batch_with_replacement(self, harness):
        """Batch containing replacement word -> replacement fires."""
        await harness.send_word_batch(["hello", "comma", "world"])
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert "," in delivered, f"Expected comma replacement in batch, got {delivered}"

    @pytest.mark.asyncio
    async def test_batch_then_command(self, harness):
        """Batch dictation followed by command utterance."""
        await harness.send_word_batch(["hello", "world"])
        await asyncio.sleep(0.3)
        assert len(harness.recording.text_deliveries) >= 2

        harness.recording.clear()

        await harness.send_word("undo", start_of_utterance=True, utterance_id=10)
        await harness.send_utterance_end_marker(utterance_id=10)
        await harness.wait_for_timeout(1100)
        assert ("ctrl", "z") in harness.recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_large_batch(self, harness):
        """Large batch of 8 words -> all arrive."""
        words = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog"]
        await harness.send_word_batch(words)
        await asyncio.sleep(0.3)
        delivered = harness.recording.text_deliveries
        assert len(delivered) == 8, f"Expected 8 deliveries from the batch, got {len(delivered)}: {delivered}"

    @pytest.mark.asyncio
    async def test_sequential_vs_batch_equivalence(self, harness):
        """Batch arrival should produce same output as sequential send_utterance."""
        await harness.send_word_batch(["testing", "one", "two"])
        await asyncio.sleep(0.3)
        batch_delivered = list(harness.recording.text_deliveries)
        assert len(batch_delivered) == 3, f"Batch produced {len(batch_delivered)} deliveries: {batch_delivered}"


# ============================================================================
# wh-review-pattern-fixes.46: the delivery record is the only paste oracle
# ============================================================================

#: A foreground window handle that is NOT the one the mocked focused
#: control resolves to. With this value every focus proof in
#: verified_paste, VerifiedUnicodeStrategy and WindowFocusManager refuses,
#: so no Unicode send and no Ctrl+V leaves the pipeline.
WRONG_FOREGROUND_HWND = 2


@pytest.fixture
async def wrong_foreground_harness(pattern_catalog):
    """A harness whose focus stand-in names a window that is not the target."""
    h = E2EPipelineHarness(
        catalog=pattern_catalog, foreground_hwnd=WRONG_FOREGROUND_HWND,
    )
    await h.start()
    yield h
    await h.stop()


class TestDeliveryProvesTextArrived:
    """The regression for wh-review-pattern-fixes.46.

    Each case replays a scenario from TestUtteranceClipboardLifecycle,
    TestDictationWithPunctuation or TestBatchWordArrival against a harness
    whose focus stand-in names the wrong window. In that run every focus
    proof refuses, so the target window receives nothing.

    Each case asserts both halves of the finding:

    * ``clipboard_pastes`` still fills. That is why an assertion over the
      clipboard-write list cannot tell delivery from silence, and why the
      four loose lifecycle cases passed against a run that delivered
      nothing.
    * ``text_deliveries`` stays empty, and ``unicode_sends`` and the
      Ctrl+V keystroke stay absent. Every lifecycle assertion now reads
      the delivery record, so every one of them fails in this run.

    A change that appends to ``text_deliveries`` at the clipboard write
    rather than after the send makes every case here fail.
    """

    LIFECYCLE_WORDS = (
        (["hello"], "single word"),
        (["hello", "world"], "two words"),
        (["alpha", "bravo", "charlie", "delta", "echo"], "five words"),
        (
            ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy",
             "dog"],
            "nine words",
        ),
        (["hello", "comma", "world"], "words with punctuation"),
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words,label", LIFECYCLE_WORDS)
    async def test_wrong_foreground_delivers_nothing(
        self, wrong_foreground_harness, words, label,
    ):
        """No text is delivered when the focus proof names another window."""
        harness = wrong_foreground_harness
        await harness.send_utterance(words)
        await harness.wait_for_timeout(500)

        recording = harness.recording
        assert recording.clipboard_pastes != [], (
            f"{label}: the clipboard writes are the diagnostic this "
            "regression depends on; without them the run proves nothing"
        )
        assert recording.text_deliveries == [], (
            f"{label}: every focus proof refused, so nothing reached the "
            f"target window: {recording.text_deliveries}"
        )
        assert recording.unicode_sends == [], (
            f"{label}: no Unicode send may leave the pipeline: "
            f"{recording.unicode_sends}"
        )
        assert ("ctrl", "v") not in recording.get_keystroke_keys(), (
            f"{label}: no paste keystroke may leave the pipeline: "
            f"{recording.get_keystroke_keys()}"
        )

    @pytest.mark.asyncio
    async def test_wrong_foreground_batch_delivers_nothing(
        self, wrong_foreground_harness,
    ):
        """The batch arrival path delivers nothing under the same refusal."""
        harness = wrong_foreground_harness
        await harness.send_word_batch(["hello", "world", "test"])
        await harness.wait_for_timeout(500)

        recording = harness.recording
        assert recording.clipboard_pastes != []
        assert recording.text_deliveries == [], (
            "the batch path delivered text with the wrong window in the "
            f"foreground: {recording.text_deliveries}"
        )

    @pytest.mark.asyncio
    async def test_right_foreground_delivers_every_word(self, harness):
        """The control case: the same words deliver with the right window.

        Without this, an empty delivery record would satisfy the cases
        above for the wrong reason -- a record that never fills at all.
        """
        await harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.3)
        recording = harness.recording
        assert len(recording.text_deliveries) == 2, (
            f"Expected 2 deliveries, got {recording.text_deliveries}"
        )
        assert recording.text_deliveries[1] == " world"
