"""Tests for WordEvent trace_id field."""

from speech.word_event import WordEvent


class TestWordEventTraceId:
    """trace_id field on WordEvent dataclass."""

    def test_trace_id_default_is_none(self):
        event = WordEvent(word="hello", start_of_utterance=True, end_of_utterance=False)
        assert event.trace_id is None

    def test_trace_id_explicit(self):
        event = WordEvent(
            word="delete",
            start_of_utterance=True,
            end_of_utterance=False,
            trace_id="T-000001",
        )
        assert event.trace_id == "T-000001"

    def test_trace_id_on_end_marker(self):
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=5,
            is_utterance_end_marker=True,
            trace_id="T-000003",
        )
        assert event.trace_id == "T-000003"
        assert event.is_utterance_end_marker is True

    def test_trace_id_on_retraction_marker(self):
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=10,
            is_retraction_marker=True,
            retraction_full_text="corrected text",
            trace_id="T-000007",
        )
        assert event.trace_id == "T-000007"
        assert event.is_retraction_marker is True
