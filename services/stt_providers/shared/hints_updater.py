"""Shared hints file updater.

This module provides utilities for updating the shared hints.txt file
used by all STT providers. Hints are added one per line.

Key Functions:
  - add_hint: Add a phrase to the shared hints file

Typical Usage:
  from shared.hints_updater import add_hint

  success = add_hint("antigravity")
  if success:
      print("Hint added successfully")
"""
import logging
from pathlib import Path

from shared_stt.redact import redact_transcript

logger = logging.getLogger(__name__)


def get_hints_path() -> Path:
    """Get the path to the shared hints.txt file."""
    return Path(__file__).parent / "hints.txt"


def add_hint(hint: str) -> bool:
    """Add a hint to the shared hints.txt file.

    Args:
        hint: The phrase to add to the hints list

    Returns:
        True if hint was added, False if it already exists or an error occurred
    """
    # Normalize hint (strip whitespace)
    hint = hint.strip()
    if not hint:
        logger.warning("Cannot add empty hint")
        return False

    # Validate hint length
    if len(hint) > 100:
        logger.warning(f"Hint too long ({len(hint)} chars), truncating to 100")
        hint = hint[:100].strip()

    hints_path = get_hints_path()

    try:
        # Read existing hints
        existing_hints = set()
        if hints_path.exists():
            content = hints_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    existing_hints.add(line.lower())

        # Check if hint already exists (case-insensitive)
        if hint.lower() in existing_hints:
            logger.info(f"Hint already exists: '{redact_transcript(hint)}'")
            return False

        # Append hint to file
        with open(hints_path, "a", encoding="utf-8") as f:
            f.write(f"\n{hint}")

        logger.info(f"Added hint: '{redact_transcript(hint)}'")
        return True

    except Exception as e:
        logger.error(f"Error adding hint: {e}")
        return False


def get_hints() -> list[str]:
    """Get the current list of hints from the shared hints file.

    Returns:
        List of hint strings, or empty list if error occurs
    """
    hints_path = get_hints_path()

    try:
        if not hints_path.exists():
            return []

        content = hints_path.read_text(encoding="utf-8")
        hints = []
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                hints.append(line)
        return hints

    except Exception as e:
        logger.error(f"Error reading hints: {e}")
        return []
