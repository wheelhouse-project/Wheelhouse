"""Smoke tests for the E2E pipeline harness itself."""
import asyncio
import pytest
from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


class TestE2EHarnessBasics:
    """Verify the harness wires up correctly."""

    @pytest.fixture
    async def harness(self, pattern_catalog):
        h = E2EPipelineHarness(catalog=pattern_catalog)
        await h.start()
        yield h
        await h.stop()

    @pytest.mark.asyncio
    async def test_dictation_reaches_handler(self, harness):
        """A plain word should flow through to intelligent_insert_text without crash."""
        await harness.send_word("hello", start_of_utterance=True)
        await asyncio.sleep(0.1)
        assert harness.recording is not None

    @pytest.mark.asyncio
    async def test_command_reaches_handler(self, harness):
        """'backspace' should produce a press_key_action keystroke."""
        await harness.send_word("backspace", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert any("backspace" in str(k) for k in keys), f"Expected backspace keystroke, got {keys}"

    @pytest.mark.asyncio
    async def test_hotkey_command(self, harness):
        """'undo' should produce a Ctrl+Z hotkey."""
        await harness.send_word("undo", start_of_utterance=True)
        await harness.wait_for_timeout(1100)
        keys = harness.recording.get_keystroke_keys()
        assert any("ctrl" in str(k) and "z" in str(k) for k in keys), \
            f"Expected ctrl+z keystroke, got {keys}"

    @pytest.mark.asyncio
    async def test_harness_stop_cleans_up(self, harness):
        """Harness stop should not raise."""
        # Just verifying the fixture teardown works
        await harness.send_word("test", start_of_utterance=True)
        await asyncio.sleep(0.05)
