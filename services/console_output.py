"""
Console output utilities with proper Unicode handling.

This module provides cross-platform, Unicode-safe console output for WheelHouse.
Addresses Windows cmd/PowerShell encoding issues (cp1252) that cause crashes
when printing Unicode characters like emoji.

Usage:
    from services.console_output import console_print as print
    
    # Safe Unicode output - works everywhere
    print("[OK] Task completed!")      # Always works
    print("[OK] Task completed!")         # Automatically falls back if needed
"""
import sys
from typing import Any


# Emoji mapping for fallback
EMOJI_FALLBACK = {
    '\u2705': '[OK]',        # White Heavy Check Mark
    '\u274C': '[ERROR]',     # Cross Mark
    '\u26A0\uFE0F': '[WARNING]', # Warning
    '\u26A0': '[WARNING]',   # Warning (variant)
    '\u2713': '[+]',         # Check Mark
    '\u2717': '[-]',         # Ballot X
    '\u2192': '->',          # Right Arrow
    '\u2190': '<-',          # Left Arrow
    '\u2194': '<->',         # Left Right Arrow
    '\u2022': '*',           # Bullet
    '\u25C6': '*',           # Black Diamond
    '\u25B8': '>',           # Black Right-Pointing Small Triangle
    '\u25AA': '-',           # Black Small Square
    '\u2139\uFE0F': '[INFO]', # Information Source
    '\u2139': '[INFO]',      # Information Source (variant)
    '\u1F6A8': '[CRITICAL]', # Police Car Light
}


def can_encode_unicode() -> bool:
    """
    Check if current console can encode Unicode characters.
    
    Returns:
        True if console supports Unicode, False if needs ASCII fallback
    """
    try:
        # Try encoding a test emoji
        test_char = '\u2705'
        sys.stdout.encoding  # Ensure encoding is available
        test_char.encode(sys.stdout.encoding or 'utf-8')
        return True
    except (UnicodeEncodeError, AttributeError, LookupError):
        return False


def safe_str(text: Any) -> str:
    """
    Convert text to Unicode-safe string for current console.
    
    Args:
        text: Any object to convert to string
        
    Returns:
        String safe for current console encoding
    """
    text_str = str(text)
    
    # If console supports Unicode, return as-is
    if can_encode_unicode():
        return text_str
    
    # Otherwise, replace known Unicode chars with ASCII equivalents
    result = text_str
    for emoji, fallback in EMOJI_FALLBACK.items():
        result = result.replace(emoji, fallback)
    
    return result


def console_print(*args: Any, **kwargs: Any) -> None:
    """
    Unicode-safe print function.
    
    Automatically handles encoding issues by falling back to ASCII
    when the console doesn't support Unicode.
    
    Args:
        *args: Arguments to print (same as built-in print)
        **kwargs: Keyword arguments (same as built-in print)
    """
    # Convert all args to safe strings
    safe_args = [safe_str(arg) for arg in args]
    
    try:
        print(*safe_args, **kwargs)
    except UnicodeEncodeError:
        # Final fallback: encode with errors='replace'
        safe_args = [str(arg).encode('ascii', errors='replace').decode('ascii') 
                     for arg in args]
        print(*safe_args, **kwargs)


# Convenience aliases
cprint = console_print
safe_print = console_print


def format_status(status: str, message: str) -> str:
    """
    Format a status message with appropriate prefix.
    
    Args:
        status: One of 'ok', 'error', 'warning', 'info'
        message: The message to display
        
    Returns:
        Formatted status message (Unicode-safe)
    """
    prefixes = {
        'ok': '[OK]',
        'error': '[ERROR]',
        'warning': '[WARNING]',
        'info': '[INFO]',
        'success': '[OK]',
        'fail': '[ERROR]',
    }
    
    prefix = prefixes.get(status.lower(), status)
    return safe_str(f"{prefix} {message}")


if __name__ == "__main__":
    # Test the module
    console_print("Testing Unicode support...")
    console_print(f"Console encoding: {sys.stdout.encoding}")
    console_print(f"Can encode Unicode: {can_encode_unicode()}")
    console_print()
    console_print("Testing emoji fallback:")
    console_print("[OK] Success message")
    console_print("[ERROR] Error message")
    console_print("[WARNING] Warning message")
    console_print("[+] Checkmark")
    console_print("-> Arrow")
    console_print()
    console_print("Testing format_status:")
    console_print(format_status('ok', 'Everything working'))
    console_print(format_status('error', 'Something failed'))
    console_print(format_status('warning', 'Be careful'))
