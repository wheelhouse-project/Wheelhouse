"""Text transformation utilities for speech processing.

This module contains pure text transformation functions applied during
dictation before text is inserted into applications.
"""
import re
import logging

from utils.redact import redact_transcript

logger = logging.getLogger(__name__)

# Pattern to match 3+ single letters separated by spaces
# Matches: "w a s h i n g t o n", "c a t", etc.
# Does NOT match: "a b" (only 2 letters), "ab c" (multi-char tokens)
_SPELLED_LETTERS_PATTERN = re.compile(
    r'\b([a-zA-Z]) ([a-zA-Z])(?: ([a-zA-Z]))+\b'
)


def auto_compress_spelled_letters(text: str) -> str:
    """Compress sequences of 3+ single letters to words.
    
    Detects patterns like "w a s h i n g t o n" (single letters separated
    by spaces) and compresses them to "washington". Requires at least 3
    letters to trigger compression.
    
    Args:
        text: Input text that may contain spelled-out letters
        
    Returns:
        Text with spelled letter sequences compressed
        
    Examples:
        >>> auto_compress_spelled_letters("w a s h i n g t o n")
        'washington'
        >>> auto_compress_spelled_letters("hello w a s h i n g t o n world")
        'hello washington world'
        >>> auto_compress_spelled_letters("a b")
        'a b'
        >>> auto_compress_spelled_letters("c a t")
        'cat'
    """
    if not text:
        return text
    
    def compress_match(match: re.Match) -> str:
        """Compress a matched sequence by removing spaces."""
        matched_text = match.group(0)
        compressed = matched_text.replace(' ', '')
        logger.debug(f"Auto-compressed: '{redact_transcript(matched_text)}' -> '{redact_transcript(compressed)}'")
        return compressed
    
    result = _SPELLED_LETTERS_PATTERN.sub(compress_match, text)
    
    if result != text:
        logger.info(f"Auto-compress applied: '{redact_transcript(text)}' -> '{redact_transcript(result)}'")
    
    return result
