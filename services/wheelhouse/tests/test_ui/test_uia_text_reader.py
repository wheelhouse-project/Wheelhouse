"""Tests for UIA text reader - TextPattern/ValuePattern context reading.

Covers:
- read_context_via_text_pattern() - Fast context from TextPattern
- read_value_pattern_text() - Full text from ValuePattern
- Fallbacks: no control, no pattern, COM errors
"""
import pytest
from unittest.mock import MagicMock, patch
import _ctypes

_MOD = "ui.uia_text_reader"


def _mock_uia_init(mock_auto):
    """Set up UIAutomationInitializerInThread as a working context manager."""
    mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
    mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)
    mock_auto.TextPatternRangeEndpoint.End = "End"
    mock_auto.TextPatternRangeEndpoint.Start = "Start"


def _setup_text_pattern_mock(mock_auto, text_before_caret="hello ab", selection_text=""):
    """Configure mock UIA for TextPattern with given text state.

    text_before_caret: full text from doc start to caret position.
        The reader takes the last max_chars (default 2) from this.
    selection_text: text currently selected (empty = no selection).
    """
    _mock_uia_init(mock_auto)

    focused = MagicMock()
    mock_auto.GetFocusedControl.return_value = focused

    text_pattern = MagicMock()
    focused.GetPattern.return_value = text_pattern

    sel_range = MagicMock()
    sel_range.GetText.return_value = selection_text
    text_pattern.GetSelection.return_value = [sel_range]

    doc_range = MagicMock()
    text_pattern.DocumentRange = doc_range
    pre_range = MagicMock()
    doc_range.Clone.return_value = pre_range
    pre_range.GetText.return_value = text_before_caret

    return focused, text_pattern


class TestReadContextViaTextPattern:
    """read_context_via_text_pattern() reads caret context using UIA."""

    @patch(f"{_MOD}.auto")
    def test_returns_preceding_chars(self, mock_auto):
        """Should return last 2 chars before caret from TextPattern."""
        _setup_text_pattern_mock(mock_auto, text_before_caret="hello ab")
        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern()
        assert result is not None
        assert result['preceding_chars'] == "ab"

    @patch(f"{_MOD}.auto")
    def test_returns_single_char_near_doc_start(self, mock_auto):
        """Should return available chars when fewer than max_chars exist."""
        _setup_text_pattern_mock(mock_auto, text_before_caret="x")
        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern()
        assert result['preceding_chars'] == "x"

    @patch(f"{_MOD}.auto")
    def test_returns_empty_at_document_beginning(self, mock_auto):
        """Should return empty string when caret is at document start."""
        _setup_text_pattern_mock(mock_auto, text_before_caret="")
        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern()
        assert result['preceding_chars'] == ""

    @patch(f"{_MOD}.auto")
    def test_detects_no_selection(self, mock_auto):
        """Should report has_selection=False when nothing selected."""
        _setup_text_pattern_mock(mock_auto, selection_text="")
        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern()
        assert result['has_selection'] is False

    @patch(f"{_MOD}.auto")
    def test_detects_active_selection(self, mock_auto):
        """Should report has_selection=True when text is selected."""
        _setup_text_pattern_mock(mock_auto, selection_text="selected text")
        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern()
        assert result['has_selection'] is True

    @patch(f"{_MOD}.auto")
    def test_returns_none_when_no_focused_control(self, mock_auto):
        """Should return None when no control is focused."""
        _mock_uia_init(mock_auto)
        mock_auto.GetFocusedControl.return_value = None
        from ui.uia_text_reader import read_context_via_text_pattern
        assert read_context_via_text_pattern() is None

    @patch(f"{_MOD}.auto")
    def test_returns_none_when_no_text_pattern(self, mock_auto):
        """Should return None when control doesn't support TextPattern."""
        _mock_uia_init(mock_auto)
        focused = MagicMock()
        focused.GetPattern.return_value = None
        mock_auto.GetFocusedControl.return_value = focused
        from ui.uia_text_reader import read_context_via_text_pattern
        assert read_context_via_text_pattern() is None

    @patch(f"{_MOD}.auto")
    def test_returns_none_on_com_error(self, mock_auto):
        """Should return None gracefully on COM error."""
        _mock_uia_init(mock_auto)
        mock_auto.GetFocusedControl.side_effect = _ctypes.COMError(-2147220991, "test", ())
        from ui.uia_text_reader import read_context_via_text_pattern
        assert read_context_via_text_pattern() is None

    @patch(f"{_MOD}.auto")
    def test_uses_provided_focused_control(self, mock_auto):
        """Should use provided control instead of calling GetFocusedControl."""
        _mock_uia_init(mock_auto)
        provided_control = MagicMock()
        text_pattern = MagicMock()
        provided_control.GetPattern.return_value = text_pattern

        sel_range = MagicMock()
        sel_range.GetText.return_value = ""
        text_pattern.GetSelection.return_value = [sel_range]

        doc_range = MagicMock()
        text_pattern.DocumentRange = doc_range
        pre_range = MagicMock()
        doc_range.Clone.return_value = pre_range
        pre_range.GetText.return_value = "hi"

        from ui.uia_text_reader import read_context_via_text_pattern
        result = read_context_via_text_pattern(focused_control=provided_control)
        assert result is not None
        mock_auto.GetFocusedControl.assert_not_called()

    @patch(f"{_MOD}.auto")
    def test_returns_none_on_empty_selection_array(self, mock_auto):
        """Should return None when GetSelection returns empty array."""
        _mock_uia_init(mock_auto)
        focused = MagicMock()
        mock_auto.GetFocusedControl.return_value = focused
        text_pattern = MagicMock()
        focused.GetPattern.return_value = text_pattern
        text_pattern.GetSelection.return_value = []
        from ui.uia_text_reader import read_context_via_text_pattern
        assert read_context_via_text_pattern() is None


class TestReadValuePatternText:
    """read_value_pattern_text() reads full text via ValuePattern."""

    @patch(f"{_MOD}.auto")
    def test_returns_control_text(self, mock_auto):
        """Should return the Value property from ValuePattern."""
        _mock_uia_init(mock_auto)
        focused = MagicMock()
        mock_auto.GetFocusedControl.return_value = focused
        value_pattern = MagicMock()
        value_pattern.Value = "hello world"
        focused.GetPattern.return_value = value_pattern
        from ui.uia_text_reader import read_value_pattern_text
        assert read_value_pattern_text() == "hello world"

    @patch(f"{_MOD}.auto")
    def test_returns_none_when_no_value_pattern(self, mock_auto):
        """Should return None when control doesn't support ValuePattern."""
        _mock_uia_init(mock_auto)
        focused = MagicMock()
        focused.GetPattern.return_value = None
        mock_auto.GetFocusedControl.return_value = focused
        from ui.uia_text_reader import read_value_pattern_text
        assert read_value_pattern_text() is None

    @patch(f"{_MOD}.auto")
    def test_returns_none_when_no_focused_control(self, mock_auto):
        """Should return None when nothing is focused."""
        _mock_uia_init(mock_auto)
        mock_auto.GetFocusedControl.return_value = None
        from ui.uia_text_reader import read_value_pattern_text
        assert read_value_pattern_text() is None

    @patch(f"{_MOD}.auto")
    def test_returns_none_on_com_error(self, mock_auto):
        """Should handle COM errors gracefully."""
        _mock_uia_init(mock_auto)
        mock_auto.GetFocusedControl.side_effect = _ctypes.COMError(-2147220991, "test", ())
        from ui.uia_text_reader import read_value_pattern_text
        assert read_value_pattern_text() is None

    @patch(f"{_MOD}.auto")
    def test_uses_provided_focused_control(self, mock_auto):
        """Should use provided control instead of calling GetFocusedControl."""
        _mock_uia_init(mock_auto)
        provided_control = MagicMock()
        value_pattern = MagicMock()
        value_pattern.Value = "test text"
        provided_control.GetPattern.return_value = value_pattern
        from ui.uia_text_reader import read_value_pattern_text
        assert read_value_pattern_text(focused_control=provided_control) == "test text"
        mock_auto.GetFocusedControl.assert_not_called()


# ---------------------------------------------------------------------------
# TextPattern2 fast-path caret read (wh-uia-caret-fastpath-dead)
#
# The fast path must call GetCaretRange on the RAW comtypes pointer
# (pattern2.pattern) and drive MoveEndpointByRange over the RAW document range
# (doc_range.textRange). Calling either on the uiautomation wrapper is the bug
# this guards: the wrapper has no GetCaretRange (AttributeError, swallowed ->
# None -> silent ~500ms legacy fallback), and the wrapper's MoveEndpointByRange
# is the ~500ms cost the fast path exists to avoid.
# shadow_buffer.py:_get_cursor_pos_fast is the correct reference.
#
# These fakes deliberately do NOT mock `auto`: the fast path needs the real
# integer PatternId constants -- a fully mocked uiautomation makes
# _has_text_pattern2 return False and skips the branch, which is exactly why
# the wrapper bug went unnoticed by the mock-based tests above.
# ---------------------------------------------------------------------------
import ui.uia_text_reader as _reader_mod


class _FakeRawRange:
    """A raw comtypes text range: Clone / MoveEndpointByRange / GetText."""

    def __init__(self, text, calls, label):
        self._text = text
        self._calls = calls
        self._label = label

    def Clone(self):
        self._calls.append(f"{self._label}.Clone")
        return _FakeRawRange(self._text, self._calls, self._label)

    def MoveEndpointByRange(self, endpoint, other_range, other_endpoint):
        # The real fast path narrows to text before the caret; the fake keeps
        # its text so the test can assert which range object was walked.
        pass

    def GetText(self, max_length):
        return self._text


class _FakeRawTP2:
    """Raw comtypes TextPattern2 pointer: exposes GetCaretRange."""

    def __init__(self, caret_range, calls):
        self._caret_range = caret_range
        self._calls = calls

    def GetCaretRange(self):
        self._calls.append("raw.GetCaretRange")
        return (True, self._caret_range)


class _FakeWrapperTP2:
    """uiautomation TextPattern2 wrapper.

    Mirrors reality: the wrapper does NOT expose GetCaretRange; only the raw
    comtypes pointer under ``.pattern`` does. The production failure is exactly
    "'TextPattern2' object has no attribute 'GetCaretRange'".
    """

    def __init__(self, raw):
        self.pattern = raw


class _FakeDocRangeWrapper:
    """uiautomation document-range wrapper.

    Has BOTH ``.textRange`` (the raw comtypes pointer) and a ``.Clone()`` --
    the real wrapper has Clone too, but using it is the ~500ms slow path.
    Cloning here records "wrapper.Clone" and returns range text that differs
    from the raw text, so the test can prove the fast path unwrapped to the
    raw range instead of walking the wrapper.
    """

    def __init__(self, raw_doc, calls):
        self.textRange = raw_doc
        self._calls = calls

    def Clone(self):
        self._calls.append("wrapper.Clone")
        return _FakeRawRange("SLOW_WRAPPER_PATH", self._calls, "wrapper")


class _FakeTextPattern:
    def __init__(self, doc_text, calls):
        self.DocumentRange = _FakeDocRangeWrapper(
            _FakeRawRange(doc_text, calls, "raw"), calls
        )

    def GetSelection(self):
        return []


class _FakeControlTP2:
    def __init__(self, tp2_wrapper, text_pattern):
        self._tp2 = tp2_wrapper
        self._text_pattern = text_pattern

    def GetPattern(self, pattern_id):
        if pattern_id == _reader_mod.auto.PatternId.TextPattern2:
            return self._tp2
        if pattern_id == _reader_mod.auto.PatternId.TextPattern:
            return self._text_pattern
        return None


class TestReadViaTextPattern2FastPath:
    """_read_via_text_pattern2 must use raw comtypes pointers, not wrappers."""

    def _build(self):
        calls = []
        raw_tp2 = _FakeRawTP2(
            caret_range=_FakeRawRange("", calls, "caret"), calls=calls
        )
        ctrl = _FakeControlTP2(
            _FakeWrapperTP2(raw_tp2),
            _FakeTextPattern("hello world", calls),
        )
        return ctrl, calls

    def test_reads_caret_via_raw_pointer_not_wrapper(self):
        ctrl, calls = self._build()
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result is not None, (
            "fast path returned None -- it called GetCaretRange on the "
            "uiautomation wrapper (which has no such method) instead of the "
            "raw comtypes pointer"
        )
        assert "raw.GetCaretRange" in calls

    def test_moves_endpoints_over_raw_document_range_not_wrapper(self):
        ctrl, calls = self._build()
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result is not None
        # "ld" is the last 2 chars of the RAW document text "hello world";
        # the wrapper slow path would yield "SLOW_WRAPPER_PATH"[-2:] == "TH".
        assert result["preceding_chars"] == "ld"
        assert "wrapper.Clone" not in calls
        assert "raw.Clone" in calls

    def test_reports_no_selection_when_selection_empty(self):
        ctrl, _ = self._build()
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result is not None
        assert result["has_selection"] is False
