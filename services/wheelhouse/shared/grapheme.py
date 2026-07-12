"""Shared grapheme-cluster helpers (wh-g2-refactor.15).

Promoted from ``services.wheelhouse.ui.clipboard_operations`` so the
GUI process can call the segmenter without importing
``ClipboardOperations`` (which lives in the Input process and pulls in
Win32 dependencies the GUI does not need).

The Input-process ``ClipboardOperations.count_grapheme_clusters`` and
``ClipboardOperations.text_contains_grapheme_unsafe_chars`` continue
to exist; they delegate to the functions defined here so wh-pkhrp.2's
behaviour and test surface stay intact.

Three callables are exported:

* ``count_grapheme_clusters(text)`` -- visible grapheme cluster count.
  Hand-rolled segmenter covering the wh-pkhrp.2.1.1 / wh-pkhrp.2.2.1
  cluster cases (ZWJ chains, ZWNJ, variation selectors,
  regional-indicator pairs, Fitzpatrick modifiers, combining marks,
  subdivision-flag tag sequences). It is NOT a full UAX #29
  implementation, but the ZWJ absorb loop does chain multi-element
  ZWJ sequences per GB11: the four-member family emoji (four bases
  joined by three ZWJs), profession sequences, and VS16-mid-chain
  sequences each stay a single cluster (verified by the GB11 tests in
  tests/test_shared_grapheme.py, wh-grapheme-gb11-multi-zwj; an
  earlier review-round claim that only one ZWJ pair was absorbed did
  not hold against the code or the tests). Known divergence from
  strict UAX #29: ZWJ absorbs ANY following code point, not only
  Extended_Pictographic, so non-emoji text like ``a ZWJ b`` counts 1
  cluster where GB999 would count 2. STT output never contains ZWJ,
  and ``text_contains_grapheme_unsafe_chars`` routes any ZWJ-bearing
  text to this counter deliberately, so the over-join is harmless for
  the retract path.
* ``text_contains_grapheme_unsafe_chars(text)`` -- predicate that
  reports whether the string contains characters that break the
  ``len(text)`` == backspace-count equivalence on Qt-backed targets.
  Used by the Input retract path to pick the cluster counter over
  the code-unit counter.
* ``split_at_cluster_boundary_from_right(text, clusters_from_right)``
  -- returns ``(kept, removed)`` where ``removed`` is the rightmost
  ``clusters_from_right`` grapheme clusters and ``kept`` is the
  remainder. New helper used by the persistent editor's
  retract-and-replay partial-trim branch (Section 3 of the G2 design
  refinements doc).
"""

from __future__ import annotations

import unicodedata


def normalize_line_endings(text: str) -> str:
    """Map CRLF and bare CR to a single LF.

    The hand-rolled segmenter below does NOT implement the UAX #29 GB3
    rule (do not break between CR and LF), so ``count_grapheme_clusters``
    counts ``\\r\\n`` as two clusters. The editor's credit ledger
    canonicalises CRLF / CR to LF before recording a run's cluster count
    (see ``shared.ledger._canonical_text``), so a CRLF lands as ONE
    cluster on the ledger side. Any caller that must agree with the
    ledger's recorded cluster count -- notably the speech processor's
    post-retract accounting reset -- normalises line endings through this
    helper first so the two sides count the same units
    (wh-editor-retract-dup.2.2).
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def count_grapheme_clusters(text: str) -> int:
    """Count visible grapheme clusters in ``text`` (wh-pkhrp.2).

    See ``services.wheelhouse.ui.clipboard_operations`` for the
    historical placement of this segmenter. The cluster rules covered:

    * Zero Width Joiner (U+200D) -- absorbs the following code point
      into the current cluster.
    * Zero Width Non-Joiner (U+200C) -- extending Cf code point
      (wh-pkhrp.2.2.1).
    * Variation selectors U+FE00..U+FE0F and supplementary
      U+E0100..U+E01EF.
    * Tag characters U+E0020..U+E007F (subdivision-flag emoji).
    * Fitzpatrick skin-tone selectors U+1F3FB..U+1F3FF.
    * Combining marks of general category Mn / Mc / Me.
    * Regional indicator pairs (two consecutive U+1F1E6..U+1F1FF form
      one flag cluster).

    Returns 0 for empty input. Delegates to ``_cluster_lengths`` so the
    counter and ``split_at_cluster_boundary_from_right`` share one
    segmenter implementation -- two structurally-different copies
    invite drift (finding wh-g2-refactor.25.2).
    """
    return len(_cluster_lengths(text))


def text_contains_grapheme_unsafe_chars(text: str) -> bool:
    """True when ``text`` contains characters that break
    ``len(text)`` == backspace-count on Qt-backed targets.

    wh-pkhrp.1.7. A non-BMP code point (>= U+10000) is one Python
    string element but two UTF-16 code units; a Zero Width Joiner
    (U+200D) folds adjacent code points into one cluster. Either
    pattern makes Python ``len`` an over-count of the grapheme
    clusters Qt's ``deletePreviousChar`` would delete.

    Returns False for empty input.
    """
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if cp >= 0x10000:
            return True
        if cp == 0x200D:
            return True
    return False


def _cluster_lengths(text: str) -> list[int]:
    """Return the length (in Python code points) of each grapheme
    cluster in ``text``, in left-to-right order.

    Internal helper for :func:`split_at_cluster_boundary_from_right`.
    The segmentation rules match :func:`count_grapheme_clusters`.
    """
    if not text:
        return []
    lengths: list[int] = []
    i = 0
    n = len(text)
    prev_was_unpaired_ri = False
    while i < n:
        cluster_start = i
        cp = ord(text[i])
        is_ri = 0x1F1E6 <= cp <= 0x1F1FF
        if is_ri and prev_was_unpaired_ri:
            # Second RI of a pair extends the previous cluster --
            # extend the last recorded length rather than starting a
            # new one.
            i += 1
            prev_was_unpaired_ri = False
            # Continue the absorb loop on the extended cluster.
            while i < n:
                nx_cp = ord(text[i])
                if nx_cp == 0x200D:
                    i += 1
                    if i < n:
                        i += 1
                    continue
                if nx_cp == 0x200C:
                    i += 1
                    continue
                if 0xFE00 <= nx_cp <= 0xFE0F:
                    i += 1
                    continue
                if 0xE0100 <= nx_cp <= 0xE01EF:
                    i += 1
                    continue
                if 0xE0020 <= nx_cp <= 0xE007F:
                    i += 1
                    continue
                if 0x1F3FB <= nx_cp <= 0x1F3FF:
                    i += 1
                    continue
                if unicodedata.category(text[i]) in ("Mn", "Mc", "Me"):
                    i += 1
                    continue
                break
            # Replace the last cluster length with its extended length.
            lengths[-1] += (i - cluster_start)
            continue

        prev_was_unpaired_ri = is_ri
        i += 1
        while i < n:
            nx_cp = ord(text[i])
            if nx_cp == 0x200D:
                i += 1
                if i < n:
                    i += 1
                prev_was_unpaired_ri = False
                continue
            if nx_cp == 0x200C:
                i += 1
                continue
            if 0xFE00 <= nx_cp <= 0xFE0F:
                i += 1
                continue
            if 0xE0100 <= nx_cp <= 0xE01EF:
                i += 1
                continue
            if 0xE0020 <= nx_cp <= 0xE007F:
                i += 1
                continue
            if 0x1F3FB <= nx_cp <= 0x1F3FF:
                i += 1
                prev_was_unpaired_ri = False
                continue
            if unicodedata.category(text[i]) in ("Mn", "Mc", "Me"):
                i += 1
                continue
            break
        lengths.append(i - cluster_start)
    return lengths


def split_at_cluster_boundary_from_right(
    text: str, clusters_from_right: int,
) -> tuple[str, str]:
    """Split ``text`` at the grapheme-cluster boundary
    ``clusters_from_right`` positions from the right.

    Returns ``(kept, removed)`` where ``removed`` is the rightmost
    ``clusters_from_right`` clusters and ``kept`` is the remainder.

    If ``clusters_from_right`` exceeds the number of clusters in
    ``text``, ``kept`` is empty and ``removed`` is the whole text.
    Used by the persistent editor's retract-and-replay partial-trim
    branch (Section 3 of the G2 refinements doc).

    Raises ``ValueError`` for negative ``clusters_from_right``.
    """
    if clusters_from_right < 0:
        raise ValueError(
            f"clusters_from_right must be >= 0, got {clusters_from_right}"
        )
    if clusters_from_right == 0:
        return text, ""
    lengths = _cluster_lengths(text)
    if clusters_from_right >= len(lengths):
        return "", text
    # Sum the rightmost N cluster lengths to find the split point in
    # Python code points.
    removed_codepoints = sum(lengths[-clusters_from_right:])
    split = len(text) - removed_codepoints
    return text[:split], text[split:]
