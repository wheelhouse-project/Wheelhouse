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


class TestReadContextCaretInNewEmptyParagraph:
    """wh-line-start-space-capital: the legacy reader in a new empty paragraph."""

    @staticmethod
    def _setup(mock_auto, before, enclosing_class, enclosing_text=None,
               selection_text=""):
        focused, text_pattern = _setup_text_pattern_mock(
            mock_auto, text_before_caret=before, selection_text=selection_text
        )
        sel_range = text_pattern.GetSelection.return_value[0]
        sel_range.GetEnclosingControl.return_value = MagicMock(
            ClassName=enclosing_class
        )
        mock_auto.ControlsAreSame.return_value = False  # a child element
        if enclosing_text is not None:
            text_pattern.pattern.RangeFromChild.return_value.GetText.return_value = enclosing_text
        return focused

    @patch(f"{_MOD}.auto")
    def test_period_line(self, mock_auto):
        focused = self._setup(mock_auto, "Hello world.", "is-empty")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == ".\n"

    @patch(f"{_MOD}.auto")
    def test_line_without_period(self, mock_auto):
        focused = self._setup(mock_auto, "It should not be there", "is-empty")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "e\n"

    @patch(f"{_MOD}.auto")
    def test_placeholder_text_is_stripped(self, mock_auto):
        focused = self._setup(
            mock_auto, "Hello world.\nType / for commands", "is-empty",
            enclosing_text="Type / for commands\n",
        )
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == ".\n"

    @patch(f"{_MOD}.auto")
    def test_claude_box_empty_paragraph_without_class(self, mock_auto):
        focused = self._setup(mock_auto, "Hello world", "", enclosing_text="\n")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "d\n"

    @patch(f"{_MOD}.auto")
    def test_argument_error_without_class_keeps_today_read(self, mock_auto):
        import ctypes

        focused = self._setup(mock_auto, "Hello world.", "")
        focused.GetPattern.return_value.pattern.RangeFromChild.side_effect = (
            ctypes.ArgumentError("argument 1")
        )
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result is not None
        assert result["preceding_chars"] == "d."

    @patch(f"{_MOD}.auto")
    def test_raw_range_is_read_for_the_enclosing_element(self, mock_auto):
        """The raw RangeFromChild receives enclosing.Element (not the control)."""
        focused = self._setup(mock_auto, "Hello world", "", enclosing_text="\n")
        text_pattern = focused.GetPattern.return_value
        enclosing = text_pattern.GetSelection.return_value[0].GetEnclosingControl.return_value
        _reader_mod.read_context_via_text_pattern(focused_control=focused)
        text_pattern.pattern.RangeFromChild.assert_called_once_with(enclosing.Element)

    @patch(f"{_MOD}.auto")
    def test_empty_text_before_caret_is_read_without_error(self, mock_auto):
        focused = self._setup(mock_auto, "", "", enclosing_text="\n")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result is not None
        assert result["preceding_chars"] == ""

    @patch(f"{_MOD}.auto")
    def test_caret_at_end_of_line_with_text_node_keeps_today_read(self, mock_auto):
        """Measured in the Claude box: enclosing text node, class '', own text 'Hello world'."""
        focused = self._setup(mock_auto, "Hello world", "", enclosing_text="Hello world")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result is not None
        assert result["preceding_chars"] == "ld"

    @patch(f"{_MOD}.auto")
    def test_mid_line_keeps_today_read(self, mock_auto):
        focused = self._setup(mock_auto, "Hello world.", "tiptap ProseMirror",
                              enclosing_text="Hello world.")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "d."

    @patch(f"{_MOD}.auto")
    def test_text_already_after_line_break_is_unchanged(self, mock_auto):
        focused = self._setup(mock_auto, "ab\n", "is-empty")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "b\n"

    @patch(f"{_MOD}.auto")
    def test_selection_keeps_today_read(self, mock_auto):
        focused = self._setup(mock_auto, "Hello world.", "is-empty",
                              selection_text="ld")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "d."
        assert result["has_selection"] is True


class TestReadContextSingleElementEditor:
    """wh-line-start-space-capital.1.1: enclosing element IS the focused control."""

    @patch(f"{_MOD}.auto")
    def test_legacy_single_element_editor_keeps_today_read(self, mock_auto):
        focused = TestReadContextCaretInNewEmptyParagraph._setup(
            mock_auto, "Hello world.", "is-empty", enclosing_text="\n"
        )
        mock_auto.ControlsAreSame.return_value = True
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "d."
        enclosing = focused.GetPattern.return_value.GetSelection.return_value[0].GetEnclosingControl.return_value
        mock_auto.ControlsAreSame.assert_called_once_with(enclosing, focused)

    @patch(f"{_MOD}.auto")
    def test_compare_error_keeps_the_line_start_rule(self, mock_auto):
        focused = TestReadContextCaretInNewEmptyParagraph._setup(
            mock_auto, "Hello world.", "is-empty"
        )
        mock_auto.ControlsAreSame.side_effect = _ctypes.COMError(-1, "x", (None,) * 5)
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == ".\n"


class TestReadContextEmptyInputControl:
    """wh-google-messages-first-word: the focused control's own class marks an empty box."""

    @staticmethod
    def _setup(mock_auto, before, control_class):
        focused, _tp = _setup_text_pattern_mock(mock_auto, text_before_caret=before)
        focused.ClassName = control_class
        return focused

    @patch(f"{_MOD}.auto")
    def test_empty_box_returns_empty_context(self, mock_auto):
        focused = self._setup(mock_auto, "Text message", "input is-empty")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result == {"preceding_chars": "", "has_selection": False}

    @patch(f"{_MOD}.auto")
    def test_box_with_text_keeps_today_read(self, mock_auto):
        focused = self._setup(mock_auto, "Hello there", "input")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "re"

    @patch(f"{_MOD}.auto")
    def test_other_token_keeps_today_read(self, mock_auto):
        focused = self._setup(mock_auto, "Hello there", "input is-empty-ish")
        result = _reader_mod.read_context_via_text_pattern(focused_control=focused)
        assert result["preceding_chars"] == "re"


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


class TestReadViaTextPattern2NewEmptyParagraph:
    """wh-line-start-space-capital: the TextPattern2 path applies the rule too."""

    def _build(self, enclosing_class, own_text="\n"):
        calls = []
        sel_range = MagicMock()
        sel_range.GetText.return_value = ""
        sel_range.GetEnclosingControl.return_value = MagicMock(
            ClassName=enclosing_class
        )
        text_pattern = _FakeTextPattern("Hello world.", calls)
        text_pattern.GetSelection = lambda: [sel_range]
        text_pattern.pattern = MagicMock()
        text_pattern.pattern.RangeFromChild.return_value.GetText.return_value = own_text
        raw_tp2 = _FakeRawTP2(
            caret_range=_FakeRawRange("", calls, "caret"), calls=calls
        )
        return _FakeControlTP2(_FakeWrapperTP2(raw_tp2), text_pattern)

    def test_empty_paragraph_puts_caret_after_line_break(self):
        ctrl = self._build("is-empty")
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result["preceding_chars"] == ".\n"

    def test_tp2_claude_box_empty_paragraph_without_class(self):
        ctrl = self._build("", "\n")
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result["preceding_chars"] == ".\n"

    def test_text_node_own_text_without_class_keeps_today_read(self):
        ctrl = self._build("", "Hello world.")
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result["preceding_chars"] == "d."

    def test_other_class_keeps_today_read(self):
        ctrl = self._build("tiptap ProseMirror", "Hello world.")
        result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result["preceding_chars"] == "d."

    def test_tp2_single_element_editor_keeps_today_read(self):
        """wh-line-start-space-capital.1.1: enclosing element is the focused control."""
        ctrl = self._build("is-empty")
        with patch.object(_reader_mod.auto, "ControlsAreSame",
                          return_value=True) as same:
            result = _reader_mod._read_via_text_pattern2(ctrl, max_chars=2)
        assert result["preceding_chars"] == "d."
        assert same.called
        assert same.call_args.args[1] is ctrl


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
