r"""Shared pattern transformation utilities for speech command patterns.

This module provides centralized pattern transformation logic used by both
PatternCatalog and TextParser to ensure consistent pattern processing
across the speech recognition system.

Key Functions:
  - transform_pattern: Auto-detect and transform special pattern types

Transformations:
  1. Greedy patterns (.+ or .*) → Mark as is_greedy
  2. Numeric patterns (\d+) → Transform to (\w+), add validation metadata

This ensures that both the pattern catalog (for buffering decisions) and
text parser (for execution) apply identical transformations.
"""
import re
from typing import Dict, Tuple, Any

# Greedy capture tail: ``(.*)``, ``(.+)``, bare ``.*``, or bare ``.+``.
# Shared with the router's prefix probe via extract_literal_prefix.
_GREEDY_TAIL_RE = re.compile(r"\(?\.[\*\+]\)?")


def extract_literal_prefix(pattern_str: str) -> str:
    """Strip anchors / boundaries / greedy tail and return the literal core.

    Example: ``\\bangle brackets(.*)$`` -> ``angle brackets``.
             ``^hey Google.*$``         -> ``hey Google``.

    Returns an empty string when the source has no greedy tail (so the
    caller can skip non-greedy patterns) or when stripping leaves nothing.

    Lives here (not in the router) so the prefix is computed ONCE at
    catalog load time and stored as ``literal_prefix`` in the pattern
    metadata (wh-greedy-prefix-precompute); the router's runtime probe
    reads the stored string instead of re-parsing the regex on the hot
    path.
    """
    p = pattern_str
    if p.endswith("$"):
        p = p[:-1]
    if p.startswith("^"):
        p = p[1:]
    if p.startswith(r"\b"):
        p = p[2:]
    m = _GREEDY_TAIL_RE.search(p)
    if not m:
        return ""
    return p[: m.start()].rstrip()


def transform_pattern(pattern_str: str) -> Tuple[str, Dict[str, Any]]:
    r"""Auto-detect special pattern types and transform if needed.
    
    Detects and handles:
    1. Greedy patterns (.+ or .*) → Mark as is_greedy
    2. Numeric patterns (\d+) → Transform to (\w+), add validation
    
    Args:
        pattern_str: Original regex pattern string
        
    Returns:
        Tuple of (transformed_pattern, metadata_dict)
        where metadata_dict may contain:
        - is_greedy: bool (if greedy pattern detected)
        - validation_group: str (if numeric pattern transformed, e.g., 'g1', 'g2')
        
    Examples:
        >>> transform_pattern(r'(backspace|back space)\s*(\d+)?$')
        (r'(backspace|back space)\s*(\w+)?$', {'validation_group': 'g2'})
        
        >>> transform_pattern(r'(?:prefix )?(\d+) times')
        (r'(?:prefix )?(\w+) times', {'validation_group': 'g1'})
        
        >>> transform_pattern(r'select (.+)')
        (r'select (.+)', {'is_greedy': True})
    
    Notes:
        Shared utility for PatternCatalog and TextParser to ensure consistent
        pattern transformation across the speech recognition system.
    """
    metadata = {}
    transformed = pattern_str
    
    # 1. Detect greedy patterns (.+ or .*)
    if '.+' in pattern_str or '.*' in pattern_str:
        metadata['is_greedy'] = True
    
    # 2. Detect and transform numeric patterns (\d+)
    # First, count ALL capturing groups to determine absolute positions
    all_groups = re.findall(r'\((?!\?:)', pattern_str)
    total_groups = len(all_groups)
    
    # Now find and transform \d+ groups while tracking their absolute position
    # We need to track position in the original pattern, not just \d+ groups
    numeric_group_index = None
    current_group = 0
    
    def replace_numeric_group(match):
        r"""Regex substitution function to transform \d+ to \w+ and track validation.
        
        Args:
            match: re.Match object from re.sub()
            
        Returns:
            str: Replacement string with \w+ instead of \d+
        """
        nonlocal numeric_group_index, current_group
        group_type = match.group(1)    # '(' or '(?:'
        closing = match.group(3)        # ')'
        quantifier = match.group(4) or ''  # Optional '?', '*', '+', etc. (empty string if None)
        
        # Count all capturing groups before this match
        # by finding all '(' not followed by '?:' up to this point
        match_start = match.start()
        groups_before = len(re.findall(r'\((?!\?:)', pattern_str[:match_start]))
        
        # Check if this is a capturing group (not non-capturing)
        if group_type == '(':
            # This is a capturing group - its absolute position is groups_before + 1
            absolute_group_num = groups_before + 1
            # First numeric capturing group gets validation metadata
            if numeric_group_index is None:
                numeric_group_index = absolute_group_num
                metadata['validation_group'] = f"g{absolute_group_num}"
        
        # Replace \d+ with \w+ in both capturing and non-capturing groups
        return f"{group_type}\\w+{closing}{quantifier}"
    
    # Pattern matches: (capturing or (?:non-capturing) + \d+ + closing paren + optional quantifier
    # Group 1: opening paren type '(' or '(?:'
    # Group 2: \d+
    # Group 3: closing paren ')'
    # Group 4: optional quantifier '?', '*', '+', '{m,n}', or empty string
    # This allows any content between the group and its quantifier (like \s* in "(\d+)?")
    pattern = r'(\(\?:|\()(\\d\+)(\))([?*+]|\{[\d,]+\})?'
    transformed = re.sub(pattern, replace_numeric_group, transformed)

    # 3. Pre-compute the greedy literal prefix from the FINAL transformed
    # pattern -- the same string the catalog compiles -- so the stored
    # prefix always equals what a runtime extraction from the compiled
    # pattern would produce (wh-greedy-prefix-precompute).
    if metadata.get('is_greedy'):
        metadata['literal_prefix'] = extract_literal_prefix(transformed)

    return transformed, metadata
