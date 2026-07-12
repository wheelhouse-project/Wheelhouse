"""Tests for the live hotword refresh (wh-user-patterns-split.4).

Changing the hotword must take effect without a restart. SpeechProcessor owns
both its own hotword copy and its router's copy; apply_hotword updates both.
SpeechHandler.apply_hotword delegates to the processor when it exists.
"""
import asyncio
from unittest.mock import MagicMock

from speech.speech_processor import SpeechProcessor
from speech.speech_handler import SpeechHandler


def _make_processor(hotword="x-ray"):
    catalog = MagicMock()
    catalog.command_hotword = hotword
    catalog.get_trailing_command.return_value = None
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=MagicMock(),
        app=MagicMock(),
        replacement_timeout_ms=700,
        command_timeout_ms=1000,
        hotword=hotword,
    )
    processor.context_mirror = MagicMock()
    return processor


class TestProcessorApplyHotword:
    def test_updates_both_processor_and_router(self):
        p = _make_processor("x-ray")
        assert p.hotword == "x-ray"
        assert p.router.hotword == "x-ray"

        p.apply_hotword("computer")

        assert p.hotword == "computer"
        assert p.router.hotword == "computer"

    def test_lowercases_the_new_hotword(self):
        p = _make_processor("x-ray")
        p.apply_hotword("Computer")
        assert p.hotword == "computer"
        assert p.router.hotword == "computer"

    def test_strips_surrounding_whitespace(self):
        # wh-user-patterns-split.8.1: a stray space must not survive onto the
        # router, which compares an STT token to an exact stored hotword.
        p = _make_processor("x-ray")
        p.apply_hotword("  Computer  ")
        assert p.hotword == "computer"
        assert p.router.hotword == "computer"


class TestHandlerApplyHotword:
    def test_delegates_to_processor(self):
        # Bypass the heavy __init__; exercise only apply_hotword.
        handler = SpeechHandler.__new__(SpeechHandler)
        handler.speech_processor = MagicMock()

        handler.apply_hotword("computer")

        handler.speech_processor.apply_hotword.assert_called_once_with("computer")

    def test_no_processor_is_a_noop(self):
        handler = SpeechHandler.__new__(SpeechHandler)
        handler.speech_processor = None
        # Must not raise when the processor has not been created yet.
        handler.apply_hotword("computer")
