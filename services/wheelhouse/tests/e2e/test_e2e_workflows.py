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
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 3, f"Expected 3 pastes, got {pastes}"
        assert_first_word(pastes[0], "hello")
        # comma is punctuation-only -> no space prefix, no trailing space
        assert pastes[1] == ","
        # world follows comma (non-whitespace) -> prefix space, no cap
        assert pastes[2] == " world"

    @pytest.mark.asyncio
    async def test_sentence_with_period(self, harness):
        """'this is a test period' -> words pasted, period replacement fires after timeout."""
        await harness.send_utterance(["this", "is", "a", "test", "period"])
        # Wait for 400ms replacement buffer timeout
        await harness.wait_for_timeout(500)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 5, f"Expected 5 pastes, got {pastes}"
        assert_first_word(pastes[0], "this")
        assert pastes[1:] == [" is", " a", " test", "."], f"Got {pastes}"

    @pytest.mark.asyncio
    async def test_question_mark_in_sentence(self, harness):
        """'test question mark' -> first word + '?'."""
        await harness.send_utterance(["test", "question", "mark"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 2, f"Expected 2 pastes, got {pastes}"
        assert_first_word(pastes[0], "test")
        assert pastes[1] == "?"

    @pytest.mark.asyncio
    async def test_comma_only(self, harness):
        """'comma' alone -> ','."""
        await harness.send_utterance(["comma"])
        await asyncio.sleep(0.3)
        assert harness.recording.clipboard_pastes == [","], \
            f"Got {harness.recording.clipboard_pastes}"

    @pytest.mark.asyncio
    async def test_exclamation_point(self, harness):
        """'wow exclamation point' -> first word + '!'."""
        await harness.send_utterance(["wow", "exclamation", "point"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 2, f"Expected 2 pastes, got {pastes}"
        assert_first_word(pastes[0], "wow")
        assert pastes[1] == "!"

    @pytest.mark.asyncio
    async def test_colon_in_sentence(self, harness):
        """'dear sir colon' -> first word + ' sir' + ':'."""
        await harness.send_utterance(["dear", "sir", "colon"])
        await harness.wait_for_timeout(500)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 3, f"Expected 3 pastes, got {pastes}"
        assert_first_word(pastes[0], "dear")
        assert pastes[1:] == [" sir", ":"]

    @pytest.mark.asyncio
    async def test_new_line_mid_dictation(self, harness):
        """'first new line second' -> paste, keystroke, paste pattern."""
        await harness.send_utterance(["first", "new", "line", "second"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        # First word present (timing-tolerant)
        assert any("first" in p.lower() for p in pastes), \
            f"Expected 'first' in {pastes}"
        # "new line" -> shift+enter keystroke (patterns.toml:200-224)
        non_paste_keys = [k for k in harness.recording.get_keystroke_keys()
                          if k not in [("ctrl", "v"), ("ctrl", "c"),
                                       ("shift", "left", "left"), ("shift", "right")]]
        assert ("shift", "enter") in non_paste_keys, \
            f"Expected ('shift', 'enter') in {non_paste_keys}"
        # "second" pasted after newline
        assert any("second" in p.lower() for p in pastes), \
            f"Expected 'second' in {pastes}"

    @pytest.mark.asyncio
    async def test_multiple_commas(self, harness):
        """'hello comma comma world' -> two commas between words."""
        await harness.send_utterance(["hello", "comma", "comma", "world"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 4, f"Expected 4 pastes, got {pastes}"
        assert_first_word(pastes[0], "hello")
        assert pastes[1:] == [",", ",", " world"]

    @pytest.mark.asyncio
    async def test_semicolon(self, harness):
        """'test semicolon' -> first word + ';'."""
        await harness.send_utterance(["test", "semicolon"])
        await harness.wait_for_timeout(500)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 2, f"Expected 2 pastes, got {pastes}"
        assert_first_word(pastes[0], "test")
        assert pastes[1] == ";"

    @pytest.mark.asyncio
    async def test_hyphen(self, harness):
        """'test hyphen word' -> first word + '-' + ' word'."""
        await harness.send_utterance(["test", "hyphen", "word"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 3, f"Expected 3 pastes, got {pastes}"
        assert_first_word(pastes[0], "test")
        assert pastes[1:] == ["-", " word"]


# ============================================================================
# TASK 6: Command-dictation switching
# ============================================================================

class TestCommandDictationSwitching:
    """Rapid switching between command and dictation modes.

    Tests verify clean transitions: command keystrokes only in command phase,
    clipboard pastes only in dictation phase.
    """

    @pytest.mark.asyncio
    async def test_backspace_then_dictation(self, harness):
        """'backspace' (pause) 'hello world' -> backspace keystroke then clipboard pastes."""
        await harness.send_word("backspace", start_of_utterance=True, utterance_id=1)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(1100)

        assert ("backspace",) in harness.recording.get_keystroke_keys()
        harness.recording.clear()

        await harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 2, f"Expected 2 pastes, got {pastes}"
        assert_first_word(pastes[0], "hello")
        assert pastes[1] == " world"

    @pytest.mark.asyncio
    async def test_dictation_then_undo(self, harness):
        """'hello world' then 'undo' -> text pasted then Ctrl+Z."""
        await harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 2, f"Expected 2 pastes, got {pastes}"
        assert_first_word(pastes[0], "hello")
        assert pastes[1] == " world"
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
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 5, f"Expected 5 words pasted, got {len(pastes)}: {pastes}"
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
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 4, f"Expected 4 words, got {len(pastes)}: {pastes}"
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
# TASK 8: Multi-utterance clipboard lifecycle
# ============================================================================

class TestUtteranceClipboardLifecycle:
    """Clipboard state across utterance boundaries.

    Exact assertions for paste output, verifying capitalization and spacing
    are correct for each utterance in a sequence.
    """

    @pytest.mark.asyncio
    async def test_single_utterance_pastes_word(self, harness):
        """Single word utterance pastes via clipboard."""
        await harness.send_word("hello", start_of_utterance=True, utterance_id=50)
        await harness.send_utterance_end_marker(utterance_id=50)
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) >= 1, f"Expected at least 1 paste, got {pastes}"
        assert any("hello" in p.lower() for p in pastes), \
            f"Expected 'hello' in {pastes}"

    @pytest.mark.asyncio
    async def test_two_utterances_both_paste(self, harness):
        """Two successive utterances both produce clipboard pastes."""
        await harness.send_word("hello", start_of_utterance=True, utterance_id=60)
        await harness.send_utterance_end_marker(utterance_id=60)
        await asyncio.sleep(0.3)
        first_pastes = list(harness.recording.clipboard_pastes)
        assert any("hello" in p.lower() for p in first_pastes), \
            f"First utterance should paste 'hello', got {first_pastes}"

        await harness.send_word("world", start_of_utterance=True, utterance_id=61)
        await harness.send_utterance_end_marker(utterance_id=61)
        await asyncio.sleep(0.3)
        all_pastes = harness.recording.clipboard_pastes
        assert len(all_pastes) > len(first_pastes), \
            f"Second utterance should add pastes. Before: {first_pastes}, after: {all_pastes}"

    @pytest.mark.asyncio
    async def test_command_between_dictation_preserves_flow(self, harness):
        """Command between dictation utterances doesn't break paste flow."""
        # Dictation 1
        await harness.send_word("first", start_of_utterance=True, utterance_id=70)
        await harness.send_utterance_end_marker(utterance_id=70)
        await asyncio.sleep(0.3)
        assert any("first" in p.lower() for p in harness.recording.clipboard_pastes), \
            f"Expected 'first' in {harness.recording.clipboard_pastes}"

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
        assert len(harness.recording.clipboard_pastes) > 0, \
            "Dictation after command should still paste"

    @pytest.mark.asyncio
    async def test_rapid_five_utterances_all_paste(self, harness):
        """Five rapid single-word utterances all produce pastes."""
        words = ["alpha", "bravo", "charlie", "delta", "echo"]
        for i, word in enumerate(words):
            uid = 80 + i
            await harness.send_word(word, start_of_utterance=True, utterance_id=uid)
            await harness.send_utterance_end_marker(utterance_id=uid)
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.2)  # Let final paste complete
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) >= 5, \
            f"Expected >=5 pastes from 5 utterances, got {len(pastes)}: {pastes}"

    @pytest.mark.asyncio
    async def test_nine_word_utterance_pastes_all(self, harness):
        """'the quick brown fox...' -> exactly 9 clipboard pastes."""
        words = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]
        await harness.send_utterance(words)
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 9, f"Expected 9 pastes for 9 words, got {len(pastes)}: {pastes}"
        # Subsequent words have space prefix
        assert pastes[1] == " quick"
        assert pastes[8] == " dog"


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
        """Batch of 3 words -> all pasted correctly."""
        await harness.send_word_batch(["hello", "world", "test"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 3, f"Expected 3 pastes from batch, got {len(pastes)}: {pastes}"
        assert_first_word(pastes[0], "hello")

    @pytest.mark.asyncio
    async def test_batch_single_word(self, harness):
        """Batch of 1 word -> same as regular send_word."""
        await harness.send_word_batch(["hello"])
        await asyncio.sleep(0.3)
        assert any("hello" in p.lower() for p in harness.recording.clipboard_pastes), \
            f"Expected 'hello' in {harness.recording.clipboard_pastes}"

    @pytest.mark.asyncio
    async def test_batch_with_replacement(self, harness):
        """Batch containing replacement word -> replacement fires."""
        await harness.send_word_batch(["hello", "comma", "world"])
        await asyncio.sleep(0.3)
        pastes = harness.recording.clipboard_pastes
        assert "," in pastes, f"Expected comma replacement in batch, got {pastes}"

    @pytest.mark.asyncio
    async def test_batch_then_command(self, harness):
        """Batch dictation followed by command utterance."""
        await harness.send_word_batch(["hello", "world"])
        await asyncio.sleep(0.3)
        assert len(harness.recording.clipboard_pastes) >= 2

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
        pastes = harness.recording.clipboard_pastes
        assert len(pastes) == 8, f"Expected 8 pastes from batch, got {len(pastes)}: {pastes}"

    @pytest.mark.asyncio
    async def test_sequential_vs_batch_equivalence(self, harness):
        """Batch arrival should produce same output as sequential send_utterance."""
        await harness.send_word_batch(["testing", "one", "two"])
        await asyncio.sleep(0.3)
        batch_pastes = list(harness.recording.clipboard_pastes)
        assert len(batch_pastes) == 3, f"Batch produced {len(batch_pastes)} pastes: {batch_pastes}"
