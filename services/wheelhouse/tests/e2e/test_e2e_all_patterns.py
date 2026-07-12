"""
E2E TESTS FOR ALL PATTERNS (hand-maintained)

These tests feed WordEvents through the full pipeline:
  WordEvent -> SpeechProcessor -> TextParser -> UIActionHandler -> Recording

Validates:
1. Pattern flows through the full pipeline without crashing
2. Command patterns produce keystrokes in the recording
3. Replacement/insert patterns dispatch without error

Originally generated from patterns.toml, but hand-tuned since and now
maintained by hand: greedy patterns signal end-of-utterance explicitly
(wh-greedy-helper-impl), some tests carry strict xfail markers, and unicode
routing was adjusted in place (wh-wxkp). Do NOT regenerate over this file.

For new patterns, draft tests with:
    python tests/speech/generate_smoke_tests.py --e2e
which writes e2e_all_patterns_scaffold.py next to this file (gitignored);
copy the new tests from there and adjust by hand (wh-smoke-generator-crash).
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


class TestE2ECommandPatterns:
    """E2E tests for command patterns (^ anchor)."""

    @pytest.mark.asyncio
    async def test_e2e_cmd_001_zoom_in(self, harness):
        """Pattern: ^zoom in$"""
        await harness.send_word("zoom", start_of_utterance=True)
        await harness.send_word("in", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "+",) in keys, \
            f"Expected ctrl++ in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_002_zoom_out(self, harness):
        """Pattern: ^zoom out$"""
        await harness.send_word("zoom", start_of_utterance=True)
        await harness.send_word("out", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "-",) in keys, \
            f"Expected ctrl+- in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_003_save(self, harness):
        """Pattern: ^save$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("save", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "s",) in keys, \
            f"Expected ctrl+s in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_004_create_tab(self, harness):
        """Pattern: ^create tab$"""
        await harness.send_word("create", start_of_utterance=True)
        await harness.send_word("tab", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "n",) in keys, \
            f"Expected ctrl+n in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_005_create_window(self, harness):
        """Pattern: ^create window$"""
        await harness.send_word("create", start_of_utterance=True)
        await harness.send_word("window", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("shift", "ctrl", "n",) in keys, \
            f"Expected shift+ctrl+n in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_006_close_window(self, harness):
        """Pattern: ^close window$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("close", delay_before_ms=50)
        await harness.send_word("window", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("alt", "f4",) in keys, \
            f"Expected alt+f4 in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_007_desktop(self, harness):
        """Pattern: ^desktop$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("desktop", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("win", "d",) in keys, \
            f"Expected win+d in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_008_maximize(self, harness):
        """Pattern: ^maximize$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("maximize", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("win", "up",) in keys, \
            f"Expected win+up in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_009_minimize(self, harness):
        """Pattern: ^minimize$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("minimize", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("win", "down",) in keys, \
            f"Expected win+down in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_010_Windows_settings(self, harness):
        """Pattern: ^Windows settings$"""
        await harness.send_word("Windows", start_of_utterance=True)
        await harness.send_word("settings", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        assert len(harness.recording.run_programs) > 0, \
            "Expected run_programs recording from: Windows settings"

    @pytest.mark.asyncio
    async def test_e2e_cmd_011_activate_chrome(self, harness):
        """Pattern: ^activates? (.+)$ (greedy command, wh-greedy-helper-impl)."""
        # ^activates? (.+)$ is greedy, so the 5000 ms greedy buffer timer
        # applies. Signal end-of-utterance explicitly instead of waiting on
        # the buffer timer. The action dispatch path is exercised before the
        # post-marker delay returns (smoke check kept as ``pass``).
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("activate", delay_before_ms=50)
        await harness.send_word("chrome", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        # Action type: activate_window - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_012_browser(self, harness):
        """Pattern: ^browser$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("browser", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: activate_window - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_013_notepad(self, harness):
        """Pattern: ^notepad$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("notepad", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: activate_window - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_015_item_5(self, harness):
        """Pattern: ^item (\\d+)$"""
        await harness.send_word("item", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: intelligent_insert_text - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_016_delete_word(self, harness):
        """Pattern: ^delete word$"""
        await harness.send_word("delete", start_of_utterance=True)
        await harness.send_word("word", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("del",) in keys, \
            f"Expected del in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_017_delete_5(self, harness):
        """Pattern: ^delete\\s*(\\d+)?$"""
        await harness.send_word("delete", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("del",)] * 5, \
            f"Expected 5x del, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_018_backspace_5(self, harness):
        """Pattern: ^backspace\\s*(\\d+)?$"""
        await harness.send_word("backspace", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("backspace",)] * 5, \
            f"Expected 5x backspace, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_019_tab_5(self, harness):
        """Pattern: ^(tab|indent)\\s+(\\d+)$"""
        await harness.send_word("tab", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("tab",)] * 5, \
            f"Expected 5x tab, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_020_shift_tab(self, harness):
        """Pattern: ^(shift tab|outdent)$"""
        await harness.send_word("shift", start_of_utterance=True)
        await harness.send_word("tab", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("shift", "tab",) in keys, \
            f"Expected shift+tab in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_021_press_hello_world(self, harness):
        """Pattern: ^press\\s*(.+)$ (greedy command, wh-greedy-helper-impl)."""
        # ^press\s*(.+)$ is greedy, so the 5000 ms greedy buffer timer
        # applies. Signal end-of-utterance explicitly instead of waiting on
        # the buffer timer. press_keys("hello world") is not a real key
        # combo so no keystroke gets recorded, but the action path is
        # exercised before the post-marker delay returns (smoke check).
        await harness.send_word("press", start_of_utterance=True)
        await harness.send_word("hello", delay_before_ms=50)
        await harness.send_word("world", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        # Action type: press_keys - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_022_escape(self, harness):
        """Pattern: ^escape$"""
        await harness.send_word("escape", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert keys == [("esc",)] * 1, \
            f"Expected 1x esc, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_027_undo_5(self, harness):
        """Pattern: ^undo\\s*(\\d+)?$"""
        await harness.send_word("undo", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "z",) in keys, \
            f"Expected ctrl+z in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_028_redo_5(self, harness):
        """Pattern: ^redo\\s*(\\d+)?$"""
        await harness.send_word("redo", start_of_utterance=True)
        await harness.send_word("5", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "y",) in keys, \
            f"Expected ctrl+y in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_029_copy_all(self, harness):
        """Pattern: ^copy all$"""
        await harness.send_word("copy", start_of_utterance=True)
        await harness.send_word("all", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "c",) in keys, \
            f"Expected ctrl+c in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_030_copy_line(self, harness):
        """Pattern: ^copy line$"""
        await harness.send_word("copy", start_of_utterance=True)
        await harness.send_word("line", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("left",) in keys, \
            f"Expected left in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_031_copy_screen(self, harness):
        """Pattern: ^copy screen$"""
        await harness.send_word("copy", start_of_utterance=True)
        await harness.send_word("screen", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("win", "shift", "s",) in keys, \
            f"Expected win+shift+s in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_032_copy(self, harness):
        """Pattern: ^copy$"""
        await harness.send_word("copy", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "c",) in keys, \
            f"Expected ctrl+c in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_033_cut(self, harness):
        """Pattern: ^cut$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("cut", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "x",) in keys, \
            f"Expected ctrl+x in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_034_paste(self, harness):
        """Pattern: ^paste$"""
        await harness.send_word("paste", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "v",) in keys, \
            f"Expected ctrl+v in keystrokes, got {keys}"

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "wh-replace-all-shadowed: ^replace$ EXECUTEs on the first word, "
            "so ^replace all$ never matches in streamed input; 'all' is "
            "dictated. This test previously passed only because the dictated "
            "word's clipboard paste supplied a coincidental ctrl+v keystroke, "
            "which the Unicode insertion path (wh-wxkp) no longer produces. "
            "strict=True so a router/pattern fix flips this to xpass and "
            "forces the marker's removal."
        ),
    )
    async def test_e2e_cmd_035_replace_all(self, harness):
        """Pattern: ^replace all$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("replace", delay_before_ms=50)
        await harness.send_word("all", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "v",) in keys, \
            f"Expected ctrl+v in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_036_select_all(self, harness):
        """Pattern: ^select all$"""
        await harness.send_word("select", start_of_utterance=True)
        await harness.send_word("all", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "a",) in keys, \
            f"Expected ctrl+a in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_037_select_word(self, harness):
        """Pattern: ^select word$"""
        await harness.send_word("select", start_of_utterance=True)
        await harness.send_word("word", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("shift", "ctrl", "right",) in keys, \
            f"Expected shift+ctrl+right in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_038_select_line(self, harness):
        """Pattern: ^select line$"""
        await harness.send_word("select", start_of_utterance=True)
        await harness.send_word("line", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("shift", "end",) in keys, \
            f"Expected shift+end in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_039_select_paragraph(self, harness):
        """Pattern: ^select paragraph$"""
        await harness.send_word("select", start_of_utterance=True)
        await harness.send_word("paragraph", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("shift", "ctrl", "down",) in keys, \
            f"Expected shift+ctrl+down in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_040_find_test(self, harness):
        """Pattern: ^find\\s*(.*)$ (greedy command, wh-greedy-helper-impl)."""
        # ^find\s*(.*)$ is greedy, so the 5000 ms greedy buffer timer
        # applies. Signal end-of-utterance explicitly instead of waiting on
        # the buffer timer. The action path emits ctrl+f then type_text("test");
        # assert both side effects to make sure the dispatch actually
        # happened (mirrors test_find_dispatches_type_text in
        # test_e2e_workflows.py).
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("find", delay_before_ms=50)
        await harness.send_word("test", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "f") in keys, \
            f"Expected ctrl+f from find command, got {keys}"
        assert "test" in harness.recording.typed_texts, \
            f"Expected type_text('test') from find, got {harness.recording.typed_texts}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_041_replace(self, harness):
        """Pattern: ^replace$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("replace", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "h",) in keys, \
            f"Expected ctrl+h in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_042_search(self, harness):
        """Pattern: ^search$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("search", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "c",) in keys, \
            f"Expected ctrl+c in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_043_bold_text(self, harness):
        """Pattern: ^bold text$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("bold", delay_before_ms=50)
        await harness.send_word("text", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "b",) in keys, \
            f"Expected ctrl+b in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_044_italics(self, harness):
        """Pattern: ^italics$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("italics", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "i",) in keys, \
            f"Expected ctrl+i in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_045_underline(self, harness):
        """Pattern: ^underline$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("underline", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "u",) in keys, \
            f"Expected ctrl+u in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_046_uppercase(self, harness):
        """Pattern: ^uppercase$"""
        await harness.send_word("uppercase", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_047_lowercase(self, harness):
        """Pattern: ^lowercase$"""
        await harness.send_word("lowercase", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_048_capitalize(self, harness):
        """Pattern: ^capitalize$"""
        await harness.send_word("capitalize", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_049_title_case(self, harness):
        """Pattern: ^title case$"""
        await harness.send_word("title", start_of_utterance=True)
        await harness.send_word("case", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_050_snake_case(self, harness):
        """Pattern: ^snake case$"""
        await harness.send_word("snake", start_of_utterance=True)
        await harness.send_word("case", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_051_camel_case(self, harness):
        """Pattern: ^camel case$"""
        await harness.send_word("camel", start_of_utterance=True)
        await harness.send_word("case", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_052_pascal_case(self, harness):
        """Pattern: ^pascal case$"""
        await harness.send_word("pascal", start_of_utterance=True)
        await harness.send_word("case", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_053_kebab_case(self, harness):
        """Pattern: ^kebab case$"""
        await harness.send_word("kebab", start_of_utterance=True)
        await harness.send_word("case", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_054_compress(self, harness):
        """Pattern: ^compress$"""
        await harness.send_word("compress", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        # Action type: transform_selection - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_055_boost(self, harness):
        """Pattern: ^boost$"""
        await harness.send_word(harness.hotword, start_of_utterance=True)
        await harness.send_word("boost", delay_before_ms=50)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "c",) in keys, \
            f"Expected ctrl+c in keystrokes, got {keys}"

    @pytest.mark.asyncio
    async def test_e2e_cmd_065_ok_Google(self, harness):
        """Pattern: ^ok Google.*$ (greedy replacement, wh-greedy-helper-impl)."""
        # ^ok Google.*$ is greedy, so the 5000 ms greedy buffer timer
        # applies. Signal end-of-utterance explicitly instead of waiting on
        # the buffer timer. The action is text("") -- the recording
        # captures it as an empty typed_text entry (or nothing, depending
        # on the strategy), so the assertion below just keeps the smoke
        # check while ensuring the action path is exercised.
        await harness.send_word("ok", start_of_utterance=True)
        await harness.send_word("Google.*", delay_before_ms=50)
        await harness.send_utterance_end_marker(utterance_id=1)
        await harness.wait_for_timeout(200)
        # Action type: intelligent_insert_text - verify no crash
        pass

    @pytest.mark.asyncio
    async def test_e2e_cmd_077_play(self, harness):
        """Pattern: ^play\\b"""
        await harness.send_word("play", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        # Action type: intelligent_insert_text - verify no crash
        pass


class TestE2EReplacementPatterns:
    """E2E tests for replacement patterns (no ^ anchor).

    Replacements trigger mid-dictation. We send them as part of a
    dictation utterance and verify no crash through the full pipeline.
    """

    @pytest.mark.asyncio
    async def test_e2e_repl_000_literal_hello_world(self, harness):
        """Pattern: literal (.+)$"""
        await harness.send_utterance(["literal", "hello", "world"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_023_new_line(self, harness):
        """Pattern: (?<!\\s)new\\s+line\\b"""
        await harness.send_utterance(["new", "line"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_024_new_line(self, harness):
        """Pattern: (?<=\\s)new\\s+line\\b"""
        await harness.send_utterance(["new", "line"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_025_new_paragraph(self, harness):
        """Pattern: (?<!\\s)new\\s+paragraph\\b"""
        await harness.send_utterance(["new", "paragraph"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_026_new_paragraph(self, harness):
        """Pattern: (?<=\\s)new\\s+paragraph\\b"""
        await harness.send_utterance(["new", "paragraph"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_056_parenthesestest(self, harness):
        """Pattern: \\bparentheses(.*)$"""
        await harness.send_utterance(["parenthesestest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_057_angle_bracketstest(self, harness):
        """Pattern: \\bangle brackets(.*)$"""
        await harness.send_utterance(["angle", "bracketstest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_058_bracketstest(self, harness):
        """Pattern: \\bbrackets(.*)$"""
        await harness.send_utterance(["bracketstest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_059_bracestest(self, harness):
        """Pattern: \\bbraces(.*)$"""
        await harness.send_utterance(["bracestest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_060_single_quotestest(self, harness):
        """Pattern: \\bsingle quotes?(.*)$"""
        await harness.send_utterance(["single", "quotestest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_061_quotestest(self, harness):
        """Pattern: \\bquotes?(.*)$"""
        await harness.send_utterance(["quotestest"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_066_period(self, harness):
        """Pattern: \\bperiod\\b"""
        await harness.send_utterance(["period"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_067_comma(self, harness):
        """Pattern: \\bcomma\\b"""
        await harness.send_utterance(["comma"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_069_colon(self, harness):
        """Pattern: \\bcolon\\b"""
        await harness.send_utterance(["colon"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_070_semicolon(self, harness):
        """Pattern: \\bsemicolon\\b"""
        await harness.send_utterance(["semicolon"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_071_question_mark(self, harness):
        """Pattern: \\bquestion mark\\b"""
        await harness.send_utterance(["question", "mark"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_072_exclamation_point(self, harness):
        """Pattern: \\bexclamation (?:point|mark)\\b"""
        await harness.send_utterance(["exclamation", "point"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_073_hyphen(self, harness):
        """Pattern: \\bhyphen\\b"""
        await harness.send_utterance(["hyphen"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_074_dash(self, harness):
        """Pattern: \\bdash\\b"""
        await harness.send_utterance(["dash"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_075_space_bar(self, harness):
        """Pattern: \\bspace bar\\b"""
        await harness.send_utterance(["space", "bar"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

    @pytest.mark.asyncio
    async def test_e2e_repl_076_spacebar(self, harness):
        """Pattern: \\bspacebar\\b"""
        await harness.send_utterance(["spacebar"])
        await asyncio.sleep(0.1)
        # Replacement pattern - verify no crash through full pipeline

