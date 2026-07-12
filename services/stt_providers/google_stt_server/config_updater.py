"""Config file updater with comment preservation using tomlkit.

This module provides utilities for updating the STT server's config.toml file
while preserving all comments, formatting, and structure. It uses the tomlkit
library which maintains the original TOML file structure during modifications.

Key Functions:
  - add_hint_to_config: Add a phrase hint to the config file

Typical Usage:
  from config_updater import add_hint_to_config
  
  success = add_hint_to_config("antigravity")
  if success:
      print("Hint added successfully")
"""
import logging

import tomlkit
from pathlib import Path

from shared_stt.redact import redact_transcript

logger = logging.getLogger("GoogleSTT")


def add_hint_to_config(hint: str, config_path: Path = None) -> bool:
    """
    Add a hint to the config.toml file, preserving all comments and formatting.
    
    This function uses tomlkit to load, modify, and save the TOML configuration
    file while maintaining all existing comments, whitespace, and structure.
    
    Args:
        hint: The phrase to add to the hints list
        config_path: Path to config.toml (defaults to config.toml in this directory)
    
    Returns:
        True if hint was added, False if it already exists or an error occurred
    
    Example:
        >>> add_hint_to_config("machine learning")
        [config] Added hint: 'machine learning'
        True
        
        >>> add_hint_to_config("claude")  # Already exists
        [config] Hint already exists: 'claude'
        False
    """
    if config_path is None:
        config_path = Path(__file__).parent / "config.toml"
    
    # Normalize hint (strip whitespace)
    hint = hint.strip()
    if not hint:
        logger.info("[config] Cannot add empty hint")
        return False
    
    # Validate hint length
    if len(hint) > 100:
        logger.info(f"[config] Hint too long ({len(hint)} chars), truncating to 100")
        hint = hint[:100].strip()
    
    try:
        # Load config with tomlkit to preserve comments
        with open(config_path, "r", encoding="utf-8") as f:
            config = tomlkit.load(f)
        
        # Check if hint already exists (case-insensitive)
        adaptation = config.get("adaptation", {})
        hints = adaptation.get("hints", [])
        
        if any(str(h).lower() == hint.lower() for h in hints):
            logger.info(f"[config] Hint already exists: '{redact_transcript(hint)}'")
            return False
        
        # Add hint to the list
        hints.append(hint)
        
        # Update the config (tomlkit preserves structure)
        if "adaptation" not in config:
            config["adaptation"] = {}
        config["adaptation"]["hints"] = hints
        
        # Write back with preserved formatting
        with open(config_path, "w", encoding="utf-8") as f:
            tomlkit.dump(config, f)
        
        logger.info(f"[config] Added hint: '{redact_transcript(hint)}'")
        return True
        
    except FileNotFoundError:
        logger.info(f"[config] Config file not found: {config_path}")
        return False
    except Exception as e:
        logger.info(f"[config] Error adding hint: {e}")
        return False


def get_hints(config_path: Path = None) -> list[str]:
    """
    Get the current list of hints from the config file.
    
    Args:
        config_path: Path to config.toml (defaults to config.toml in this directory)
    
    Returns:
        List of hint strings, or empty list if error occurs
    """
    if config_path is None:
        config_path = Path(__file__).parent / "config.toml"
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = tomlkit.load(f)
        
        adaptation = config.get("adaptation", {})
        hints = adaptation.get("hints", [])
        return [str(h) for h in hints]
        
    except Exception as e:
        logger.info(f"[config] Error reading hints: {e}")
        return []
