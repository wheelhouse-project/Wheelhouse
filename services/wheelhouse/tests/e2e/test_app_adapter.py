"""Tests for AppAdapter dispatch logic."""
import pytest
from unittest.mock import MagicMock, patch
from services.wheelhouse.tests.e2e.app_adapter import AppAdapter
from services.wheelhouse.tests.e2e.os_mocks import Recording, make_mock_context


class TestAppAdapterDispatch:
    """Verify AppAdapter routes action dicts to UIActionHandler methods."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_press_key_dispatches(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "press_key_action",
            "params": {"key": "backspace", "repeat": 1}
        })
        assert len(recording.keystrokes) >= 1
        assert recording.get_keystroke_keys()[0] == ("backspace",)

    @pytest.mark.asyncio
    async def test_hotkey_dispatches(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "hotkey_action",
            "params": {"keys": ["ctrl", "z"], "repeat": 1}
        })
        assert len(recording.keystrokes) >= 1
        assert recording.get_keystroke_keys()[0] == ("ctrl", "z")

    @pytest.mark.asyncio
    async def test_send_request_returns_response(self, setup):
        adapter, recording = setup
        result = await adapter.send_request("press_key_action", {"key": "enter", "repeat": 1})
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_action_does_not_crash(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "nonexistent_action",
            "params": {}
        })
        # No exception raised


class TestSubprocessInterception:
    """Verify subprocess.Popen is intercepted and recorded."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_run_program_records_command(self, setup):
        """run_program() should record the command instead of executing it."""
        from services.wheelhouse.speech.actions import subprocess
        # subprocess.Popen is patched -- calling it records to run_programs
        subprocess.Popen("explorer.exe ms-settings:", shell=True)
        _, recording = setup
        assert "explorer.exe ms-settings:" in recording.run_programs

    @pytest.mark.asyncio
    async def test_run_programs_starts_empty(self, setup):
        _, recording = setup
        assert recording.run_programs == []

    @pytest.mark.asyncio
    async def test_clear_resets_run_programs(self, setup):
        _, recording = setup
        recording.run_programs.append("test.exe")
        recording.clear()
        assert recording.run_programs == []


class TestUnicodeRouting:
    """wh-wxkp: short text routes to VerifiedUnicodeStrategy end-to-end.

    The harness previously forced ui_actions.verified_unicode.max_chars=0
    so every insertion fell through to the clipboard path. With the UIA
    shadow-buffer surface and the Unicode Win32 boundary mocked, the
    production default (50 chars) is active and short text must deliver
    via type_string_verified, not clipboard paste.
    """

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_short_text_routes_to_unicode(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "hello"},
        })
        # Perfected against the empty mock shadow buffer: capitalized,
        # no leading space, delivered via SendInput.
        assert recording.unicode_sends == ["Hello"]
        # Assertion-compatibility mirror: the send also lands in
        # clipboard_pastes so pre-Unicode e2e assertions keep passing.
        assert recording.clipboard_pastes == ["Hello"]
        # No paste keystroke fired -- delivery was Unicode SendInput.
        assert ("ctrl", "v") not in recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_unicode_output_matches_clipboard_path_composition(self, setup):
        """Streamed words compose identically to the old clipboard path."""
        adapter, recording = setup
        for word in ["hello", "world"]:
            await adapter.send_command({
                "action": "intelligent_insert_text",
                "params": {"insertion_string": word},
            })
        # Same observable text output the clipboard path produced:
        # first word capitalized, second word space-prefixed lowercase.
        assert recording.clipboard_pastes == ["Hello", " world"]
        assert recording.unicode_sends == ["Hello", " world"]

    @pytest.mark.asyncio
    async def test_long_text_still_routes_to_clipboard(self, setup):
        adapter, recording = setup
        long_text = "this is a deliberately long dictated phrase that exceeds fifty characters"
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": long_text},
        })
        assert recording.unicode_sends == []
        assert len(recording.clipboard_pastes) == 1
        assert ("ctrl", "v") in recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_clear_resets_unicode_sends(self, setup):
        _, recording = setup
        recording.unicode_sends.append("test")
        recording.clear()
        assert recording.unicode_sends == []


class TestTypeTextRecording:
    """Verify type_text actions dispatch through handler and record via type_string mock."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_type_text_recorded(self, setup):
        adapter, recording = setup
        await adapter.send_command({"action": "type_text", "params": {"text": "hello world"}})
        assert recording.typed_texts == ["hello world"]

    @pytest.mark.asyncio
    async def test_type_text_uses_type_string_not_clipboard(self, setup):
        """type_text should use raw keystrokes (type_string), not clipboard paste."""
        adapter, recording = setup
        await adapter.send_command({"action": "type_text", "params": {"text": "test"}})
        assert recording.typed_texts == ["test"]
        assert len(recording.keystrokes) == 0
        assert len(recording.clipboard_pastes) == 0

    @pytest.mark.asyncio
    async def test_typed_texts_starts_empty(self, setup):
        _, recording = setup
        assert recording.typed_texts == []

    @pytest.mark.asyncio
    async def test_clear_resets_typed_texts(self, setup):
        _, recording = setup
        recording.typed_texts.append("test")
        recording.clear()
        assert recording.typed_texts == []
