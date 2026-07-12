"""Tests for the OS mock infrastructure itself."""
from services.wheelhouse.tests.e2e.os_mocks import Recording, make_mock_context


class TestRecording:
    def test_records_keystrokes(self):
        rec = Recording()
        rec.press_keys("ctrl", "c")
        rec.press_keys("backspace")
        assert rec.get_keystroke_keys() == [("ctrl", "c"), ("backspace",)]

    def test_clear_resets(self):
        rec = Recording()
        rec.press_keys("a")
        rec.clipboard_pastes.append("hello")
        rec.run_programs.append("test.exe")
        rec.typed_texts.append("text")
        rec.clear()
        assert rec.keystrokes == []
        assert rec.clipboard_pastes == []
        assert rec.run_programs == []
        assert rec.typed_texts == []


class TestMockContext:
    def test_default_notepad(self):
        ctx = make_mock_context()
        assert ctx.process_name == "notepad.exe"
        assert not ctx.is_flutter
        assert not ctx.is_terminal

    def test_terminal(self):
        ctx = make_mock_context(is_terminal=True)
        assert ctx.is_terminal
