"""Tests for the shared grapheme helpers (wh-g2-refactor.15).

Section 3 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
promotes the editor / clipboard grapheme-cluster helpers into a single
module under ``services.wheelhouse.shared.grapheme`` so the Input
process (``ClipboardOperations``) and the GUI process (the persistent
editor's credit ledger) can both call them without dragging
``ClipboardOperations`` into the GUI.

The new module exposes:

* ``count_grapheme_clusters(text)`` -- the segmenter previously named
  ``ClipboardOperations.count_grapheme_clusters``. Same semantics, same
  hand-rolled rules from wh-pkhrp.2.
* ``text_contains_grapheme_unsafe_chars(text)`` -- the surrogate-pair
  / ZWJ predicate previously named
  ``ClipboardOperations.text_contains_grapheme_unsafe_chars``.
* ``split_at_cluster_boundary_from_right(text, clusters_from_right)``
  -- returns ``(kept, removed)`` where ``removed`` is the
  rightmost ``clusters_from_right`` grapheme clusters and ``kept`` is
  everything before that boundary. New helper used by the
  retract-and-replay partial-trim branch.

The Input-process ``ClipboardOperations.count_grapheme_clusters`` and
``ClipboardOperations.text_contains_grapheme_unsafe_chars`` continue
to work as static methods and delegate to the shared module so the
existing test surface (``tests/test_phase3_wiring.py``) keeps passing
unchanged.
"""

from __future__ import annotations

import pytest


def test_module_exports_count_grapheme_clusters():
    from services.wheelhouse.shared import grapheme

    assert callable(grapheme.count_grapheme_clusters)


def test_module_exports_text_contains_grapheme_unsafe_chars():
    from services.wheelhouse.shared import grapheme

    assert callable(grapheme.text_contains_grapheme_unsafe_chars)


def test_module_exports_split_at_cluster_boundary_from_right():
    from services.wheelhouse.shared import grapheme

    assert callable(grapheme.split_at_cluster_boundary_from_right)


# ---------------------------------------------------------------------------
# count_grapheme_clusters behaviour parity with the legacy static method.
# Mirrors a representative sample of the existing wh-pkhrp.2 test cases so
# the shared module is exercised directly rather than only through the
# clipboard_operations passthrough.
# ---------------------------------------------------------------------------


def test_count_grapheme_clusters_empty_returns_zero():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    assert count_grapheme_clusters("") == 0


def test_count_grapheme_clusters_ascii_one_per_char():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    assert count_grapheme_clusters("hello") == 5


def test_count_grapheme_clusters_single_emoji_is_one_cluster():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    grinning = "\U0001F600"
    assert count_grapheme_clusters(grinning) == 1


def test_count_grapheme_clusters_zwj_family_is_one_cluster():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    zwj_family = (
        "\U0001F468"  # man
        "‍"
        "\U0001F469"  # woman
        "‍"
        "\U0001F467"  # girl
    )
    assert count_grapheme_clusters(zwj_family) == 1


def test_count_grapheme_clusters_four_member_family_is_one_cluster():
    """GB11 multi-element ZWJ chain: four bases joined by three ZWJs
    (wh-grapheme-gb11-multi-zwj)."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    family4 = (
        "\U0001F468"  # man
        "‍"
        "\U0001F469"  # woman
        "‍"
        "\U0001F467"  # girl
        "‍"
        "\U0001F466"  # boy
    )
    assert count_grapheme_clusters(family4) == 1


def test_count_grapheme_clusters_profession_zwj_vs16_is_one_cluster():
    """EP + ZWJ + EP + VS16 (man health worker) stays one cluster."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    health_worker = "\U0001F468‍⚕️"
    assert count_grapheme_clusters(health_worker) == 1


def test_count_grapheme_clusters_kiss_sequence_is_one_cluster():
    """Longest common ZWJ emoji: woman ZWJ heart VS16 ZWJ kiss-mark ZWJ man
    -- VS16 sits mid-chain between ZWJ segments."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    kiss = "\U0001F469‍❤️‍\U0001F48B‍\U0001F468"
    assert count_grapheme_clusters(kiss) == 1


def test_count_grapheme_clusters_rainbow_flag_is_one_cluster():
    """EP + VS16 + ZWJ + EP (rainbow flag): the Extend mark between the
    base and the ZWJ must not split the GB11 chain."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    rainbow = "\U0001F3F3️‍\U0001F308"
    assert count_grapheme_clusters(rainbow) == 1


def test_count_grapheme_clusters_skin_tone_zwj_chain_is_one_cluster():
    """Fitzpatrick modifier inside a ZWJ chain (woman + tone + ZWJ + rocket)."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    astronaut = "\U0001F469\U0001F3FD‍\U0001F680"
    assert count_grapheme_clusters(astronaut) == 1


def test_split_peels_four_member_family_as_one_cluster():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    family4 = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
    text = "a" + family4 + "b"
    kept, removed = split_at_cluster_boundary_from_right(text, 2)
    assert kept == "a"
    assert removed == family4 + "b"


def test_count_grapheme_clusters_combining_mark_folds_into_base():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    # 'e' + U+0301 combining acute, NFD form of u'é'.
    nfd = "é"
    assert count_grapheme_clusters(nfd) == 1


def test_count_grapheme_clusters_us_flag_is_one_cluster():
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    us_flag = "\U0001F1FA\U0001F1F8"
    assert count_grapheme_clusters(us_flag) == 1


# ---------------------------------------------------------------------------
# text_contains_grapheme_unsafe_chars behaviour parity.
# ---------------------------------------------------------------------------


def test_unsafe_chars_false_for_pure_ascii():
    from services.wheelhouse.shared.grapheme import (
        text_contains_grapheme_unsafe_chars,
    )

    assert text_contains_grapheme_unsafe_chars("") is False
    assert text_contains_grapheme_unsafe_chars("hello") is False


def test_unsafe_chars_true_for_non_bmp_emoji():
    from services.wheelhouse.shared.grapheme import (
        text_contains_grapheme_unsafe_chars,
    )

    assert text_contains_grapheme_unsafe_chars("\U0001F600") is True


def test_unsafe_chars_true_for_zwj():
    from services.wheelhouse.shared.grapheme import (
        text_contains_grapheme_unsafe_chars,
    )

    assert text_contains_grapheme_unsafe_chars("a‍b") is True


# ---------------------------------------------------------------------------
# split_at_cluster_boundary_from_right -- new helper.
# ---------------------------------------------------------------------------


def test_split_empty_zero_clusters_returns_empty_pair():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    assert split_at_cluster_boundary_from_right("", 0) == ("", "")


def test_split_ascii_one_from_right():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    kept, removed = split_at_cluster_boundary_from_right("hello", 1)
    assert kept == "hell"
    assert removed == "o"


def test_split_ascii_all_from_right():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    kept, removed = split_at_cluster_boundary_from_right("hello", 5)
    assert kept == ""
    assert removed == "hello"


def test_split_oversize_request_returns_whole_text():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    # Requesting more clusters than exist removes everything; it does
    # not raise. The caller (retract-and-replay) checks the underrun
    # case via the returned ``kept`` length.
    kept, removed = split_at_cluster_boundary_from_right("abc", 99)
    assert kept == ""
    assert removed == "abc"


def test_split_zero_returns_text_kept_intact():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    kept, removed = split_at_cluster_boundary_from_right("abc", 0)
    assert kept == "abc"
    assert removed == ""


def test_split_one_zwj_family_from_right_returns_whole_family():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    family = (
        "\U0001F468"
        "‍"
        "\U0001F469"
        "‍"
        "\U0001F467"
    )
    text = "hi " + family
    kept, removed = split_at_cluster_boundary_from_right(text, 1)
    assert kept == "hi "
    assert removed == family


def test_split_two_clusters_from_right_across_emoji_and_letter():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    text = "ab\U0001F600c"  # 4 clusters: a, b, emoji, c
    kept, removed = split_at_cluster_boundary_from_right(text, 2)
    assert kept == "ab"
    assert removed == "\U0001F600c"


def test_split_one_cluster_across_combining_mark_keeps_base_with_mark():
    """e + combining acute is one cluster; removing one cluster from
    the right must consume both code points together."""
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    text = "café"  # c, a, f, (e + combining acute) -- 4 clusters
    kept, removed = split_at_cluster_boundary_from_right(text, 1)
    assert kept == "caf"
    assert removed == "é"


def test_split_negative_clusters_raises():
    from services.wheelhouse.shared.grapheme import (
        split_at_cluster_boundary_from_right,
    )

    with pytest.raises(ValueError):
        split_at_cluster_boundary_from_right("abc", -1)


# --- finding wh-g2-refactor.25.4: cross-validation + missing inputs --------


# A regression input set covering every cluster rule the segmenter must
# recognise. Used by the count / _cluster_lengths cross-validation test
# below; future segmenter changes get one place to add a new case.
_SEGMENTER_REGRESSION_INPUTS = [
    "",
    "a",
    "hello",
    "hello, world!",
    "\U0001F680",                              # single non-BMP emoji
    "ab\U0001F600c",                           # ASCII around emoji
    "café",                                     # NFC combining mark
    "café",                                # NFD: e + combining acute
    "\U0001F468‍\U0001F469‍\U0001F467",   # ZWJ family
    "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466",  # 4-member family (GB11 chain)
    "\U0001F469‍❤️‍\U0001F48B‍\U0001F468",  # kiss sequence (VS16 mid-chain)
    "\U0001F3F3️‍\U0001F308",             # rainbow flag (VS16 before ZWJ)
    "\U0001F44D\U0001F3FD",                    # thumbs up + skin tone
    "\U0001F44D\U0001F3FE",                    # thumbs up + medium-dark
    "\U0001F1FA\U0001F1F8",                    # US flag (two RIs)
    "\U0001F1FA\U0001F1F8\U0001F1EB\U0001F1F7", # US + FR flags
    "abc‌def",                            # ZWNJ (extends cluster)
    "ab\U0001F600️c",                     # variation selector
    "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
                                                # subdivision flag (tag chars)
    "αβγ",                                     # Greek
    "日本語",                                   # CJK BMP
    "\U00020000",                              # CJK non-BMP
    "line1\nline2",                            # newline
]


@pytest.mark.parametrize("text", _SEGMENTER_REGRESSION_INPUTS)
def test_count_grapheme_clusters_agrees_with_cluster_lengths(text):
    """``count_grapheme_clusters`` and ``_cluster_lengths`` must report
    the same cluster count for every input the segmenter is expected
    to handle. Their agreement is a correctness invariant: the ledger
    stores ``run.clusters`` from the counter, but the partial-trim
    branch peels via ``split_at_cluster_boundary_from_right`` which
    measures via ``_cluster_lengths``. After
    wh-g2-refactor.25.2 the counter delegates to ``_cluster_lengths``,
    so this test guards against a future regression that re-introduces
    a parallel implementation."""
    from services.wheelhouse.shared.grapheme import (
        _cluster_lengths,
        count_grapheme_clusters,
    )

    assert len(_cluster_lengths(text)) == count_grapheme_clusters(text), text


def test_count_grapheme_clusters_emoji_plus_skin_tone():
    """A base emoji followed by a Fitzpatrick skin-tone selector is one
    cluster, not two. Finding wh-g2-refactor.25.4 named this case as
    missing from the isolated grapheme tests."""
    from services.wheelhouse.shared.grapheme import count_grapheme_clusters

    # Thumbs up + medium-dark skin tone (U+1F44D U+1F3FE).
    assert count_grapheme_clusters("\U0001F44D\U0001F3FE") == 1
    # Surrounded by ASCII: ascii + emoji-with-modifier + ascii = 3.
    assert count_grapheme_clusters("a\U0001F44D\U0001F3FEb") == 3
