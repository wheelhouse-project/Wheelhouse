"""Tests for WordEvent retraction marker fields."""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.word_event import WordEvent


class TestRetractionMarker:
    """Tests for retraction marker WordEvent creation."""

    def test_retraction_marker_creation(self):
        """Retraction marker carries full final text for replay."""
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=42,
            is_retraction_marker=True,
            retraction_full_text="hello world corrected",
        )
        assert event.is_retraction_marker is True
        assert event.retraction_full_text == "hello world corrected"
        assert event.word == ""
        assert event.utterance_id == 42

    def test_default_retraction_fields(self):
        """Normal WordEvents default to non-retraction."""
        event = WordEvent(word="hello", start_of_utterance=True, end_of_utterance=False)
        assert event.is_retraction_marker is False
        assert event.retraction_full_text is None

    def test_retraction_marker_is_frozen(self):
        """Retraction marker is immutable like all WordEvents."""
        import pytest
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            is_retraction_marker=True,
            retraction_full_text="test",
        )
        with pytest.raises(AttributeError):
            event.retraction_full_text = "changed"
